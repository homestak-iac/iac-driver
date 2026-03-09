#!/usr/bin/env python3
"""Tests for manifest.py - manifest loading and validation.

Tests verify:
1. Manifest dataclass creation
2. Schema validation
3. Node parsing
4. YAML file loading
5. JSON serialization/deserialization
6. Depth limiting
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile

import pytest
from config import ConfigError


class TestManifestSettings:
    """Test ManifestSettings dataclass."""

    def test_defaults(self):
        """Should have sensible defaults."""
        from manifest import ManifestSettings

        settings = ManifestSettings()

        assert settings.verify_ssh is True
        assert settings.cleanup_on_failure is True
        assert settings.timeout_buffer == 60

    def test_from_dict_none(self):
        """Should return defaults for None input."""
        from manifest import ManifestSettings

        settings = ManifestSettings.from_dict(None)

        assert settings.verify_ssh is True
        assert settings.cleanup_on_failure is True

    def test_from_dict_partial(self):
        """Should apply partial overrides."""
        from manifest import ManifestSettings

        settings = ManifestSettings.from_dict({'cleanup_on_failure': False})

        assert settings.verify_ssh is True  # default
        assert settings.cleanup_on_failure is False  # overridden


class TestManifestV1Rejected:
    """Test that v1 manifests are rejected."""

    def test_v1_explicit_version_rejected(self):
        """v1 manifest with explicit schema_version should be rejected."""
        from manifest import Manifest

        data = {
            'schema_version': 1,
            'name': 'test',
            'levels': [{'name': 'level1', 'env': 'test'}]
        }

        with pytest.raises(ConfigError, match='Unsupported manifest schema version'):
            Manifest.from_dict(data)

    def test_missing_schema_version_rejected(self):
        """Manifest without schema_version should be rejected."""
        from manifest import Manifest

        data = {
            'name': 'test',
            'levels': [{'name': 'level1', 'env': 'test'}]
        }

        with pytest.raises(ConfigError, match='missing required field: schema_version'):
            Manifest.from_dict(data)


class TestManifest:
    """Test Manifest dataclass."""

    def test_missing_name_raises_error(self):
        """Should raise error when name missing."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'nodes': [{'name': 'test', 'type': 'vm'}]
        }

        with pytest.raises(ConfigError, match='missing required field: name'):
            Manifest.from_dict(data)

    def test_unsupported_schema_version(self):
        """Should raise error for unsupported schema version."""
        from manifest import Manifest

        data = {
            'schema_version': 99,
            'name': 'test',
            'nodes': [{'name': 'test', 'type': 'vm'}]
        }

        with pytest.raises(ConfigError, match='Unsupported manifest schema version'):
            Manifest.from_dict(data)


class TestManifestNode:
    """Test ManifestNode dataclass."""

    def test_from_dict_minimal(self):
        """Should create node with minimal required fields."""
        from manifest import ManifestNode

        data = {'name': 'test', 'type': 'vm'}
        node = ManifestNode.from_dict(data)

        assert node.name == 'test'
        assert node.type == 'vm'
        assert node.spec is None
        assert node.preset is None
        assert node.image is None
        assert node.vmid is None
        assert node.disk is None
        assert node.parent is None
        assert node.execution_mode is None

    def test_from_dict_full(self):
        """Should create node with all fields."""
        from manifest import ManifestNode

        data = {
            'name': 'child-pve',
            'type': 'pve',
            'spec': 'pve',
            'preset': 'vm-large',
            'image': 'pve-9',
            'vmid': 99011,
            'disk': 64,
            'parent': None,
            'execution': {'mode': 'push'},
        }
        node = ManifestNode.from_dict(data)

        assert node.name == 'child-pve'
        assert node.type == 'pve'
        assert node.spec == 'pve'
        assert node.preset == 'vm-large'
        assert node.image == 'pve-9'
        assert node.vmid == 99011
        assert node.disk == 64
        assert node.parent is None
        assert node.execution_mode == 'push'

    def test_from_dict_with_parent(self):
        """Should create child node with parent reference."""
        from manifest import ManifestNode

        data = {
            'name': 'test',
            'type': 'vm',
            'preset': 'vm-small',
            'image': 'debian-12',
            'vmid': 99021,
            'parent': 'child-pve',
        }
        node = ManifestNode.from_dict(data)

        assert node.parent == 'child-pve'

    def test_to_dict_roundtrip(self):
        """Should survive dict roundtrip."""
        from manifest import ManifestNode

        original = {
            'name': 'test',
            'type': 'vm',
            'preset': 'vm-small',
            'image': 'debian-12',
            'vmid': 99021,
            'parent': 'child-pve',
        }
        node = ManifestNode.from_dict(original)
        result = node.to_dict()

        assert result['name'] == 'test'
        assert result['type'] == 'vm'
        assert result['parent'] == 'child-pve'
        assert result['vmid'] == 99021

    def test_to_dict_omits_none_fields(self):
        """Should omit None optional fields in serialization."""
        from manifest import ManifestNode

        node = ManifestNode(name='test', type='vm')
        result = node.to_dict()

        assert 'spec' not in result
        assert 'parent' not in result
        assert 'disk' not in result
        assert 'execution' not in result

    def test_to_dict_includes_execution_mode(self):
        """Should include execution mode when set."""
        from manifest import ManifestNode

        node = ManifestNode(name='test', type='vm', execution_mode='pull')
        result = node.to_dict()

        assert result['execution'] == {'mode': 'pull'}


class TestManifestV2:
    """Test v2 manifest parsing (graph-based nodes)."""

    def test_from_dict_flat(self):
        """Should parse flat (single-node) v2 manifest."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'n1-push',
            'pattern': 'flat',
            'nodes': [
                {'name': 'edge', 'type': 'vm', 'preset': 'vm-small', 'image': 'debian-12', 'vmid': 99001}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert manifest.schema_version == 2
        assert manifest.name == 'n1-push'
        assert manifest.pattern == 'flat'
        assert manifest.nodes is not None
        assert len(manifest.nodes) == 1
        assert manifest.nodes[0].name == 'edge'

    def test_from_dict_tiered(self):
        """Should parse tiered (parent-child) v2 manifest."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'n2-tiered',
            'pattern': 'tiered',
            'nodes': [
                {'name': 'root-pve', 'type': 'pve', 'preset': 'vm-large', 'image': 'pve-9', 'vmid': 99011},
                {'name': 'edge', 'type': 'vm', 'preset': 'vm-medium', 'image': 'debian-12', 'vmid': 99021, 'parent': 'root-pve'}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert manifest.schema_version == 2
        assert manifest.pattern == 'tiered'
        assert len(manifest.nodes) == 2

    def test_from_dict_three_level(self):
        """Should parse 3-level tiered manifest."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'n3-deep',
            'pattern': 'tiered',
            'nodes': [
                {'name': 'root-pve', 'type': 'pve', 'preset': 'vm-large', 'image': 'pve-9', 'vmid': 99011},
                {'name': 'leaf-pve', 'type': 'pve', 'preset': 'vm-medium', 'image': 'pve-9', 'vmid': 99021, 'parent': 'root-pve'},
                {'name': 'edge', 'type': 'vm', 'preset': 'vm-small', 'image': 'debian-12', 'vmid': 99031, 'parent': 'leaf-pve'}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert len(manifest.nodes) == 3
        assert manifest.depth == 3

    def test_default_pattern_is_flat(self):
        """Should default to flat pattern when not specified."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'test', 'type': 'vm', 'vmid': 99001}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert manifest.pattern == 'flat'

    def test_default_execution_mode_is_push(self):
        """Should default to push execution mode."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'test', 'type': 'vm', 'vmid': 99001}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert manifest.execution_mode == 'push'

    def test_missing_nodes_raises_error(self):
        """Should raise error when nodes missing."""
        from manifest import Manifest

        data = {'schema_version': 2, 'name': 'test'}

        with pytest.raises(ConfigError, match='missing required field: nodes'):
            Manifest.from_dict(data)

    def test_empty_nodes_raises_error(self):
        """Should raise error when nodes empty."""
        from manifest import Manifest

        data = {'schema_version': 2, 'name': 'test', 'nodes': []}

        with pytest.raises(ConfigError, match='must have at least one node'):
            Manifest.from_dict(data)

    def test_node_missing_name_raises_error(self):
        """Should raise error when node missing name."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [{'type': 'vm'}]
        }

        with pytest.raises(ConfigError, match='Node 0 missing required field: name'):
            Manifest.from_dict(data)

    def test_node_missing_type_raises_error(self):
        """Should raise error when node missing type."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [{'name': 'test'}]
        }

        with pytest.raises(ConfigError, match='missing required field: type'):
            Manifest.from_dict(data)

    def test_settings_with_on_error(self):
        """Should parse on_error setting."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [{'name': 'test', 'type': 'vm'}],
            'settings': {'on_error': 'rollback'}
        }
        manifest = Manifest.from_dict(data)

        assert manifest.settings.on_error == 'rollback'


class TestManifestV2GraphValidation:
    """Test graph validation for v2 manifests."""

    def test_duplicate_names_raises_error(self):
        """Should raise error for duplicate node names."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'test', 'type': 'vm'},
                {'name': 'test', 'type': 'vm'}
            ]
        }

        with pytest.raises(ConfigError, match="Duplicate node name: 'test'"):
            Manifest.from_dict(data)

    def test_dangling_parent_raises_error(self):
        """Should raise error for dangling parent reference."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'test', 'type': 'vm', 'parent': 'nonexistent'}
            ]
        }

        with pytest.raises(ConfigError, match="references unknown parent 'nonexistent'"):
            Manifest.from_dict(data)

    def test_cycle_raises_error(self):
        """Should raise error for cycles in parent graph."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'a', 'type': 'vm', 'parent': 'b'},
                {'name': 'b', 'type': 'vm', 'parent': 'a'}
            ]
        }

        with pytest.raises(ConfigError, match='Cycle detected'):
            Manifest.from_dict(data)

    def test_self_reference_raises_error(self):
        """Should raise error for self-referencing parent."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'a', 'type': 'vm', 'parent': 'a'}
            ]
        }

        with pytest.raises(ConfigError, match='Cycle detected'):
            Manifest.from_dict(data)

    def test_valid_tree_passes(self):
        """Should accept valid tree structure."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'nodes': [
                {'name': 'root', 'type': 'pve'},
                {'name': 'child1', 'type': 'vm', 'parent': 'root'},
                {'name': 'child2', 'type': 'vm', 'parent': 'root'}
            ]
        }
        manifest = Manifest.from_dict(data)

        assert len(manifest.nodes) == 3


class TestManifestV2Serialization:
    """Test v2 manifest serialization."""

    def test_to_dict_v2(self):
        """Should serialize v2 manifest correctly."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'test',
            'pattern': 'tiered',
            'nodes': [
                {'name': 'pve', 'type': 'pve', 'vmid': 99011},
                {'name': 'test', 'type': 'vm', 'vmid': 99021, 'parent': 'pve'}
            ]
        }
        manifest = Manifest.from_dict(data)
        result = manifest.to_dict()

        assert result['schema_version'] == 2
        assert result['pattern'] == 'tiered'
        assert len(result['nodes']) == 2
        assert result['nodes'][0]['name'] == 'pve'
        assert result['nodes'][1]['parent'] == 'pve'

    def test_json_roundtrip_v2(self):
        """Should survive JSON roundtrip for v2 manifests."""
        from manifest import Manifest

        data = {
            'schema_version': 2,
            'name': 'n2-tiered',
            'description': 'Test',
            'pattern': 'tiered',
            'nodes': [
                {'name': 'root-pve', 'type': 'pve', 'preset': 'vm-large', 'image': 'pve-9', 'vmid': 99011},
                {'name': 'edge', 'type': 'vm', 'preset': 'vm-small', 'image': 'debian-12', 'vmid': 99021, 'parent': 'root-pve'}
            ],
            'settings': {'on_error': 'rollback'}
        }
        manifest = Manifest.from_dict(data)

        json_str = manifest.to_json()
        restored = Manifest.from_json(json_str)

        assert restored.schema_version == 2
        assert restored.name == manifest.name
        assert restored.pattern == 'tiered'
        assert len(restored.nodes) == 2
        assert restored.settings.on_error == 'rollback'


class TestManifestLoader:
    """Test ManifestLoader class."""

    def test_list_manifests(self):
        """Should list available manifests."""
        from manifest import ManifestLoader

        with tempfile.TemporaryDirectory() as tmpdir:
            manifests_dir = Path(tmpdir) / 'manifests'
            manifests_dir.mkdir()

            (manifests_dir / 'n2-tiered.yaml').write_text(
                'schema_version: 2\nname: n2-tiered\nnodes:\n  - name: test\n    type: vm\n')
            (manifests_dir / 'n3-deep.yaml').write_text(
                'schema_version: 2\nname: n3-deep\nnodes:\n  - name: test\n    type: vm\n')

            loader = ManifestLoader(site_config_path=tmpdir)
            manifests = loader.list_manifests()

            assert 'n2-tiered' in manifests
            assert 'n3-deep' in manifests

    def test_load_manifest(self):
        """Should load manifest by name."""
        from manifest import ManifestLoader

        with tempfile.TemporaryDirectory() as tmpdir:
            manifests_dir = Path(tmpdir) / 'manifests'
            manifests_dir.mkdir()

            yaml_content = """
schema_version: 2
name: test-manifest
description: Test description
nodes:
  - name: inner
    type: pve
    image: pve-9
"""
            (manifests_dir / 'test-manifest.yaml').write_text(yaml_content)

            loader = ManifestLoader(site_config_path=tmpdir)
            manifest = loader.load('test-manifest')

            assert manifest.name == 'test-manifest'
            assert manifest.description == 'Test description'
            assert manifest.nodes[0].image == 'pve-9'

    def test_load_nonexistent_raises_error(self):
        """Should raise error for nonexistent manifest."""
        from manifest import ManifestLoader

        with tempfile.TemporaryDirectory() as tmpdir:
            manifests_dir = Path(tmpdir) / 'manifests'
            manifests_dir.mkdir()

            loader = ManifestLoader(site_config_path=tmpdir)

            with pytest.raises(ConfigError, match='not found'):
                loader.load('nonexistent')


class TestLoadManifestFunction:
    """Test load_manifest convenience function."""

    def test_load_from_json(self):
        """Should load from JSON string."""
        from manifest import load_manifest

        json_str = '{"schema_version": 2, "name": "inline", "nodes": [{"name": "l1", "type": "vm"}]}'

        manifest = load_manifest(json_str=json_str)

        assert manifest.name == 'inline'

    def test_depth_limit(self):
        """Should apply depth limit."""
        from manifest import load_manifest

        json_str = json.dumps({
            'schema_version': 2,
            'name': 'deep',
            'nodes': [
                {'name': 'n1', 'type': 'vm'},
                {'name': 'n2', 'type': 'vm'},
                {'name': 'n3', 'type': 'vm'},
            ]
        })

        manifest = load_manifest(json_str=json_str, depth=2)

        assert manifest.depth == 2
        assert manifest.nodes[0].name == 'n1'
        assert manifest.nodes[1].name == 'n2'

    def test_depth_limit_larger_than_manifest(self):
        """Depth limit larger than manifest should not change it."""
        from manifest import load_manifest

        json_str = '{"schema_version": 2, "name": "short", "nodes": [{"name": "n1", "type": "vm"}]}'

        manifest = load_manifest(json_str=json_str, depth=5)

        assert manifest.depth == 1  # Not modified
