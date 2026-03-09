"""Tests for config_apply module."""

import json
from unittest.mock import patch

import pytest

from config_apply import (
    spec_to_ansible_vars,
    _load_spec,
    _fetch_spec,
    _write_marker,
    apply_config,
    ConfigError,
    MARKER_PATH,
)


class TestSpecToAnsibleVars:
    """Test spec-to-ansible variable mapping."""

    def test_empty_spec(self):
        """Empty spec returns empty vars."""
        result = spec_to_ansible_vars({})
        assert result == {}

    def test_platform_packages(self):
        """Platform packages map to ansible packages var."""
        spec = {'platform': {'packages': ['htop', 'curl', 'wget']}}
        result = spec_to_ansible_vars(spec)
        assert result['packages'] == ['htop', 'curl', 'wget']

    def test_timezone(self):
        """Config timezone maps to ansible timezone var."""
        spec = {'config': {'timezone': 'America/Denver'}}
        result = spec_to_ansible_vars(spec)
        assert result['timezone'] == 'America/Denver'

    def test_first_user(self):
        """First user maps to local_user var."""
        spec = {
            'access': {
                'users': [
                    {'name': 'homestak', 'sudo': True, 'ssh_keys': ['ssh-ed25519 AAA...']}
                ]
            }
        }
        result = spec_to_ansible_vars(spec)
        assert result['local_user'] == 'homestak'
        assert result['user_sudo'] is True
        assert result['ssh_authorized_keys'] == ['ssh-ed25519 AAA...']

    def test_multiple_users_keys_merged(self):
        """SSH keys from all users are collected."""
        spec = {
            'access': {
                'users': [
                    {'name': 'alice', 'ssh_keys': ['key-alice']},
                    {'name': 'bob', 'ssh_keys': ['key-bob']},
                ]
            }
        }
        result = spec_to_ansible_vars(spec)
        assert result['ssh_authorized_keys'] == ['key-alice', 'key-bob']

    def test_posture_settings(self):
        """Posture settings map to security role vars."""
        spec = {
            'access': {
                '_posture': {
                    'ssh': {
                        'port': 22,
                        'permit_root_login': 'yes',
                        'password_authentication': 'yes',
                    },
                    'sudo': {'nopasswd': True},
                    'fail2ban': {'enabled': False},
                }
            }
        }
        result = spec_to_ansible_vars(spec)
        assert result['ssh_port'] == 22
        assert result['ssh_permit_root_login'] == 'yes'
        assert result['ssh_password_authentication'] == 'yes'
        assert result['sudo_nopasswd'] is True
        assert result['fail2ban_enabled'] is False

    def test_posture_packages_merged(self):
        """Posture packages merge with platform packages (deduped)."""
        spec = {
            'platform': {'packages': ['htop', 'curl']},
            'access': {
                '_posture': {
                    'packages': ['net-tools', 'curl'],  # curl is duplicate
                }
            }
        }
        result = spec_to_ansible_vars(spec)
        assert result['packages'] == ['htop', 'curl', 'net-tools']

    def test_services(self):
        """Service enable/disable lists map to ansible vars."""
        spec = {
            'platform': {
                'services': {
                    'enable': ['pveproxy', 'pvedaemon'],
                    'disable': ['rpcbind'],
                }
            }
        }
        result = spec_to_ansible_vars(spec)
        assert result['services_enable'] == ['pveproxy', 'pvedaemon']
        assert result['services_disable'] == ['rpcbind']

    def test_full_pve_spec(self):
        """Full PVE spec maps correctly."""
        spec = {
            'identity': {'hostname': 'test-pve'},
            'access': {
                'posture': 'dev',
                'users': [
                    {'name': 'homestak', 'sudo': True, 'ssh_keys': ['ssh-ed25519 test-key']},
                ],
                '_posture': {
                    'ssh': {'port': 22, 'permit_root_login': 'yes', 'password_authentication': 'yes'},
                    'sudo': {'nopasswd': True},
                    'fail2ban': {'enabled': False},
                    'packages': ['net-tools', 'strace'],
                },
            },
            'platform': {
                'packages': ['htop', 'curl'],
                'services': {'enable': ['pveproxy'], 'disable': ['rpcbind']},
            },
            'config': {'timezone': 'America/Denver'},
        }
        result = spec_to_ansible_vars(spec)
        assert result['timezone'] == 'America/Denver'
        assert result['local_user'] == 'homestak'
        assert result['packages'] == ['htop', 'curl', 'net-tools', 'strace']
        assert result['services_enable'] == ['pveproxy']
        assert result['services_disable'] == ['rpcbind']
        assert result['ssh_permit_root_login'] == 'yes'
        assert result['sudo_nopasswd'] is True
        assert result['fail2ban_enabled'] is False


class TestLoadSpec:
    """Test spec loading."""

    def test_missing_spec(self, tmp_path):
        """Missing spec raises ConfigError."""
        with pytest.raises(ConfigError, match="Spec not found"):
            _load_spec(tmp_path / 'nonexistent.yaml')

    def test_valid_spec(self, tmp_path):
        """Valid spec loads correctly."""
        spec_file = tmp_path / 'test.yaml'
        spec_file.write_text('schema_version: 1\nplatform:\n  packages:\n    - htop\n')
        result = _load_spec(spec_file)
        assert result['platform']['packages'] == ['htop']

    def test_empty_spec(self, tmp_path):
        """Empty spec raises ConfigError."""
        spec_file = tmp_path / 'empty.yaml'
        spec_file.write_text('')
        with pytest.raises(ConfigError, match="Invalid spec"):
            _load_spec(spec_file)


class TestWriteMarker:
    """Test marker file writing."""

    def test_marker_written(self, tmp_path, monkeypatch):
        """Marker file is written with correct content."""
        marker_path = tmp_path / 'state' / 'config-complete.json'
        monkeypatch.setattr('config_apply.MARKER_PATH', marker_path)

        result = _write_marker(
            {'packages': ['a', 'b'], 'local_user': 'test', 'services_enable': ['svc']},
            'test-spec',
        )

        assert result == marker_path
        assert marker_path.exists()

        data = json.loads(marker_path.read_text())
        assert data['phase'] == 'config'
        assert data['status'] == 'complete'
        assert data['spec'] == 'test-spec'
        assert data['packages'] == 2
        assert data['services_enabled'] == 1
        assert data['users'] == 1


class TestApplyConfig:
    """Test the apply_config function."""

    def test_missing_spec_returns_failure(self, tmp_path):
        """apply_config with missing spec returns failure."""
        result = apply_config(spec_path=tmp_path / 'nonexistent.yaml')
        assert not result.success
        assert 'not found' in result.message.lower()

    def test_dry_run(self, tmp_path, capsys):
        """Dry run prints preview without executing."""
        spec_file = tmp_path / 'test.yaml'
        spec_file.write_text(
            'schema_version: 1\n'
            'identity:\n  hostname: dry-test\n'
            'platform:\n  packages:\n    - htop\n'
        )
        result = apply_config(spec_path=spec_file, dry_run=True)
        assert result.success
        captured = capsys.readouterr()
        assert 'dry-test' in captured.out.lower() or 'Dry-run' in captured.out

    def test_dry_run_json(self, tmp_path, capsys):
        """Dry run with JSON output emits JSON."""
        spec_file = tmp_path / 'test.yaml'
        spec_file.write_text(
            'schema_version: 1\n'
            'identity:\n  hostname: json-test\n'
            'platform:\n  packages:\n    - htop\n'
        )
        result = apply_config(spec_path=spec_file, dry_run=True, json_output=True)
        assert result.success
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data['spec'] == 'json-test'
        assert data['dry_run'] is True


class TestFetchSpec:
    """Test _fetch_spec function."""

    def test_missing_server_returns_none(self):
        """Missing HOMESTAK_SERVER returns None."""
        with patch.dict('os.environ', {}, clear=True):
            result = _fetch_spec()
        assert result is None

    def test_missing_token_returns_none(self):
        """Missing HOMESTAK_TOKEN returns None."""
        with patch.dict('os.environ', {'HOMESTAK_SERVER': 'https://localhost:44443'}, clear=True):
            result = _fetch_spec()
        assert result is None

    def test_successful_fetch_returns_path(self, tmp_path):
        """Successful fetch returns path to saved spec."""
        spec_file = tmp_path / 'spec.yaml'
        mock_spec = {'schema_version': 1, 'identity': {'hostname': 'test'}}

        with patch.dict('os.environ', {
            'HOMESTAK_SERVER': 'https://localhost:44443',
            'HOMESTAK_TOKEN': 'test-provisioning-token',
        }, clear=True):
            with patch('resolver.spec_client.SpecClient') as MockClient:
                instance = MockClient.return_value
                instance.fetch_and_save.return_value = (mock_spec, spec_file)
                result = _fetch_spec()

        assert result == spec_file
