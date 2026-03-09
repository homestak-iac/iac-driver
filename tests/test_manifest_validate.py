#!/usr/bin/env python3
"""Tests for manifest validate verb — FK validation against config.

Tests verify:
1. Valid manifests pass validation
2. Missing spec FK detected
3. Missing preset FK detected
4. Multiple errors reported
5. Nodes without FKs pass (optional fields)
6. CLI exit codes
"""

from unittest.mock import patch
import tempfile

import pytest
from manifest import Manifest, ManifestNode
from manifest_opr.cli import validate_manifest_fks


def _make_site_config(tmp_path, specs=None, presets=None):
    """Create a mock config directory with specs and presets."""
    specs_dir = tmp_path / 'specs'
    specs_dir.mkdir()
    presets_dir = tmp_path / 'presets'
    presets_dir.mkdir()

    for name in (specs or []):
        (specs_dir / f'{name}.yaml').write_text(f'schema_version: 1\n')

    for name in (presets or []):
        (presets_dir / f'{name}.yaml').write_text(f'cores: 2\n')

    return tmp_path


def _make_manifest(nodes):
    """Create a Manifest with given ManifestNode list."""
    return Manifest(
        schema_version=2,
        name='test-manifest',
        nodes=nodes,
    )


class TestValidateManifestFks:
    """Test validate_manifest_fks() function."""

    def test_valid_all_fks_resolve(self, tmp_path):
        """All spec and preset FKs resolve to existing files."""
        site = _make_site_config(tmp_path, specs=['base', 'pve'], presets=['vm-small', 'vm-large'])
        manifest = _make_manifest([
            ManifestNode(name='root-pve', type='pve', spec='pve', preset='vm-large'),
            ManifestNode(name='edge', type='vm', spec='base', preset='vm-small', parent='root-pve'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert errors == []

    def test_missing_spec(self, tmp_path):
        """Missing spec FK produces clear error."""
        site = _make_site_config(tmp_path, specs=['base'], presets=['vm-small'])
        manifest = _make_manifest([
            ManifestNode(name='edge', type='vm', spec='nonexistent', preset='vm-small'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert len(errors) == 1
        assert "Node 'edge'" in errors[0]
        assert "unknown spec 'nonexistent'" in errors[0]
        assert "specs/nonexistent.yaml" in errors[0]

    def test_missing_preset(self, tmp_path):
        """Missing preset FK produces clear error."""
        site = _make_site_config(tmp_path, specs=['base'], presets=['vm-small'])
        manifest = _make_manifest([
            ManifestNode(name='edge', type='vm', spec='base', preset='vm-xlarge'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert len(errors) == 1
        assert "Node 'edge'" in errors[0]
        assert "unknown preset 'vm-xlarge'" in errors[0]
        assert "presets/vm-xlarge.yaml" in errors[0]

    def test_multiple_errors(self, tmp_path):
        """Multiple FK errors reported for different nodes."""
        site = _make_site_config(tmp_path, specs=[], presets=[])
        manifest = _make_manifest([
            ManifestNode(name='pve', type='pve', spec='missing-spec', preset='missing-preset'),
            ManifestNode(name='vm', type='vm', spec='also-missing', parent='pve'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert len(errors) == 3  # 2 for pve (spec + preset), 1 for vm (spec)

    def test_no_fks_is_valid(self, tmp_path):
        """Nodes without spec/preset FKs pass validation."""
        site = _make_site_config(tmp_path)
        manifest = _make_manifest([
            ManifestNode(name='root-pve', type='pve', image='pve-9'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert errors == []

    def test_spec_only_no_preset(self, tmp_path):
        """Node with spec but no preset validates spec only."""
        site = _make_site_config(tmp_path, specs=['base'])
        manifest = _make_manifest([
            ManifestNode(name='edge', type='vm', spec='base'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert errors == []

    def test_preset_only_no_spec(self, tmp_path):
        """Node with preset but no spec validates preset only."""
        site = _make_site_config(tmp_path, presets=['vm-small'])
        manifest = _make_manifest([
            ManifestNode(name='edge', type='vm', preset='vm-small'),
        ])

        errors = validate_manifest_fks(manifest, site)

        assert errors == []


class TestValidateMainCli:
    """Test validate_main() CLI integration."""

    def test_valid_manifest_exits_zero(self, tmp_path, capsys):
        """Valid manifest returns exit code 0."""
        from manifest_opr.cli import validate_main

        site = _make_site_config(tmp_path, specs=['base'], presets=['vm-small'])

        # Write a manifest file
        manifest_file = tmp_path / 'test.yaml'
        manifest_file.write_text(
            'schema_version: 2\n'
            'name: test\n'
            'nodes:\n'
            '  - name: edge\n'
            '    type: vm\n'
            '    spec: base\n'
            '    preset: vm-small\n'
        )

        with patch('manifest.get_site_config_dir', return_value=site), \
             patch('config.get_site_config_dir', return_value=site):
            rc = validate_main(['--manifest-file', str(manifest_file)])

        assert rc == 0
        captured = capsys.readouterr()
        assert "valid" in captured.out
        assert "1 node" in captured.out

    def test_invalid_fk_exits_one(self, tmp_path, capsys):
        """Invalid FK returns exit code 1."""
        from manifest_opr.cli import validate_main

        site = _make_site_config(tmp_path, specs=[], presets=[])

        manifest_file = tmp_path / 'test.yaml'
        manifest_file.write_text(
            'schema_version: 2\n'
            'name: test\n'
            'nodes:\n'
            '  - name: edge\n'
            '    type: vm\n'
            '    spec: nonexistent\n'
        )

        with patch('manifest.get_site_config_dir', return_value=site), \
             patch('config.get_site_config_dir', return_value=site):
            rc = validate_main(['--manifest-file', str(manifest_file)])

        assert rc == 1
        captured = capsys.readouterr()
        assert "validation error" in captured.err

    def test_bad_manifest_exits_one(self, tmp_path, capsys):
        """Malformed manifest returns exit code 1."""
        from manifest_opr.cli import validate_main

        manifest_file = tmp_path / 'bad.yaml'
        manifest_file.write_text('schema_version: 2\nname: bad\n')

        rc = validate_main(['--manifest-file', str(manifest_file)])

        assert rc == 1

    def test_no_manifest_specified_exits_one(self, capsys):
        """No manifest source specified returns exit code 1."""
        from manifest_opr.cli import validate_main

        rc = validate_main([])

        assert rc == 1
        captured = capsys.readouterr()
        assert "specify a manifest" in captured.err
