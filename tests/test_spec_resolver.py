"""Tests for resolver/spec_resolver.py - spec resolution with FK expansion."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

# Add src to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from resolver.spec_resolver import (
    SpecResolver,
    SpecNotFoundError,
    SchemaValidationError,
)
from resolver.base import PostureNotFoundError, SSHKeyNotFoundError


class TestSpecResolverErrors:
    """Tests for error classes."""

    def test_spec_not_found_error(self):
        """SpecNotFoundError has correct code and message."""
        error = SpecNotFoundError("test-identity")
        assert error.code == "E200"
        assert "test-identity" in error.message

    def test_schema_validation_error(self):
        """SchemaValidationError has correct code and message."""
        error = SchemaValidationError("field X required")
        assert error.code == "E400"
        assert "field X required" in error.message


class TestSpecResolver:
    """Tests for SpecResolver class."""

    @pytest.fixture
    def site_config(self, tmp_path):
        """Create a minimal config structure for spec resolution."""
        # Create directories
        (tmp_path / "specs").mkdir(parents=True)
        (tmp_path / "postures").mkdir(parents=True)

        # Create site.yaml
        site_yaml = {
            "defaults": {
                "timezone": "America/Denver",
                "domain": "example.com",
            }
        }
        (tmp_path / "site.yaml").write_text(yaml.dump(site_yaml))

        # Create secrets.yaml
        secrets_yaml = {
            "ssh_keys": {
                "user1": "ssh-rsa AAAA... user1@host",
                "admin": "ssh-ed25519 AAAA... admin@host",
            },
            "auth": {
                "signing_key": "a" * 64,
            },
        }
        (tmp_path / "secrets.yaml").write_text(yaml.dump(secrets_yaml))

        # Create postures
        dev_posture = {
            "auth": {"method": "network"},
            "ssh": {"port": 22},
        }
        (tmp_path / "postures" / "dev.yaml").write_text(yaml.dump(dev_posture))

        stage_posture = {
            "auth": {"method": "site_token"},
            "ssh": {"port": 22},
        }
        (tmp_path / "postures" / "stage.yaml").write_text(yaml.dump(stage_posture))

        prod_posture = {
            "auth": {"method": "node_token"},
            "ssh": {"port": 22},
        }
        (tmp_path / "postures" / "prod.yaml").write_text(yaml.dump(prod_posture))

        # Create specs
        base_spec = {
            "schema_version": 1,
            "access": {
                "posture": "dev",
                "users": [
                    {"name": "root", "ssh_keys": ["user1"]},
                ],
            },
            "platform": {
                "packages": ["htop", "vim"],
            },
        }
        (tmp_path / "specs" / "base.yaml").write_text(yaml.dump(base_spec))

        pve_spec = {
            "schema_version": 1,
            "identity": {
                "hostname": "pve-host",
            },
            "access": {
                "posture": "stage",
                "users": [
                    {"name": "root", "ssh_keys": ["admin"]},
                ],
            },
            "config": {
                "pve": {"remove_subscription_nag": True},
            },
        }
        (tmp_path / "specs" / "pve.yaml").write_text(yaml.dump(pve_spec))

        prod_spec = {
            "schema_version": 1,
            "access": {
                "posture": "prod",
            },
        }
        (tmp_path / "specs" / "prod-vm.yaml").write_text(yaml.dump(prod_spec))

        return tmp_path

    def test_init_with_path(self, site_config):
        """SpecResolver initializes with explicit path."""
        resolver = SpecResolver(etc_path=site_config)
        assert resolver.etc_path == site_config

    def test_init_auto_discover(self, site_config):
        """SpecResolver auto-discovers path from HOMESTAK_ROOT."""
        import shutil
        root = site_config.parent
        config_dir = root / "config"
        config_dir.mkdir(exist_ok=True)
        for item in site_config.iterdir():
            dest = config_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        with patch.dict(os.environ, {"HOMESTAK_ROOT": str(root)}, clear=True):
            resolver = SpecResolver()
            assert resolver.etc_path == config_dir

    def test_list_specs(self, site_config):
        """list_specs returns available spec names."""
        resolver = SpecResolver(etc_path=site_config)
        specs = resolver.list_specs()
        assert "base" in specs
        assert "pve" in specs
        assert "prod-vm" in specs

    def test_list_specs_empty_dir(self, tmp_path):
        """list_specs returns empty list when no specs exist."""
        resolver = SpecResolver(etc_path=tmp_path)
        specs = resolver.list_specs()
        assert specs == []

    def test_load_spec_success(self, site_config):
        """_load_spec loads raw spec by identity."""
        resolver = SpecResolver(etc_path=site_config)
        spec = resolver._load_spec("base")
        assert spec["schema_version"] == 1
        assert spec["access"]["posture"] == "dev"

    def test_load_spec_not_found(self, site_config):
        """_load_spec raises SpecNotFoundError."""
        resolver = SpecResolver(etc_path=site_config)
        with pytest.raises(SpecNotFoundError) as exc_info:
            resolver._load_spec("nonexistent")
        assert exc_info.value.code == "E200"

    def test_apply_site_defaults_domain(self, site_config):
        """_apply_site_defaults applies domain from site.yaml."""
        resolver = SpecResolver(etc_path=site_config)
        spec = {}
        spec = resolver._apply_site_defaults(spec)
        assert spec["identity"]["domain"] == "example.com"

    def test_apply_site_defaults_timezone(self, site_config):
        """_apply_site_defaults applies timezone from site.yaml."""
        resolver = SpecResolver(etc_path=site_config)
        spec = {}
        spec = resolver._apply_site_defaults(spec)
        assert spec["config"]["timezone"] == "America/Denver"

    def test_apply_site_defaults_no_overwrite(self, site_config):
        """_apply_site_defaults does not overwrite existing values."""
        resolver = SpecResolver(etc_path=site_config)
        spec = {
            "identity": {"domain": "custom.local"},
            "config": {"timezone": "UTC"},
        }
        spec = resolver._apply_site_defaults(spec)
        assert spec["identity"]["domain"] == "custom.local"
        assert spec["config"]["timezone"] == "UTC"

    def test_resolve_full(self, site_config):
        """resolve returns fully expanded spec."""
        resolver = SpecResolver(etc_path=site_config)
        spec = resolver.resolve("base")

        # Check identity defaults
        assert spec["identity"]["hostname"] == "base"
        assert spec["identity"]["domain"] == "example.com"

        # Check posture expansion
        assert "_posture" in spec["access"]
        assert spec["access"]["_posture"]["auth"]["method"] == "network"

        # Check SSH key resolution
        users = spec["access"]["users"]
        assert len(users) == 1
        assert users[0]["ssh_keys"][0].startswith("ssh-rsa")

    def test_resolve_with_explicit_hostname(self, site_config):
        """resolve uses explicit hostname when specified."""
        resolver = SpecResolver(etc_path=site_config)
        spec = resolver.resolve("pve")
        assert spec["identity"]["hostname"] == "pve-host"

    def test_resolve_caching(self, site_config):
        """resolve caches resolved specs."""
        resolver = SpecResolver(etc_path=site_config)
        spec1 = resolver.resolve("base")
        spec2 = resolver.resolve("base")
        assert spec1 is spec2

    def test_resolve_posture_not_found(self, site_config):
        """resolve raises PostureNotFoundError for bad posture FK."""
        # Create spec with bad posture reference
        bad_spec = {
            "schema_version": 1,
            "access": {"posture": "nonexistent"},
        }
        (site_config / "specs" / "bad-posture.yaml").write_text(
            yaml.dump(bad_spec)
        )

        resolver = SpecResolver(etc_path=site_config)
        with pytest.raises(PostureNotFoundError) as exc_info:
            resolver.resolve("bad-posture")
        assert exc_info.value.code == "E201"

    def test_resolve_ssh_key_not_found(self, site_config):
        """resolve raises SSHKeyNotFoundError for bad SSH key FK."""
        # Create spec with bad SSH key reference
        bad_spec = {
            "schema_version": 1,
            "access": {
                "posture": "dev",
                "users": [{"name": "root", "ssh_keys": ["nonexistent"]}],
            },
        }
        (site_config / "specs" / "bad-ssh.yaml").write_text(
            yaml.dump(bad_spec)
        )

        resolver = SpecResolver(etc_path=site_config)
        with pytest.raises(SSHKeyNotFoundError) as exc_info:
            resolver.resolve("bad-ssh")
        assert exc_info.value.code == "E202"

    def test_clear_cache(self, site_config):
        """clear_cache clears all cached data including specs."""
        resolver = SpecResolver(etc_path=site_config)

        # Load data to populate cache
        resolver._load_secrets()
        resolver._load_site()
        resolver.resolve("base")

        # Verify spec cache is populated
        assert len(resolver._spec_cache) > 0

        # Clear cache
        resolver.clear_cache()

        # Verify all caches are cleared
        assert resolver._secrets is None
        assert resolver._site is None
        assert len(resolver._spec_cache) == 0
