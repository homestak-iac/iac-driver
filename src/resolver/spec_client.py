"""HTTP client for fetching specs from the server.

Fetches specs from the server, validates them, and persists to local state.
Migrated from bootstrap/lib/spec_client.py for unified server architecture.
"""

import argparse
import json
import logging
import os
import ssl
import sys
from pathlib import Path
from typing import Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Exit codes
EXIT_SUCCESS = 0
EXIT_CLIENT_ERROR = 1  # Missing args, invalid config
EXIT_SERVER_ERROR = 2  # Network, HTTP error
EXIT_VALIDATION_ERROR = 3  # Schema invalid


class SpecClientError(Exception):
    """Base exception for spec client errors."""

    def __init__(self, code: str, message: str, exit_code: int = EXIT_SERVER_ERROR):
        self.code = code
        self.message = message
        self.exit_code = exit_code
        super().__init__(f"{code}: {message}")


def discover_state_path() -> Path:
    """Discover the config state directory path.

    Derived from $HOMESTAK_ROOT/.state/config.

    Returns:
        Path to state directory
    """
    root = Path(os.environ.get("HOMESTAK_ROOT", str(Path.home())))
    return root / ".state" / "config"


class SpecClient:
    """HTTP client for fetching specs from the controller."""

    def __init__(
        self,
        server: str,
        identity: str,
        token: Optional[str] = None,
        insecure: bool = False,
        state_path: Optional[Path] = None,
        ca_cert: Optional[Path] = None,
    ):
        """Initialize spec client.

        Args:
            server: Server URL (e.g., https://srv1:44443)
            identity: Node identity (e.g., dev1)
            token: Bearer token for authentication (if required by posture)
            insecure: Skip SSL certificate verification
            state_path: Override state directory path
            ca_cert: Path to CA certificate for verification
        """
        if yaml is None:
            raise SpecClientError(
                "E500",
                "PyYAML not installed. Run: apt install python3-yaml",
                EXIT_CLIENT_ERROR
            )

        self.server = server.rstrip("/")
        self.identity = identity
        self.token = token
        self.insecure = insecure
        self.state_path = state_path or discover_state_path()
        self.ca_cert = ca_cert

    def _create_ssl_context(self) -> Optional[ssl.SSLContext]:
        """Create SSL context, optionally skipping verification."""
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx

        if self.ca_cert and self.ca_cert.exists():
            ctx = ssl.create_default_context(cafile=str(self.ca_cert))
            return ctx

        return None

    def _build_request(self) -> Request:
        """Build HTTP request for spec fetch."""
        url = f"{self.server}/spec/{self.identity}"
        request = Request(url)
        request.add_header("Accept", "application/json")

        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")

        return request

    def _parse_error_response(self, body: bytes) -> Tuple[str, str]:
        """Parse error response from server.

        Returns:
            Tuple of (error_code, error_message)
        """
        try:
            data = json.loads(body.decode("utf-8"))
            error = data.get("error", {})
            return error.get("code", "E500"), error.get("message", "Unknown error")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return "E500", "Failed to parse error response"

    def fetch(self) -> dict:
        """Fetch spec from server.

        Returns:
            Parsed spec dictionary

        Raises:
            SpecClientError: On fetch failure
        """
        request = self._build_request()
        ssl_context = self._create_ssl_context()

        logger.debug("Fetching spec from %s", request.full_url)

        try:
            with urlopen(request, context=ssl_context, timeout=30) as response:
                body = response.read()
                result: dict = json.loads(body.decode("utf-8"))
                return result

        except HTTPError as e:
            # Parse error response from server
            error_body = e.read()
            code, message = self._parse_error_response(error_body)

            # Map HTTP status to exit code
            if e.code in (400, 401):
                exit_code = EXIT_CLIENT_ERROR
            elif e.code == 422:
                exit_code = EXIT_VALIDATION_ERROR
            else:
                exit_code = EXIT_SERVER_ERROR

            raise SpecClientError(code, message, exit_code) from e

        except URLError as e:
            raise SpecClientError(
                "E501", f"Cannot connect to server: {e.reason}", EXIT_SERVER_ERROR
            ) from e

        except json.JSONDecodeError as e:
            raise SpecClientError(
                "E500", f"Invalid JSON response: {e}", EXIT_SERVER_ERROR
            ) from e

    def _ensure_state_dir(self):
        """Create state directory if it doesn't exist."""
        if not self.state_path.exists():
            logger.debug("Creating state directory: %s", self.state_path)
            self.state_path.mkdir(parents=True, mode=0o755)

    def _backup_previous(self, spec_file: Path):
        """Backup previous spec file if it exists."""
        if spec_file.exists():
            prev_file = spec_file.with_suffix(".yaml.prev")
            logger.debug("Backing up %s to %s", spec_file, prev_file)
            prev_file.write_text(spec_file.read_text())

    def save(self, spec: dict) -> Path:
        """Save spec to state directory.

        Uses atomic write (write to .tmp, rename) to prevent partial writes.

        Args:
            spec: Spec dictionary to save

        Returns:
            Path to saved spec file

        Raises:
            SpecClientError: On write failure
        """
        self._ensure_state_dir()

        spec_file = self.state_path / "spec.yaml"
        tmp_file = self.state_path / "spec.yaml.tmp"

        try:
            # Backup previous spec
            self._backup_previous(spec_file)

            # Write to temp file first (atomic write pattern)
            logger.debug("Writing spec to %s", tmp_file)
            tmp_file.write_text(yaml.dump(spec, default_flow_style=False, sort_keys=False))

            # Rename to final location (atomic on POSIX)
            tmp_file.rename(spec_file)

            return spec_file

        except OSError as e:
            # Clean up temp file if it exists
            if tmp_file.exists():
                tmp_file.unlink()
            raise SpecClientError(
                "E500", f"Failed to save spec: {e}", EXIT_CLIENT_ERROR
            ) from e

    def fetch_and_save(self) -> Tuple[dict, Path]:
        """Fetch spec from server and save to state directory.

        Returns:
            Tuple of (spec dict, path to saved file)

        Raises:
            SpecClientError: On fetch or save failure
        """
        spec = self.fetch()
        path = self.save(spec)
        return spec, path


def get_config_from_env() -> dict:
    """Get configuration from environment variables.

    Environment variables (#231):
      HOMESTAK_SERVER  - Server URL (replaces HOMESTAK_SPEC_SERVER)
      HOMESTAK_TOKEN   - Provisioning token (replaces HOMESTAK_IDENTITY + HOMESTAK_AUTH_TOKEN)

    Identity is derived from hostname, not env var.

    Returns:
        Dict with server, identity, token (if set)
    """
    config = {}

    # HOMESTAK_SERVER (preferred, #231), fallback to HOMESTAK_SPEC_SERVER
    server = os.environ.get("HOMESTAK_SERVER") or os.environ.get("HOMESTAK_SPEC_SERVER")
    if server:
        config["server"] = server

    # Identity from hostname (not env var)
    import socket
    config["identity"] = socket.gethostname()

    # HOMESTAK_TOKEN (provisioning token, required for pull mode, #231)
    token = os.environ.get("HOMESTAK_TOKEN")
    if token:
        config["token"] = token

    return config


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Fetch spec from controller",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--server",
        "-s",
        help="Server URL (e.g., https://srv1:44443). "
        "Env: HOMESTAK_SERVER",
    )
    parser.add_argument(
        "--identity",
        "-i",
        help="Node identity (default: hostname). "
        "Override for testing only.",
    )
    parser.add_argument(
        "--token",
        "-t",
        help="Provisioning token. "
        "Env: HOMESTAK_TOKEN",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Override output directory (default: auto-discovered state dir)",
    )
    parser.add_argument(
        "--ca-cert",
        type=Path,
        help="CA certificate for server verification",
    )
    parser.add_argument(
        "--insecure",
        "-k",
        action="store_true",
        help="Skip SSL certificate verification (for self-signed certs)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose output",
    )

    args = parser.parse_args()

    # Configure logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(message)s",  # Simple format for CLI output
    )

    # Merge CLI args with env vars (CLI takes precedence)
    env_config = get_config_from_env()

    server = args.server or env_config.get("server")
    identity = args.identity or env_config.get("identity")
    token = args.token or env_config.get("token")

    # Validate required args
    if not server:
        logger.error("Error: --server or HOMESTAK_SERVER required")
        sys.exit(EXIT_CLIENT_ERROR)

    if not identity:
        logger.error("Error: --identity required (default: hostname)")
        sys.exit(EXIT_CLIENT_ERROR)

    # Warn about insecure mode
    if args.insecure:
        logger.warning("Warning: SSL certificate verification disabled")

    # Create client and fetch
    try:
        client = SpecClient(
            server=server,
            identity=identity,
            token=token,
            insecure=args.insecure,
            state_path=args.output,
            ca_cert=args.ca_cert,
        )
    except SpecClientError as e:
        logger.error("Error: %s - %s", e.code, e.message)
        sys.exit(e.exit_code)

    logger.info("Fetching spec for '%s' from %s...", identity, server)

    try:
        spec, path = client.fetch_and_save()

        # Display summary
        logger.info("Spec fetched successfully")
        logger.info("  Schema version: %s", spec.get("schema_version", "unknown"))

        if access := spec.get("access"):
            posture = access.get("posture", "unknown")
            logger.info("  Posture: %s", posture)

        if platform := spec.get("platform"):
            packages = platform.get("packages", [])
            logger.info("  Packages: %d", len(packages))

        logger.info("Saved to: %s", path)
        sys.exit(EXIT_SUCCESS)

    except SpecClientError as e:
        logger.error("Error fetching spec: %s - %s", e.code, e.message)
        sys.exit(e.exit_code)


if __name__ == "__main__":
    main()
