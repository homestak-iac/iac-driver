"""CLI for the server command.

Provides the `server` verb for server daemon management (start/stop/status).
"""

import argparse
import json
import logging
import os
import secrets
import sys
from pathlib import Path

from server.httpd import Server, DEFAULT_PORT, DEFAULT_BIND
from server.tls import generate_self_signed_cert, TLSConfig
from server.repos import RepoManager
from server.daemon import (
    daemonize,
    stop_daemon,
    check_status,
    DEFAULT_LOG_FILE,
)
from resolver.spec_resolver import SpecResolver
from resolver.base import ResolverError

logger = logging.getLogger(__name__)


def get_default_repos_dir() -> Path:
    """Get default repos directory (parent of iac-driver)."""
    return Path(__file__).resolve().parent.parent.parent.parent


def generate_repo_token(length: int = 16) -> str:
    """Generate a random repo token."""
    return secrets.token_urlsafe(length)[:length]


def _add_common_args(parser: argparse.ArgumentParser):
    """Add common arguments shared between start and foreground."""
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help="Port to listen on",
    )
    parser.add_argument(
        "--bind", "-b",
        default=DEFAULT_BIND,
        help="Address to bind to",
    )

    # TLS options
    parser.add_argument(
        "--cert",
        type=Path,
        help="Path to TLS certificate (auto-generated if not provided)",
    )
    parser.add_argument(
        "--key",
        type=Path,
        help="Path to TLS private key (required if --cert is provided)",
    )
    parser.add_argument(
        "--cert-dir",
        type=Path,
        help="Directory for auto-generated certificate",
    )

    # Repo options
    parser.add_argument(
        "--repos", "-r",
        action="store_true",
        help="Enable repo serving",
    )
    parser.add_argument(
        "--repos-dir",
        type=Path,
        help="Directory containing source repos (default: auto-detected)",
    )
    parser.add_argument(
        "--repo-token",
        help="Token for repo authentication (auto-generated if not provided)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        help="Exclude specific repo from serving (repeatable)",
    )

    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )


def _create_server(args) -> Server:
    """Create a Server instance from parsed arguments.

    Returns:
        Server instance (not yet started).

    Raises:
        SystemExit: On configuration errors.
    """
    # Initialize spec resolver
    try:
        spec_resolver = SpecResolver()
        logger.info("Using site-config at: %s", spec_resolver.etc_path)
    except ResolverError as e:
        logger.error("Failed to initialize: %s", e.message)
        sys.exit(1)

    # Initialize TLS
    tls_config = None
    if args.cert:
        if not args.key:
            logger.error("--key is required when --cert is provided")
            sys.exit(1)
        try:
            tls_config = TLSConfig.from_paths(args.cert, args.key)
        except FileNotFoundError as e:
            logger.error("TLS file not found: %s", e)
            sys.exit(1)
    else:
        try:
            tls_config = generate_self_signed_cert(cert_dir=args.cert_dir)
        except Exception as e:
            logger.error("Failed to generate TLS cert: %s", e)
            sys.exit(1)

    # Initialize repos if requested
    repo_manager = None
    repo_token = ""
    if args.repos:
        repos_dir = args.repos_dir or get_default_repos_dir()
        # Repos outside repos_dir (~/iac/): config at ~/config, bootstrap at ~/bootstrap
        extra_paths = {}
        workspace_root = Path(os.environ.get('HOMESTAK_ROOT', str(Path.home())))
        config_dir = workspace_root / 'config'
        if config_dir.is_dir() and (config_dir / '.git').is_dir():
            extra_paths['config'] = config_dir
            logger.info("Using config at %s", config_dir)
        else:
            logger.warning("config not found at %s", config_dir)
        bootstrap_dir = workspace_root / 'bootstrap'
        if bootstrap_dir.is_dir() and (bootstrap_dir / '.git').is_dir():
            extra_paths['bootstrap'] = bootstrap_dir
        repo_manager = RepoManager(
            repos_dir=repos_dir,
            exclude_repos=args.exclude,
            extra_paths=extra_paths,
        )
        repo_token = (
            args.repo_token
            if args.repo_token is not None
            else generate_repo_token()
        )

    return Server(
        bind=args.bind,
        port=args.port,
        spec_resolver=spec_resolver,
        repo_manager=repo_manager,
        repo_token=repo_token,
        tls_config=tls_config,
    )


def _handle_start(argv):
    """Handle 'server start' — daemonize the server."""
    parser = argparse.ArgumentParser(
        prog="run.sh server start",
        description="Start the server daemon (background)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    _add_common_args(parser)
    parser.add_argument(
        "--log",
        type=Path,
        default=DEFAULT_LOG_FILE,
        help="Log file path for daemon output",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Run in foreground instead of daemonizing",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output startup info as JSON",
    )

    args = parser.parse_args(argv)

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if args.foreground:
        return _run_foreground(args)

    # Build a server factory for the daemon process
    def server_factory():
        server = _create_server(args)
        server.start()
        return server

    return daemonize(
        server_factory=server_factory,
        port=args.port,
        log_file=args.log,
    )


def _run_foreground(args):
    """Run server in foreground (for development)."""
    server = _create_server(args)

    try:
        server.start()
    except RuntimeError as e:
        logger.error("Failed to start server: %s", e)
        return 1

    # Output startup info
    if args.json:
        info = {
            "url": f"https://{args.bind}:{args.port}",
            "port": args.port,
            "fingerprint": server.tls_config.fingerprint,
            "specs": server.spec_resolver.list_specs(),
        }
        if server.repo_manager:
            info["repo_token"] = server.repo_token
            info["repos"] = list(server.repo_manager.repo_status.keys())
        print(json.dumps(info, indent=2))
    else:
        print(f"\nServer running at https://{args.bind}:{args.port}")
        print(f"Certificate fingerprint: {server.tls_config.fingerprint}")
        print(f"Available specs: {', '.join(server.spec_resolver.list_specs())}")
        if server.repo_manager:
            print(f"Repo token: {server.repo_token}")
            prepared = [
                k for k, v in server.repo_manager.repo_status.items()
                if v.get("status") == "ok"
            ]
            print(f"Available repos: {', '.join(prepared)}")
        print("\nPress Ctrl+C to stop...")

    server.serve_forever()
    return 0


def _handle_stop(argv):
    """Handle 'server stop' — stop the daemon."""
    parser = argparse.ArgumentParser(
        prog="run.sh server stop",
        description="Stop the server daemon",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help="Port of server to stop",
    )

    args = parser.parse_args(argv)

    status = check_status(args.port)
    if not status["running"]:
        print(f"Server not running (port {args.port})")
        return 0

    print(f"Stopping server (PID {status['pid']}, port {args.port})...")
    success = stop_daemon(args.port)

    if success:
        print("Server stopped")
        return 0

    print("Error: Failed to stop server", file=sys.stderr)
    return 1


def _handle_status(argv):
    """Handle 'server status' — check daemon status."""
    parser = argparse.ArgumentParser(
        prog="run.sh server status",
        description="Check server daemon status",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--port", "-p",
        type=int,
        default=DEFAULT_PORT,
        help="Port to check",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output as JSON",
    )

    args = parser.parse_args(argv)

    status = check_status(args.port)

    if args.json:
        print(json.dumps(status, indent=2))
    else:
        if status["running"]:
            health = "healthy" if status["healthy"] else "unhealthy"
            print(f"Server: running (PID {status['pid']}, port {args.port}, {health})")
        else:
            print(f"Server: not running (port {args.port})")

    # Exit codes: 0 = running+healthy, 1 = not running, 2 = running+unhealthy
    if not status["running"]:
        return 1
    if not status["healthy"]:
        return 2
    return 0


def main(argv=None):
    """CLI entry point for server command.

    Dispatches to start/stop/status subcommands.

    Args:
        argv: Command line arguments (default: sys.argv[1:])

    Returns:
        Exit code
    """
    if argv is None:
        argv = sys.argv[1:]

    subcommands = {
        "start": _handle_start,
        "stop": _handle_stop,
        "status": _handle_status,
    }

    if not argv or argv[0] in ("-h", "--help"):
        print("Usage: ./run.sh server <command> [options]")
        print()
        print("Commands:")
        print("  start    Start the server daemon")
        print("  stop     Stop the server daemon")
        print("  status   Check server daemon status")
        print()
        print("Run './run.sh server <command> --help' for command-specific options.")
        return 0

    subcmd = argv[0]
    if subcmd not in subcommands:
        print(f"Error: Unknown server command '{subcmd}'")
        print(f"Available commands: {', '.join(subcommands)}")
        return 1

    return subcommands[subcmd](argv[1:])


if __name__ == "__main__":
    sys.exit(main())
