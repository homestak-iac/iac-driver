"""Tests for actions/pve_lifecycle module.

Unit tests for PVE lifecycle actions and helpers.
"""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from common import ActionResult
from actions.pve_lifecycle import _image_to_asset_name


class TestImageToAssetName:
    """Tests for _image_to_asset_name helper."""

    def test_debian_12(self):
        assert _image_to_asset_name('debian-12') == 'debian-12.qcow2'

    def test_debian_13(self):
        assert _image_to_asset_name('debian-13') == 'debian-13.qcow2'

    def test_pve_image(self):
        assert _image_to_asset_name('pve-9') == 'pve-9.qcow2'

    def test_unknown_image(self):
        assert _image_to_asset_name('ubuntu-22') == 'ubuntu-22.qcow2'


class TestEnsureImageAction:
    """Tests for EnsureImageAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_image_exists(self, mock_ssh):
        from actions.pve_lifecycle import EnsureImageAction

        mock_ssh.return_value = (0, '/var/lib/vz/template/iso/debian-12.img\n', '')

        action = EnsureImageAction(name='test-ensure')
        config = MagicMock()
        config.ssh_host = '198.51.100.61'
        config.ssh_user = 'root'

        result = action.run(config, {})
        assert result.success is True
        assert 'exists' in result.message.lower() or 'found' in result.message.lower() or result.success

    @patch('actions.pve_lifecycle.run_ssh')
    def test_image_not_found(self, mock_ssh):
        from actions.pve_lifecycle import EnsureImageAction

        mock_ssh.return_value = (1, '', 'No such file')

        action = EnsureImageAction(name='test-ensure')
        config = MagicMock()
        config.ssh_host = '198.51.100.61'
        config.ssh_user = 'root'

        result = action.run(config, {})
        # Should fail when image not found
        assert result.success is False


class TestBootstrapAction:
    """Tests for BootstrapAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_requires_host_attr_in_context(self, mock_ssh):
        from actions.pve_lifecycle import BootstrapAction

        action = BootstrapAction(name='test-bootstrap', host_attr='pve_ip')
        config = MagicMock()

        result = action.run(config, {})
        assert result.success is False
        assert 'pve_ip' in result.message

    @patch('actions.pve_lifecycle.run_ssh')
    def test_success_with_host_in_context(self, mock_ssh):
        from actions.pve_lifecycle import BootstrapAction

        # Simulate bootstrap success
        mock_ssh.return_value = (0, 'Bootstrap complete', '')

        action = BootstrapAction(name='test-bootstrap', host_attr='pve_ip')
        config = MagicMock()
        config.ssh_user = 'root'

        result = action.run(config, {'pve_ip': '198.51.100.10'})
        assert result.success is True

    @patch('actions.pve_lifecycle.run_ssh')
    @patch.dict('os.environ', {'HOMESTAK_SERVER': 'https://198.51.100.61:44443', 'HOMESTAK_REF': '_working'}, clear=False)
    def test_serve_repos_uses_insecure_tls(self, mock_ssh):
        """Serve-repos path must pass -k to curl and HOMESTAK_INSECURE=1."""
        from actions.pve_lifecycle import BootstrapAction

        mock_ssh.return_value = (0, 'Bootstrap complete', '')

        action = BootstrapAction(name='test-bootstrap', host_attr='pve_ip')
        config = MagicMock()
        config.automation_user = 'homestak'

        result = action.run(config, {'pve_ip': '198.51.100.10'})
        assert result.success is True

        # Verify the SSH command used curl -k and HOMESTAK_INSECURE=1
        ssh_cmd = mock_ssh.call_args_list[-1][0][1]  # last call, second positional arg
        assert 'curl -fsSLk' in ssh_cmd, f"Expected curl -k flag in: {ssh_cmd}"
        assert 'HOMESTAK_INSECURE=1' in ssh_cmd, f"Expected HOMESTAK_INSECURE=1 in: {ssh_cmd}"


class TestCopySecretsAction:
    """Tests for CopySecretsAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_requires_host_attr_in_context(self, mock_ssh):
        from actions.pve_lifecycle import CopySecretsAction

        action = CopySecretsAction(name='test-secrets', host_attr='pve_ip')
        config = MagicMock()

        result = action.run(config, {})
        assert result.success is False
        assert 'pve_ip' in result.message

    @patch('actions.pve_lifecycle.subprocess')
    @patch('actions.pve_lifecycle.run_ssh')
    @patch('config.get_site_config_dir')
    def test_sets_restrictive_permissions(self, mock_dir, mock_ssh, mock_sub):
        """Secrets must be chmod 600 after scp to ~/config/."""
        from actions.pve_lifecycle import CopySecretsAction

        # Setup: secrets.yaml exists at mocked path, scp succeeds, ssh succeeds
        mock_dir.return_value = Path('/tmp/test-config')
        mock_sub.run.return_value = MagicMock(returncode=0)
        mock_ssh.return_value = (0, '', '')

        action = CopySecretsAction(name='test-secrets')
        config = MagicMock()
        config.automation_user = 'homestak'

        # Create a temporary secrets.yaml so exists() returns True
        secrets = Path('/tmp/test-config/secrets.yaml')
        secrets.parent.mkdir(parents=True, exist_ok=True)
        secrets.write_text('test: true')
        try:
            result = action.run(config, {'vm_ip': '198.51.100.10'})
            assert result.success is True

            # Verify the install command includes chmod (no chown — user-owned)
            install_cmd = mock_ssh.call_args[0][1]
            assert 'chmod 600' in install_cmd, f"Expected chmod 600 in: {install_cmd}"
            assert 'sudo' not in install_cmd, f"Expected no sudo in: {install_cmd}"
        finally:
            secrets.unlink(missing_ok=True)
            secrets.parent.rmdir()


    @patch('actions.pve_lifecycle.run_ssh')
    @patch('config.get_site_config_dir')
    def test_not_decrypted_suggests_make_decrypt(self, mock_dir, mock_ssh):
        """When secrets.yaml.enc exists but secrets.yaml doesn't, suggest decrypt."""
        from actions.pve_lifecycle import CopySecretsAction
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir.return_value = Path(tmpdir)
            # Create .enc but not plaintext
            (Path(tmpdir) / 'secrets.yaml.enc').write_text('encrypted')

            action = CopySecretsAction(name='test-secrets')
            config = MagicMock()

            result = action.run(config, {'vm_ip': '198.51.100.10'})
            assert result.success is False
            assert 'not decrypted' in result.message
            assert 'make decrypt' in result.message

    @patch('actions.pve_lifecycle.run_ssh')
    @patch('config.get_site_config_dir')
    def test_missing_secrets_no_enc(self, mock_dir, mock_ssh):
        """When neither secrets.yaml nor .enc exists, use 'not found' message."""
        from actions.pve_lifecycle import CopySecretsAction
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_dir.return_value = Path(tmpdir)

            action = CopySecretsAction(name='test-secrets')
            config = MagicMock()

            result = action.run(config, {'vm_ip': '198.51.100.10'})
            assert result.success is False
            assert 'not found' in result.message
            assert 'make decrypt' not in result.message


class TestCreateApiTokenAction:
    """Tests for CreateApiTokenAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_requires_host_attr_in_context(self, mock_ssh):
        from actions.pve_lifecycle import CreateApiTokenAction

        action = CreateApiTokenAction(name='test-token', host_attr='pve_ip')
        config = MagicMock()

        result = action.run(config, {})
        assert result.success is False
        assert 'pve_ip' in result.message


class TestConfigureNetworkBridgeAction:
    """Tests for ConfigureNetworkBridgeAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_requires_host_attr_in_context(self, mock_ssh):
        from actions.pve_lifecycle import ConfigureNetworkBridgeAction

        action = ConfigureNetworkBridgeAction(name='test-bridge', host_attr='pve_ip')
        config = MagicMock()

        result = action.run(config, {})
        assert result.success is False
        assert 'pve_ip' in result.message


class TestGenerateNodeConfigAction:
    """Tests for GenerateNodeConfigAction."""

    @patch('actions.pve_lifecycle.run_ssh')
    def test_requires_host_attr_in_context(self, mock_ssh):
        from actions.pve_lifecycle import GenerateNodeConfigAction

        action = GenerateNodeConfigAction(name='test-nodeconfig', host_attr='pve_ip')
        config = MagicMock()

        result = action.run(config, {})
        assert result.success is False
        assert 'pve_ip' in result.message
