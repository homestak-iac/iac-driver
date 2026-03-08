"""Main HTTPS server.

Unified daemon serving both specs and repos on a single HTTPS port.
"""

import json
import logging
import signal
import ssl
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import Optional
from urllib.parse import urlparse

from config import _load_secrets, get_site_config_dir
from resolver.spec_resolver import SpecResolver
from resolver.base import ResolverError

from server.tls import TLSConfig, generate_self_signed_cert
from server.specs import handle_spec_request, handle_specs_list
from server.repos import RepoManager, handle_repo_request

logger = logging.getLogger(__name__)

# Default configuration
DEFAULT_PORT = 44443
DEFAULT_BIND = "0.0.0.0"


class ServerHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the unified server."""

    # Class-level state (shared across requests)
    spec_resolver: Optional[SpecResolver] = None
    repo_manager: Optional[RepoManager] = None
    repo_token: str = ""
    signing_key: str = ""  # Provisioning token signing key (#231)
    _head_only: bool = False

    def log_message(self, format: str, *args):  # pylint: disable=redefined-builtin
        """Override to use Python logging."""
        logger.info("%s - %s", self.address_string(), format % args)

    def log_request(self, code='-', size='-'):
        """Log HTTP requests, downgrading expected git protocol 404s to DEBUG.

        Git dumb HTTP clients probe for loose objects before falling back
        to packfiles. These 404s are normal protocol behavior, not errors.
        """
        if str(code) == '404' and hasattr(self, 'path') and '/objects/' in self.path:
            logger.debug('%s - "%s" %s %s',
                         self.address_string(), self.requestline, code, size)
            return
        super().log_request(code, size)

    def send_json(self, data: dict, status: int = 200):
        """Send JSON response."""
        body = json.dumps(data, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not getattr(self, '_head_only', False):
            self.wfile.write(body)

    def send_bytes(self, content: bytes, status: int, content_type: str):
        """Send bytes response."""
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if not getattr(self, '_head_only', False):
            self.wfile.write(content)

    def do_GET(self):  # pylint: disable=invalid-name
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        # Health check endpoint
        if path == "/health":
            self.send_json({"status": "ok"})
            return

        # Spec endpoints
        if path.startswith("/spec/"):
            self._handle_spec(path)
            return

        if path == "/specs":
            self._handle_specs_list()
            return

        # Repo endpoints (/*.git/...)
        if ".git/" in path or path.endswith(".git"):
            self._handle_repo(path)
            return

        # Unknown endpoint
        self.send_json({"error": {"code": "E100", "message": f"Unknown endpoint: {path}"}}, 400)

    def do_HEAD(self):  # pylint: disable=invalid-name
        """Handle HEAD requests — return headers only, no body.

        Git dumb HTTP protocol uses HEAD to check if objects/refs exist.
        Per HTTP spec, HEAD must return the same headers as GET but no body.
        Sending a body corrupts persistent connections (the client reads
        leftover body bytes as the next response, causing empty/corrupt
        git objects).
        """
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def _handle_spec(self, path: str):
        """Handle /spec/{identity} request."""
        if not self.spec_resolver:
            self.send_json({"error": {"code": "E500", "message": "Resolver not initialized"}}, 500)
            return

        identity = path[6:]  # Remove "/spec/" prefix
        if not identity:
            self.send_json({"error": {"code": "E101", "message": "Missing identity"}}, 400)
            return

        auth_header = self.headers.get("Authorization", "")
        response, status = handle_spec_request(
            identity, auth_header, self.spec_resolver, self.signing_key
        )
        self.send_json(response, status)

    def _handle_specs_list(self):
        """Handle /specs request."""
        if not self.spec_resolver:
            self.send_json({"error": {"code": "E500", "message": "Resolver not initialized"}}, 500)
            return

        response, status = handle_specs_list(self.spec_resolver)
        self.send_json(response, status)

    def _handle_repo(self, path: str):
        """Handle /*.git/* request."""
        if not self.repo_manager or not self.repo_manager.serve_dir:
            self.send_json({"error": {"code": "E500", "message": "Repos not initialized"}}, 500)
            return

        auth_header = self.headers.get("Authorization", "")
        content, status, content_type = handle_repo_request(
            path, auth_header, self.repo_token, self.repo_manager.serve_dir
        )

        if content_type == "application/json":
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if not getattr(self, '_head_only', False):
                self.wfile.write(content)
        else:
            self.send_bytes(content, status, content_type)


class Server:
    """Unified HTTPS server for specs and repos."""

    def __init__(
        self,
        bind: str = DEFAULT_BIND,
        port: int = DEFAULT_PORT,
        spec_resolver: Optional[SpecResolver] = None,
        repo_manager: Optional[RepoManager] = None,
        repo_token: str = "",
        tls_config: Optional[TLSConfig] = None,
    ):
        """Initialize server.

        Args:
            bind: Address to bind to
            port: Port to listen on
            spec_resolver: SpecResolver for spec endpoints (auto-created if None)
            repo_manager: RepoManager for repo endpoints (optional)
            repo_token: Token for repo authentication
            tls_config: TLS configuration (auto-generated if None)
        """
        self.bind = bind
        self.port = port
        self.spec_resolver = spec_resolver
        self.repo_manager = repo_manager
        self.repo_token = repo_token
        self.tls_config = tls_config
        self.server: Optional[HTTPServer] = None

    def start(self):
        """Start the HTTPS server.

        Raises:
            RuntimeError: If server cannot be started
        """
        # Auto-create resolver if not provided
        if self.spec_resolver is None:
            try:
                self.spec_resolver = SpecResolver()
                logger.info("Using config at: %s", self.spec_resolver.etc_path)
            except ResolverError as e:
                logger.error("Failed to initialize resolver: %s", e.message)
                raise RuntimeError(f"Resolver init failed: {e.message}") from e

        # Auto-generate TLS cert if not provided
        if self.tls_config is None:
            try:
                self.tls_config = generate_self_signed_cert()
            except Exception as e:
                logger.error("Failed to generate TLS cert: %s", e)
                raise RuntimeError(f"TLS init failed: {e}") from e

        # Prepare repos if manager provided
        if self.repo_manager:
            try:
                self.repo_manager.prepare()
            except Exception as e:
                logger.error("Failed to prepare repos: %s", e)
                raise RuntimeError(f"Repos init failed: {e}") from e

        # Load signing key from secrets for provisioning token verification
        try:
            site_config_path = self.spec_resolver.etc_path if self.spec_resolver else get_site_config_dir()
            secrets = _load_secrets(site_config_path) or {}
            signing_key = secrets.get("auth", {}).get("signing_key", "")
            if not signing_key:
                logger.warning("auth.signing_key not found in secrets.yaml — spec auth disabled")
        except Exception as e:
            logger.warning("Failed to load signing key: %s — spec auth disabled", e)
            signing_key = ""

        # Set handler class attributes
        ServerHandler.spec_resolver = self.spec_resolver
        ServerHandler.repo_manager = self.repo_manager
        ServerHandler.repo_token = self.repo_token
        ServerHandler.signing_key = signing_key

        # Create HTTP server
        self.server = HTTPServer((self.bind, self.port), ServerHandler)

        # Wrap with TLS
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(
            certfile=str(self.tls_config.cert_path),
            keyfile=str(self.tls_config.key_path),
        )
        self.server.socket = context.wrap_socket(
            self.server.socket,
            server_side=True,
        )

        # Log startup info
        logger.info("Server starting on https://%s:%d", self.bind, self.port)
        logger.info("Certificate fingerprint: %s", self.tls_config.fingerprint)
        if self.spec_resolver:
            logger.info("Available specs: %s", ", ".join(self.spec_resolver.list_specs()))
        if self.repo_manager:
            prepared = [k for k, v in self.repo_manager.repo_status.items() if v.get("status") == "ok"]
            logger.info("Available repos: %s", ", ".join(prepared))

        # Setup signal handlers
        self._setup_signal_handlers()

    def serve_forever(self):
        """Start serving requests."""
        if not self.server:
            raise RuntimeError("Server not started")

        try:
            self.server.serve_forever()
        except KeyboardInterrupt:
            logger.info("Shutdown requested")
        finally:
            self.shutdown()

    def shutdown(self):
        """Shutdown the server and cleanup."""
        logger.info("Shutting down server")

        if self.server:
            self.server.shutdown()
            self.server = None

        if self.repo_manager:
            self.repo_manager.cleanup()

    def _setup_signal_handlers(self):
        """Setup signal handlers for cache management and shutdown."""

        def handle_sighup(_signum, _frame):
            """Handle SIGHUP by clearing caches."""
            logger.info("Received SIGHUP, clearing caches")
            if self.spec_resolver:
                self.spec_resolver.clear_cache()
            # Re-prepare repos (refreshes _working branches)
            if self.repo_manager:
                logger.info("Refreshing repos")
                self.repo_manager.cleanup()
                self.repo_manager.prepare()
                ServerHandler.repo_manager = self.repo_manager

        def handle_sigterm(_signum, _frame):
            """Handle SIGTERM for graceful shutdown."""
            logger.info("Received SIGTERM")
            self.shutdown()
            sys.exit(0)

        signal.signal(signal.SIGHUP, handle_sighup)
        signal.signal(signal.SIGTERM, handle_sigterm)


def create_server(
    bind: str = DEFAULT_BIND,
    port: int = DEFAULT_PORT,
    spec_resolver: Optional[SpecResolver] = None,
    repo_manager: Optional[RepoManager] = None,
    repo_token: str = "",
    tls_config: Optional[TLSConfig] = None,
) -> Server:
    """Create a server instance.

    Convenience function for creating a server.

    Args:
        bind: Address to bind to
        port: Port to listen on
        spec_resolver: SpecResolver for spec endpoints
        repo_manager: RepoManager for repo endpoints
        repo_token: Token for repo authentication
        tls_config: TLS configuration

    Returns:
        Server instance (not yet started)
    """
    return Server(
        bind=bind,
        port=port,
        spec_resolver=spec_resolver,
        repo_manager=repo_manager,
        repo_token=repo_token,
        tls_config=tls_config,
    )
