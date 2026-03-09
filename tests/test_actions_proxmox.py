"""Tests for Proxmox action classes.

Tests for local actions (run_command) and remote actions (run_ssh).
"""

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


@dataclass
class MockHostConfig:
    """Minimal host config for testing."""
    name: str = 'test-host'
    ssh_host: str = '192.0.2.1'
    ssh_user: str = 'root'
    automation_user: str = 'homestak'
    vm_id: int = 99913
    config_file: Path = Path('/tmp/test.yaml')


# -- Module-level helpers --------------------------------------------------

class TestGetLocalVmIp:
    """Test _get_local_vm_ip helper."""

    def test_returns_ip_on_success(self):
        """Should parse guest agent JSON and return first non-loopback IPv4."""
        from actions.proxmox import _get_local_vm_ip

        ifaces = [
            {'name': 'lo', 'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '127.0.0.1'}
            ]},
            {'name': 'eth0', 'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '192.0.2.10'}
            ]},
        ]
        with patch('actions.proxmox.run_command',
                    return_value=(0, json.dumps(ifaces), '')):
            ip = _get_local_vm_ip(99900)

        assert ip == '192.0.2.10'

    def test_returns_none_on_command_failure(self):
        """Should return None when qm guest cmd fails."""
        from actions.proxmox import _get_local_vm_ip

        with patch('actions.proxmox.run_command', return_value=(1, '', 'error')):
            ip = _get_local_vm_ip(99900)

        assert ip is None

    def test_returns_none_on_invalid_json(self):
        """Should return None when output is not valid JSON."""
        from actions.proxmox import _get_local_vm_ip

        with patch('actions.proxmox.run_command',
                    return_value=(0, 'not json', '')):
            ip = _get_local_vm_ip(99900)

        assert ip is None

    def test_returns_none_when_no_ipv4(self):
        """Should return None when no non-loopback IPv4 found."""
        from actions.proxmox import _get_local_vm_ip

        ifaces = [
            {'name': 'lo', 'ip-addresses': [
                {'ip-address-type': 'ipv4', 'ip-address': '127.0.0.1'}
            ]},
        ]
        with patch('actions.proxmox.run_command',
                    return_value=(0, json.dumps(ifaces), '')):
            ip = _get_local_vm_ip(99900)

        assert ip is None

    def test_skips_ipv6_addresses(self):
        """Should skip IPv6 addresses and return IPv4."""
        from actions.proxmox import _get_local_vm_ip

        ifaces = [
            {'name': 'eth0', 'ip-addresses': [
                {'ip-address-type': 'ipv6', 'ip-address': 'fe80::1'},
                {'ip-address-type': 'ipv4', 'ip-address': '192.0.2.20'},
            ]},
        ]
        with patch('actions.proxmox.run_command',
                    return_value=(0, json.dumps(ifaces), '')):
            ip = _get_local_vm_ip(99900)

        assert ip == '192.0.2.20'


# -- Local actions ---------------------------------------------------------

class TestStartVMAction:
    """Test StartVMAction (local execution via run_command)."""

    def test_start_vm_success(self):
        """Successful VM start should return success."""
        from actions.proxmox import StartVMAction

        action = StartVMAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {}

        with patch('actions.proxmox.run_command', return_value=(0, '', '')):
            result = action.run(config, context)

        assert result.success is True
        assert '99913' in result.message

    def test_start_vm_failure(self):
        """Failed VM start should return failure with error message."""
        from actions.proxmox import StartVMAction

        action = StartVMAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {}

        with patch('actions.proxmox.run_command',
                    return_value=(1, '', 'VM is locked')):
            result = action.run(config, context)

        assert result.success is False
        assert 'Failed' in result.message
        assert 'VM is locked' in result.message

    def test_missing_vm_id_returns_error(self):
        """Missing vm_id should return failure."""
        from actions.proxmox import StartVMAction

        action = StartVMAction(name='test', vm_id_attr='missing_id')
        config = MockHostConfig()
        config.missing_id = None
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'missing' in result.message.lower()

    def test_resolves_from_context(self):
        """Should prefer context over config for vm_id."""
        from actions.proxmox import StartVMAction

        action = StartVMAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {'vm_id': 12345}

        with patch('actions.proxmox.run_command', return_value=(0, '', '')) as mock:
            result = action.run(config, context)

        assert result.success is True
        # Verify run_command was called with the context value
        cmd = mock.call_args[0][0]
        assert '12345' in cmd

    def test_uses_sudo_qm(self):
        """Should call sudo qm start."""
        from actions.proxmox import StartVMAction

        action = StartVMAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {}

        with patch('actions.proxmox.run_command', return_value=(0, '', '')) as mock:
            action.run(config, context)

        cmd = mock.call_args[0][0]
        assert cmd == ['sudo', 'qm', 'start', '99913']


class TestWaitForGuestAgentAction:
    """Test WaitForGuestAgentAction (local execution)."""

    def test_success_returns_ip(self):
        """Should return IP in context_updates on success."""
        from actions.proxmox import WaitForGuestAgentAction

        action = WaitForGuestAgentAction(
            name='test', vm_id_attr='vm_id',
            ip_context_key='node_ip', timeout=5,
        )
        config = MockHostConfig()
        context = {}

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    return_value='192.0.2.10'):
            result = action.run(config, context)

        assert result.success is True
        assert result.context_updates == {'node_ip': '192.0.2.10'}

    def test_failure_returns_error(self):
        """Should return failure when guest agent times out."""
        from actions.proxmox import WaitForGuestAgentAction

        action = WaitForGuestAgentAction(
            name='test', vm_id_attr='vm_id', timeout=5,
        )
        config = MockHostConfig()
        context = {}

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    return_value=None):
            result = action.run(config, context)

        assert result.success is False

    def test_missing_vm_id(self):
        """Should fail when vm_id is missing."""
        from actions.proxmox import WaitForGuestAgentAction

        action = WaitForGuestAgentAction(name='test', vm_id_attr='missing')
        config = MockHostConfig()
        config.missing = None
        context = {}

        result = action.run(config, context)

        assert result.success is False


class TestLookupVMIPAction:
    """Test LookupVMIPAction (local execution)."""

    def test_success_returns_ip(self):
        """Should return IP in context_updates."""
        from actions.proxmox import LookupVMIPAction

        action = LookupVMIPAction(
            name='test', vmid=99900, ip_context_key='vm_ip',
        )

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    return_value='192.0.2.50'):
            result = action.run(MockHostConfig(), {})

        assert result.success is True
        assert result.context_updates == {'vm_ip': '192.0.2.50'}

    def test_failure_when_no_ip(self):
        """Should fail when guest agent returns no IP."""
        from actions.proxmox import LookupVMIPAction

        action = LookupVMIPAction(
            name='test', vmid=99900, ip_context_key='vm_ip',
        )

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    return_value=None):
            result = action.run(MockHostConfig(), {})

        assert result.success is False


class TestStartProvisionedVMsAction:
    """Test StartProvisionedVMsAction (local execution)."""

    def test_starts_all_vms(self):
        """Should start all VMs from provisioned_vms context."""
        from actions.proxmox import StartProvisionedVMsAction

        action = StartProvisionedVMsAction(name='test')
        config = MockHostConfig()
        context = {
            'provisioned_vms': [
                {'name': 'vm1', 'vmid': 99901},
                {'name': 'vm2', 'vmid': 99902},
            ]
        }

        with patch('actions.proxmox.run_command', return_value=(0, '', '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'vm1' in result.message
        assert 'vm2' in result.message

    def test_no_provisioned_vms(self):
        """Should fail when no provisioned_vms in context."""
        from actions.proxmox import StartProvisionedVMsAction

        action = StartProvisionedVMsAction(name='test')
        result = action.run(MockHostConfig(), {})

        assert result.success is False

    def test_stops_on_first_failure(self):
        """Should stop and return failure if any VM fails to start."""
        from actions.proxmox import StartProvisionedVMsAction

        action = StartProvisionedVMsAction(name='test')
        context = {
            'provisioned_vms': [
                {'name': 'vm1', 'vmid': 99901},
                {'name': 'vm2', 'vmid': 99902},
            ]
        }

        with patch('actions.proxmox.run_command',
                    return_value=(1, '', 'start failed')):
            result = action.run(MockHostConfig(), context)

        assert result.success is False
        assert 'vm1' in result.message


class TestWaitForProvisionedVMsAction:
    """Test WaitForProvisionedVMsAction (local execution)."""

    def test_collects_all_ips(self):
        """Should collect IPs for all VMs and set vm_ip for backward compat."""
        from actions.proxmox import WaitForProvisionedVMsAction

        action = WaitForProvisionedVMsAction(name='test', timeout=5)
        context = {
            'provisioned_vms': [
                {'name': 'vm1', 'vmid': 99901},
                {'name': 'vm2', 'vmid': 99902},
            ]
        }

        ips = {'99901': '192.0.2.10', '99902': '192.0.2.11'}

        def mock_wait(vm_id, **kwargs):
            return ips.get(str(vm_id))

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    side_effect=mock_wait):
            result = action.run(MockHostConfig(), context)

        assert result.success is True
        assert result.context_updates['vm1_ip'] == '192.0.2.10'
        assert result.context_updates['vm2_ip'] == '192.0.2.11'
        assert result.context_updates['vm_ip'] == '192.0.2.10'  # First VM

    def test_no_provisioned_vms(self):
        """Should fail when no provisioned_vms in context."""
        from actions.proxmox import WaitForProvisionedVMsAction

        action = WaitForProvisionedVMsAction(name='test', timeout=5)
        result = action.run(MockHostConfig(), {})

        assert result.success is False

    def test_fails_if_any_vm_timeout(self):
        """Should fail if any VM's guest agent times out."""
        from actions.proxmox import WaitForProvisionedVMsAction

        action = WaitForProvisionedVMsAction(name='test', timeout=5)
        context = {
            'provisioned_vms': [
                {'name': 'vm1', 'vmid': 99901},
            ]
        }

        with patch('actions.proxmox._wait_for_local_guest_agent',
                    return_value=None):
            result = action.run(MockHostConfig(), context)

        assert result.success is False


# -- Remote actions --------------------------------------------------------

class TestStartVMRemoteAction:
    """Test StartVMRemoteAction (SSH execution)."""

    def test_success(self):
        """Successful remote VM start."""
        from actions.proxmox import StartVMRemoteAction

        action = StartVMRemoteAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {'node_ip': '192.0.2.1', 'vm_id': 99920}

        with patch('actions.proxmox.run_ssh', return_value=(0, '', '')):
            result = action.run(config, context)

        assert result.success is True

    def test_missing_pve_host(self):
        """Should fail when PVE host not in context."""
        from actions.proxmox import StartVMRemoteAction

        action = StartVMRemoteAction(name='test', vm_id_attr='vm_id')
        config = MockHostConfig()
        context = {'vm_id': 99920}

        result = action.run(config, context)

        assert result.success is False
        assert 'node_ip' in result.message


class TestDiscoverVMsAction:
    """Test DiscoverVMsAction (remote PVE API query)."""

    def test_discovers_matching_vms(self):
        """Should discover VMs matching name pattern and vmid range."""
        from actions.proxmox import DiscoverVMsAction

        action = DiscoverVMsAction(
            name='test', name_pattern='test*',
            vmid_range=(99900, 99999),
        )
        config = MockHostConfig()
        context = {'ssh_host': '192.0.2.1'}

        vms_json = json.dumps([
            {'name': 'test-vm1', 'vmid': 99901, 'status': 'running', 'node': 'pve'},
            {'name': 'test-vm2', 'vmid': 99902, 'status': 'stopped', 'node': 'pve'},
            {'name': 'other', 'vmid': 10000, 'status': 'running', 'node': 'pve'},
        ])

        with patch('actions.proxmox.run_ssh',
                    return_value=(0, vms_json, '')):
            result = action.run(config, context)

        assert result.success is True
        discovered = result.context_updates['discovered_vms']
        assert len(discovered) == 2
        assert discovered[0]['name'] == 'test-vm1'
        assert discovered[1]['name'] == 'test-vm2'

    def test_filters_by_vmid_range(self):
        """Should exclude VMs outside vmid range."""
        from actions.proxmox import DiscoverVMsAction

        action = DiscoverVMsAction(
            name='test', name_pattern='test*',
            vmid_range=(99900, 99999),
        )
        config = MockHostConfig()
        context = {'ssh_host': '192.0.2.1'}

        vms_json = json.dumps([
            {'name': 'test-low', 'vmid': 100, 'status': 'running', 'node': 'pve'},
            {'name': 'test-ok', 'vmid': 99950, 'status': 'running', 'node': 'pve'},
        ])

        with patch('actions.proxmox.run_ssh',
                    return_value=(0, vms_json, '')):
            result = action.run(config, context)

        discovered = result.context_updates['discovered_vms']
        assert len(discovered) == 1
        assert discovered[0]['name'] == 'test-ok'
