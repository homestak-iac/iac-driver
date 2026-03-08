#!/usr/bin/env python3
"""Tests for config.py - host configuration and discovery.

Tests verify:
1. Site-config directory discovery (HOMESTAK_ROOT)
2. Host listing (YAML nodes)
3. Host config loading with secrets resolution
4. HostConfig dataclass behavior
5. Error handling for missing config
"""

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))

import pytest
from config import (
    ConfigError,
    HostConfig,
    get_site_config_dir,
    get_base_dir,
    get_sibling_dir,
    list_hosts,
    load_host_config,
    load_secrets,
    _parse_yaml,
)


class TestGetSiteConfigDir:
    """Test site-config discovery logic."""

    def test_homestak_root_env_var(self, tmp_path):
        """HOMESTAK_ROOT env var derives config path."""
        config_dir = tmp_path / 'config'
        config_dir.mkdir()

        with patch.dict(os.environ, {'HOMESTAK_ROOT': str(tmp_path)}):
            result = get_site_config_dir()
            assert result == config_dir

    def test_home_fallback(self, tmp_path):
        """Falls back to $HOME/config when HOMESTAK_ROOT not set."""
        config_dir = tmp_path / 'config'
        config_dir.mkdir()

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop('HOMESTAK_ROOT', None)
            with patch('pathlib.Path.home', return_value=tmp_path):
                result = get_site_config_dir()
                assert result == config_dir

    def test_no_config_raises(self, tmp_path):
        """Should raise ConfigError when config dir doesn't exist."""
        # Point to a root with no config/ subdirectory
        with patch.dict(os.environ, {'HOMESTAK_ROOT': str(tmp_path)}, clear=True):
            with pytest.raises(ConfigError) as exc_info:
                get_site_config_dir()
            assert 'not found' in str(exc_info.value)
            assert 'HOMESTAK_ROOT' in str(exc_info.value)


class TestListHosts:
    """Test host listing from site-config."""

    def test_lists_yaml_nodes(self, tmp_path):
        """Should list nodes from nodes/*.yaml."""
        nodes_dir = tmp_path / 'nodes'
        nodes_dir.mkdir()
        (nodes_dir / 'father.yaml').write_text('node: father')
        (nodes_dir / 'mother.yaml').write_text('node: mother')

        with patch('config.get_site_config_dir', return_value=tmp_path):
            hosts = list_hosts()
            assert hosts == ['father', 'mother']

    def test_lists_hosts_yaml(self, tmp_path):
        """Should list hosts from hosts/*.yaml (pre-PVE physical machines)."""
        hosts_dir = tmp_path / 'hosts'
        hosts_dir.mkdir()
        (hosts_dir / 'host1.yaml').write_text('ip: 198.51.100.1')
        (hosts_dir / 'host2.yaml').write_text('ip: 198.51.100.2')

        with patch('config.get_site_config_dir', return_value=tmp_path):
            hosts = list_hosts()
            assert hosts == ['host1', 'host2']

    def test_empty_returns_empty_list(self, tmp_path):
        """Should return empty list if no hosts found."""
        with patch('config.get_site_config_dir', return_value=tmp_path):
            hosts = list_hosts()
            assert hosts == []

    def test_config_error_returns_empty(self):
        """Should return empty list on ConfigError."""
        with patch('config.get_site_config_dir', side_effect=ConfigError('test')):
            hosts = list_hosts()
            assert hosts == []


class TestLoadHostConfig:
    """Test host config loading."""

    def test_loads_yaml_node(self, tmp_path):
        """Should load config from nodes/*.yaml."""
        nodes_dir = tmp_path / 'nodes'
        nodes_dir.mkdir()
        (nodes_dir / 'test.yaml').write_text("""
node: test
api_endpoint: https://198.51.100.10:8006
""")
        (tmp_path / 'site.yaml').write_text('defaults: {}')
        (tmp_path / 'secrets.yaml').write_text('api_tokens: {}')

        with patch('config.get_site_config_dir', return_value=tmp_path):
            config = load_host_config('test')
            assert config.name == 'test'
            assert config.api_endpoint == 'https://198.51.100.10:8006'

    def test_loads_host_yaml(self, tmp_path):
        """Should load from hosts/*.yaml (pre-PVE physical machines)."""
        hosts_dir = tmp_path / 'hosts'
        hosts_dir.mkdir()
        (hosts_dir / 'testhost.yaml').write_text("""
ip: 192.0.2.1
access:
  ssh_user: root
""")

        with patch('config.get_site_config_dir', return_value=tmp_path):
            config = load_host_config('testhost')
            assert config.name == 'testhost'
            assert config.ssh_host == '192.0.2.1'
            assert config.is_host_only is True

    def test_unknown_host_raises(self, tmp_path):
        """Should raise ValueError for unknown host."""
        with patch('config.get_site_config_dir', return_value=tmp_path):
            with pytest.raises(ValueError) as exc_info:
                load_host_config('nonexistent')
            assert "Host 'nonexistent' not found" in str(exc_info.value)


class TestHostConfig:
    """Test HostConfig dataclass."""

    def test_derives_ssh_host_from_api_endpoint(self, tmp_path):
        """Should derive ssh_host from api_endpoint hostname."""
        config_file = tmp_path / 'test.yaml'
        config_file.write_text('')  # Empty file

        config = HostConfig(
            name='test',
            config_file=config_file,
            api_endpoint='https://198.51.100.10:8006'
        )
        assert config.ssh_host == '198.51.100.10'

    def test_default_values(self, tmp_path):
        """Should have sensible defaults."""
        config_file = tmp_path / 'test.yaml'
        config_file.write_text('')

        config = HostConfig(name='test', config_file=config_file)
        assert config.ssh_user == os.getenv('USER', '')  # Defaults to current user
        assert config.automation_user == 'homestak'  # For VMs via cloud-init
        assert config.packer_release == 'latest'


class TestParseYaml:
    """Test YAML parsing helper."""

    def test_parses_valid_yaml(self, tmp_path):
        """Should parse valid YAML file."""
        yaml_file = tmp_path / 'test.yaml'
        yaml_file.write_text("""
key: value
nested:
  inner: data
list:
  - item1
  - item2
""")
        result = _parse_yaml(yaml_file)
        assert result['key'] == 'value'
        assert result['nested']['inner'] == 'data'
        assert result['list'] == ['item1', 'item2']

    def test_empty_file_returns_empty_dict(self, tmp_path):
        """Empty file should return empty dict."""
        yaml_file = tmp_path / 'empty.yaml'
        yaml_file.write_text('')
        result = _parse_yaml(yaml_file)
        assert result == {}


class TestLoadSecrets:
    """Test secrets loading."""

    def test_loads_secrets_yaml(self, tmp_path):
        """Should load decrypted secrets."""
        (tmp_path / 'secrets.yaml').write_text("""
api_tokens:
  test: "secret-token"
passwords:
  vm_root: "hash"
""")

        with patch('config.get_site_config_dir', return_value=tmp_path):
            secrets = load_secrets()
            assert secrets['api_tokens']['test'] == 'secret-token'
            assert secrets['passwords']['vm_root'] == 'hash'

    def test_missing_secrets_raises(self, tmp_path):
        """Should raise ConfigError if secrets not found."""
        with patch('config.get_site_config_dir', return_value=tmp_path):
            with pytest.raises(ConfigError) as exc_info:
                load_secrets()
            assert 'not found' in str(exc_info.value)
