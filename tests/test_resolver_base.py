"""Tests for resolver/base.py - shared FK resolution utilities."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

# Add src to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from resolver.base import (
    ResolverError,
    PostureNotFoundError,
    SSHKeyNotFoundError,
    SecretsNotFoundError,
    discover_etc_path,
    ResolverBase,
)


class TestDiscoverEtcPath:
    """Tests for discover_etc_path()."""

    def test_homestak_root_env_var(self, tmp_path):
        """HOMESTAK_ROOT env var derives config path."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        with patch.dict(os.environ, {"HOMESTAK_ROOT": str(tmp_path)}, clear=True):
            assert discover_etc_path() == config_dir

    def test_home_fallback(self, tmp_path):
        """Falls back to $HOME/config when HOMESTAK_ROOT not set."""
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("HOMESTAK_ROOT", None)
            with patch("pathlib.Path.home", return_value=tmp_path):
                assert discover_etc_path() == config_dir

    def test_no_path_found_raises_error(self):
        """ResolverError raised when config dir doesn't exist."""
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("HOMESTAK_ROOT", None)
            with patch.object(Path, "is_dir", return_value=False):
                with pytest.raises(ResolverError) as exc_info:
                    discover_etc_path()
                assert exc_info.value.code == "E500"
                assert "Cannot find config" in exc_info.value.message


class TestResolverBase:
    """Tests for ResolverBase class."""

    @pytest.fixture
    def site_config(self, tmp_path):
        """Create a minimal site-config structure."""
        # Create directories
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
                "user2": "ssh-ed25519 AAAA... user2@host",
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

        return tmp_path

    def test_init_with_path(self, site_config):
        """ResolverBase initializes with explicit path."""
        resolver = ResolverBase(etc_path=site_config)
        assert resolver.etc_path == site_config

    def test_init_auto_discover(self, site_config):
        """ResolverBase auto-discovers path from HOMESTAK_ROOT."""
        # site_config fixture is already the config dir, so root is its parent
        root = site_config.parent
        config_dir = root / "config"
        config_dir.mkdir(exist_ok=True)
        # Copy fixture contents to config_dir
        import shutil
        for item in site_config.iterdir():
            dest = config_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)
        with patch.dict(os.environ, {"HOMESTAK_ROOT": str(root)}, clear=True):
            resolver = ResolverBase()
            assert resolver.etc_path == config_dir

    def test_load_yaml(self, site_config):
        """_load_yaml loads YAML file."""
        resolver = ResolverBase(etc_path=site_config)
        data = resolver._load_yaml(site_config / "site.yaml")
        assert data["defaults"]["timezone"] == "America/Denver"

    def test_load_yaml_missing_file(self, site_config):
        """_load_yaml returns empty dict for missing file."""
        resolver = ResolverBase(etc_path=site_config)
        data = resolver._load_yaml(site_config / "nonexistent.yaml")
        assert data == {}

    def test_load_secrets(self, site_config):
        """_load_secrets loads and caches secrets."""
        resolver = ResolverBase(etc_path=site_config)
        secrets = resolver._load_secrets()
        assert "ssh_keys" in secrets
        assert secrets["ssh_keys"]["user1"].startswith("ssh-rsa")

        # Verify caching
        secrets2 = resolver._load_secrets()
        assert secrets is secrets2

    def test_load_secrets_missing_raises(self, tmp_path):
        """_load_secrets raises SecretsNotFoundError if missing."""
        resolver = ResolverBase(etc_path=tmp_path)
        with pytest.raises(SecretsNotFoundError) as exc_info:
            resolver._load_secrets()
        assert exc_info.value.code == "E500"
        assert "not found" in str(exc_info.value)

    def test_load_secrets_not_decrypted_message(self, tmp_path):
        """SecretsNotFoundError suggests decrypt when .enc exists."""
        (tmp_path / "secrets.yaml.enc").write_text("encrypted")
        resolver = ResolverBase(etc_path=tmp_path)
        with pytest.raises(SecretsNotFoundError) as exc_info:
            resolver._load_secrets()
        assert "not decrypted" in str(exc_info.value)
        assert "make decrypt" in str(exc_info.value)

    def test_load_site(self, site_config):
        """_load_site loads and caches site.yaml."""
        resolver = ResolverBase(etc_path=site_config)
        site = resolver._load_site()
        assert site["defaults"]["domain"] == "example.com"

        # Verify caching
        site2 = resolver._load_site()
        assert site is site2

    def test_load_posture(self, site_config):
        """_load_posture loads posture."""
        resolver = ResolverBase(etc_path=site_config)
        posture = resolver._load_posture("dev")
        assert posture["auth"]["method"] == "network"

    def test_load_posture_caching(self, site_config):
        """_load_posture caches postures."""
        resolver = ResolverBase(etc_path=site_config)
        posture1 = resolver._load_posture("dev")
        posture2 = resolver._load_posture("dev")
        assert posture1 is posture2

    def test_load_posture_not_found(self, site_config):
        """_load_posture raises PostureNotFoundError."""
        resolver = ResolverBase(etc_path=site_config)
        with pytest.raises(PostureNotFoundError) as exc_info:
            resolver._load_posture("nonexistent")
        assert exc_info.value.code == "E201"

    def test_resolve_ssh_keys(self, site_config):
        """_resolve_ssh_keys resolves key references."""
        resolver = ResolverBase(etc_path=site_config)
        keys = resolver._resolve_ssh_keys(["user1", "user2"])
        assert len(keys) == 2
        assert keys[0].startswith("ssh-rsa")
        assert keys[1].startswith("ssh-ed25519")

    def test_resolve_ssh_keys_by_id(self, site_config):
        """_resolve_ssh_keys resolves bare key identifiers."""
        resolver = ResolverBase(etc_path=site_config)
        keys = resolver._resolve_ssh_keys(["user1"])
        assert len(keys) == 1
        assert keys[0].startswith("ssh-rsa")

    def test_resolve_ssh_keys_not_found(self, site_config):
        """_resolve_ssh_keys raises SSHKeyNotFoundError."""
        resolver = ResolverBase(etc_path=site_config)
        with pytest.raises(SSHKeyNotFoundError) as exc_info:
            resolver._resolve_ssh_keys(["nonexistent"])
        assert exc_info.value.code == "E202"

    def test_get_site_defaults(self, site_config):
        """_get_site_defaults returns defaults section."""
        resolver = ResolverBase(etc_path=site_config)
        defaults = resolver._get_site_defaults()
        assert defaults["timezone"] == "America/Denver"

    def test_get_signing_key(self, site_config):
        """get_signing_key returns signing key from secrets."""
        resolver = ResolverBase(etc_path=site_config)
        key = resolver.get_signing_key()
        assert key == "a" * 64

    def test_get_signing_key_missing(self, tmp_path):
        """get_signing_key returns None when secrets missing."""
        resolver = ResolverBase(etc_path=tmp_path)
        key = resolver.get_signing_key()
        assert key is None

    def test_get_signing_key_no_auth_section(self, tmp_path):
        """get_signing_key returns None when auth section missing."""
        (tmp_path / "postures").mkdir(parents=True)
        (tmp_path / "site.yaml").write_text("defaults: {}")
        (tmp_path / "secrets.yaml").write_text("ssh_keys: {}")
        resolver = ResolverBase(etc_path=tmp_path)
        key = resolver.get_signing_key()
        assert key is None

    def test_clear_cache(self, site_config):
        """clear_cache clears all cached data."""
        resolver = ResolverBase(etc_path=site_config)

        # Load data to populate cache
        resolver._load_secrets()
        resolver._load_site()
        resolver._load_posture("dev")

        # Verify cache is populated
        assert resolver._secrets is not None
        assert resolver._site is not None
        assert len(resolver._posture_cache) > 0

        # Clear cache
        resolver.clear_cache()

        # Verify cache is cleared
        assert resolver._secrets is None
        assert resolver._site is None
        assert len(resolver._posture_cache) == 0
