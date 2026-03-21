"""Pre-flight validation checks for scenarios.

This module provides readiness checks that run before scenarios execute,
catching configuration issues early with actionable error messages.
"""

import logging
import os
import socket
from pathlib import Path
from typing import Optional

import requests
import urllib3

# Suppress SSL warnings for self-signed certs
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# API Token Validation
# -----------------------------------------------------------------------------

def validate_api_token(api_endpoint: str, api_token: str, node_name: str) -> list[str]:
    """Validate Proxmox API token is present and valid.

    Args:
        api_endpoint: PVE API URL (e.g., https://198.51.100.61:8006)
        api_token: Full token string (e.g., root@pam!homestak=uuid)
        node_name: Node name for error messages

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    # Check API endpoint is configured
    if not api_endpoint:
        errors.append(
            f"API endpoint not configured for node '{node_name}'\n"
            f"  Add 'api_endpoint' to config/nodes/{node_name}.yaml"
        )
        return errors

    # Check token is present
    if not api_token:
        errors.append(
            f"API token not found for node '{node_name}'\n"
            f"  Ensure secrets.yaml is decrypted: cd ../config && make decrypt\n"
            f"  Ensure token exists: secrets.api_tokens.{node_name}"
        )
        return errors

    # Check token format (PVE format: user@realm!tokenname=tokenvalue)
    if '!' not in api_token or '=' not in api_token:
        errors.append(
            f"API token for '{node_name}' has invalid format\n"
            f"  Expected: user@realm!tokenname=tokenvalue\n"
            f"  Got: {api_token[:20]}..."
        )
        return errors

    # Validate token against API
    try:
        resp = requests.get(
            f"{api_endpoint}/api2/json/version",
            headers={"Authorization": f"PVEAPIToken={api_token}"},
            verify=False,  # Self-signed cert
            timeout=10
        )

        if resp.status_code == 401:
            errors.append(
                f"API token invalid for node '{node_name}'\n"
                f"  Regenerate: pveum user token add root@pam homestak --privsep 0\n"
                f"  Then update secrets.yaml and run: cd ../config && make encrypt"
            )
        elif resp.status_code != 200:
            errors.append(
                f"Unexpected API response for node '{node_name}': {resp.status_code}\n"
                f"  Response: {resp.text[:100]}"
            )
        else:
            data = resp.json().get("data", {})
            version = data.get("version", "unknown")
            logger.info(f"API token valid for {node_name} (PVE {version})")

    except requests.exceptions.ConnectionError:
        errors.append(
            f"Cannot connect to {api_endpoint}\n"
            f"  Check: host is online, port 8006 is open, firewall allows access"
        )
    except requests.exceptions.Timeout:
        errors.append(f"Timeout connecting to {api_endpoint}")
    except Exception as e:
        errors.append(f"Error validating token: {e}")

    return errors


# -----------------------------------------------------------------------------
# Host Availability Validation
# -----------------------------------------------------------------------------

def validate_host_resolvable(hostname: str) -> tuple[bool, str]:
    """Check if hostname resolves to an IP address.

    Args:
        hostname: Hostname or IP to resolve

    Returns:
        (success, ip_or_error) tuple
    """
    try:
        ip = socket.gethostbyname(hostname)
        return True, ip
    except socket.gaierror:
        return False, f"Cannot resolve hostname '{hostname}'"


def validate_host_reachable(host: str, port: int = 22, timeout: float = 5.0) -> tuple[bool, str]:
    """Check if host is reachable on specified port.

    Args:
        host: Hostname or IP
        port: Port to check (default: 22 for SSH)
        timeout: Connection timeout in seconds

    Returns:
        (success, message) tuple
    """
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        sock.close()
        return True, f"Port {port} reachable"
    except socket.timeout:
        return False, f"Timeout connecting to {host}:{port}"
    except socket.error as e:
        return False, f"Cannot connect to {host}:{port}: {e}"


def validate_host_availability(ssh_host: str, node_name: str,
                               check_ssh: bool = True,
                               check_api: bool = True,
                               timeout: float = 5.0) -> list[str]:
    """Validate host is resolvable and reachable.

    Args:
        ssh_host: Hostname or IP to validate
        node_name: Node name for error messages
        check_ssh: Check SSH port 22
        check_api: Check API port 8006
        timeout: Connection timeout per check

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    if not ssh_host:
        errors.append(
            f"SSH host not configured for node '{node_name}'\n"
            f"  Derived from api_endpoint or set explicitly in nodes/{node_name}.yaml"
        )
        return errors

    # Check hostname resolution
    success, result = validate_host_resolvable(ssh_host)
    if not success:
        errors.append(
            f"{result}\n"
            f"  Check: nodes/{node_name}.yaml has correct api_endpoint or ip\n"
            f"  Or use --host with a resolvable hostname/IP"
        )
        return errors  # Can't check ports if hostname doesn't resolve

    ip = result
    logger.info(f"Host {ssh_host} resolves to {ip}")

    # Check SSH port
    if check_ssh:
        success, message = validate_host_reachable(ip, port=22, timeout=timeout)
        if not success:
            errors.append(
                f"SSH not available on {ssh_host} ({ip})\n"
                f"  {message}\n"
                f"  Check: host is online, SSH is enabled, firewall allows port 22"
            )

    # Check API port
    if check_api:
        success, message = validate_host_reachable(ip, port=8006, timeout=timeout)
        if not success:
            errors.append(
                f"PVE API not available on {ssh_host} ({ip})\n"
                f"  {message}\n"
                f"  Check: pveproxy service is running, firewall allows port 8006"
            )

    return errors


# -----------------------------------------------------------------------------
# Bootstrap Installation Validation
# -----------------------------------------------------------------------------

def get_homestak_paths() -> tuple[Path, Path]:
    """Get homestak installation paths.

    Returns:
        (lib_path, etc_path) tuple - paths for code repos and config
    """
    # User-owned paths (~homestak/)
    home_lib = Path.home() / 'lib'
    home_etc = Path.home() / 'etc'

    return home_lib, home_etc


def validate_bootstrap_installed() -> list[str]:
    """Validate that bootstrap is installed.

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    lib_path, etc_path = get_homestak_paths()

    # Check config exists
    if not etc_path.exists():
        errors.append(
            f"Bootstrap not complete - config not found\n"
            f"  Expected: {etc_path}\n"
            f"  Run: curl -fsSL https://raw.githubusercontent.com/homestak/bootstrap/master/install | bash"
        )
        return errors

    # Check core repos exist
    core_repos = ['ansible', 'iac-driver', 'tofu']
    missing_repos = []
    for repo in core_repos:
        repo_path = lib_path / repo
        if not repo_path.exists():
            missing_repos.append(repo)

    if missing_repos:
        errors.append(
            f"Bootstrap incomplete - missing repos: {', '.join(missing_repos)}\n"
            f"  Expected at: {lib_path}\n"
            f"  Re-run: curl -fsSL https://raw.githubusercontent.com/homestak/bootstrap/master/install | bash"
        )

    return errors


def validate_site_config(_config) -> list[str]:
    """Validate site.yaml has required network settings.

    Returns:
        List of error strings (empty if valid)
    """
    from config import get_site_config_dir
    errors: list[str] = []

    try:
        site_config_dir = get_site_config_dir()
        site_file = site_config_dir / 'site.yaml'
        if not site_file.exists():
            return errors
        import yaml
        with open(site_file, encoding='utf-8') as f:
            site = yaml.safe_load(f) or {}
        defaults = site.get('defaults', {})
    except Exception:
        return errors

    if not defaults.get('gateway'):
        errors.append(
            "gateway not configured in site.yaml\n"
            "  Edit site.yaml: defaults.gateway (e.g., 192.168.1.1)"
        )
    if not defaults.get('dns_servers'):
        errors.append(
            "dns_servers not configured in site.yaml\n"
            "  Edit site.yaml: defaults.dns_servers (e.g., [192.168.1.1])"
        )
    if not defaults.get('domain'):
        logger.warning("domain not configured in site.yaml")
        logger.debug(
            "VMs won't have a DNS search domain. "
            "Edit site.yaml: defaults.domain (e.g., home.arpa)"
        )

    # Check secrets.yaml for SSH keys
    secrets_file = site_config_dir / 'secrets.yaml'
    if secrets_file.exists():
        with open(secrets_file, encoding='utf-8') as f:
            secrets = yaml.safe_load(f) or {}
        ssh_keys = secrets.get('ssh_keys', {})
        if not ssh_keys or not isinstance(ssh_keys, dict) or not any(ssh_keys.values()):
            errors.append(
                "No SSH keys in secrets.yaml — VMs will be unreachable\n"
                "  Run: sudo homestak site-init"
            )

    return errors


def validate_site_init_complete(hostname: Optional[str] = None) -> list[str]:
    """Validate that site-init has been run.

    Args:
        hostname: Hostname to check (defaults to current hostname)

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []
    _, etc_path = get_homestak_paths()

    if hostname is None:
        hostname = socket.gethostname()

    # Check secrets.yaml exists
    secrets_path = etc_path / 'secrets.yaml'
    if not secrets_path.exists():
        errors.append(
            f"Site not initialized - secrets.yaml not found\n"
            f"  Expected: {secrets_path}\n"
            f"  Run: homestak site-init"
        )
        return errors  # Can't check decryption if file doesn't exist

    # Check secrets.yaml is decrypted (not SOPS encrypted)
    try:
        with open(secrets_path, 'r', encoding='utf-8') as f:
            first_line = f.readline().strip()
        # SOPS encrypted files start with 'sops:' key
        if first_line.startswith('sops:') or first_line == 'sops':
            errors.append(
                f"secrets.yaml not decrypted\n"
                f"  Run: cd {etc_path} && make decrypt"
            )
    except Exception as e:
        errors.append(f"Cannot read secrets.yaml: {e}")

    # Check node config exists
    node_path = etc_path / 'nodes' / f'{hostname}.yaml'
    if not node_path.exists():
        errors.append(
            f"Node config not found: nodes/{hostname}.yaml\n"
            f"  Expected: {node_path}\n"
            f"  Run: homestak site-init\n"
            f"  Or create manually: cd {etc_path} && make node-config"
        )

    return errors


# -----------------------------------------------------------------------------
# Nested Virtualization Validation
# -----------------------------------------------------------------------------

def validate_nested_virt() -> list[str]:
    """Validate nested virtualization is enabled.

    Returns:
        List of validation error messages (empty if valid)
    """
    errors = []

    # Check Intel nested virt
    intel_path = Path('/sys/module/kvm_intel/parameters/nested')
    amd_path = Path('/sys/module/kvm_amd/parameters/nested')

    nested_enabled = False
    try:
        if intel_path.exists():
            with open(intel_path, 'r', encoding='utf-8') as f:
                value = f.read().strip()
            if value in ('Y', '1'):
                nested_enabled = True
        elif amd_path.exists():
            with open(amd_path, 'r', encoding='utf-8') as f:
                value = f.read().strip()
            if value in ('Y', '1'):
                nested_enabled = True
        else:
            errors.append(
                "Cannot detect KVM module\n"
                "  Check: KVM is enabled (kvm_intel or kvm_amd module loaded)"
            )
            return errors
    except Exception as e:
        errors.append(f"Cannot check nested virt status: {e}")
        return errors

    if not nested_enabled:
        errors.append(
            "Nested virtualization not enabled\n"
            "  For Intel: echo 'options kvm_intel nested=1' > /etc/modprobe.d/kvm-intel.conf\n"
            "  For AMD: echo 'options kvm_amd nested=1' > /etc/modprobe.d/kvm-amd.conf\n"
            "  Then reboot or reload module: modprobe -r kvm_intel && modprobe kvm_intel"
        )

    return errors


# -----------------------------------------------------------------------------
# Provider Lockfile Validation
# -----------------------------------------------------------------------------

def parse_provider_version(providers_tf: Path) -> Optional[str]:
    """Extract provider version constraint from providers.tf.

    Parses HCL to find the bpg/proxmox provider version constraint:
        required_providers {
            proxmox = {
                version = "0.93.0"
            }
        }

    Args:
        providers_tf: Path to providers.tf file

    Returns:
        Version string (e.g., "0.93.0") or None if not found
    """
    import re

    if not providers_tf.exists():
        return None

    try:
        content = providers_tf.read_text()
    except Exception as e:
        logger.warning(f"Cannot read providers.tf: {e}")
        return None

    # Match: version = "X.Y.Z" (with optional spaces)
    # This handles the common case of exact version pinning
    match = re.search(r'version\s*=\s*"([^"]+)"', content)
    if match:
        return match.group(1)

    return None


def parse_lockfile_version(lockfile: Path) -> Optional[str]:
    """Extract provider version from .terraform.lock.hcl.

    Parses HCL to find the bpg/proxmox provider version:
        provider "registry.opentofu.org/bpg/proxmox" {
            version = "0.93.0"
            ...
        }

    Args:
        lockfile: Path to .terraform.lock.hcl file

    Returns:
        Version string (e.g., "0.93.0") or None if not found
    """
    import re

    if not lockfile.exists():
        return None

    try:
        content = lockfile.read_text()
    except Exception as e:
        logger.warning(f"Cannot read lockfile {lockfile}: {e}")
        return None

    # Look for bpg/proxmox provider block and extract version
    # Pattern: provider "...bpg/proxmox" { ... version = "X.Y.Z" ... }
    pattern = r'provider\s+"[^"]*bpg/proxmox"[^}]*version\s*=\s*"([^"]+)"'
    match = re.search(pattern, content, re.DOTALL)
    if match:
        return match.group(1)

    return None


def validate_provider_lockfiles(auto_fix: bool = True,
                                 verbose: bool = False,
                                 _tofu_dir: Optional[Path] = None,
                                 _states_dir: Optional[Path] = None) -> tuple[list[str], list[str]]:
    """Validate provider lockfiles are in sync with version constraints.

    Compares the version in tofu/envs/generic/providers.tf with cached
    lockfiles in $HOMESTAK_ROOT/.state/tofu/*/data/.terraform.lock.hcl.

    When a mismatch is found:
    - auto_fix=True: Delete stale lockfile (will regenerate on next tofu init)
    - auto_fix=False: Return error message with fix instructions

    Args:
        auto_fix: If True, automatically clear stale lockfiles
        verbose: If True, log detailed information
        _tofu_dir: Override tofu directory (for testing)
        _states_dir: Override states directory (for testing)

    Returns:
        Tuple of (errors, fixed) where:
        - errors: List of error messages (empty if all valid or fixed)
        - fixed: List of fixed lockfile descriptions (for reporting)
    """
    from common import get_state_dir
    from config import get_sibling_dir

    errors: list[str] = []
    fixed: list[str] = []

    # Get required version from providers.tf
    tofu_dir = _tofu_dir if _tofu_dir else get_sibling_dir('tofu')
    providers_tf = tofu_dir / 'envs' / 'generic' / 'providers.tf'
    required_version = parse_provider_version(providers_tf)

    if not required_version:
        if verbose:
            logger.info("Could not determine required provider version from providers.tf")
        return errors, fixed

    if verbose:
        logger.info(f"Required provider version: bpg/proxmox {required_version}")

    # Find all cached lockfiles in .state/tofu/
    states_dir = _states_dir if _states_dir else get_state_dir() / 'tofu'
    if not states_dir.exists():
        return errors, fixed  # No state dirs yet, nothing to validate

    stale_lockfiles = []
    for state_dir in states_dir.iterdir():
        if not state_dir.is_dir():
            continue

        lockfile = state_dir / 'data' / '.terraform.lock.hcl'
        if not lockfile.exists():
            continue

        locked_version = parse_lockfile_version(lockfile)
        if not locked_version:
            continue

        if locked_version != required_version:
            stale_lockfiles.append({
                'path': lockfile,
                'env': state_dir.name,
                'locked': locked_version,
                'required': required_version,
            })

    # Handle stale lockfiles
    for item in stale_lockfiles:
        if auto_fix:
            try:
                assert isinstance(item['path'], Path)
                item['path'].unlink()
                msg = f"{item['env']} ({item['locked']} -> {item['required']})"
                fixed.append(msg)
                logger.info(f"Cleared stale lockfile: {msg}")
            except Exception as e:
                errors.append(
                    f"Cannot clear stale lockfile {item['path']}: {e}"
                )
        else:
            errors.append(
                f"Stale provider lockfile in {item['env']}\n"
                f"  Locked: {item['locked']}, Required: {item['required']}\n"
                f"  Fix: rm {item['path']}"
            )

    return errors, fixed


# -----------------------------------------------------------------------------
# Combined Validation
# -----------------------------------------------------------------------------

def validate_readiness(config, scenario_class, timeout: float = 10.0,
                       local_mode: bool = False) -> list[str]:
    """Run all readiness checks for a scenario.

    Args:
        config: HostConfig instance
        scenario_class: Scenario class with requirement attributes
        timeout: Connection timeout for network checks
        local_mode: If True, skip remote connectivity checks

    Returns:
        Combined list of all validation errors
    """
    errors = []

    # Check scenario requirements
    requires_api = getattr(scenario_class, 'requires_api', True)
    requires_host_ssh = getattr(scenario_class, 'requires_host_ssh', True)
    requires_nested_virt = getattr(scenario_class, 'requires_nested_virt', False)

    # API token validation (for local mode, validate local token)
    if requires_api:
        api_token = getattr(config, '_api_token', None) or getattr(config, 'api_token', None)
        if callable(api_token):
            api_token = api_token()
        errors.extend(validate_api_token(
            api_endpoint=config.api_endpoint,
            api_token=api_token or '',
            node_name=config.name
        ))

    # Host availability validation (skip for local mode or if API validation already failed on connection)
    if requires_host_ssh and not local_mode and not any("Cannot connect" in e for e in errors):
        ssh_host = getattr(config, 'ssh_host', None) or getattr(config, 'ip', None)
        errors.extend(validate_host_availability(
            ssh_host=ssh_host or '',
            node_name=config.name,
            check_ssh=requires_host_ssh,
            check_api=requires_api,
            timeout=timeout
        ))

    # Site config validation (gateway, dns_servers must be set for VM provisioning)
    requires_host_config = getattr(scenario_class, 'requires_host_config', True)
    if requires_host_config:
        errors.extend(validate_site_config(config))

    # Nested virtualization check (for tiered PVE scenarios)
    if requires_nested_virt:
        errors.extend(validate_nested_virt())

    # Provider lockfile validation (auto-fix stale lockfiles)
    lockfile_errors, _ = validate_provider_lockfiles(auto_fix=True)
    errors.extend(lockfile_errors)

    return errors


def run_preflight_checks(local_mode: bool = True,  # pylint: disable=unused-argument
                         hostname: Optional[str] = None,
                         check_nested_virt: bool = False,
                         verbose: bool = False) -> tuple[bool, dict]:
    """Run standalone preflight checks.

    This provides a comprehensive check of the system's readiness
    for running homestak scenarios.

    Args:
        local_mode: If True, run checks for local host
        hostname: Hostname to check (defaults to current hostname)
        check_nested_virt: Include nested virtualization check
        verbose: Print detailed output

    Returns:
        (success, results) tuple where results contains check details
    """
    from config import get_site_config_dir
    from config_resolver import ConfigResolver

    results: dict[str, dict[str, list[str]]] = {
        'bootstrap': {'passed': [], 'failed': []},
        'site_config': {'passed': [], 'failed': []},
        'pve': {'passed': [], 'failed': []},
        'tofu': {'passed': [], 'failed': []},
        'hardware': {'passed': [], 'failed': []},
    }

    if hostname is None:
        hostname = socket.gethostname()

    # Bootstrap checks
    bootstrap_errors = validate_bootstrap_installed()
    if bootstrap_errors:
        results['bootstrap']['failed'].extend(bootstrap_errors)
    else:
        _lib_path, etc_path = get_homestak_paths()
        results['bootstrap']['passed'].append(f"{etc_path} exists")
        results['bootstrap']['passed'].append("Core repos present: ansible, iac-driver, tofu")

    # Site configuration checks
    site_errors = validate_site_init_complete(hostname)
    if site_errors:
        results['site_config']['failed'].extend(site_errors)
    else:
        _, etc_path = get_homestak_paths()
        results['site_config']['passed'].append("secrets.yaml decrypted")
        results['site_config']['passed'].append(f"nodes/{hostname}.yaml exists")

    # PVE connectivity checks (only if site config is valid)
    if not results['site_config']['failed']:  # pylint: disable=too-many-nested-blocks
        try:
            site_config_dir = get_site_config_dir()
            resolver = ConfigResolver(str(site_config_dir))

            # Load secrets to get API token
            secrets = resolver._load_yaml(site_config_dir / 'secrets.yaml')  # pylint: disable=protected-access
            api_token = secrets.get('api_tokens', {}).get(hostname)

            # Load node config to get API endpoint
            node_path = site_config_dir / 'nodes' / f'{hostname}.yaml'
            if node_path.exists():
                node_config = resolver._load_yaml(node_path)  # pylint: disable=protected-access
                api_endpoint = node_config.get('api_endpoint', 'https://localhost:8006')

                pve_errors = validate_api_token(api_endpoint, api_token, hostname)
                if pve_errors:
                    results['pve']['failed'].extend(pve_errors)
                else:
                    # Get PVE version from successful validation
                    try:
                        resp = requests.get(
                            f"{api_endpoint}/api2/json/version",
                            headers={"Authorization": f"PVEAPIToken={api_token}"},
                            verify=False,
                            timeout=10
                        )
                        if resp.status_code == 200:
                            version = resp.json().get("data", {}).get("version", "unknown")
                            results['pve']['passed'].append(f"API token valid (PVE {version})")
                    except Exception:
                        results['pve']['passed'].append("API token valid")
        except Exception as e:
            results['pve']['failed'].append(f"Cannot validate PVE: {e}")

    # Tofu provider lockfile checks
    try:
        from common import get_state_dir
        from config import get_sibling_dir

        lockfile_errors, lockfile_fixed = validate_provider_lockfiles(
            auto_fix=True, verbose=verbose
        )

        if lockfile_errors:
            results['tofu']['failed'].extend(lockfile_errors)
        else:
            # Get current version for display
            providers_tf = get_sibling_dir('tofu') / 'envs' / 'generic' / 'providers.tf'
            version = parse_provider_version(providers_tf)
            if version:
                results['tofu']['passed'].append(f"Provider version: bpg/proxmox {version}")

            # Report state directories status
            states_dir = get_state_dir() / 'tofu'
            if states_dir.exists():
                state_count = len([d for d in states_dir.iterdir() if d.is_dir()])
                if state_count > 0:
                    if lockfile_fixed:
                        results['tofu']['passed'].append(
                            f"Lockfiles in sync ({state_count} state dirs, "
                            f"{len(lockfile_fixed)} cleared)"
                        )
                    else:
                        results['tofu']['passed'].append(
                            f"Lockfiles in sync ({state_count} state dirs)"
                        )
    except Exception as e:
        results['tofu']['failed'].append(f"Cannot validate tofu: {e}")

    # Hardware checks
    if check_nested_virt:
        nested_errors = validate_nested_virt()
        if nested_errors:
            results['hardware']['failed'].extend(nested_errors)
        else:
            results['hardware']['passed'].append("Nested virtualization enabled")

    # Get system resources
    try:
        cpu_count = os.cpu_count() or 0
        results['hardware']['passed'].append(f"CPU cores: {cpu_count}")

        # Get memory info
        with open('/proc/meminfo', 'r', encoding='utf-8') as f:
            for line in f:
                if line.startswith('MemTotal:'):
                    mem_kb = int(line.split()[1])
                    mem_gb = mem_kb // (1024 * 1024)
                    results['hardware']['passed'].append(f"Memory: {mem_gb}GB")
                    break
    except Exception:
        pass  # Non-critical - just skip resource info

    # Determine overall success
    all_failed = []
    for category in results.values():
        all_failed.extend(category['failed'])

    return len(all_failed) == 0, results


def format_preflight_results(hostname: str, results: dict) -> str:
    """Format preflight check results for display.

    Args:
        hostname: Hostname that was checked
        results: Results dict from run_preflight_checks

    Returns:
        Formatted string for display
    """
    lines = [f"\nPreflight checks for local host '{hostname}':\n"]

    category_names = {
        'bootstrap': 'Bootstrap',
        'site_config': 'Site configuration',
        'pve': 'PVE connectivity',
        'tofu': 'Tofu providers',
        'hardware': 'Hardware',
    }

    for key, name in category_names.items():
        category = results.get(key, {'passed': [], 'failed': []})
        if category['passed'] or category['failed']:
            lines.append(f"{name}:")
            for item in category['passed']:
                lines.append(f"✓ {item}")
            for item in category['failed']:
                # Handle multi-line errors
                first_line = item.split('\n')[0]
                lines.append(f"✗ {first_line}")
                for line in item.split('\n')[1:]:
                    lines.append(f"  {line}")
            lines.append("")

    # Final summary
    all_passed = all(
        len(cat['failed']) == 0
        for cat in results.values()
    )

    if all_passed:
        lines.append("All checks passed. Ready for scenarios.")
    else:
        lines.append("Some checks failed. Fix issues before running scenarios.")

    return '\n'.join(lines)
