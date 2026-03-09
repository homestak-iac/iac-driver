"""Tests for manifest_opr.executor module.

Uses mocked action classes to test execution ordering, error handling,
and dry-run behavior without real infrastructure.
"""

import os
import socket
from unittest.mock import patch, MagicMock

import pytest

from common import ActionResult
from manifest import Manifest
from manifest_opr.executor import NodeExecutor
from manifest_opr.graph import ManifestGraph
from manifest_opr.server_mgmt import ServerManager

# Save originals before any autouse fixtures patch them
_original_ensure = ServerManager.ensure
_original_stop = ServerManager.stop


def _make_manifest(nodes_data, name='test', pattern='flat', on_error='stop'):
    """Helper to create a v2 manifest from node dicts."""
    return Manifest.from_dict({
        'schema_version': 2,
        'name': name,
        'pattern': pattern,
        'nodes': nodes_data,
        'settings': {'on_error': on_error, 'verify_ssh': False},
    })


def _make_config():
    """Create a mock HostConfig."""
    config = MagicMock()
    config.name = 'test-host'
    config.ssh_host = '198.51.100.61'
    config.ssh_user = 'root'
    config.automation_user = 'homestak'
    return config


def _success_result(**ctx):
    """Create a successful ActionResult with optional context updates."""
    return ActionResult(success=True, message='ok', duration=0.1, context_updates=ctx)


def _fail_result(msg='failed'):
    """Create a failed ActionResult."""
    return ActionResult(success=False, message=msg, duration=0.1)


@pytest.fixture(autouse=True)
def _skip_server(monkeypatch):
    """Prevent real SSH calls to start/stop the spec server in unit tests."""
    monkeypatch.setattr(
        'manifest_opr.server_mgmt.ServerManager.ensure', lambda self: None,
    )
    monkeypatch.setattr(
        'manifest_opr.server_mgmt.ServerManager.stop', lambda self: None,
    )


class TestNodeExecutorDryRun:
    """Tests for dry-run (preview) mode."""

    def test_create_dry_run(self, capsys):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config, dry_run=True,
        )
        success, state = executor.create({})

        assert success is True
        captured = capsys.readouterr()
        assert 'DRY-RUN CREATE' in captured.out
        assert 'test' in captured.out

    def test_destroy_dry_run(self, capsys):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config, dry_run=True,
        )
        success, state = executor.destroy({})

        assert success is True
        captured = capsys.readouterr()
        assert 'DRY-RUN DESTROY' in captured.out


class TestNodeExecutorCreate:
    """Tests for create lifecycle with mocked actions."""

    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_single_node_create(self, mock_create):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(test_vm_id=99001, test_ip='198.51.100.10')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is True
        assert mock_create.call_count == 1
        assert state.get_node('test').status == 'completed'

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_tiered_create_delegates_children(self, mock_create, mock_delegate):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(pve_vm_id=99001, pve_ip='198.51.100.10')
        mock_delegate.return_value = _success_result(test_vm_id=99002, test_ip='198.51.100.11')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is True
        assert mock_create.call_count == 1  # Only root (pve) created locally
        assert mock_delegate.call_count == 1  # Children delegated

    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_create_stop_on_root_failure(self, mock_create):
        """When root node fails, stop immediately (children never attempted)."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered', on_error='stop')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _fail_result('tofu apply failed')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is False
        assert mock_create.call_count == 1  # Only root attempted
        assert state.get_node('pve').status == 'failed'
        assert state.get_node('test').status == 'pending'  # Never attempted (delegation skipped)

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree_destroy')
    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree')
    @patch('manifest_opr.executor.NodeExecutor._destroy_node')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_create_rollback_on_delegation_failure(self, mock_create, mock_destroy, mock_delegate, mock_delegate_destroy):
        """When subtree delegation fails with on_error=rollback, root node should be rolled back."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered', on_error='rollback')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(pve_vm_id=99001, pve_ip='198.51.100.10')
        mock_delegate.return_value = _fail_result('delegation failed')
        mock_destroy.return_value = _success_result()
        mock_delegate_destroy.return_value = _success_result()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is False
        assert mock_destroy.call_count == 1  # Rolled back pve
        assert state.get_node('pve').status == 'destroyed'

    @patch('manifest_opr.executor.NodeExecutor._destroy_node')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_create_rollback_flat_failure(self, mock_create, mock_destroy):
        """Flat manifest rollback: first VM succeeds, second fails, first rolled back."""
        manifest = _make_manifest([
            {'name': 'vm1', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small'},
        ], on_error='rollback')
        graph = ManifestGraph(manifest)
        config = _make_config()

        call_count = [0]

        def create_side_effect(exec_node, context):
            call_count[0] += 1
            if call_count[0] == 1:
                return _success_result(vm1_vm_id=99001, vm1_ip='198.51.100.10')
            return _fail_result('provision failed')

        mock_create.side_effect = create_side_effect
        mock_destroy.return_value = _success_result()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is False
        assert mock_destroy.call_count == 1  # Rolled back vm1
        assert state.get_node('vm1').status == 'destroyed'

    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_create_continue_on_failure(self, mock_create):
        manifest = _make_manifest([
            {'name': 'vm1', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small'},
        ], on_error='continue')
        graph = ManifestGraph(manifest)
        config = _make_config()

        call_count = [0]

        def side_effect(exec_node, context):
            call_count[0] += 1
            if call_count[0] == 1:
                return _fail_result('vm1 failed')
            return _success_result(vm2_vm_id=99002, vm2_ip='198.51.100.11')

        mock_create.side_effect = side_effect

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is False  # Overall failed because vm1 failed
        assert mock_create.call_count == 2  # Both attempted
        assert state.get_node('vm1').status == 'failed'
        assert state.get_node('vm2').status == 'completed'


class TestNodeExecutorDestroy:
    """Tests for destroy lifecycle."""

    @patch('manifest_opr.executor.NodeExecutor._destroy_node')
    def test_destroy_flat(self, mock_destroy):
        """Flat manifest: all VMs destroyed locally."""
        manifest = _make_manifest([
            {'name': 'vm1', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        destroy_order = []

        def side_effect(exec_node, context):
            destroy_order.append(exec_node.name)
            return _success_result()

        mock_destroy.side_effect = side_effect

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.destroy({})

        assert success is True
        # Destroy order is reversed create order: vm2, vm1
        assert set(destroy_order) == {'vm1', 'vm2'}

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree_destroy')
    @patch('manifest_opr.executor.NodeExecutor._destroy_node')
    def test_destroy_tiered_delegates_children(self, mock_destroy, mock_delegate_destroy):
        """Tiered manifest: children delegated, root destroyed locally."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_destroy.return_value = _success_result()
        mock_delegate_destroy.return_value = _success_result()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        # Put PVE IP in context so delegation can find it
        success, state = executor.destroy({'pve_ip': '198.51.100.10'})

        assert success is True
        assert mock_delegate_destroy.call_count == 1  # Children delegated
        assert mock_destroy.call_count == 1  # Root destroyed locally


class TestNodeExecutorDelegation:
    """Tests for PVE subtree delegation."""

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_pve_with_children_delegates(self, mock_create, mock_delegate):
        """PVE root node with children should trigger delegation."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(pve_vm_id=99001, pve_ip='198.51.100.10')
        mock_delegate.return_value = _success_result(test_vm_id=99002, test_ip='198.51.100.11')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is True
        assert mock_create.call_count == 1  # Only root node
        assert mock_delegate.call_count == 1  # Delegation called

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_vm_root_no_delegation(self, mock_create, mock_delegate):
        """VM root node without children should not delegate."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(test_vm_id=99001, test_ip='198.51.100.10')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is True
        assert mock_delegate.call_count == 0

    @patch('manifest_opr.executor.NodeExecutor._delegate_subtree')
    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_delegation_failure_marks_descendants(self, mock_create, mock_delegate):
        """Failed delegation should mark all descendants as failed."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        mock_create.return_value = _success_result(pve_vm_id=99001, pve_ip='198.51.100.10')
        mock_delegate.return_value = _fail_result('SSH connection refused')

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, state = executor.create({})

        assert success is False
        assert state.get_node('pve').status == 'completed'
        assert state.get_node('test').status == 'failed'
        assert 'Delegation failed' in state.get_node('test').error

    @patch('manifest_opr.executor.NodeExecutor._create_node')
    def test_only_root_nodes_created_locally(self, mock_create):
        """Only depth-0 nodes should be passed to _create_node."""
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'preset': 'vm-medium', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        created_names = []

        def side_effect(exec_node, context):
            created_names.append(exec_node.name)
            return _success_result(**{f'{exec_node.name}_vm_id': exec_node.manifest_node.vmid, f'{exec_node.name}_ip': '198.51.100.10'})

        mock_create.side_effect = side_effect

        # Mock _delegate_subtree since root has children
        with patch.object(NodeExecutor, '_delegate_subtree') as mock_delegate:
            mock_delegate.return_value = _success_result(leaf_vm_id=99002, leaf_ip='198.51.100.11', test_vm_id=99003, test_ip='198.51.100.12')
            executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
            success, state = executor.create({})

        assert success is True
        assert created_names == ['root']  # Only root created locally

    def test_delegate_subtree_passes_self_addr(self):
        """Delegation command should include --self-addr with PVE node's IP (#200)."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9', 'preset': 'vm-large'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'preset': 'vm-small', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        from manifest_opr.state import ExecutionState
        state = ExecutionState('test', 'test-host')
        state.add_node('pve')
        state.add_node('test')

        context = {'pve_ip': '198.51.100.153'}

        with patch('actions.recursive.RecursiveScenarioAction') as MockAction:
            mock_instance = MagicMock()
            mock_instance.run.return_value = _success_result()
            MockAction.return_value = mock_instance

            executor._delegate_subtree(graph.get_node('pve'), context)

            # Verify RecursiveScenarioAction was called with --self-addr in raw_command
            assert MockAction.called
            raw_cmd = MockAction.call_args.kwargs['raw_command']
            assert '--self-addr 198.51.100.153' in raw_cmd


class TestNodeExecutorTest:
    """Tests for test lifecycle (create + verify + destroy)."""

    @patch('manifest_opr.executor.NodeExecutor.destroy')
    @patch('manifest_opr.executor.NodeExecutor.create')
    def test_test_success(self, mock_create, mock_destroy):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        from manifest_opr.state import ExecutionState
        state = ExecutionState('test', 'test-host')
        state.add_node('test').complete(vm_id=99001, ip='198.51.100.10')

        mock_create.return_value = (True, state)
        mock_destroy.return_value = (True, state)

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, _ = executor.test({})

        assert success is True
        assert mock_create.call_count == 1
        assert mock_destroy.call_count == 1

    @patch('manifest_opr.executor.NodeExecutor.destroy')
    @patch('manifest_opr.executor.NodeExecutor.create')
    def test_test_cleanup_on_create_failure(self, mock_create, mock_destroy):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        from manifest_opr.state import ExecutionState
        state = ExecutionState('test', 'test-host')
        state.add_node('test').fail('provision error')

        mock_create.return_value = (False, state)
        mock_destroy.return_value = (True, state)

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        success, _ = executor.test({})

        assert success is False
        assert mock_destroy.call_count == 1  # Cleanup called


class TestServerPortResolution:
    """Tests for server port resolution from config.spec_server URL."""

    def test_port_from_spec_server_url(self):
        """Port extracted from spec_server URL."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        config.spec_server = 'https://controller:55555'

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        assert executor._server.port == 55555

    def test_default_port_when_no_spec_server(self):
        """Default port used when spec_server is empty."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        config.spec_server = ''

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        assert executor._server.port == 44443

    def test_default_port_when_url_has_no_port(self):
        """Default port used when spec_server URL omits port."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        config.spec_server = 'https://controller'

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        assert executor._server.port == 44443



class TestServerSourceEnv:
    """Tests for HOMESTAK_SOURCE env var lifecycle in _ensure_server/_stop_server.

    These tests do NOT use the autouse _skip_server fixture — they need
    the real _ensure_server/_stop_server methods with SSH calls mocked.
    """

    @pytest.fixture(autouse=True)
    def _clean_env(self):
        """Ensure env vars are clean before and after each test."""
        for var in ('HOMESTAK_SOURCE', 'HOMESTAK_REF', 'HOMESTAK_SELF_ADDR'):
            os.environ.pop(var, None)
        yield
        for var in ('HOMESTAK_SOURCE', 'HOMESTAK_REF', 'HOMESTAK_SELF_ADDR'):
            os.environ.pop(var, None)

    @pytest.fixture(autouse=True)
    def _override_skip_server(self, monkeypatch):
        """Re-enable real ensure/stop for this class.

        The module-level _skip_server fixture replaces them with no-ops.
        We restore from _original_* saved at import time (before patching).
        """
        monkeypatch.setattr(
            'manifest_opr.server_mgmt.ServerManager.ensure',
            _original_ensure,
        )
        monkeypatch.setattr(
            'manifest_opr.server_mgmt.ServerManager.stop',
            _original_stop,
        )

    def _make_executor(self):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        return NodeExecutor(manifest=manifest, graph=graph, config=config)

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_ensure_server_sets_env_on_fresh_start(self, mock_ssh):
        """When server is not running, start it and set HOMESTAK_SOURCE."""
        # First call: status check → not running
        # Second call: server start → success
        mock_ssh.side_effect = [
            (1, '', 'not running'),  # status check fails
            (0, '', ''),             # server start succeeds
        ]

        executor = self._make_executor()
        executor._server.ensure()

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'
        assert os.environ.get('HOMESTAK_REF') == '_working'

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_ensure_server_sets_env_on_reuse(self, mock_ssh):
        """When server is already running, reuse it and still set HOMESTAK_SOURCE."""
        mock_ssh.return_value = (0, '{"running": true, "healthy": true}', '')

        executor = self._make_executor()
        executor._server.ensure()

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'
        assert executor._server._started is False  # Didn't start it ourselves

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_stop_server_clears_env(self, mock_ssh):
        """_stop_server clears HOMESTAK_SOURCE when ref count drops to zero."""
        # Start: not running → start
        mock_ssh.side_effect = [
            (1, '', 'not running'),  # status check
            (0, '', ''),             # start
            (0, '', ''),             # stop
        ]

        executor = self._make_executor()
        executor._server.ensure()
        assert 'HOMESTAK_SOURCE' in os.environ

        executor._server.stop()
        assert 'HOMESTAK_SOURCE' not in os.environ
        assert 'HOMESTAK_REF' not in os.environ

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_ref_counting_preserves_env(self, mock_ssh):
        """Nested _ensure_server calls preserve env until outermost _stop_server."""
        mock_ssh.side_effect = [
            (1, '', ''),   # status check (first ensure)
            (0, '', ''),   # start
        ]

        executor = self._make_executor()
        executor._server.ensure()  # refs=1, starts server
        executor._server.ensure()  # refs=2, no-op

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'

        # Inner stop: decrements but doesn't clear
        executor._server.stop()  # refs=1
        assert 'HOMESTAK_SOURCE' in os.environ

        # Outer stop: clears env and stops server
        mock_ssh.side_effect = [(0, '', '')]  # stop call
        executor._server.stop()  # refs=0
        assert 'HOMESTAK_SOURCE' not in os.environ

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_stop_clears_env_even_for_reused_server(self, mock_ssh):
        """Env vars are cleared on stop even when we didn't start the server."""
        mock_ssh.return_value = (0, '{"running": true, "healthy": true}', '')

        executor = self._make_executor()
        executor._server.ensure()
        assert executor._server._started is False  # Reused

        executor._server.stop()
        assert 'HOMESTAK_SOURCE' not in os.environ

    @patch('manifest_opr.server_mgmt.run_ssh')
    def test_existing_homestak_ref_preserved(self, mock_ssh):
        """If HOMESTAK_REF is already set, _set_source_env doesn't overwrite it."""
        os.environ['HOMESTAK_REF'] = 'custom-branch'

        mock_ssh.side_effect = [
            (1, '', ''),   # status check
            (0, '', ''),   # start
        ]

        executor = self._make_executor()
        executor._server.ensure()

        assert os.environ.get('HOMESTAK_REF') == 'custom-branch'  # Preserved
        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'

    def test_set_source_env_uses_self_addr_for_localhost(self):
        """When host is localhost and self_addr is set, use self_addr (#200)."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        config.ssh_host = 'localhost'

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config,
            self_addr='198.51.100.153',
        )
        executor._server._set_source_env('localhost')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.153:44443'

    def test_set_source_env_uses_self_addr_for_127(self):
        """When host is 127.0.0.1 and self_addr is set, use self_addr (#200)."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config,
            self_addr='198.51.100.153',
        )
        executor._server._set_source_env('127.0.0.1')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.153:44443'

    def test_set_source_env_ignores_self_addr_for_routable_host(self):
        """When host is already routable, self_addr is not used."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config,
            self_addr='198.51.100.153',
        )
        executor._server._set_source_env('198.51.100.61')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'

    def test_set_source_env_localhost_falls_back_to_detect(self):
        """When host is localhost and no self_addr, detect external IP (#200)."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        with patch.object(ServerManager, 'detect_external_ip', return_value='198.51.100.61'):
            executor._server._set_source_env('localhost')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.61:44443'

    def test_set_source_env_localhost_detect_fails_uses_localhost(self):
        """When detection fails and no self_addr, fall back to localhost."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        with patch.object(ServerManager, 'detect_external_ip', return_value=None):
            executor._server._set_source_env('localhost')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://localhost:44443'

    def test_detect_external_ip_returns_address(self):
        """_detect_external_ip returns a non-loopback address."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()
        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)

        result = executor._server.detect_external_ip()
        # On a machine with network, this should return a real IP
        # On CI without network, it may return None
        if result is not None:
            assert result != '0.0.0.0'
            assert result != '127.0.0.1'

    def test_set_source_env_uses_env_var_for_localhost(self):
        """HOMESTAK_SELF_ADDR env var overrides localhost when no --self-addr (#200)."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        os.environ['HOMESTAK_SELF_ADDR'] = '198.51.100.99'
        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        executor._server._set_source_env('localhost')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.99:44443'

    def test_self_addr_takes_precedence_over_env_var(self):
        """--self-addr CLI arg takes priority over HOMESTAK_SELF_ADDR env var."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        os.environ['HOMESTAK_SELF_ADDR'] = '198.51.100.99'
        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config,
            self_addr='198.51.100.153',
        )
        executor._server._set_source_env('localhost')

        assert os.environ.get('HOMESTAK_SOURCE') == 'https://198.51.100.153:44443'

    def test_validate_addr_rejects_loopback(self):
        """_validate_addr raises ValueError for loopback addresses."""
        with pytest.raises(ValueError, match='loopback'):
            ServerManager.validate_addr('localhost', '--self-addr')
        with pytest.raises(ValueError, match='loopback'):
            ServerManager.validate_addr('127.0.0.1', 'HOMESTAK_SELF_ADDR')

    def test_validate_addr_rejects_empty(self):
        """_validate_addr raises ValueError for empty addresses."""
        with pytest.raises(ValueError, match='empty'):
            ServerManager.validate_addr('', '--self-addr')
        with pytest.raises(ValueError, match='empty'):
            ServerManager.validate_addr('   ', '--self-addr')

    def test_validate_addr_accepts_routable(self):
        """_validate_addr returns stripped address for valid inputs."""
        assert ServerManager.validate_addr('198.51.100.61', 'test') == '198.51.100.61'
        assert ServerManager.validate_addr('  198.51.100.61  ', 'test') == '198.51.100.61'

    def test_set_source_env_rejects_loopback_self_addr(self):
        """Passing localhost as --self-addr raises ValueError, not silent fallback."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(
            manifest=manifest, graph=graph, config=config,
            self_addr='localhost',
        )
        with pytest.raises(ValueError, match='loopback'):
            executor._server._set_source_env('127.0.0.1')


class TestServerManagerIsLocal:
    """Tests for ServerManager._is_local detection (#299)."""

    def test_loopback_is_local(self):
        """Loopback addresses are detected as local."""
        for addr in ('localhost', '127.0.0.1', '::1'):
            mgr = ServerManager(addr, 'root')
            assert mgr._is_local is True, f"{addr} should be local"

    def test_hostname_is_local(self):
        """Machine's own hostname is detected as local."""
        hostname = socket.gethostname()
        mgr = ServerManager(hostname, 'root')
        assert mgr._is_local is True

    @patch.object(ServerManager, 'detect_external_ip', return_value='198.51.100.61')
    def test_own_ip_is_local(self, _mock_detect):
        """Machine's own IP is detected as local (#299)."""
        mgr = ServerManager('198.51.100.61', 'root')
        assert mgr._is_local is True

    @patch.object(ServerManager, 'detect_external_ip', return_value='198.51.100.61')
    def test_remote_ip_is_not_local(self, _mock_detect):
        """A different host's IP is not local."""
        mgr = ServerManager('198.51.100.99', 'root')
        assert mgr._is_local is False

    @patch.object(ServerManager, 'detect_external_ip', return_value=None)
    def test_detect_fails_falls_back_to_not_local(self, _mock_detect):
        """When IP detection fails, non-loopback/hostname IPs are not local."""
        mgr = ServerManager('198.51.100.61', 'root')
        assert mgr._is_local is False


class TestPushConfig:
    """Tests for push-mode config phase (_push_config)."""

    @patch('manifest_opr.executor.run_ssh')
    @patch('manifest_opr.executor.run_command')
    def test_push_config_success(self, mock_run_command, mock_ssh):
        """Push config resolves spec, runs ansible from controller, writes marker."""
        manifest = _make_manifest([
            {'name': 'edge', 'type': 'vm', 'spec': 'base', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        exec_node = graph.get_node('edge')

        # Mock ansible-playbook success
        mock_run_command.return_value = (0, 'ok', '')

        # Mock SSH marker write success
        mock_ssh.return_value = (0, '', '')

        with patch('resolver.spec_resolver.SpecResolver') as MockResolver, \
             patch('config_apply.spec_to_ansible_vars') as mock_s2a, \
             patch('manifest_opr.executor.get_sibling_dir') as mock_dir, \
             patch('actions.ssh.WaitForFileAction') as MockWait:
            mock_resolver = MagicMock()
            mock_resolver.resolve.return_value = {
                'schema_version': 1,
                'identity': {'hostname': 'edge'},
                'access': {'posture': 'dev'},
            }
            MockResolver.return_value = mock_resolver
            mock_s2a.return_value = {'packages': ['htop'], 'timezone': 'UTC'}
            mock_ansible_dir = MagicMock()
            mock_ansible_dir.exists.return_value = True
            mock_dir.return_value = mock_ansible_dir

            mock_wait_instance = MagicMock()
            mock_wait_instance.run.return_value = ActionResult(
                success=True, message='found', duration=0.1
            )
            MockWait.return_value = mock_wait_instance

            result = executor._push_config(exec_node, '198.51.100.10', {})

        assert result.success is True
        assert 'Push config complete' in result.message
        mock_resolver.resolve.assert_called_once_with('base')
        mock_run_command.assert_called_once()

    @patch('manifest_opr.executor.run_ssh')
    @patch('manifest_opr.executor.run_command')
    def test_push_config_spec_resolve_failure(self, mock_run_command, mock_ssh):
        """Push config fails gracefully when spec resolution fails."""
        manifest = _make_manifest([
            {'name': 'edge', 'type': 'vm', 'spec': 'nonexistent', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        exec_node = graph.get_node('edge')

        with patch('resolver.spec_resolver.SpecResolver') as MockResolver:
            mock_resolver = MagicMock()
            mock_resolver.resolve.side_effect = Exception("Spec not found")
            MockResolver.return_value = mock_resolver

            result = executor._push_config(exec_node, '198.51.100.10', {})

        assert result.success is False
        assert "resolve spec" in result.message.lower()

    @patch('manifest_opr.executor.run_ssh')
    @patch('manifest_opr.executor.run_command')
    def test_push_config_apply_failure(self, mock_run_command, mock_ssh):
        """Push config reports failure when ansible-playbook fails."""
        manifest = _make_manifest([
            {'name': 'edge', 'type': 'vm', 'spec': 'base', 'preset': 'vm-small'},
        ])
        graph = ManifestGraph(manifest)
        config = _make_config()

        executor = NodeExecutor(manifest=manifest, graph=graph, config=config)
        exec_node = graph.get_node('edge')

        # Mock SSH (apt-get update before ansible)
        mock_ssh.return_value = (0, '', '')

        # Mock ansible-playbook failure
        mock_run_command.return_value = (1, '', 'ansible playbook failed')

        with patch('resolver.spec_resolver.SpecResolver') as MockResolver, \
             patch('config_apply.spec_to_ansible_vars') as mock_s2a, \
             patch('manifest_opr.executor.get_sibling_dir') as mock_dir:
            mock_resolver = MagicMock()
            mock_resolver.resolve.return_value = {'schema_version': 1}
            MockResolver.return_value = mock_resolver
            mock_s2a.return_value = {}
            mock_ansible_dir = MagicMock()
            mock_ansible_dir.exists.return_value = True
            mock_dir.return_value = mock_ansible_dir

            result = executor._push_config(exec_node, '198.51.100.10', {})

        assert result.success is False
        assert "Config apply failed" in result.message
