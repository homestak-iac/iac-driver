"""Tests for manifest_opr.state module."""

import json
import time

import pytest

from manifest_opr.state import NodeState, ExecutionState


class TestNodeState:
    """Tests for NodeState dataclass."""

    def test_defaults(self):
        state = NodeState(name='test')
        assert state.name == 'test'
        assert state.status == 'pending'
        assert state.vm_id is None
        assert state.ip is None
        assert state.duration is None

    def test_start(self):
        state = NodeState(name='test')
        state.start()
        assert state.status == 'running'
        assert state.started_at is not None

    def test_complete(self):
        state = NodeState(name='test')
        state.start()
        state.complete(vm_id=99001, ip='198.51.100.10')
        assert state.status == 'completed'
        assert state.vm_id == 99001
        assert state.ip == '198.51.100.10'
        assert state.completed_at is not None
        assert state.duration is not None
        assert state.duration >= 0

    def test_fail(self):
        state = NodeState(name='test')
        state.start()
        state.fail('tofu apply failed')
        assert state.status == 'failed'
        assert state.error == 'tofu apply failed'
        assert state.completed_at is not None

    def test_mark_destroyed(self):
        state = NodeState(name='test')
        state.start()
        state.complete(vm_id=99001)
        state.mark_destroyed()
        assert state.status == 'destroyed'

    def test_to_dict(self):
        state = NodeState(name='test', status='completed', vm_id=99001, ip='198.51.100.10')
        d = state.to_dict()
        assert d['name'] == 'test'
        assert d['status'] == 'completed'
        assert d['vm_id'] == 99001
        assert d['ip'] == '198.51.100.10'

    def test_to_dict_minimal(self):
        state = NodeState(name='test')
        d = state.to_dict()
        assert d == {'name': 'test', 'status': 'pending'}

    def test_from_dict_roundtrip(self):
        original = NodeState(name='test', status='completed', vm_id=99001, ip='198.51.100.10')
        original.started_at = 1000.0
        original.completed_at = 1010.0
        d = original.to_dict()
        restored = NodeState.from_dict(d)
        assert restored.name == original.name
        assert restored.status == original.status
        assert restored.vm_id == original.vm_id
        assert restored.ip == original.ip


class TestExecutionState:
    """Tests for ExecutionState class."""

    def test_add_and_get_node(self):
        state = ExecutionState('test-manifest', 'father')
        ns = state.add_node('test')
        assert ns.name == 'test'
        assert state.get_node('test') is ns

    def test_get_node_not_found(self):
        state = ExecutionState('test-manifest', 'father')
        with pytest.raises(KeyError):
            state.get_node('nonexistent')

    def test_start_and_finish(self):
        state = ExecutionState('test-manifest', 'father')
        state.start()
        assert state.started_at is not None
        state.finish()
        assert state.completed_at is not None

    def test_to_context(self):
        state = ExecutionState('test-manifest', 'father')
        ns = state.add_node('pve')
        ns.complete(vm_id=99001, ip='198.51.100.10')

        ns2 = state.add_node('test')
        ns2.complete(vm_id=99002, ip='198.51.100.11')

        ctx = state.to_context()
        assert ctx['pve_vm_id'] == 99001
        assert ctx['pve_ip'] == '198.51.100.10'
        assert ctx['test_vm_id'] == 99002
        assert ctx['test_ip'] == '198.51.100.11'

    def test_to_context_skips_pending(self):
        state = ExecutionState('test-manifest', 'father')
        state.add_node('test')  # pending, no vm_id or ip
        ctx = state.to_context()
        assert ctx == {}

    def test_nodes_property(self):
        state = ExecutionState('test-manifest', 'father')
        state.add_node('pve')
        state.add_node('test')
        nodes = state.nodes
        assert 'pve' in nodes
        assert 'test' in nodes
        assert len(nodes) == 2

    def test_save_and_load(self, tmp_path):
        # Save
        state = ExecutionState('test-manifest', 'father')
        state.start()
        ns = state.add_node('test')
        ns.start()
        ns.complete(vm_id=99001, ip='198.51.100.10')
        state.finish()

        save_path = tmp_path / 'execution.json'
        state.save(path=save_path)

        # Load
        loaded = ExecutionState.load('test-manifest', 'father', path=save_path)
        assert loaded.manifest_name == 'test-manifest'
        assert loaded.host_name == 'father'
        assert loaded.started_at is not None
        assert loaded.completed_at is not None

        loaded_node = loaded.get_node('test')
        assert loaded_node.status == 'completed'
        assert loaded_node.vm_id == 99001
        assert loaded_node.ip == '198.51.100.10'

    def test_save_creates_directory(self, tmp_path):
        state = ExecutionState('test-manifest', 'father')
        state.add_node('test')

        save_path = tmp_path / 'subdir' / 'execution.json'
        state.save(path=save_path)
        assert save_path.exists()

    def test_load_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            ExecutionState.load('nonexistent', 'father', path=tmp_path / 'missing.json')

    def test_save_load_roundtrip(self, tmp_path):
        state = ExecutionState('my-manifest', 'host1')
        state.start()

        ns1 = state.add_node('pve')
        ns1.start()
        ns1.complete(vm_id=99001, ip='198.51.100.50')

        ns2 = state.add_node('test')
        ns2.start()
        ns2.fail('timeout')

        state.finish()

        save_path = tmp_path / 'state.json'
        state.save(path=save_path)

        loaded = ExecutionState.load('my-manifest', 'host1', path=save_path)

        # Verify context matches
        orig_ctx = state.to_context()
        loaded_ctx = loaded.to_context()
        assert orig_ctx == loaded_ctx

        # Verify node states
        assert loaded.get_node('pve').status == 'completed'
        assert loaded.get_node('test').status == 'failed'
        assert loaded.get_node('test').error == 'timeout'
