"""Server lifecycle management for the operator.

Manages the spec/repo server daemon used during manifest operations.
Handles start/stop with reference counting, HOMESTAK_SERVER env var
propagation, and address resolution for nested deployments.
"""

import json
import logging
import os
import signal
import socket
import time
from typing import Optional

from common import run_command, run_ssh

logger = logging.getLogger(__name__)

DEFAULT_SERVER_PORT = 44443


class ServerManager:
    """Manages the spec server lifecycle during manifest operations.

    Uses reference counting so nested calls (test → create → destroy)
    only start/stop once. First call starts if needed; subsequent calls
    just increment the ref count.

    Attributes:
        ssh_host: SSH host for the target PVE node
        ssh_user: SSH user for the target PVE node
        self_addr: Explicit routable address (from --self-addr)
        port: Server port (default: 44443, or extracted from server_url)
    """

    def __init__(self, ssh_host: str, ssh_user: str,
                 self_addr: Optional[str] = None,
                 port: int = DEFAULT_SERVER_PORT) -> None:
        self.ssh_host = ssh_host
        self.ssh_user = ssh_user
        self.self_addr = self_addr
        self.port = port
        self._refs: int = 0
        self._started: bool = False
        loopback = ('localhost', '127.0.0.1', '::1')
        self._is_local = ssh_host in loopback or ssh_host in (
            socket.gethostname(), socket.getfqdn()
        ) or ssh_host == ServerManager.detect_external_ip()

    def _run_on_host(self, cmd: str, timeout: int = 15) -> tuple[int, str, str]:
        """Run a command on the target host, locally or via SSH."""
        if self._is_local:
            from config import get_base_dir
            result: tuple[int, str, str] = run_command(
                ['bash', '-c', cmd],
                cwd=get_base_dir(), timeout=timeout,
            )
            return result
        result = run_ssh(self.ssh_host, f'cd ~/iac/iac-driver && {cmd}',
                         user=self.ssh_user, timeout=timeout)
        return result

    def ensure(self) -> None:
        """Ensure the spec server is running on the target host.

        Uses reference counting so nested calls only start/stop once.
        First call starts if needed; subsequent calls just increment.
        """
        self._refs += 1
        if self._refs > 1:
            return

        # Check current status
        rc, stdout, _ = self._run_on_host(
            f'./run.sh server status --json --port {self.port}',
            timeout=15,
        )

        if rc == 0:
            try:
                status = json.loads(stdout.strip())
                if status.get('running') and status.get('healthy'):
                    logger.info("Server already running on %s:%d (reusing)",
                                self.ssh_host, self.port)
                    self._started = False
                    pid = status.get('pid')
                    if pid:
                        if self._is_local:
                            os.kill(pid, signal.SIGHUP)
                        else:
                            self._run_on_host(
                                f'kill -HUP {pid}', timeout=5)
                        logger.info(
                            "Refreshed bare repos on running server "
                            "(PID %d)", pid)
                        time.sleep(1)
                    self._set_source_env(self.ssh_host)
                    return
            except (json.JSONDecodeError, ValueError):
                pass

        # Start the server (with repo serving for pull mode bootstrap)
        logger.info("Starting server on %s:%d", self.ssh_host, self.port)
        rc, stdout, stderr = self._run_on_host(
            f"./run.sh server start --port {self.port} --repos --repo-token ''",
            timeout=30,
        )

        if rc != 0:
            logger.warning("Server start returned rc=%d: %s",
                           rc, stderr.strip() or stdout.strip())
        else:
            logger.info("Server started on %s:%d (log: ~/logs/server.log)",
                        self.ssh_host, self.port)

        self._started = True
        self._set_source_env(self.ssh_host)

    def stop(self) -> None:
        """Stop the spec server if we started it.

        Only actually stops when the ref count reaches zero (outermost caller).
        Preserves user-managed servers (those we didn't start).
        """
        self._refs = max(0, self._refs - 1)
        if self._refs > 0:
            return

        self._clear_source_env()

        if not self._started:
            return

        logger.info("Stopping server on %s:%d", self.ssh_host, self.port)
        rc, _, stderr = self._run_on_host(
            f'./run.sh server stop --port {self.port}',
            timeout=15,
        )

        if rc != 0:
            logger.warning("Server stop returned rc=%d: %s", rc, stderr.strip())
        else:
            logger.info("Server stopped on %s:%d", self.ssh_host, self.port)

        self._started = False

    @staticmethod
    def resolve_port(server_url: str) -> int:
        """Extract port from a server URL, or return default.

        Args:
            server_url: URL like "https://controller:44443"

        Returns:
            Extracted port or DEFAULT_SERVER_PORT
        """
        if not server_url:
            return DEFAULT_SERVER_PORT
        try:
            from urllib.parse import urlparse
            parsed = urlparse(server_url)
            if parsed.port:
                return int(parsed.port)
        except Exception:  # pylint: disable=broad-except
            pass
        return DEFAULT_SERVER_PORT

    @staticmethod
    def detect_external_ip() -> Optional[str]:
        """Detect this machine's routable IP address.

        Uses a UDP socket connect to a public IP (no traffic sent) to
        determine which local interface the OS would route through.
        Falls back to None if detection fails or returns a non-routable address.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(('198.51.100.1', 1))  # RFC 5737, no traffic sent
                addr: str = s.getsockname()[0]
                if addr and addr not in ('0.0.0.0', '127.0.0.1'):
                    return addr
        except OSError:
            pass
        return None

    @staticmethod
    def validate_addr(addr: str, source: str) -> str:
        """Validate an address provided for HOMESTAK_SERVER.

        Args:
            addr: IP address or hostname to validate
            source: Description of where the address came from

        Returns:
            The validated address

        Raises:
            ValueError: If the address is empty or loopback
        """
        if not addr or not addr.strip():
            raise ValueError(f"{source} is empty")
        addr = addr.strip()
        if addr in ('localhost', '127.0.0.1'):
            raise ValueError(
                f"{source} resolved to loopback ({addr}); "
                "use --self-addr or HOMESTAK_SELF_ADDR to specify "
                "a routable address"
            )
        return addr

    def _resolve_self_addr(self) -> Optional[str]:
        """Resolve the routable address for this host.

        Priority order:
        1. --self-addr CLI argument (set during subtree delegation)
        2. HOMESTAK_SELF_ADDR environment variable (manual override)

        Returns:
            Routable address, or None if not explicitly provided
        """
        if self.self_addr:
            return self.self_addr
        env_addr = os.environ.get('HOMESTAK_SELF_ADDR')
        if env_addr:
            return env_addr.strip()
        return None

    def _set_source_env(self, host: str) -> None:
        """Set HOMESTAK_SERVER env var so downstream actions use serve-repos.

        When host is loopback, resolves to a routable address using:
        1. --self-addr CLI arg or HOMESTAK_SELF_ADDR env var
        2. detect_external_ip() (socket-based detection)
        3. Falls back to loopback with warning

        Args:
            host: IP or hostname of the server
        """
        addr = host
        if host in ('localhost', '127.0.0.1'):
            explicit = self._resolve_self_addr()
            if explicit:
                addr = self.validate_addr(
                    explicit, '--self-addr / HOMESTAK_SELF_ADDR')
                logger.info(
                    "Using explicit address %s instead of loopback %s "
                    "for HOMESTAK_SERVER", addr, host,
                )
            else:
                detected = self.detect_external_ip()
                if detected:
                    addr = detected
                    logger.info(
                        "Auto-detected IP %s for HOMESTAK_SERVER", addr,
                    )
                else:
                    logger.warning(
                        "Could not detect external IP; using loopback %s "
                        "for HOMESTAK_SERVER (child VMs will not be able "
                        "to reach this address — use --self-addr or "
                        "HOMESTAK_SELF_ADDR to set a routable address)",
                        host,
                    )
        os.environ['HOMESTAK_SERVER'] = f'https://{addr}:{self.port}'
        os.environ.setdefault('HOMESTAK_REF', '_working')
        logger.info(
            "Set HOMESTAK_SERVER=https://%s:%d (ref=%s)",
            addr, self.port, os.environ.get('HOMESTAK_REF'),
        )

    @staticmethod
    def _clear_source_env() -> None:
        """Clear HOMESTAK_SERVER env vars set by _set_source_env."""
        for var in ('HOMESTAK_SERVER', 'HOMESTAK_REF'):
            os.environ.pop(var, None)
        logger.debug("Cleared HOMESTAK_SERVER env vars")
