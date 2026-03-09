"""Tests for manifest_opr.graph module."""

import pytest

from manifest import Manifest, ManifestNode
from manifest_opr.graph import ExecutionNode, ManifestGraph


def _make_manifest(nodes_data, name='test', pattern='flat'):
    """Helper to create a v2 manifest from node dicts."""
    return Manifest.from_dict({
        'schema_version': 2,
        'name': name,
        'pattern': pattern,
        'nodes': nodes_data,
    })


class TestExecutionNode:
    """Tests for ExecutionNode dataclass."""

    def test_properties(self):
        mn = ManifestNode(name='test', type='vm', vmid=99001)
        node = ExecutionNode(manifest_node=mn, depth=0)
        assert node.name == 'test'
        assert node.type == 'vm'
        assert node.is_root is True
        assert node.is_leaf is True

    def test_non_root_node(self):
        parent_mn = ManifestNode(name='pve', type='pve', vmid=99001)
        parent = ExecutionNode(manifest_node=parent_mn, depth=0)

        child_mn = ManifestNode(name='test', type='vm', vmid=99002, parent='pve')
        child = ExecutionNode(manifest_node=child_mn, parent=parent, depth=1)
        parent.children.append(child)

        assert child.is_root is False
        assert child.is_leaf is True
        assert parent.is_leaf is False

    def test_repr(self):
        mn = ManifestNode(name='test', type='vm', vmid=99001)
        node = ExecutionNode(manifest_node=mn, depth=0)
        assert 'test' in repr(node)
        assert 'vm' in repr(node)


class TestManifestGraph:
    """Tests for ManifestGraph class."""

    def test_flat_single_node(self):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)

        assert len(graph.roots) == 1
        assert graph.roots[0].name == 'test'
        assert graph.max_depth == 0

    def test_flat_multiple_roots(self):
        manifest = _make_manifest([
            {'name': 'vm1', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)

        assert len(graph.roots) == 2
        assert graph.max_depth == 0

    def test_tiered_two_level(self):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'preset': 'vm-large', 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'preset': 'vm-small', 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)

        assert len(graph.roots) == 1
        assert graph.roots[0].name == 'pve'
        assert len(graph.roots[0].children) == 1
        assert graph.roots[0].children[0].name == 'test'
        assert graph.max_depth == 1

    def test_three_level_chain(self):
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)

        assert len(graph.roots) == 1
        assert graph.max_depth == 2
        assert graph.get_node('test').depth == 2

    def test_get_node(self):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)

        node = graph.get_node('test')
        assert node.name == 'test'
        assert node.depth == 1

    def test_get_node_not_found(self):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)

        with pytest.raises(KeyError):
            graph.get_node('nonexistent')

    def test_requires_manifest_with_nodes(self):
        manifest = Manifest(
            schema_version=2,
            name='empty-test',
        )
        with pytest.raises(ValueError, match="requires a manifest with nodes"):
            ManifestGraph(manifest)


class TestManifestGraphOrdering:
    """Tests for create_order and destroy_order."""

    def test_create_order_parents_before_children(self):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        order = graph.create_order()

        names = [n.name for n in order]
        assert names == ['pve', 'test']

    def test_destroy_order_children_before_parents(self):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        order = graph.destroy_order()

        names = [n.name for n in order]
        assert names == ['test', 'pve']

    def test_three_level_create_order(self):
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        order = graph.create_order()

        names = [n.name for n in order]
        assert names == ['root', 'leaf', 'test']

    def test_three_level_destroy_order(self):
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        order = graph.destroy_order()

        names = [n.name for n in order]
        assert names == ['test', 'leaf', 'root']

    def test_flat_multiple_roots_order(self):
        manifest = _make_manifest([
            {'name': 'vm1', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12'},
            {'name': 'vm3', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)
        order = graph.create_order()

        names = [n.name for n in order]
        assert names == ['vm1', 'vm2', 'vm3']

    def test_branching_topology(self):
        """Test tree with one parent and two children."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'vm1', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)

        create = [n.name for n in graph.create_order()]
        assert create[0] == 'pve'  # Parent first
        assert set(create[1:]) == {'vm1', 'vm2'}  # Children after

        destroy = [n.name for n in graph.destroy_order()]
        assert destroy[-1] == 'pve'  # Parent last
        assert set(destroy[:-1]) == {'vm1', 'vm2'}  # Children first


class TestManifestGraphExtractSubtree:
    """Tests for extract_subtree method."""

    def test_extract_simple_subtree(self):
        """Extract single child from PVE parent."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        subtree = graph.extract_subtree('pve')

        assert subtree.schema_version == 2
        assert subtree.name == 'pve-subtree'
        assert len(subtree.nodes) == 1
        assert subtree.nodes[0].name == 'test'
        assert subtree.nodes[0].parent is None  # Promoted to root

    def test_extract_multi_child_subtree(self):
        """Extract multiple children from PVE parent."""
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'vm1', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
            {'name': 'vm2', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        subtree = graph.extract_subtree('pve')

        assert len(subtree.nodes) == 2
        names = {n.name for n in subtree.nodes}
        assert names == {'vm1', 'vm2'}
        # Both promoted to root
        for n in subtree.nodes:
            assert n.parent is None

    def test_extract_deep_subtree(self):
        """Extract 3-level chain: direct children become roots, deeper keep parents."""
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        subtree = graph.extract_subtree('root')

        assert len(subtree.nodes) == 2
        node_map = {n.name: n for n in subtree.nodes}

        # leaf is direct child of root -> promoted to root
        assert node_map['leaf'].parent is None

        # test is child of leaf -> keeps parent reference
        assert node_map['test'].parent == 'leaf'

    def test_extract_subtree_no_children_raises(self):
        """Extracting from a leaf node should raise ValueError."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)
        with pytest.raises(ValueError, match="no children"):
            graph.extract_subtree('test')

    def test_extract_subtree_not_found_raises(self):
        """Extracting from nonexistent node should raise KeyError."""
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)
        with pytest.raises(KeyError):
            graph.extract_subtree('nonexistent')

    def test_extract_subtree_preserves_settings(self):
        """Subtree should inherit settings from original manifest."""
        manifest = Manifest.from_dict({
            'schema_version': 2,
            'name': 'original',
            'pattern': 'tiered',
            'nodes': [
                {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
                {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
            ],
            'settings': {
                'verify_ssh': False,
                'cleanup_on_failure': True,
                'on_error': 'rollback',
            },
        })
        graph = ManifestGraph(manifest)
        subtree = graph.extract_subtree('pve')

        assert subtree.settings.verify_ssh is False
        assert subtree.settings.cleanup_on_failure is True
        assert subtree.settings.on_error == 'rollback'

    def test_extract_subtree_builds_valid_graph(self):
        """Extracted subtree should be usable to build a new ManifestGraph."""
        manifest = _make_manifest([
            {'name': 'root', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'leaf', 'type': 'pve', 'vmid': 99002, 'image': 'pve-9', 'parent': 'root'},
            {'name': 'test', 'type': 'vm', 'vmid': 99003, 'image': 'debian-12', 'parent': 'leaf'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        subtree = graph.extract_subtree('root')

        # Should be usable to build a new graph
        sub_graph = ManifestGraph(subtree)
        assert len(sub_graph.roots) == 1
        assert sub_graph.roots[0].name == 'leaf'
        assert len(sub_graph.roots[0].children) == 1
        assert sub_graph.roots[0].children[0].name == 'test'


class TestManifestGraphParentIPKey:
    """Tests for get_parent_ip_key."""

    def test_root_uses_ssh_host(self):
        manifest = _make_manifest([
            {'name': 'test', 'type': 'vm', 'vmid': 99001, 'image': 'debian-12'},
        ])
        graph = ManifestGraph(manifest)
        root = graph.get_node('test')

        assert graph.get_parent_ip_key(root) == 'ssh_host'

    def test_child_uses_parent_ip(self):
        manifest = _make_manifest([
            {'name': 'pve', 'type': 'pve', 'vmid': 99001, 'image': 'pve-9'},
            {'name': 'test', 'type': 'vm', 'vmid': 99002, 'image': 'debian-12', 'parent': 'pve'},
        ], pattern='tiered')
        graph = ManifestGraph(manifest)
        child = graph.get_node('test')

        assert graph.get_parent_ip_key(child) == 'pve_ip'
