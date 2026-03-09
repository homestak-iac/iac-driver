"""Tests for server/daemon.py - daemon lifecycle management."""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from server.daemon import (
    get_pid_file,
    _read_pid,
    _process_alive,
    _health_check,
    check_status,
    _check_existing,
    stop_daemon,
    _kill_process,
    _parent_wait,
    PID_DIR,
)


class TestPidFile:
    """Tests for PID file path and I/O."""

    def test_get_pid_file_default_port(self):
        """PID file path uses port number."""
        path = get_pid_file(44443)
        assert path == PID_DIR / "server-44443.pid"

    def test_get_pid_file_custom_port(self):
        """Different ports produce different PID files."""
        p1 = get_pid_file(8443)
        p2 = get_pid_file(9443)
        assert p1 != p2
        assert "8443" in str(p1)
        assert "9443" in str(p2)

    def test_read_pid_valid(self, tmp_path):
        """_read_pid reads integer PID from file."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345\n")
        assert _read_pid(pid_file) == 12345

    def test_read_pid_missing_file(self, tmp_path):
        """_read_pid returns None for missing file."""
        assert _read_pid(tmp_path / "nonexistent.pid") is None

    def test_read_pid_invalid_content(self, tmp_path):
        """_read_pid returns None for non-integer content."""
        pid_file = tmp_path / "bad.pid"
        pid_file.write_text("not-a-pid\n")
        assert _read_pid(pid_file) is None

    def test_read_pid_empty_file(self, tmp_path):
        """_read_pid returns None for empty file."""
        pid_file = tmp_path / "empty.pid"
        pid_file.write_text("")
        assert _read_pid(pid_file) is None


class TestProcessAlive:
    """Tests for process existence checks."""

    def test_current_process(self):
        """Current process is alive."""
        assert _process_alive(os.getpid()) is True

    def test_nonexistent_pid(self):
        """Very high PID is not alive."""
        # PID 4194304 is max on most Linux systems, unlikely to exist
        assert _process_alive(4194304) is False

    def test_permission_error_means_alive(self):
        """PermissionError means process exists but we can't signal it."""
        with patch("os.kill", side_effect=PermissionError):
            assert _process_alive(1) is True


class TestHealthCheck:
    """Tests for HTTPS health check."""

    def test_no_server_returns_false(self):
        """Health check returns False when no server is listening."""
        # Use a port that's very unlikely to have a server
        assert _health_check(19999, timeout=0.5) is False

    def test_successful_health_check(self):
        """Health check returns True on 200 response."""
        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 200
        mock_conn.getresponse.return_value = mock_response

        with patch("server.daemon.http.client.HTTPSConnection", return_value=mock_conn):
            assert _health_check(44443) is True

        mock_conn.request.assert_called_once_with("GET", "/health")

    def test_non_200_returns_false(self):
        """Health check returns False on non-200 response."""
        mock_conn = MagicMock()
        mock_response = MagicMock()
        mock_response.status = 500
        mock_conn.getresponse.return_value = mock_response

        with patch("server.daemon.http.client.HTTPSConnection", return_value=mock_conn):
            assert _health_check(44443) is False

    def test_connection_error_returns_false(self):
        """Health check returns False on connection error."""
        with patch("server.daemon.http.client.HTTPSConnection", side_effect=ConnectionRefusedError):
            assert _health_check(44443) is False


class TestCheckStatus:
    """Tests for daemon status checking."""

    def test_no_pid_file(self):
        """No PID file means not running."""
        with patch("server.daemon.get_pid_file") as mock_pf:
            mock_pf.return_value = Path("/nonexistent/server-44443.pid")
            status = check_status(44443)

        assert status == {"running": False, "pid": None, "healthy": False}

    def test_stale_pid_file(self, tmp_path):
        """Stale PID file (process dead) is cleaned up."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("99999999")  # Non-existent PID

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=False):
            status = check_status(44443)

        assert status == {"running": False, "pid": None, "healthy": False}
        assert not pid_file.exists()  # Cleaned up

    def test_running_healthy(self, tmp_path):
        """Running + healthy server."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("12345")

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=True), \
             patch("server.daemon._health_check", return_value=True):
            status = check_status(44443)

        assert status == {"running": True, "pid": 12345, "healthy": True}

    def test_running_unhealthy(self, tmp_path):
        """Running but unhealthy server."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("12345")

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=True), \
             patch("server.daemon._health_check", return_value=False):
            status = check_status(44443)

        assert status == {"running": True, "pid": 12345, "healthy": False}


class TestCheckExisting:
    """Tests for _check_existing helper."""

    def test_none_when_not_running(self):
        """Returns 'none' when no server is running."""
        with patch("server.daemon.check_status", return_value={"running": False, "pid": None, "healthy": False}):
            assert _check_existing(44443) == "none"

    def test_healthy_when_running_and_healthy(self):
        """Returns 'healthy' when server is running and healthy."""
        with patch("server.daemon.check_status", return_value={"running": True, "pid": 123, "healthy": True}):
            assert _check_existing(44443) == "healthy"

    def test_stale_when_running_but_unhealthy(self):
        """Returns 'stale' when server is running but not healthy."""
        with patch("server.daemon.check_status", return_value={"running": True, "pid": 123, "healthy": False}):
            assert _check_existing(44443) == "stale"


class TestStopDaemon:
    """Tests for stop_daemon."""

    def test_not_running(self, tmp_path):
        """Stopping a non-running server returns True."""
        with patch("server.daemon.get_pid_file", return_value=tmp_path / "missing.pid"):
            assert stop_daemon(44443) is True

    def test_stale_pid_cleaned(self, tmp_path):
        """Stale PID file is cleaned up, returns True."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("99999999")

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=False):
            assert stop_daemon(44443) is True

        assert not pid_file.exists()

    def test_running_process_killed(self, tmp_path):
        """Running process is killed and PID file cleaned."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("12345")

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=True), \
             patch("server.daemon._kill_process", return_value=True) as mock_kill:
            assert stop_daemon(44443) is True
            mock_kill.assert_called_once_with(12345)

    def test_kill_failure(self, tmp_path):
        """Returns False when kill fails."""
        pid_file = tmp_path / "server-44443.pid"
        pid_file.write_text("12345")

        with patch("server.daemon.get_pid_file", return_value=pid_file), \
             patch("server.daemon._process_alive", return_value=True), \
             patch("server.daemon._kill_process", return_value=False):
            assert stop_daemon(44443) is False


class TestKillProcess:
    """Tests for _kill_process."""

    def test_already_dead(self):
        """Already-dead process returns True."""
        with patch("server.daemon._process_alive", return_value=False):
            assert _kill_process(12345) is True

    def test_sigterm_kills(self):
        """SIGTERM succeeds on first check."""
        alive_calls = [True, False]  # Alive, then dead after SIGTERM

        with patch("server.daemon._process_alive", side_effect=alive_calls), \
             patch("os.kill") as mock_kill, \
             patch("time.sleep"):
            assert _kill_process(12345, timeout=0.1) is True
            mock_kill.assert_called_once_with(12345, __import__("signal").SIGTERM)

    def test_process_gone_during_sigterm(self):
        """ProcessLookupError during SIGTERM means process already exited."""
        with patch("server.daemon._process_alive", return_value=True), \
             patch("os.kill", side_effect=ProcessLookupError):
            assert _kill_process(12345) is True


class TestParentWait:
    """Tests for _parent_wait (parent side of daemon startup)."""

    def test_timeout_returns_error(self):
        """Timeout waiting for ready signal returns 1."""
        read_fd, write_fd = os.pipe()
        os.close(write_fd)  # Close write end — select will timeout

        with patch("os.wait"):
            result = _parent_wait(read_fd, 44443, timeout=0.1)

        assert result == 1

    def test_error_signal_returns_error(self):
        """Error signal from daemon returns 1."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"error\n")
        os.close(write_fd)

        with patch("os.wait"):
            result = _parent_wait(read_fd, 44443, timeout=1.0)

        assert result == 1

    def test_ready_with_health_check(self):
        """Ready signal + health check returns 0."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"ready\n")
        os.close(write_fd)

        with patch("os.wait"), \
             patch("server.daemon._health_check", return_value=True), \
             patch("server.daemon.check_status", return_value={"pid": 12345}):
            result = _parent_wait(read_fd, 44443, timeout=1.0)

        assert result == 0

    def test_ready_but_health_check_fails(self):
        """Ready signal but health check fails returns 1."""
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"ready\n")
        os.close(write_fd)

        with patch("os.wait"), \
             patch("server.daemon._health_check", return_value=False), \
             patch("time.sleep"):
            result = _parent_wait(read_fd, 44443, timeout=1.0)

        assert result == 1
