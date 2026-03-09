"""Server daemon management.

Provides double-fork daemonization, PID file management, and health-check
startup gate for the server daemon.
"""

import http.client
import logging
import os
import signal
import ssl
import sys
import time
from pathlib import Path

from common import get_homestak_root

logger = logging.getLogger(__name__)


def get_pid_dir() -> Path:
    """Return PID directory: $HOMESTAK_ROOT/.run/."""
    result: Path = get_homestak_root() / '.run'
    return result


def get_log_dir() -> Path:
    """Return log directory: $HOMESTAK_ROOT/logs/."""
    result: Path = get_homestak_root() / 'logs'
    return result


def get_pid_file(port: int) -> Path:
    """Return PID file path for given port.

    Port-qualified filename supports multiple servers on different ports.
    """
    return get_pid_dir() / f"server-{port}.pid"


def _read_pid(pid_file: Path) -> int | None:
    """Read PID from file. Returns None if file doesn't exist or is invalid."""
    try:
        return int(pid_file.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _process_alive(pid: int) -> bool:
    """Check if process with given PID exists."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # Process exists but we can't signal it


def _health_check(port: int, timeout: float = 2.0) -> bool:
    """Check server health via /health endpoint.

    Returns True if server responds with 200.
    """
    try:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        conn = http.client.HTTPSConnection(
            "127.0.0.1", port, timeout=timeout, context=context
        )
        conn.request("GET", "/health")
        response = conn.getresponse()
        return response.status == 200
    except Exception:
        return False


def check_status(port: int) -> dict:
    """Check daemon status.

    Returns:
        Dict with keys: running (bool), pid (int|None), healthy (bool).
    """
    pid_file = get_pid_file(port)
    pid = _read_pid(pid_file)

    if pid is None:
        return {"running": False, "pid": None, "healthy": False}

    if not _process_alive(pid):
        # Stale PID file — clean it up
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return {"running": False, "pid": None, "healthy": False}

    healthy = _health_check(port)
    return {"running": True, "pid": pid, "healthy": healthy}


def _check_existing(port: int) -> str:
    """Check for existing server.

    Returns: 'none', 'healthy', or 'stale'.
    """
    status = check_status(port)
    if not status["running"]:
        return "none"
    if status["healthy"]:
        return "healthy"
    return "stale"


def daemonize(
    server_factory,
    port: int,
    log_file: Path | None = None,
) -> int:
    """Double-fork daemonization with health-check gate.

    The parent process blocks until the daemon signals readiness, then
    verifies via health check before returning.

    Args:
        server_factory: Callable that returns a started Server instance.
            Called in the daemon process after double-fork.
        port: Port the server will listen on (for PID file and health check).
        log_file: Path for daemon stdout/stderr. Defaults to ~/logs/.

    Returns:
        Exit code: 0 = daemon started, 1 = error.
    """
    pid_file = get_pid_file(port)
    if log_file is None:
        log_file = get_log_dir() / "server.log"

    # Check for existing server
    existing = _check_existing(port)
    if existing == "healthy":
        status = check_status(port)
        print(f"Server already running (PID {status['pid']}, port {port})")
        return 0
    if existing == "stale":
        status = check_status(port)
        logger.warning("Killing stale server (PID %d)", status["pid"])
        _kill_process(status["pid"])
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass

    # Ensure directories exist
    get_pid_dir().mkdir(parents=True, exist_ok=True)
    get_log_dir().mkdir(parents=True, exist_ok=True)

    # Create pipe for parent-child coordination
    read_fd, write_fd = os.pipe()

    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent: wait for ready signal from daemon
        os.close(write_fd)
        return _parent_wait(read_fd, port)

    # Child 1: new session leader
    os.setsid()

    # Second fork
    pid = os.fork()
    if pid > 0:
        # Child 1: exit immediately
        os._exit(0)

    # Daemon (Child 2): the actual server process
    os.close(read_fd)
    os.chdir("/")
    os.umask(0o022)

    # Redirect I/O to log file
    log_fd = os.open(str(log_file), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, sys.stdout.fileno())
    os.dup2(log_fd, sys.stderr.fileno())
    os.close(log_fd)

    # Redirect stdin from /dev/null
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, sys.stdin.fileno())
    os.close(devnull)

    # Reconfigure logging to use the redirected stderr
    root_logger = logging.getLogger()
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root_logger.addHandler(handler)

    # Start server
    try:
        server = server_factory()
    except Exception as e:
        logger.error("Failed to start server: %s", e)
        os.write(write_fd, b"error\n")
        os.close(write_fd)
        os._exit(1)

    # Write PID file (after successful start)
    pid_file.write_text(str(os.getpid()))

    # Install SIGTERM handler that cleans up PID file
    def _handle_sigterm(_signum, _frame):
        logger.info("Received SIGTERM, shutting down")
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        server.shutdown()
        os._exit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # Signal parent: ready
    os.write(write_fd, b"ready\n")
    os.close(write_fd)

    logger.info("Daemon started (PID %d, port %d)", os.getpid(), port)

    # Serve forever
    try:
        server.serve_forever()
    except Exception as e:
        logger.error("Server error: %s", e)
    finally:
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        server.shutdown()

    return 1  # Unreachable; daemon exits via os._exit or serve_forever


def _parent_wait(read_fd: int, port: int, timeout: float = 10.0) -> int:
    """Parent waits for daemon ready signal and verifies health.

    Args:
        read_fd: Read end of pipe from daemon.
        port: Port to health-check.
        timeout: Max seconds to wait for ready signal.

    Returns:
        Exit code: 0 = success, 1 = error.
    """
    # Wait for first child to exit (reap zombie)
    os.wait()

    # Read from pipe with timeout
    import select
    ready, _, _ = select.select([read_fd], [], [], timeout)
    if not ready:
        os.close(read_fd)
        print("Error: Timed out waiting for server to start", file=sys.stderr)
        return 1

    data = os.read(read_fd, 64).decode().strip()
    os.close(read_fd)

    if data != "ready":
        print(f"Error: Server failed to start: {data}", file=sys.stderr)
        return 1

    # Verify health check
    retries = 10
    for _ in range(retries):
        if _health_check(port):
            status = check_status(port)
            print(f"Server started (PID {status['pid']}, port {port})")
            return 0
        time.sleep(0.2)

    print("Error: Server started but health check failed", file=sys.stderr)
    return 1


def _kill_process(pid: int, timeout: float = 5.0) -> bool:
    """Kill process: SIGTERM then SIGKILL after timeout.

    Returns True if process was stopped.
    """
    if not _process_alive(pid):
        return True

    # SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True

    # Wait for clean exit
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return True
        time.sleep(0.2)

    # SIGKILL
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True

    # Wait briefly for SIGKILL
    time.sleep(0.5)
    return not _process_alive(pid)


def stop_daemon(port: int) -> bool:
    """Stop daemon by PID file.

    Args:
        port: Server port (determines PID file path).

    Returns:
        True if server was stopped (or wasn't running).
    """
    pid_file = get_pid_file(port)
    pid = _read_pid(pid_file)

    if pid is None:
        return True  # Not running

    if not _process_alive(pid):
        # Stale PID file
        try:
            pid_file.unlink()
        except FileNotFoundError:
            pass
        return True

    # Kill the process
    success = _kill_process(pid)

    # Clean up PID file
    try:
        pid_file.unlink()
    except FileNotFoundError:
        pass

    return success
