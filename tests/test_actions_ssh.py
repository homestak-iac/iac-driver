"""Tests for SSH-based action classes.

Tests for SSHCommandAction, WaitForSSHAction, WaitForFileAction,
VerifyPackagesAction, VerifyUserAction, and ActionResult.
"""

from pathlib import Path
from unittest.mock import patch

from common import ActionResult
from conftest import MockHostConfig


class TestSSHCommandAction:
    """Test SSHCommandAction."""

    def test_success_returns_action_result(self):
        """Successful SSH command should return success=True."""
        from actions.ssh import SSHCommandAction

        action = SSHCommandAction(name='test', command='echo hello', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ssh.run_ssh', return_value=(0, 'hello\n', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'hello' in result.message

    def test_failure_returns_action_result(self):
        """Failed SSH command should return success=False."""
        from actions.ssh import SSHCommandAction

        action = SSHCommandAction(name='test', command='false', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ssh.run_ssh', return_value=(1, '', 'command failed')):
            result = action.run(config, context)

        assert result.success is False
        assert 'failed' in result.message.lower()

    def test_missing_host_key_returns_error(self):
        """Missing host_key in context should return failure."""
        from actions.ssh import SSHCommandAction

        action = SSHCommandAction(name='test', command='echo hello', host_key='nonexistent')
        config = MockHostConfig()
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'nonexistent' in result.message


class TestWaitForSSHAction:
    """Test WaitForSSHAction."""

    def test_immediate_success(self):
        """SSH available immediately should return success."""
        from actions.ssh import WaitForSSHAction

        action = WaitForSSHAction(name='test', host_key='node_ip', timeout=5)
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ssh.wait_for_ping', return_value=True), \
             patch('actions.ssh.run_ssh', return_value=(0, 'ready', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'available' in result.message.lower()

    def test_missing_host_returns_error(self):
        """Missing host in context should return failure."""
        from actions.ssh import WaitForSSHAction

        action = WaitForSSHAction(name='test', host_key='missing')
        config = MockHostConfig()
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'missing' in result.message


class TestWaitForFileAction:
    """Test WaitForFileAction."""

    def test_file_found_immediately(self):
        """File found on first poll should return success."""
        from actions.ssh import WaitForFileAction

        action = WaitForFileAction(
            name='test', host_key='vm_ip',
            file_path='/tmp/marker.json', timeout=10, interval=1,
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('actions.ssh.run_ssh', return_value=(0, 'EXISTS', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'marker.json' in result.message

    def test_file_not_found_timeout(self):
        """File never found should return failure after timeout."""
        from actions.ssh import WaitForFileAction

        action = WaitForFileAction(
            name='test', host_key='vm_ip',
            file_path='/tmp/missing.json', timeout=1, interval=0.5,
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('actions.ssh.run_ssh', return_value=(1, '', 'not found')):
            result = action.run(config, context)

        assert result.success is False
        assert 'Timeout' in result.message

    def test_missing_host_returns_error(self):
        """Missing host in context should return failure."""
        from actions.ssh import WaitForFileAction

        action = WaitForFileAction(
            name='test', host_key='missing', file_path='/tmp/x',
        )
        config = MockHostConfig()
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'missing' in result.message


class TestVerifyPackagesAction:
    """Test VerifyPackagesAction."""

    def test_all_packages_installed(self):
        """All packages installed should return success."""
        from scenarios.vm_roundtrip import VerifyPackagesAction

        action = VerifyPackagesAction(
            name='test', host_key='vm_ip', packages=('htop', 'curl'),
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('scenarios.vm_roundtrip.run_ssh', return_value=(0, 'INSTALLED', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'htop' in result.message

    def test_missing_package_fails(self):
        """Missing package should return failure."""
        from scenarios.vm_roundtrip import VerifyPackagesAction

        action = VerifyPackagesAction(
            name='test', host_key='vm_ip', packages=('htop', 'missing-pkg'),
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('scenarios.vm_roundtrip.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (0, 'INSTALLED', ''),  # htop
                (0, 'MISSING', ''),    # missing-pkg
            ]
            result = action.run(config, context)

        assert result.success is False
        assert 'missing-pkg' in result.message

    def test_missing_host_returns_error(self):
        """Missing host in context should return failure."""
        from scenarios.vm_roundtrip import VerifyPackagesAction

        action = VerifyPackagesAction(name='test', packages=('curl',), host_key='missing')
        config = MockHostConfig()
        result = action.run(config, {})

        assert result.success is False
        assert 'missing' in result.message


class TestVerifyUserAction:
    """Test VerifyUserAction."""

    def test_user_exists(self):
        """User exists should return success."""
        from scenarios.vm_roundtrip import VerifyUserAction

        action = VerifyUserAction(
            name='test', host_key='vm_ip', username='homestak',
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('scenarios.vm_roundtrip.run_ssh',
                   return_value=(0, 'uid=1000(homestak) gid=1000(homestak)\nUSER_EXISTS', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'homestak' in result.message

    def test_user_missing_fails(self):
        """Missing user should return failure."""
        from scenarios.vm_roundtrip import VerifyUserAction

        action = VerifyUserAction(
            name='test', host_key='vm_ip', username='noone',
        )
        config = MockHostConfig()
        context = {'vm_ip': '192.0.2.1'}

        with patch('scenarios.vm_roundtrip.run_ssh',
                   return_value=(1, 'USER_MISSING', '')):
            result = action.run(config, context)

        assert result.success is False
        assert 'noone' in result.message

    def test_missing_host_returns_error(self):
        """Missing host in context should return failure."""
        from scenarios.vm_roundtrip import VerifyUserAction

        action = VerifyUserAction(name='test', username='homestak', host_key='missing')
        config = MockHostConfig()
        result = action.run(config, {})

        assert result.success is False
        assert 'missing' in result.message


class TestActionResult:
    """Test ActionResult dataclass."""

    def test_action_result_defaults(self):
        """ActionResult should have sensible defaults."""
        result = ActionResult(success=True)
        assert result.success is True
        assert result.message == ''
        assert result.duration == 0.0
        assert result.context_updates == {}
        assert result.continue_on_failure is False

    def test_action_result_with_context(self):
        """ActionResult should store context updates."""
        result = ActionResult(
            success=True,
            message='done',
            context_updates={'vm_ip': '192.0.2.1'}
        )
        assert result.context_updates == {'vm_ip': '192.0.2.1'}
