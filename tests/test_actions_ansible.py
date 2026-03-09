"""Tests for Ansible action classes.

Tests for AnsiblePlaybookAction, AnsibleLocalPlaybookAction, and EnsurePVEAction.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

from conftest import MockHostConfig


class TestAnsiblePlaybookAction:
    """Test AnsiblePlaybookAction."""

    def test_success(self, tmp_path):
        """Successful playbook run should return success."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/pve-setup.yml',
            host_key='node_ip', wait_for_ssh_before=False,
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command', return_value=(0, 'ok', '')):
            # Create the ansible dir so exists() passes
            result = action.run(config, context)

        assert result.success is True
        assert 'pve-setup.yml' in result.message

    def test_missing_host_key(self):
        """Missing host key in context should return failure."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='missing_key',
        )
        config = MockHostConfig()
        result = action.run(config, {})

        assert result.success is False
        assert 'missing_key' in result.message

    def test_missing_ansible_dir(self, tmp_path):
        """Missing ansible directory should return failure."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='node_ip', wait_for_ssh_before=False,
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.get_sibling_dir',
                   return_value=tmp_path / 'nonexistent'):
            result = action.run(config, context)

        assert result.success is False
        assert 'not found' in result.message

    def test_ssh_wait_failure(self):
        """SSH wait failure should return error."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='node_ip', wait_for_ssh_before=True, ssh_timeout=1,
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.get_sibling_dir', return_value=Path('/tmp')), \
             patch('actions.ansible.wait_for_ssh', return_value=False):
            result = action.run(config, context)

        assert result.success is False
        assert 'SSH not available' in result.message

    def test_playbook_failure(self, tmp_path):
        """Failed playbook should return error with truncated message."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='node_ip', wait_for_ssh_before=False,
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command',
                   return_value=(1, '', 'TASK [failed] fatal error')):
            result = action.run(config, context)

        assert result.success is False
        assert 'failed' in result.message.lower()

    def test_extra_vars_passed(self, tmp_path):
        """Extra vars should be passed to ansible-playbook command."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='node_ip', wait_for_ssh_before=False,
            extra_vars={'pve_hostname': 'myhost', 'timezone': 'UTC'},
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        captured_cmd = []

        def capture(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return (0, '', '')

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command', side_effect=capture):
            action.run(config, context)

        # Check extra vars are in command
        assert 'pve_hostname=myhost' in ' '.join(captured_cmd)
        assert 'timezone=UTC' in ' '.join(captured_cmd)

    def test_command_includes_inventory_and_playbook(self, tmp_path):
        """Command should include inventory and playbook args."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/pve-setup.yml',
            inventory='inventory/remote-dev.yml',
            host_key='node_ip', wait_for_ssh_before=False,
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        captured_cmd = []

        def capture(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return (0, '', '')

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command', side_effect=capture):
            action.run(config, context)

        assert 'ansible-playbook' in captured_cmd
        assert '-i' in captured_cmd
        idx = captured_cmd.index('-i')
        assert captured_cmd[idx + 1] == 'inventory/remote-dev.yml'
        assert 'playbooks/pve-setup.yml' in captured_cmd

    def test_site_config_vars_resolved(self, tmp_path):
        """Site-config vars should be resolved and passed when enabled."""
        from actions.ansible import AnsiblePlaybookAction

        action = AnsiblePlaybookAction(
            name='test', playbook='playbooks/test.yml',
            host_key='node_ip', wait_for_ssh_before=False,
            use_site_config=True, env='dev',
        )
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        captured_cmd = []

        def capture(cmd, **kwargs):
            captured_cmd.extend(cmd)
            return (0, '', '')

        mock_resolver = MagicMock()
        mock_resolver.resolve_ansible_vars.return_value = {
            'timezone': 'America/Denver',
            'sudo_nopasswd': True,
        }

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.ConfigResolver', return_value=mock_resolver), \
             patch('actions.ansible.run_command', side_effect=capture):
            action.run(config, context)

        cmd_str = ' '.join(captured_cmd)
        assert 'timezone=America/Denver' in cmd_str
        assert 'sudo_nopasswd=true' in cmd_str


class TestAnsibleLocalPlaybookAction:
    """Test AnsibleLocalPlaybookAction."""

    def test_success(self, tmp_path):
        """Successful local playbook should return success."""
        from actions.ansible import AnsibleLocalPlaybookAction

        action = AnsibleLocalPlaybookAction(
            name='test', playbook='playbooks/pve-setup.yml',
        )
        config = MockHostConfig()
        context = {}

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command', return_value=(0, 'ok', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'completed locally' in result.message

    def test_failure(self, tmp_path):
        """Failed local playbook should return error."""
        from actions.ansible import AnsibleLocalPlaybookAction

        action = AnsibleLocalPlaybookAction(
            name='test', playbook='playbooks/test.yml',
        )
        config = MockHostConfig()
        context = {}

        with patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command',
                   return_value=(1, '', 'playbook error')):
            result = action.run(config, context)

        assert result.success is False
        assert 'failed' in result.message.lower()

    def test_missing_ansible_dir(self, tmp_path):
        """Missing ansible dir should return failure."""
        from actions.ansible import AnsibleLocalPlaybookAction

        action = AnsibleLocalPlaybookAction(
            name='test', playbook='playbooks/test.yml',
        )
        config = MockHostConfig()

        with patch('actions.ansible.get_sibling_dir',
                   return_value=tmp_path / 'missing'):
            result = action.run(config, {})

        assert result.success is False
        assert 'not found' in result.message


class TestEnsurePVEAction:
    """Test EnsurePVEAction."""

    def test_pve_pre_installed_marker(self):
        """Pre-installed marker file should skip installation."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.wait_for_ssh', return_value=True), \
             patch('actions.ansible.run_ssh',
                   return_value=(0, '', '')):  # marker file exists
            result = action.run(config, context)

        assert result.success is True
        assert 'pre-installed' in result.message.lower()

    def test_pve_already_running(self):
        """Active pveproxy should skip installation."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.wait_for_ssh', return_value=True), \
             patch('actions.ansible.run_ssh') as mock_ssh:
            mock_ssh.side_effect = [
                (1, '', ''),              # marker file NOT found
                (0, 'active', ''),        # pveproxy is active
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'already installed' in result.message.lower()

    def test_missing_host_key(self):
        """Missing host key should return failure."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='missing')
        config = MockHostConfig()
        result = action.run(config, {})

        assert result.success is False
        assert 'missing' in result.message

    def test_ssh_not_available(self):
        """SSH unavailable should return failure."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='node_ip', ssh_timeout=1)
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.wait_for_ssh', return_value=False):
            result = action.run(config, context)

        assert result.success is False
        assert 'SSH not available' in result.message

    def test_install_success(self, tmp_path):
        """PVE not installed should trigger installation."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.wait_for_ssh', return_value=True), \
             patch('actions.ansible.run_ssh') as mock_ssh, \
             patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command', return_value=(0, '', '')):
            mock_ssh.side_effect = [
                (1, '', ''),  # no marker file
                (1, '', ''),  # pveproxy not active
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'installed successfully' in result.message.lower()

    def test_install_failure(self, tmp_path):
        """Failed PVE installation should return error."""
        from actions.ansible import EnsurePVEAction

        action = EnsurePVEAction(name='test', host_key='node_ip')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1'}

        with patch('actions.ansible.wait_for_ssh', return_value=True), \
             patch('actions.ansible.run_ssh') as mock_ssh, \
             patch('actions.ansible.get_sibling_dir', return_value=tmp_path), \
             patch('actions.ansible.run_command',
                   return_value=(1, '', 'install failed')):
            mock_ssh.side_effect = [
                (1, '', ''),  # no marker file
                (1, '', ''),  # pveproxy not active
            ]
            result = action.run(config, context)

        assert result.success is False
        assert 'failed' in result.message.lower()
