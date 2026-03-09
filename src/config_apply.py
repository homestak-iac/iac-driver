"""Config phase: fetch and apply a specification to the local host.

Reads a resolved spec and maps it to ansible variables, then runs the
config-apply.yml playbook to reach "platform ready".

Usage:
    ./run.sh config fetch [--insecure]       # Fetch spec from server
    ./run.sh config apply                    # Apply from default state dir
    ./run.sh config apply --spec /path.yaml  # Apply from explicit path
    ./run.sh config apply --dry-run          # Preview what would be applied

The config phase is step 2 of the 4-phase node lifecycle:
    create -> config -> run -> destroy
"""

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from common import run_command, get_state_dir, get_homestak_root

logger = logging.getLogger(__name__)


def _get_config_state_dir() -> Path:
    """Return config state directory: $HOMESTAK_ROOT/.state/config/."""
    result: Path = get_state_dir() / 'config'
    return result


def _get_marker_path() -> Path:
    """Return platform-ready marker path."""
    return _get_config_state_dir() / 'complete.json'


def _get_default_spec_path() -> Path:
    """Return default spec path (written by `homestak spec get`)."""
    return _get_config_state_dir() / 'spec.yaml'


class ConfigError(Exception):
    """Configuration error during config phase."""


@dataclass
class ConfigResult:
    """Result of a config apply operation."""
    success: bool
    message: str = ''
    duration: float = 0.0
    packages_count: int = 0
    services_enabled: int = 0
    services_disabled: int = 0
    users_count: int = 0


def _discover_ansible_dir() -> Path:
    """Discover the ansible directory.

    Derived from $HOMESTAK_ROOT/iac/ansible.
    """
    ansible_dir: Path = get_homestak_root() / 'iac' / 'ansible'
    if ansible_dir.exists():
        return ansible_dir

    raise ConfigError(
        f"Ansible directory not found at {ansible_dir}. "
        "Set HOMESTAK_ROOT to your workspace root directory."
    )


def _load_spec(spec_path: Path) -> dict:
    """Load and validate a spec YAML file.

    Args:
        spec_path: Path to the spec file

    Returns:
        Parsed spec dict

    Raises:
        ConfigError: If file not found or invalid
    """
    try:
        import yaml
    except ImportError as exc:
        raise ConfigError("PyYAML not installed. Run: apt install python3-yaml") from exc

    if not spec_path.exists():
        raise ConfigError(f"Spec not found: {spec_path}")

    with open(spec_path, encoding='utf-8') as f:
        spec = yaml.safe_load(f)

    if not spec or not isinstance(spec, dict):
        raise ConfigError(f"Invalid spec file: {spec_path}")

    result: dict = spec
    return result


def spec_to_ansible_vars(spec: dict) -> dict:
    """Map spec fields to ansible variables.

    Follows the mapping defined in the config-phase design doc:
    - platform.packages -> packages (base role)
    - config.timezone -> timezone (base role)
    - access.users[0].name -> local_user (users role)
    - access.users[0].sudo -> user_sudo (users role)
    - access.users[].ssh_keys -> ssh_authorized_keys (users role)
    - access._posture.ssh.* -> ssh_* (security role)
    - access._posture.sudo.* -> sudo_* (security role)
    - access._posture.fail2ban.* -> fail2ban_* (security role)

    Args:
        spec: Resolved spec dict (FKs already expanded)

    Returns:
        Flat dict of ansible variables
    """
    ansible_vars: dict = {}

    # Platform section -> base role
    platform = spec.get('platform', {})
    packages = platform.get('packages', [])
    if packages:
        ansible_vars['packages'] = packages

    # Config section -> base role
    config = spec.get('config', {})
    if tz_value := config.get('timezone'):
        ansible_vars['timezone'] = tz_value

    # Access section -> users + security roles
    access = spec.get('access', {})

    # Users (first user becomes local_user for the users role)
    users = access.get('users', [])
    if users:
        first_user = users[0]
        ansible_vars['local_user'] = first_user.get('name', 'homestak')
        if first_user.get('sudo'):
            ansible_vars['user_sudo'] = True

        # Collect all SSH keys from all users
        all_keys = []
        for user in users:
            keys = user.get('ssh_keys', [])
            all_keys.extend(keys)
        if all_keys:
            ansible_vars['ssh_authorized_keys'] = all_keys

    # Posture settings -> security role
    posture = access.get('_posture', {})

    ssh = posture.get('ssh', {})
    if 'port' in ssh:
        ansible_vars['ssh_port'] = ssh['port']
    if 'permit_root_login' in ssh:
        ansible_vars['ssh_permit_root_login'] = ssh['permit_root_login']
    if 'password_authentication' in ssh:
        ansible_vars['ssh_password_authentication'] = ssh['password_authentication']

    sudo = posture.get('sudo', {})
    if 'nopasswd' in sudo:
        ansible_vars['sudo_nopasswd'] = sudo['nopasswd']

    fail2ban = posture.get('fail2ban', {})
    if 'enabled' in fail2ban:
        ansible_vars['fail2ban_enabled'] = fail2ban['enabled']

    # Posture packages (merge with platform packages)
    posture_packages = posture.get('packages', [])
    if posture_packages:
        existing = ansible_vars.get('packages', [])
        merged = list(dict.fromkeys(existing + posture_packages))  # dedup, preserve order
        ansible_vars['packages'] = merged

    # Services -> config-apply.yml tasks
    services = platform.get('services', {})
    if enable := services.get('enable'):
        ansible_vars['services_enable'] = enable
    if disable := services.get('disable'):
        ansible_vars['services_disable'] = disable

    return ansible_vars


def _write_vars_file(ansible_vars: dict, vars_dir: Path) -> Path:
    """Write ansible vars to a temporary JSON file.

    Args:
        ansible_vars: Variables to write
        vars_dir: Directory for the vars file

    Returns:
        Path to the written file
    """
    vars_dir.mkdir(parents=True, exist_ok=True)
    vars_file = vars_dir / 'config-vars.json'
    with open(vars_file, 'w', encoding='utf-8') as f:
        json.dump(ansible_vars, f, indent=2)
    logger.info(f"Wrote ansible vars to {vars_file}")
    return vars_file


def _write_marker(ansible_vars: dict, spec_name: str) -> Path:
    """Write the platform-ready marker file.

    Args:
        ansible_vars: The variables that were applied
        spec_name: Name of the spec that was applied

    Returns:
        Path to the marker file
    """
    _get_marker_path().parent.mkdir(parents=True, exist_ok=True)
    marker = {
        'phase': 'config',
        'status': 'complete',
        'timestamp': datetime.now(timezone.utc).isoformat(),
        'spec': spec_name,
        'packages': len(ansible_vars.get('packages', [])),
        'services_enabled': len(ansible_vars.get('services_enable', [])),
        'services_disabled': len(ansible_vars.get('services_disable', [])),
        'users': 1 if 'local_user' in ansible_vars else 0,
    }
    with open(_get_marker_path(), 'w', encoding='utf-8') as f:
        json.dump(marker, f, indent=2)
    logger.info(f"Wrote config-complete marker to {_get_marker_path()}")
    return _get_marker_path()


def apply_config(
    spec_path: Optional[Path] = None,
    dry_run: bool = False,
    json_output: bool = False,
) -> ConfigResult:
    """Apply a spec to the local host.

    This is the main entry point for `./run.sh config`.

    Args:
        spec_path: Path to spec file (default: auto-discover)
        dry_run: Preview without executing
        json_output: Emit JSON to stdout

    Returns:
        ConfigResult with success/failure and stats
    """
    start = time.time()

    # 1. Find spec
    if spec_path is None:
        spec_path = _get_default_spec_path()

    try:
        spec = _load_spec(spec_path)
    except ConfigError as e:
        return ConfigResult(
            success=False,
            message=str(e),
            duration=time.time() - start,
        )

    spec_name = spec.get('identity', {}).get('hostname', spec_path.stem)
    logger.info(f"Applying spec '{spec_name}' from {spec_path}")

    # 2. Map to ansible vars
    ansible_vars = spec_to_ansible_vars(spec)

    if dry_run:
        result_data = {
            'spec': spec_name,
            'spec_path': str(spec_path),
            'ansible_vars': ansible_vars,
            'dry_run': True,
        }
        if json_output:
            print(json.dumps(result_data, indent=2))
        else:
            print(f"\nDry-run: config apply for spec '{spec_name}'")
            print(f"  Spec: {spec_path}")
            print(f"  Packages: {len(ansible_vars.get('packages', []))}")
            print(f"  Services enable: {ansible_vars.get('services_enable', [])}")
            print(f"  Services disable: {ansible_vars.get('services_disable', [])}")
            print(f"  User: {ansible_vars.get('local_user', '(none)')}")
            print("\nAnsible vars:")
            for k, v in ansible_vars.items():
                print(f"    {k}: {v}")
        return ConfigResult(
            success=True,
            message='Dry-run complete',
            duration=time.time() - start,
        )

    # 3. Discover ansible directory
    try:
        ansible_dir = _discover_ansible_dir()
    except ConfigError as e:
        return ConfigResult(
            success=False,
            message=str(e),
            duration=time.time() - start,
        )

    playbook = ansible_dir / 'playbooks' / 'config-apply.yml'
    if not playbook.exists():
        return ConfigResult(
            success=False,
            message=f"Playbook not found: {playbook}",
            duration=time.time() - start,
        )

    # 4. Write vars file
    state_dir = _get_config_state_dir()
    vars_file = _write_vars_file(ansible_vars, state_dir)

    # 5. Run ansible-playbook
    logger.info(f"Running config-apply.yml with vars from {vars_file}")
    cmd = [
        'ansible-playbook',
        '-i', 'inventory/local.yml',
        'playbooks/config-apply.yml',
        '-e', f'@{vars_file}',
    ]

    # Explicitly set ANSIBLE_CONFIG so ansible finds ansible.cfg (and its
    # collections_path) even in minimal environments like cloud-init runcmd.
    ansible_env = {**os.environ, 'ANSIBLE_CONFIG': str(ansible_dir / 'ansible.cfg')}
    rc, out, err = run_command(cmd, cwd=ansible_dir, timeout=600, env=ansible_env)
    if rc != 0:
        error_msg = err[-500:] if err else out[-500:]
        return ConfigResult(
            success=False,
            message=f"config-apply.yml failed: {error_msg}",
            duration=time.time() - start,
        )

    # 6. Write platform-ready marker
    _write_marker(ansible_vars, spec_name)

    result = ConfigResult(
        success=True,
        message=f"Config applied for spec '{spec_name}'",
        duration=time.time() - start,
        packages_count=len(ansible_vars.get('packages', [])),
        services_enabled=len(ansible_vars.get('services_enable', [])),
        services_disabled=len(ansible_vars.get('services_disable', [])),
        users_count=1 if 'local_user' in ansible_vars else 0,
    )

    if json_output:
        print(json.dumps({
            'phase': 'config',
            'success': True,
            'spec': spec_name,
            'duration_seconds': result.duration,
            'packages': result.packages_count,
            'services_enabled': result.services_enabled,
            'services_disabled': result.services_disabled,
            'users': result.users_count,
        }, indent=2))

    return result


def _fetch_spec(insecure: bool = False) -> Optional[Path]:
    """Fetch spec from server using environment variables.

    Uses HOMESTAK_SERVER and HOMESTAK_TOKEN from the environment (#231).
    Identity is derived from hostname.

    Args:
        insecure: Skip SSL certificate verification

    Returns:
        Path to the saved spec file, or None on failure
    """
    from resolver.spec_client import SpecClient, SpecClientError, get_config_from_env

    env_config = get_config_from_env()
    server = env_config.get('server')
    identity = env_config.get('identity')
    token = env_config.get('token')

    if not server:
        logger.error("--fetch requires HOMESTAK_SERVER environment variable")
        return None
    if not token:
        logger.error("--fetch requires HOMESTAK_TOKEN environment variable (provisioning token)")
        return None

    try:
        client = SpecClient(
            server=server,
            identity=identity,
            token=token,
            insecure=insecure,
        )
        logger.info(f"Fetching spec for '{identity}' from {server}...")
        _spec, path = client.fetch_and_save()
        result_path: Optional[Path] = path
        logger.info(f"Spec saved to {result_path}")
        return result_path
    except SpecClientError as e:
        logger.error(f"Failed to fetch spec: {e.code} - {e.message}")
        return None


def fetch_main(argv: list) -> int:
    """CLI entry point for 'config fetch'.

    Downloads spec from server and saves to state directory.

    Args:
        argv: Command line arguments after 'config fetch'

    Returns:
        Exit code (0=success, 1=error)
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog='run.sh config fetch',
        description='Fetch specification from server',
    )
    parser.add_argument(
        '--insecure', '-k',
        action='store_true',
        help='Skip SSL certificate verification (for self-signed certs)',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging',
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    spec_path = _fetch_spec(insecure=args.insecure)
    if spec_path is None:
        return 1

    return 0


def apply_main(argv: list) -> int:
    """CLI entry point for 'config apply'.

    Loads spec from state directory and applies it via ansible.

    Args:
        argv: Command line arguments after 'config apply'

    Returns:
        Exit code (0=success, 1=spec error, 2=apply error)
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog='run.sh config apply',
        description='Apply a specification to the local host',
    )
    parser.add_argument(
        '--spec',
        type=Path,
        help=f'Path to spec file (default: {_get_default_spec_path()})',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview what would be applied without running ansible',
    )
    parser.add_argument(
        '--json-output',
        action='store_true',
        help='Output structured JSON to stdout',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging',
    )

    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    result = apply_config(
        spec_path=args.spec,
        dry_run=args.dry_run,
        json_output=args.json_output,
    )

    if result.success:
        if not args.json_output and not args.dry_run:
            logger.info(f"Config complete in {result.duration:.1f}s")
        return 0

    if not args.json_output:
        print(f"Error: {result.message}", file=__import__('sys').stderr)
    return 1 if 'not found' in result.message.lower() else 2


def config_main(argv: list) -> int:
    """CLI dispatcher for 'config' noun.

    Routes to fetch_main or apply_main based on subcommand.

    Args:
        argv: Command line arguments after 'config'

    Returns:
        Exit code
    """
    if not argv or argv[0].startswith('-'):
        print("Usage: ./run.sh config <action> [options]")
        print()
        print("Actions:")
        print("  fetch    Fetch specification from server")
        print("  apply    Apply specification to local host")
        print()
        print("Run './run.sh config <action> --help' for action-specific options.")
        return 1 if not argv else 0

    action = argv[0]
    rest = argv[1:]

    if action == 'fetch':
        return fetch_main(rest)
    if action == 'apply':
        return apply_main(rest)

    print(f"Error: Unknown config action '{action}'")
    print("Available actions: fetch, apply")
    return 1
