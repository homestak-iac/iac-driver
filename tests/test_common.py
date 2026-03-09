#!/usr/bin/env python3
"""Tests for common.py - shared utilities.

Tests verify:
1. run_command execution and error handling
2. run_ssh with and without jump host
3. wait_for_ping polling
4. wait_for_ssh polling
5. Timeout behavior
"""

from unittest.mock import patch, MagicMock

import pytest
from common import (
    ActionResult,
    run_command,
    run_ssh,
    wait_for_ping,
    wait_for_ssh,
)


class TestRunCommand:
    """Test run_command utility."""

    def test_returns_success_tuple(self):
        """Should return (returncode, stdout, stderr) on success."""
        rc, stdout, stderr = run_command(['echo', 'hello'])
        assert rc == 0
        assert 'hello' in stdout
        assert stderr == ''

    def test_returns_failure_tuple(self):
        """Should return non-zero returncode on failure."""
        rc, stdout, stderr = run_command(['false'])
        assert rc != 0

    def test_captures_stdout(self):
        """Should capture stdout."""
        rc, stdout, stderr = run_command(['echo', 'test output'])
        assert 'test output' in stdout

    def test_captures_stderr(self):
        """Should capture stderr."""
        rc, stdout, stderr = run_command(['sh', '-c', 'echo error >&2'])
        assert 'error' in stderr

    def test_respects_cwd(self, tmp_path):
        """Should run command in specified directory."""
        (tmp_path / 'marker.txt').write_text('found')
        rc, stdout, stderr = run_command(['cat', 'marker.txt'], cwd=tmp_path)
        assert rc == 0
        assert 'found' in stdout

    def test_timeout_returns_error(self):
        """Should return error on timeout."""
        rc, stdout, stderr = run_command(['sleep', '10'], timeout=1)
        assert rc == -1
        assert 'timed out' in stderr.lower()

    def test_passes_env_vars(self):
        """Should pass custom environment variables."""
        import os
        custom_env = os.environ.copy()
        custom_env['TEST_VAR'] = 'test_value'
        rc, stdout, stderr = run_command(['sh', '-c', 'echo $TEST_VAR'], env=custom_env)
        assert rc == 0
        assert 'test_value' in stdout


class TestRunSSH:
    """Test run_ssh utility."""

    def test_builds_ssh_command(self):
        """Should build correct SSH command with current user as default."""
        import getpass
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (0, 'output', '')
            rc, stdout, stderr = run_ssh('198.51.100.10', 'echo hello')

            # Verify SSH was called with correct args
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert 'ssh' in cmd
            assert f'{getpass.getuser()}@198.51.100.10' in cmd
            assert 'echo hello' in cmd

    def test_uses_custom_user(self):
        """Should use specified user."""
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (0, 'output', '')
            run_ssh('198.51.100.10', 'cmd', user='admin')

            cmd = mock_run.call_args[0][0]
            assert 'admin@198.51.100.10' in cmd

    def test_includes_relaxed_host_checking(self):
        """Should include StrictHostKeyChecking=no."""
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (0, 'output', '')
            run_ssh('198.51.100.10', 'cmd')

            cmd = mock_run.call_args[0][0]
            cmd_str = ' '.join(cmd)
            assert 'StrictHostKeyChecking=no' in cmd_str

    def test_jump_host_builds_nested_ssh(self):
        """Should build nested SSH command for jump host."""
        import getpass
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (0, 'output', '')
            run_ssh('198.51.100.20', 'cmd', jump_host='198.51.100.10')

            cmd = mock_run.call_args[0][0]
            cmd_str = ' '.join(cmd)
            current_user = getpass.getuser()
            # Should connect to jump host first
            assert f'{current_user}@198.51.100.10' in cmd_str
            # Then to target
            assert f'{current_user}@198.51.100.20' in cmd_str


class TestWaitForPing:
    """Test wait_for_ping polling."""

    def test_returns_true_on_success(self):
        """Should return True when host is pingable."""
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (0, '', '')
            result = wait_for_ping('198.51.100.10', timeout=5)
            assert result is True

    def test_returns_false_on_timeout(self):
        """Should return False when timeout reached."""
        with patch('common.run_command') as mock_run:
            mock_run.return_value = (1, '', 'timeout')
            result = wait_for_ping('198.51.100.10', timeout=1, interval=0.1)
            assert result is False

    def test_retries_on_failure(self):
        """Should retry ping on failure."""
        with patch('common.run_command') as mock_run:
            # Fail twice, succeed third time
            mock_run.side_effect = [
                (1, '', 'fail'),
                (1, '', 'fail'),
                (0, '', ''),
            ]
            result = wait_for_ping('198.51.100.10', timeout=5, interval=0.1)
            assert result is True
            assert mock_run.call_count == 3


class TestWaitForSSH:
    """Test wait_for_ssh polling."""

    def test_returns_true_on_success(self):
        """Should return True when SSH is available."""
        with patch('common.wait_for_ping', return_value=True), \
             patch('common.run_ssh') as mock_ssh:
            mock_ssh.return_value = (0, 'ready', '')
            result = wait_for_ssh('198.51.100.10', timeout=5)
            assert result is True

    def test_returns_false_on_timeout(self):
        """Should return False when timeout reached."""
        with patch('common.wait_for_ping', return_value=False), \
             patch('common.run_ssh') as mock_ssh:
            mock_ssh.return_value = (1, '', 'connection refused')
            result = wait_for_ssh('198.51.100.10', timeout=1, interval=0.1)
            assert result is False

    def test_retries_on_ssh_failure(self):
        """Should retry SSH on failure."""
        with patch('common.wait_for_ping', return_value=True), \
             patch('common.run_ssh') as mock_ssh, \
             patch('time.sleep'):  # Speed up test
            # Fail twice, succeed third time
            mock_ssh.side_effect = [
                (1, '', 'refused'),
                (1, '', 'refused'),
                (0, 'ready', ''),
            ]
            result = wait_for_ssh('198.51.100.10', timeout=10, interval=0.1)
            assert result is True

    def test_checks_for_ready_in_output(self):
        """Should verify 'ready' is in SSH output."""
        with patch('common.wait_for_ping', return_value=True), \
             patch('common.run_ssh') as mock_ssh:
            # Returns success but no 'ready' output - should retry
            mock_ssh.side_effect = [
                (0, 'something else', ''),  # No 'ready'
                (0, 'ready', ''),  # Has 'ready'
            ]
            with patch('time.sleep'):
                result = wait_for_ssh('198.51.100.10', timeout=5, interval=0.1)
            assert result is True
            assert mock_ssh.call_count == 2


class TestActionResultExtended:
    """Additional ActionResult tests."""

    def test_continue_on_failure_flag(self):
        """Should support continue_on_failure flag."""
        result = ActionResult(
            success=False,
            message='Failed but continue',
            continue_on_failure=True
        )
        assert result.success is False
        assert result.continue_on_failure is True

    def test_duration_tracking(self):
        """Should store duration value."""
        result = ActionResult(success=True, duration=1.5)
        assert result.duration == 1.5

    def test_mutable_context_updates(self):
        """Context updates dict should be mutable."""
        result = ActionResult(success=True)
        result.context_updates['new_key'] = 'new_value'
        assert result.context_updates == {'new_key': 'new_value'}
