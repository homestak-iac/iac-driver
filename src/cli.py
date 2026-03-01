#!/usr/bin/env python3
"""CLI entry point for iac-driver.

Supports noun-action subcommands and legacy scenario workflows:
- Noun-action: ./run.sh manifest apply -M n2 -H srv1
- Scenarios: ./run.sh scenario run pve-setup -H srv1

Nouns:
- manifest: Infrastructure lifecycle (apply/destroy/test)
- config: Specification management (fetch/apply)
- server: Server daemon management (start/stop/status)
- scenario: Standalone scenario workflows (run)
- token: Provisioning token utilities (inspect)
"""

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path

from config import list_hosts, load_host_config, get_base_dir
from scenarios import Orchestrator, get_scenario, list_scenarios
from validation import validate_readiness, run_preflight_checks, format_preflight_results

# Noun commands (noun-action subcommands)
NOUN_COMMANDS = {
    "manifest": "Infrastructure lifecycle (apply/destroy/test)",
    "config": "Specification management (fetch/apply)",
    "server": "Server daemon management (start/stop/status)",
    "scenario": "Standalone scenario workflows (run)",
    "token": "Provisioning token utilities (inspect)",
}


def _is_ip_address(value: str) -> bool:
    """Check if value is a valid IPv4 address."""
    parts = value.split('.')
    if len(parts) != 4:
        return False
    try:
        return all(0 <= int(p) <= 255 for p in parts)
    except ValueError:
        return False


def _parse_host_arg(value: str) -> tuple[str | None, str]:
    """Parse user@host syntax from -H flag.

    Returns:
        (user, host) tuple. user is None if no @ present.
    """
    if '@' in value:
        user, host = value.split('@', 1)
        return (user or None, host)
    return (None, value)


def _create_ip_config(ip: str, ssh_user: str | None = None):
    """Create a HostConfig for a raw IP address (no site-config lookup)."""
    from config import HostConfig
    config = HostConfig(name=ip, config_file=Path('/dev/null'))
    config.ssh_host = ip
    if ssh_user:
        config.ssh_user = ssh_user
    config.is_host_only = True
    return config


def dispatch_manifest(argv: list) -> int:
    """Dispatch 'manifest' noun to action-specific handler.

    Args:
        argv: Arguments after 'manifest' (e.g., ['apply', '-M', 'n1', '-H', 'srv1'])

    Returns:
        Exit code
    """
    if not argv or argv[0].startswith('-'):
        print("Usage: ./run.sh manifest <action> [options]")
        print()
        print("Actions:")
        print("  apply     Create infrastructure from manifest")
        print("  destroy   Destroy infrastructure from manifest")
        print("  test      Create, verify, and destroy infrastructure")
        print("  validate  Validate manifest structure and FK references")
        print()
        print("Run './run.sh manifest <action> --help' for action-specific options.")
        return 1 if not argv else 0

    action = argv[0]
    rest = argv[1:]

    if action == "apply":
        from manifest_opr.cli import apply_main
        rc: int = apply_main(rest)
        return rc
    if action == "destroy":
        from manifest_opr.cli import destroy_main
        rc = destroy_main(rest)
        return rc
    if action == "test":
        from manifest_opr.cli import test_main
        rc = test_main(rest)
        return rc
    if action == "validate":
        from manifest_opr.cli import validate_main
        rc = validate_main(rest)
        return rc

    print(f"Error: Unknown manifest action '{action}'")
    print("Available actions: apply, destroy, test, validate")
    return 1


def dispatch_noun(noun: str, argv: list) -> int:
    """Dispatch to noun-specific CLI handler.

    Args:
        noun: The noun command (e.g., "manifest", "server")
        argv: Remaining command line arguments

    Returns:
        Exit code
    """
    if noun == "manifest":
        return dispatch_manifest(argv)

    if noun == "server":
        from server.cli import main as server_main
        rc: int = server_main(argv)
        return rc

    if noun == "config":
        from config_apply import config_main
        rc = config_main(argv)
        return rc

    if noun == "token":
        from token_cli import main as token_main
        rc = token_main(argv)
        return rc

    # 'scenario' noun is handled in main() before reaching here
    print(f"Error: Noun '{noun}' not yet implemented")
    return 1


def get_version():
    """Get version from git tags (do not use hardcoded VERSION constant)."""
    try:
        result = subprocess.run(
            ['git', 'describe', '--tags', '--abbrev=0'],
            capture_output=True, text=True,
            cwd=Path(__file__).parent,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else 'dev'
    except Exception:
        return 'dev'

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)


def create_local_config():
    """Create HostConfig for local execution with auto-derived values.

    Derives API endpoint and attempts to load API token for current hostname.
    Used when --local flag is specified.
    """
    from config import HostConfig
    from config_resolver import ConfigResolver

    hostname = socket.gethostname()
    config = HostConfig(
        name='local',
        config_file=Path('/dev/null'),
    )

    # Derive API endpoint for local PVE
    config.api_endpoint = 'https://localhost:8006'
    config.ssh_host = 'localhost'

    # Try to load API token for current host
    try:
        resolver = ConfigResolver()
        token = resolver.secrets.get('api_tokens', {}).get(hostname)
        if token:
            config.set_api_token(token)
            logger.info(f"Loaded API token for {hostname}")
        else:
            logger.debug(f"No API token found for hostname '{hostname}'")
    except FileNotFoundError:
        logger.debug("secrets.yaml not found, skipping API token loading")
    except Exception as e:
        logger.debug(f"Could not load API token for localhost: {e}")

    return config


def print_usage():
    """Print top-level usage showing noun commands."""
    print(f"iac-driver {get_version()}")
    print()
    print("Usage: ./run.sh <noun> <action> [options]")
    print()
    print("Commands:")
    for noun, desc in NOUN_COMMANDS.items():
        print(f"  {noun:<12} {desc}")
    print()
    print("Run './run.sh <noun> --help' for command-specific options.")
    print()
    print("Examples:")
    print("  ./run.sh manifest apply -M n1-push -H srv1")
    print("  ./run.sh manifest test -M n2-tiered -H srv1")
    print("  ./run.sh config fetch --insecure")
    print("  ./run.sh config apply")
    print("  ./run.sh server start --port 44443")
    print("  ./run.sh scenario run pve-setup -H srv1")


def _handle_scenario_verb() -> tuple[bool, int | None]:
    """Rewrite 'scenario run <name>' to '--scenario <name>' format.

    Mutates sys.argv in place when the first argument is 'scenario'.

    Returns:
        (from_verb, exit_code): from_verb is True if scenario verb was processed.
        If exit_code is not None, main() should return it.
    """
    if len(sys.argv) <= 1 or sys.argv[1] != 'scenario':
        return (False, None)

    # Handle 'scenario run <name>' (new) or 'scenario <name>' (old)
    if len(sys.argv) >= 3 and sys.argv[2] == 'run':
        # New syntax: scenario run pve-setup -H srv1
        if len(sys.argv) < 4 or sys.argv[3].startswith('-'):
            print("Usage: ./run.sh scenario run <name> [options]")
            print("\nRun './run.sh scenario run --help' to list available scenarios.")
            return (True, 1 if len(sys.argv) < 4 else 0)
        sys.argv = [sys.argv[0], '--scenario', sys.argv[3]] + sys.argv[4:]
    elif len(sys.argv) < 3 or sys.argv[2].startswith('-'):
        # Show scenario list or help
        if '--help' in sys.argv or '-h' in sys.argv:
            # Rewrite as --list-scenarios for help
            sys.argv = [sys.argv[0], '--list-scenarios']
        else:
            print("Usage: ./run.sh scenario run <name> [options]")
            print("\nRun './run.sh scenario --help' to list available scenarios.")
            return (True, 1 if len(sys.argv) < 3 else 0)
    else:
        # Legacy syntax: scenario pve-setup -H srv1 (still supported)
        sys.argv = [sys.argv[0], '--scenario', sys.argv[2]] + sys.argv[3:]

    return (True, None)


def _resolve_host(args, scenario, available_hosts):
    """Resolve HostConfig from CLI arguments.

    Handles raw IP, named host, local auto-detect, and user@ syntax.

    Returns:
        (config, exit_code): HostConfig on success (exit_code=None),
        or (None, exit_code) on error.
    """
    requires_root = getattr(scenario, 'requires_root', False)
    requires_host_config = getattr(scenario, 'requires_host_config', True)

    # Check root requirement for --local mode
    if args.local and requires_root and os.getuid() != 0:
        print(f"Error: Scenario '{args.scenario}' requires root privileges in --local mode")
        print("Run with sudo or as root")
        return (None, 1)

    # Handle --host resolution (supports user@host syntax)
    host_arg = args.host
    ssh_user_override = None

    if host_arg:
        ssh_user_override, host = _parse_host_arg(host_arg)
    else:
        host = None

    # Auto-detect host from hostname when --local and no --host
    if args.local and not host:
        hostname = socket.gethostname()
        if hostname in available_hosts:
            host = hostname
            logger.debug(f"Auto-detected host from hostname: {host}")
        elif not requires_host_config:
            # Scenario doesn't need host config, proceed without
            host = None
            logger.debug(f"No host config needed for scenario '{args.scenario}'")
        else:
            print(f"Error: Could not auto-detect host. Hostname '{hostname}' not in available hosts.")
            print(f"Available hosts: {', '.join(available_hosts) if available_hosts else 'none configured'}")
            print("\nEither:")
            print(f"  1. Create nodes/{hostname}.yaml in site-config")
            print("  2. Specify --host explicitly")
            return (None, 1)

    # Validate --host is provided for scenarios that need it (when not in --local mode)
    if not args.local and requires_host_config and not host:
        print(f"Error: --host is required for scenario '{args.scenario}'")
        print(f"Available hosts: {', '.join(available_hosts) if available_hosts else 'none configured'}")
        print(f"\nUsage: ./run.sh --scenario {args.scenario} --host <host>")
        return (None, 1)

    # Validate --host value if provided
    is_raw_ip = host and _is_ip_address(host)
    if host and not is_raw_ip and host not in available_hosts:
        print(f"Error: Unknown host '{host}'")
        print(f"Available hosts: {', '.join(available_hosts) if available_hosts else 'none configured'}")
        return (None, 1)

    # Load config (use local config with auto-derived values for --local)
    if is_raw_ip:
        assert host is not None  # guaranteed by _is_ip_address check
        config = _create_ip_config(host, ssh_user=ssh_user_override)
        logger.info(f"Using raw IP: {host} (no site-config lookup)")
    elif host:
        config = load_host_config(host)
    else:
        # Create local config with auto-derived API endpoint and token
        config = create_local_config()

    # Apply user@ override if specified
    if ssh_user_override and not is_raw_ip:
        config.ssh_user = ssh_user_override

    # Override packer release if specified (CLI takes precedence)
    if args.packer_release:
        config.packer_release = args.packer_release
        logger.info(f"Using packer release override: {args.packer_release}")

    return (config, None)


def _setup_context(args, orchestrator) -> int | None:
    """Populate orchestrator context from CLI arguments.

    Loads context file, pre-populates from args (node_ip, local_mode, etc).

    Returns:
        exit_code on error, None on success.
    """
    # Load context from file if specified and exists
    if args.context_file and args.context_file.exists():
        try:
            with open(args.context_file, encoding="utf-8") as f:
                loaded_context = json.load(f)
            orchestrator.context.update(loaded_context)
            logger.info(f"Loaded context from {args.context_file}: {list(loaded_context.keys())}")
        except json.JSONDecodeError as e:
            print(f"Error: Invalid JSON in context file {args.context_file}: {e}")
            return 1
        except Exception as e:
            print(f"Error reading context file {args.context_file}: {e}")
            return 1

    # Pre-populate context if node-ip provided
    if args.node_ip:
        orchestrator.context['node_ip'] = args.node_ip

    # Pre-populate context for pve-setup and user-setup scenarios
    if args.local:
        orchestrator.context['local_mode'] = True
    if args.homestak_user:
        orchestrator.context['homestak_user'] = args.homestak_user

    # Pre-populate context with VM ID overrides
    if args.vm_id:
        vm_id_overrides = {}
        for override in args.vm_id:
            if '=' not in override:
                print(f"Error: Invalid --vm-id format '{override}'. Expected NAME=VMID (e.g., test=99990)")
                return 1
            name, vmid_str = override.split('=', 1)
            if not name:
                print(f"Error: Invalid --vm-id format '{override}'. VM name cannot be empty.")
                return 1
            try:
                vmid = int(vmid_str)
            except ValueError:
                print(f"Error: Invalid --vm-id format '{override}'. VMID must be an integer.")
                return 1
            vm_id_overrides[name] = vmid
        orchestrator.context['vm_id_overrides'] = vm_id_overrides
        logger.info(f"VM ID overrides: {vm_id_overrides}")

    return None


def _handle_results(args, orchestrator, success: bool) -> int:
    """Handle JSON output, context saving, and return exit code."""
    # Output JSON if requested
    if args.json_output:
        report_data = orchestrator.report.to_dict(orchestrator.context)
        print(json.dumps(report_data, indent=2))

    # Save context to file if specified
    if args.context_file:
        try:
            # Convert any non-serializable values to strings
            serializable_context = {}
            for key, value in orchestrator.context.items():
                try:
                    json.dumps(value)
                    serializable_context[key] = value
                except (TypeError, ValueError):
                    serializable_context[key] = str(value)

            with open(args.context_file, 'w', encoding="utf-8") as f:
                json.dump(serializable_context, f, indent=2)
            logger.info(f"Saved context to {args.context_file}: {list(serializable_context.keys())}")
        except Exception as e:
            logger.warning(f"Failed to save context to {args.context_file}: {e}")

    return 0 if success else 1


def main():
    """CLI entry point — dispatch to noun-action handlers or legacy scenario runner."""
    # Handle scenario verb syntax (rewrites sys.argv if 'scenario' command)
    from_verb, exit_code = _handle_scenario_verb()
    if exit_code is not None:
        return exit_code

    if len(sys.argv) > 1:
        first_arg = sys.argv[1]

        # Handle other nouns (manifest, server, config, token) — not scenario
        if not from_verb:
            if first_arg in NOUN_COMMANDS:
                return dispatch_noun(first_arg, sys.argv[2:])

            # Show usage when no recognized command
            if not first_arg.startswith('-'):
                print(f"Error: Unknown command '{first_arg}'")
                print_usage()
                return 1

    # Show top-level usage when no arguments
    if len(sys.argv) == 1:
        print_usage()
        return 0

    # Scenario-based CLI continues below
    available_hosts = list_hosts()
    available_scenarios = list_scenarios()

    parser = argparse.ArgumentParser(
        description='Infrastructure-as-Code Driver - Orchestrates provisioning and testing workflows'
    )
    parser.add_argument(
        '--version',
        action='version',
        version=f'iac-driver {get_version()}'
    )
    parser.add_argument(
        '--scenario', '-S',
        choices=available_scenarios,
        help=argparse.SUPPRESS  # Hidden: use 'scenario run' verb instead
    )
    parser.add_argument(
        '--host', '-H',
        help=f'Target host: named host from site-config or raw IP. Available: {", ".join(available_hosts) if available_hosts else "none configured"}'
    )
    parser.add_argument(
        '--report-dir', '-r',
        type=Path,
        default=get_base_dir() / 'reports',
        help='Directory for test reports'
    )
    parser.add_argument(
        '--skip', '-s',
        action='append',
        default=[],
        help='Phases to skip (can be repeated)'
    )
    parser.add_argument(
        '--list-scenarios',
        action='store_true',
        help='List available scenarios and exit'
    )
    parser.add_argument(
        '--list-phases',
        action='store_true',
        help='List phases for the selected scenario and exit'
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging'
    )
    parser.add_argument(
        '--node-ip',
        help='PVE node VM IP (auto-detected if not provided, required when skipping provision phases)'
    )
    parser.add_argument(
        '--local',
        action='store_true',
        help='Run scenario locally (for pve-setup, user-setup)'
    )
    parser.add_argument(
        '--homestak-user',
        help='Create this user during bootstrap (for bootstrap-install scenario)'
    )
    parser.add_argument(
        '--context-file', '-C',
        type=Path,
        help='Save/load scenario context to file for chained runs (e.g., constructor then destructor)'
    )
    parser.add_argument(
        '--packer-release',
        help='Packer release tag for image downloads (e.g., v0.8.0-rc1 or latest). Overrides site.yaml default.'
    )
    parser.add_argument(
        '--timeout', '-t',
        type=int,
        help='Overall scenario timeout in seconds. Checked between phases (does not interrupt running phases).'
    )
    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Skip confirmation prompt for destructive scenarios'
    )
    parser.add_argument(
        '--vm-id',
        action='append',
        metavar='NAME=VMID',
        help='Override VM ID (repeatable): --vm-id test=99990 --vm-id inner=99912'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be executed without running actions'
    )
    parser.add_argument(
        '--preflight',
        action='store_true',
        help='Run preflight checks only (no scenario execution)'
    )
    parser.add_argument(
        '--skip-preflight',
        action='store_true',
        help='Skip preflight checks before scenario execution'
    )
    parser.add_argument(
        '--json-output',
        action='store_true',
        help='Output structured JSON to stdout (logs go to stderr)'
    )

    # Manifest arguments for recursive-pve scenarios
    parser.add_argument(
        '--manifest', '-M',
        help='Manifest name from site-config/manifests/ (for recursive-pve scenarios)'
    )
    parser.add_argument(
        '--manifest-file',
        type=Path,
        help='Path to manifest file (for recursive-pve scenarios)'
    )
    parser.add_argument(
        '--manifest-json',
        help='Inline manifest JSON (for recursive calls, not user-facing)'
    )
    parser.add_argument(
        '--keep-on-failure',
        action='store_true',
        help='Keep levels on failure for debugging (for recursive-pve scenarios)'
    )
    parser.add_argument(
        '--depth',
        type=int,
        help='Limit manifest to first N levels (for recursive-pve scenarios)'
    )

    args = parser.parse_args()

    # Configure logging for --json-output mode
    if args.json_output:
        # Remove existing handlers and redirect to stderr
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        ))
        root_logger.addHandler(stderr_handler)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Handle --preflight mode (standalone check, no scenario)
    if args.preflight:
        hostname = socket.gethostname()
        # Check if tiered PVE scenario would be run (for nested virt check)
        check_nested = args.scenario and 'child-pve' in args.scenario

        logger.info(f"Running preflight checks for {hostname}")
        success, results = run_preflight_checks(
            local_mode=args.local or not args.host,
            hostname=hostname,
            check_nested_virt=check_nested,
            verbose=args.verbose
        )

        print(format_preflight_results(hostname, results))
        return 0 if success else 1

    if args.list_scenarios or args.scenario is None:
        print("Available scenarios:")
        for name in available_scenarios:
            scenario = get_scenario(name)
            runtime = getattr(scenario, 'expected_runtime', None)
            if runtime:
                # Format runtime nicely (e.g., 30 -> "~30s", 540 -> "~9m")
                if runtime >= 60:
                    runtime_str = f"~{runtime // 60}m"
                else:
                    runtime_str = f"~{runtime}s"
                print(f"  {name:30} {runtime_str:>6}  {scenario.description}")
            else:
                print(f"  {name:30}         {scenario.description}")
        if args.scenario is None:
            print("\nUsage: ./run.sh --scenario <name> --host <host>")
        return 0

    # Get scenario to check its requirements
    scenario = get_scenario(args.scenario)

    # Resolve host configuration
    config, exit_code = _resolve_host(args, scenario, available_hosts)
    if exit_code is not None:
        return exit_code

    # Load manifest for recursive-pve scenarios
    if args.scenario and 'recursive-pve' in args.scenario:
        from manifest import load_manifest, ConfigError as ManifestConfigError
        try:
            manifest = load_manifest(
                name=args.manifest,
                file_path=str(args.manifest_file) if args.manifest_file else None,
                json_str=args.manifest_json,
                depth=args.depth
            )
            # Set manifest on scenario
            scenario.manifest = manifest
            # Set keep_on_failure flag
            if hasattr(scenario, 'keep_on_failure'):
                scenario.keep_on_failure = args.keep_on_failure
            logger.info(f"Loaded manifest: {manifest.name} (depth={manifest.depth})")
        except ManifestConfigError as e:
            print(f"Error loading manifest: {e}")
            return 1

    # Pre-flight validation (skip for --skip-preflight, --dry-run, --list-phases)
    if not args.skip_preflight and not args.dry_run and not args.list_phases:
        scenario_class = type(scenario)
        errors = validate_readiness(
            config,
            scenario_class,
            local_mode=args.local
        )
        if errors:
            print("\nPre-flight validation failed:")
            for error in errors:
                # Indent multi-line errors
                for i, line in enumerate(error.split('\n')):
                    prefix = "  ✗ " if i == 0 else "    "
                    print(f"{prefix}{line}")
            print("\nUse --skip-preflight to bypass these checks")
            print()
            return 1
        logger.info("Pre-flight validation passed")

    if args.list_phases:
        print(f"Phases for scenario '{args.scenario}':")
        for name, _action, desc in scenario.get_phases(config):
            print(f"  {name}: {desc}")
        return 0

    # Create orchestrator
    orchestrator = Orchestrator(
        scenario=scenario,
        config=config,
        report_dir=args.report_dir,
        skip_phases=args.skip,
        timeout=args.timeout,
        dry_run=args.dry_run
    )

    # Setup context from CLI arguments
    exit_code = _setup_context(args, orchestrator)
    if exit_code is not None:
        return exit_code

    # Check for confirmation on destructive scenarios
    if getattr(scenario, 'requires_confirmation', False) and not args.yes:
        print(f"\nWARNING: '{args.scenario}' is a destructive scenario.")
        print(f"Target: {config.name}")
        print("\nThis action cannot be undone.")
        response = input("Continue? [y/N] ").strip().lower()
        if response != 'y':
            print("Aborted.")
            return 1

    success = orchestrator.run()
    return _handle_results(args, orchestrator, success)


if __name__ == '__main__':
    sys.exit(main())
