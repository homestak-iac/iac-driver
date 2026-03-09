"""Tests for OpenTofu action classes.

Tests for TofuApplyAction and TofuDestroyAction.
"""

from unittest.mock import patch, MagicMock


class TestTofuApplyAction:
    """Test TofuApplyAction."""

    def _make_action(self, **kwargs):
        """Create a TofuApplyAction with defaults."""
        from actions.tofu import TofuApplyAction
        defaults = dict(name='test', vm_name='testvm', vmid=99900)
        defaults.update(kwargs)
        return TofuApplyAction(**defaults)

    def test_apply_success(self, tmp_path):
        """Successful apply should return success with context updates."""
        from actions.tofu import TofuApplyAction

        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        mock_resolver.resolve_inline_vm.return_value = {'vms': []}

        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)
        state_dir = tmp_path / 'tofu' / 'testvm-srv1'

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command') as mock_cmd:
            # Create the temp tfvars file so cleanup doesn't fail
            (tmp_path / 'tfvars.json').touch()
            mock_cmd.side_effect = [
                (0, '', ''),  # tofu init
                (0, '', ''),  # tofu apply
            ]
            result = action.run(config, context)

        assert result.success is True
        assert 'testvm' in result.message
        assert result.context_updates['testvm_vm_id'] == 99900

    def test_apply_config_resolver_failure(self, tmp_path):
        """ConfigResolver failure should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        with patch('actions.tofu.ConfigResolver') as mock_cls:
            mock_cls.return_value.resolve_inline_vm.side_effect = \
                ValueError("preset not found: vm-small")
            result = action.run(config, context)

        assert result.success is False
        assert 'ConfigResolver failed' in result.message

    def test_apply_init_failure(self, tmp_path):
        """tofu init failure should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command', return_value=(1, '', 'init failed')):
            (tmp_path / 'tfvars.json').touch()
            result = action.run(config, context)

        assert result.success is False
        assert 'tofu init failed' in result.message

    def test_apply_apply_failure(self, tmp_path):
        """tofu apply failure should return error and clean up tfvars."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command') as mock_cmd:
            (tmp_path / 'tfvars.json').touch()
            mock_cmd.side_effect = [
                (0, '', ''),              # tofu init OK
                (1, '', 'apply failed'),  # tofu apply fails
            ]
            result = action.run(config, context)

        assert result.success is False
        assert 'tofu apply failed' in result.message
        # Temp file should be cleaned up
        assert not (tmp_path / 'tfvars.json').exists()

    def test_apply_missing_tofu_dir(self, tmp_path):
        """Missing tofu directory should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'nonexistent'):
            result = action.run(config, context)

        assert result.success is False
        assert 'not found' in result.message

    def test_apply_state_isolation(self, tmp_path):
        """State should be isolated per vm_name + node."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)

        captured_cmds = []

        def capture_cmd(cmd, **kwargs):
            captured_cmds.append(cmd)
            return (0, '', '')

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command', side_effect=capture_cmd):
            (tmp_path / 'tfvars.json').touch()
            action.run(config, context)

        # The apply command should reference testvm-srv1 in state path
        apply_cmd = captured_cmds[1]
        state_args = [a for a in apply_cmd if '-state=' in a]
        assert len(state_args) == 1
        assert 'testvm-srv1' in state_args[0]

    def test_apply_context_updates(self, tmp_path):
        """Apply should add vm_id and provisioned_vms to context."""
        action = self._make_action(vm_preset='vm-small', image='debian-12', vmid=99913)
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command', return_value=(0, '', '')):
            (tmp_path / 'tfvars.json').touch()
            result = action.run(config, context)

        assert result.context_updates['testvm_vm_id'] == 99913
        assert result.context_updates['provisioned_vms'] == [
            {'name': 'testvm', 'vmid': 99913}
        ]


class TestTofuDestroyAction:
    """Test TofuDestroyAction."""

    def _make_action(self, **kwargs):
        """Create a TofuDestroyAction with defaults."""
        from actions.tofu import TofuDestroyAction
        defaults = dict(name='test', vm_name='testvm', vmid=99900)
        defaults.update(kwargs)
        return TofuDestroyAction(**defaults)

    def test_destroy_success(self, tmp_path):
        """Successful destroy should return success."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)
        state_dir = tmp_path / 'tofu' / 'testvm-srv1'
        state_dir.mkdir(parents=True)
        (state_dir / 'terraform.tfstate').write_text('{}')

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command', return_value=(0, '', '')):
            (tmp_path / 'tfvars.json').touch()
            result = action.run(config, context)

        assert result.success is True
        assert 'destroy completed' in result.message

    def test_destroy_no_state_file(self, tmp_path):
        """Missing state file should return success (nothing to destroy)."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path):
            result = action.run(config, context)

        assert result.success is True
        assert 'nothing to destroy' in result.message

    def test_destroy_failure(self, tmp_path):
        """tofu destroy failure should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()
        tofu_dir = tmp_path / 'tofu' / 'envs' / 'generic'
        tofu_dir.mkdir(parents=True)
        state_dir = tmp_path / 'tofu' / 'testvm-srv1'
        state_dir.mkdir(parents=True)
        (state_dir / 'terraform.tfstate').write_text('{}')

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'tofu'), \
             patch('actions.tofu.get_state_dir', return_value=tmp_path), \
             patch('actions.tofu.run_command', return_value=(1, '', 'destroy failed')):
            (tmp_path / 'tfvars.json').touch()
            result = action.run(config, context)

        assert result.success is False
        assert 'tofu destroy failed' in result.message

    def test_destroy_config_resolver_failure(self):
        """ConfigResolver failure should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        with patch('actions.tofu.ConfigResolver') as mock_cls:
            mock_cls.return_value.resolve_inline_vm.side_effect = \
                ValueError("preset not found")
            result = action.run(config, context)

        assert result.success is False
        assert 'ConfigResolver failed' in result.message

    def test_destroy_missing_tofu_dir(self, tmp_path):
        """Missing tofu directory should return error."""
        action = self._make_action(vm_preset='vm-small', image='debian-12')
        config = MagicMock()
        config.name = 'srv1'
        context = {}

        mock_resolver = MagicMock()

        with patch('actions.tofu.ConfigResolver', return_value=mock_resolver), \
             patch('actions.tofu.create_temp_tfvars', return_value=tmp_path / 'tfvars.json'), \
             patch('actions.tofu.get_sibling_dir', return_value=tmp_path / 'nonexistent'):
            result = action.run(config, context)

        assert result.success is False
        assert 'not found' in result.message


class TestCreateTempTfvars:
    """Test create_temp_tfvars utility."""

    def test_creates_file_with_prefix(self):
        """Should create a temp file with env-node prefix."""
        from actions.tofu import create_temp_tfvars
        path = create_temp_tfvars('myenv', 'mynode')
        try:
            assert path.exists()
            assert 'tfvars-myenv-mynode' in path.name
            assert path.suffix == '.json'
        finally:
            path.unlink()

    def test_creates_unique_files(self):
        """Multiple calls should create unique files."""
        from actions.tofu import create_temp_tfvars
        paths = [create_temp_tfvars('env', 'node') for _ in range(3)]
        try:
            assert len(set(paths)) == 3
        finally:
            for p in paths:
                p.unlink()
