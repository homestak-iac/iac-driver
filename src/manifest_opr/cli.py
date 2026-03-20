"""CLI handlers for manifest-based verb commands (apply, destroy, test, validate).

Usage:
    ./run.sh manifest apply -M <manifest> -H <host> [--dry-run] [--json-output] [--verbose]
    ./run.sh manifest destroy -M <manifest> -H <host> [--dry-run] [--yes]
    ./run.sh manifest test -M <manifest> -H <host> [--dry-run] [--json-output]
    ./run.sh manifest validate -M <manifest> [--verbose]
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from config import load_host_config, list_hosts
from manifest import load_manifest
from manifest_opr.executor import NodeExecutor
from manifest_opr.graph import ManifestGraph
from validation import validate_readiness

logger = logging.getLogger(__name__)


def _common_parser(verb: str) -> argparse.ArgumentParser:
    """Build argument parser with common options for all verbs."""
    parser = argparse.ArgumentParser(
        prog=f'run.sh manifest {verb}',
        description=f'{verb.capitalize()} infrastructure from manifest',
    )
    parser.add_argument(
        '--manifest', '-M',
        help='Manifest name from config/manifests/',
    )
    parser.add_argument(
        '--manifest-file',
        help='Path to manifest file',
    )
    parser.add_argument(
        '--manifest-json',
        help='Inline manifest JSON',
    )
    parser.add_argument(
        '--host', '-H',
        required=True,
        help=f'Target PVE host. Available: {", ".join(list_hosts())}',
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview operations without executing',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Enable verbose logging',
    )
    parser.add_argument(
        '--json-output',
        action='store_true',
        help='Output structured JSON to stdout (logs to stderr)',
    )
    parser.add_argument(
        '--depth',
        type=int,
        help='Limit manifest to first N levels',
    )
    parser.add_argument(
        '--skip-preflight',
        action='store_true',
        help='Skip pre-flight validation checks',
    )
    parser.add_argument(
        '--self-addr',
        help='Routable address of this host for HOMESTAK_SERVER '
             '(override: HOMESTAK_SELF_ADDR env var)',
    )
    return parser


def _setup_logging(verbose: bool, json_output: bool) -> None:
    """Configure logging based on flags."""
    if json_output:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            root_logger.removeHandler(handler)
        stderr_handler = logging.StreamHandler(sys.stderr)
        stderr_handler.setFormatter(logging.Formatter(
            '%(asctime)s [%(levelname)s] %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S',
        ))
        root_logger.addHandler(stderr_handler)

    if verbose:
        logging.getLogger().setLevel(logging.DEBUG)


def _parse_host_arg(value: str) -> tuple[str | None, str]:
    """Parse user@host syntax from -H flag.

    Returns:
        (user, host) tuple. user is None if no @ present.
    """
    if '@' in value:
        user, host = value.split('@', 1)
        return (user or None, host)
    return (None, value)


def _load_manifest_and_config(args):
    """Load manifest and host config from parsed args.

    Returns:
        (manifest, config) tuple

    Raises:
        SystemExit: On validation errors
    """
    # Require explicit manifest source
    if not args.manifest and not args.manifest_file and not args.manifest_json:
        print("Error: specify a manifest with -M, --manifest-file, or --manifest-json",
              file=sys.stderr)
        sys.exit(1)

    # Load manifest
    try:
        manifest = load_manifest(
            name=args.manifest,
            file_path=args.manifest_file,
            json_str=args.manifest_json,
            depth=args.depth,
        )
    except Exception as e:
        print(f"Error loading manifest: {e}", file=sys.stderr)
        sys.exit(1)

    if manifest.schema_version != 2 or not manifest.nodes:
        print("Error: Verb commands require a v2 manifest with nodes[]", file=sys.stderr)
        sys.exit(1)

    # Parse user@host syntax
    ssh_user_override, host = _parse_host_arg(args.host)

    # Load host config
    available = list_hosts()
    if host not in available:
        print(f"Error: Unknown host '{host}'. Available: {', '.join(available)}", file=sys.stderr)
        sys.exit(1)

    config = load_host_config(host)
    if ssh_user_override:
        config.ssh_user = ssh_user_override
    return manifest, config


def _manifest_requires_nested_virt(manifest) -> bool:
    """Check if manifest has PVE nodes with children (requires nested virt)."""
    pve_names = {n.name for n in manifest.nodes if n.type == 'pve'}
    for node in manifest.nodes:
        if node.parent in pve_names:
            return True
    return False


def _run_preflight(args, config, manifest) -> int | None:
    """Run preflight checks for verb commands.

    Returns:
        None if checks pass, exit code (1) if checks fail.
    """
    if args.skip_preflight or args.dry_run:
        return None

    # Build a namespace with requirement attributes for validate_readiness()
    class _VerbRequirements:
        requires_api = True
        requires_host_ssh = True
        requires_nested_virt = _manifest_requires_nested_virt(manifest)

    errors = validate_readiness(config, _VerbRequirements)

    # Check packer images exist for root-level nodes
    ssh_host = getattr(config, 'ssh_host', None) or getattr(config, 'ip', None)
    is_local = ssh_host in (None, 'localhost', '127.0.0.1', '::1')
    for node in manifest.nodes:
        if getattr(node, 'parent', None) is None and getattr(node, 'image', None):
            img_path = f'/var/lib/vz/template/iso/{node.image}.img'
            if is_local:
                exists = Path(img_path).exists()
            else:
                import getpass
                from common import run_ssh
                ssh_user = getpass.getuser()
                rc, _, _ = run_ssh(ssh_host, f'test -f {img_path}', user=ssh_user, timeout=10)
                exists = rc == 0
            if not exists:
                errors.append(
                    f"Packer image not found: {img_path}\n"
                    f"  Run: sudo homestak images download {node.image} --publish"
                )

    if errors:
        print("\nPre-flight validation failed:")
        for error in errors:
            for i, line in enumerate(error.split('\n')):
                prefix = "  \u2717 " if i == 0 else "    "
                print(f"{prefix}{line}")
        print("\nUse --skip-preflight to bypass these checks")
        print()
        return 1
    logger.info("Pre-flight validation passed")
    return None


def _emit_json(verb: str, success: bool, state, duration: float) -> None:
    """Emit structured JSON output."""
    nodes = []
    for name, ns in state.nodes.items():
        node_data = {'name': name, 'status': ns.status}
        if ns.vm_id is not None:
            node_data['vm_id'] = ns.vm_id
        if ns.ip is not None:
            node_data['ip'] = ns.ip
        if ns.duration is not None:
            node_data['duration'] = round(ns.duration, 2)
        if ns.error is not None:
            node_data['error'] = ns.error
        nodes.append(node_data)

    output = {
        'verb': verb,
        'success': success,
        'duration_seconds': round(duration, 2),
        'nodes': nodes,
    }
    print(json.dumps(output, indent=2))


def apply_main(argv: list) -> int:
    """Handle 'manifest apply' verb."""
    parser = _common_parser('apply')
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.json_output)

    manifest, config = _load_manifest_and_config(args)

    preflight_rc = _run_preflight(args, config, manifest)
    if preflight_rc is not None:
        return preflight_rc

    graph = ManifestGraph(manifest)

    logger.info(f"Creating infrastructure from manifest '{manifest.name}' on {config.name}")

    executor = NodeExecutor(
        manifest=manifest,
        graph=graph,
        config=config,
        dry_run=args.dry_run,
        json_output=args.json_output,
        self_addr=getattr(args, 'self_addr', None),

    )

    start = time.time()
    context: dict = {}
    success, state = executor.create(context)
    duration = time.time() - start

    if args.json_output:
        _emit_json('apply', success, state, duration)

    return 0 if success else 1


def destroy_main(argv: list) -> int:
    """Handle 'manifest destroy' verb."""
    parser = _common_parser('destroy')
    parser.add_argument(
        '--yes', '-y',
        action='store_true',
        help='Skip confirmation prompt',
    )
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.json_output)

    manifest, config = _load_manifest_and_config(args)

    preflight_rc = _run_preflight(args, config, manifest)
    if preflight_rc is not None:
        return preflight_rc

    graph = ManifestGraph(manifest)

    # Confirmation for destructive operation
    if not args.dry_run and not args.yes:
        print(f"\nWARNING: This will destroy all nodes in manifest '{manifest.name}'.")
        print(f"Target host: {config.name}")
        print("This action cannot be undone.")
        response = input("Continue? [y/N] ").strip().lower()
        if response != 'y':
            print("Aborted.")
            return 1

    logger.info(f"Destroying infrastructure from manifest '{manifest.name}' on {config.name}")

    executor = NodeExecutor(
        manifest=manifest,
        graph=graph,
        config=config,
        dry_run=args.dry_run,
        json_output=args.json_output,
        self_addr=getattr(args, 'self_addr', None),

    )

    start = time.time()
    context: dict = {}
    success, state = executor.destroy(context)
    duration = time.time() - start

    if args.json_output:
        _emit_json('destroy', success, state, duration)

    return 0 if success else 1


def test_main(argv: list) -> int:
    """Handle 'manifest test' verb."""
    from reporting import TestReport

    parser = _common_parser('test')
    args = parser.parse_args(argv)
    _setup_logging(args.verbose, args.json_output)

    manifest, config = _load_manifest_and_config(args)

    preflight_rc = _run_preflight(args, config, manifest)
    if preflight_rc is not None:
        return preflight_rc

    graph = ManifestGraph(manifest)

    logger.info(f"Testing infrastructure from manifest '{manifest.name}' on {config.name}")

    executor = NodeExecutor(
        manifest=manifest,
        graph=graph,
        config=config,
        dry_run=args.dry_run,
        json_output=args.json_output,
        self_addr=getattr(args, 'self_addr', None),

    )

    # Dry-run: delegate to executor.test() without report generation
    if args.dry_run:
        start = time.time()
        context: dict = {}
        success, state = executor.test(context)
        duration = time.time() - start
        if args.json_output:
            _emit_json('test', success, state, duration)
        return 0 if success else 1

    # Real execution: track phases in a report
    report_dir = Path(__file__).resolve().parent.parent.parent / 'reports'
    report = TestReport(
        host=config.name,
        report_dir=report_dir,
        scenario=f'manifest-test-{manifest.name}',
    )
    report.start()

    start = time.time()
    context = {}

    # Manage server lifecycle at test level (ref counting ensures
    # create/destroy's internal ensure/stop are no-ops)
    executor._server.ensure()  # pylint: disable=protected-access

    try:
        # Phase 1: Create
        report.start_phase('create', 'Provision infrastructure')
        create_ok, state = executor.create(context)
        if create_ok:
            report.pass_phase('create', f'{len(state.nodes)} node(s) created')
        else:
            failed = [n for n, s in state.nodes.items() if s.status == 'failed']
            report.fail_phase('create', f'Failed node(s): {", ".join(failed)}')

        if not create_ok:
            if manifest.settings.cleanup_on_failure:
                logger.info("Create failed, cleaning up...")
                executor.destroy(context)
            success = False
            report.finish(success)
            duration = time.time() - start
            if args.json_output:
                _emit_json('test', success, state, duration)
            logger.info("Report written to %s", report_dir)
            return 1

        # Phase 2: Verify
        report.start_phase('verify', 'Verify SSH connectivity')
        verify_ok = executor._verify_nodes(context, state)  # pylint: disable=protected-access
        if verify_ok:
            report.pass_phase('verify', 'All nodes reachable')
        else:
            report.fail_phase('verify', 'SSH verification failed')

        # Phase 3: Destroy
        report.start_phase('destroy', 'Destroy infrastructure')
        destroy_ok, _ = executor.destroy(context)
        if destroy_ok:
            report.pass_phase('destroy', 'All nodes destroyed')
        else:
            report.fail_phase('destroy', 'Destroy failed')

        success = create_ok and verify_ok and destroy_ok
    finally:
        executor._server.stop()  # pylint: disable=protected-access

    duration = time.time() - start
    report.finish(success)

    if args.json_output:
        _emit_json('test', success, state, duration)

    logger.info("Report written to %s", report_dir)
    return 0 if success else 1


def validate_main(argv: list) -> int:
    """Handle 'manifest validate' verb.

    Validates manifest structure and FK references against config:
    - Schema and graph validation (existing _validate_graph)
    - spec FK: specs/{value}.yaml exists
    - preset FK: presets/{value}.yaml exists
    """
    parser = argparse.ArgumentParser(
        prog='run.sh manifest validate',
        description='Validate manifest structure and FK references',
    )
    parser.add_argument(
        '--manifest', '-M',
        help='Manifest name from config/manifests/',
    )
    parser.add_argument(
        '--manifest-file',
        help='Path to manifest file',
    )
    parser.add_argument(
        '--manifest-json',
        help='Inline manifest JSON',
    )
    parser.add_argument(
        '--verbose', '-v',
        action='store_true',
        help='Show resolved FK paths',
    )
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Require explicit manifest source (no implicit default)
    if not args.manifest and not args.manifest_file and not args.manifest_json:
        print("Error: specify a manifest with -M, --manifest-file, or --manifest-json",
              file=sys.stderr)
        return 1

    # Load manifest (validates schema + graph)
    try:
        manifest = load_manifest(
            name=args.manifest,
            file_path=args.manifest_file,
            json_str=args.manifest_json,
        )
    except Exception as e:
        print(f"Error loading manifest: {e}", file=sys.stderr)
        return 1

    if manifest.schema_version != 2 or not manifest.nodes:
        print("Error: validate requires a v2 manifest with nodes[]", file=sys.stderr)
        return 1

    # Validate FK references
    from config import get_site_config_dir
    site_config = get_site_config_dir()
    errors = validate_manifest_fks(manifest, site_config)

    if errors:
        print(f"Manifest '{manifest.name}' has {len(errors)} validation error(s):", file=sys.stderr)
        for error in errors:
            print(f"  \u2717 {error}", file=sys.stderr)
        return 1

    node_count = len(manifest.nodes)
    print(f"Manifest '{manifest.name}' is valid ({node_count} node{'s' if node_count != 1 else ''})")
    return 0


def validate_manifest_fks(manifest, site_config_dir) -> list[str]:
    """Validate FK references in manifest nodes against config.

    Checks:
    - spec: X → specs/X.yaml exists
    - preset: Y → presets/Y.yaml exists

    Args:
        manifest: Loaded Manifest instance
        site_config_dir: Path to config directory

    Returns:
        List of error messages (empty = valid)
    """
    errors = []
    specs_dir = Path(site_config_dir) / 'specs'
    presets_dir = Path(site_config_dir) / 'presets'

    for node in manifest.nodes:
        if node.spec:
            spec_path = specs_dir / f'{node.spec}.yaml'
            if not spec_path.exists():
                errors.append(
                    f"Node '{node.name}' references unknown spec '{node.spec}' "
                    f"\u2014 no file at specs/{node.spec}.yaml"
                )
            else:
                logger.debug(f"Node '{node.name}' spec '{node.spec}' -> {spec_path}")

        if node.preset:
            preset_path = presets_dir / f'{node.preset}.yaml'
            if not preset_path.exists():
                errors.append(
                    f"Node '{node.name}' references unknown preset '{node.preset}' "
                    f"\u2014 no file at presets/{node.preset}.yaml"
                )
            else:
                logger.debug(f"Node '{node.name}' preset '{node.preset}' -> {preset_path}")

    return errors
