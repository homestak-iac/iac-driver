#!/usr/bin/env python3
"""Tests for RecursiveScenarioAction.

Tests verify:
1. Basic execution flow
2. Context key extraction
3. SSH command construction
4. JSON result parsing
5. Error handling
6. PTY vs non-PTY modes
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock, Mock

import pytest
from common import ActionResult
from conftest import MockHostConfig


class TestRecursiveScenarioActionInit:
    """Test RecursiveScenarioAction initialization."""

    def test_default_values(self):
        """Should have sensible defaults."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        assert action.name == 'test'
        assert action.scenario_name == 'vm-roundtrip'
        assert action.host_attr == 'node_ip'
        assert action.timeout == 600
        assert action.scenario_args == []
        assert action.context_keys == []
        assert action.use_pty is True
        assert action.ssh_user == 'root'

    def test_custom_values(self):
        """Should accept custom values."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='custom',
            scenario_name='pve-setup',
            host_attr='leaf_ip',
            timeout=300,
            scenario_args=['--host', 'child-pve'],
            context_keys=['vm_ip', 'vm_id'],
            use_pty=False,
            ssh_user='homestak'
        )

        assert action.host_attr == 'leaf_ip'
        assert action.timeout == 300
        assert action.scenario_args == ['--host', 'child-pve']
        assert action.context_keys == ['vm_ip', 'vm_id']
        assert action.use_pty is False
        assert action.ssh_user == 'homestak'


class TestRecursiveScenarioActionRun:
    """Test RecursiveScenarioAction.run() method."""

    def test_missing_host_key_returns_error(self):
        """Missing host_key in context should return failure."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            host_attr='nonexistent'
        )
        config = MockHostConfig()
        context = {}

        result = action.run(config, context)

        assert result.success is False
        assert 'nonexistent' in result.message

    def test_success_with_json_result(self):
        """Successful execution with JSON result should parse correctly."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            host_attr='node_ip',
            context_keys=['vm_ip']
        )
        config = MockHostConfig()
        context = {'node_ip': '198.51.100.52'}

        # Mock JSON result from inner scenario
        json_result = {
            'scenario': 'vm-roundtrip',
            'success': True,
            'duration_seconds': 45.2,
            'phases': [
                {'name': 'provision', 'status': 'passed', 'duration': 6.8},
            ],
            'context': {
                'vm_ip': '198.51.100.55',
                'vm_id': 99900
            }
        }
        mock_output = f"Log line 1\nLog line 2\n{json.dumps(json_result)}"

        with patch.object(action, '_run_with_pty', return_value=(0, mock_output, '')):
            result = action.run(config, context)

        assert result.success is True
        assert 'vm-roundtrip' in result.message
        assert result.context_updates.get('vm_ip') == '198.51.100.55'

    def test_failure_with_error_message(self):
        """Failed execution should extract error message."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            host_attr='node_ip'
        )
        config = MockHostConfig()
        context = {'node_ip': '198.51.100.52'}

        json_result = {
            'scenario': 'vm-roundtrip',
            'success': False,
            'error': 'Phase provision failed: tofu apply error',
            'phases': [
                {'name': 'provision', 'status': 'failed', 'duration': 3.5},
            ]
        }
        mock_output = json.dumps(json_result)

        with patch.object(action, '_run_with_pty', return_value=(1, mock_output, '')):
            result = action.run(config, context)

        assert result.success is False
        assert 'tofu apply error' in result.message


class TestBuildRemoteCommand:
    """Test _build_remote_command method."""

    def test_basic_command(self):
        """Should build basic homestak command."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        cmd = action._build_remote_command()

        assert 'homestak' in cmd
        assert 'scenario' in cmd
        assert 'vm-roundtrip' in cmd
        assert '--json-output' in cmd

    def test_with_args(self):
        """Should include additional arguments."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='pve-setup',
            scenario_args=['--host', 'child-pve', '--verbose']
        )

        cmd = action._build_remote_command()

        assert '--host' in cmd
        assert 'child-pve' in cmd
        assert '--verbose' in cmd


class TestBuildSSHCommand:
    """Test _build_ssh_command method."""

    def test_basic_ssh_command(self):
        """Should build SSH command with standard options."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        cmd = action._build_ssh_command('198.51.100.52', 'echo hello')

        assert 'ssh' in cmd
        assert '-o' in cmd
        assert 'StrictHostKeyChecking=no' in ' '.join(cmd)
        assert 'root@198.51.100.52' in cmd
        assert 'echo hello' in cmd

    def test_pty_flag_included(self):
        """Should include -t flag when use_pty=True."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip', use_pty=True)

        cmd = action._build_ssh_command('198.51.100.52', 'echo hello')

        assert '-t' in cmd

    def test_no_pty_flag_when_disabled(self):
        """Should not include -t flag when use_pty=False."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip', use_pty=False)

        cmd = action._build_ssh_command('198.51.100.52', 'echo hello')

        # Build without PTY, then manually check
        assert '-t' not in cmd or action.use_pty

    def test_custom_user(self):
        """Should use custom SSH user."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            ssh_user='homestak'
        )

        cmd = action._build_ssh_command('198.51.100.52', 'echo hello')

        assert 'homestak@198.51.100.52' in cmd


class TestParseJSONResult:
    """Test _parse_json_result method."""

    def test_parse_clean_json(self):
        """Should parse clean JSON output."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        json_str = '{"scenario": "vm-roundtrip", "success": true}'
        result = action._parse_json_result(json_str)

        assert result['scenario'] == 'vm-roundtrip'
        assert result['success'] is True

    def test_parse_json_after_logs(self):
        """Should extract JSON from output with preceding log lines."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        output = """2026-01-21 10:00:00 [INFO] Starting scenario
Phase: provision... passed
Phase: start... passed
{"scenario": "vm-roundtrip", "success": true, "duration_seconds": 45.2}"""

        result = action._parse_json_result(output)

        assert result is not None
        assert result['scenario'] == 'vm-roundtrip'
        assert result['success'] is True

    def test_parse_multiline_json(self):
        """Should parse pretty-printed JSON."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        output = """Log line
{
  "scenario": "vm-roundtrip",
  "success": true
}"""

        result = action._parse_json_result(output)

        assert result is not None
        assert result['success'] is True

    def test_return_none_for_no_json(self):
        """Should return None when no JSON is found."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        result = action._parse_json_result("Just some text with no JSON")

        assert result is None

    def test_return_none_for_empty_output(self):
        """Should return None for empty output."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        assert action._parse_json_result('') is None
        assert action._parse_json_result(None) is None


class TestExtractContext:
    """Test _extract_context method."""

    def test_extract_specified_keys(self):
        """Should extract only specified context keys."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            context_keys=['vm_ip', 'vm_id']
        )

        json_result = {
            'context': {
                'vm_ip': '198.51.100.55',
                'vm_id': 99900,
                'other_key': 'ignored'
            }
        }

        context_updates = action._extract_context(json_result)

        assert context_updates == {'vm_ip': '198.51.100.55', 'vm_id': 99900}
        assert 'other_key' not in context_updates

    def test_missing_keys_not_included(self):
        """Should not include keys that don't exist in result."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            context_keys=['vm_ip', 'nonexistent']
        )

        json_result = {
            'context': {
                'vm_ip': '198.51.100.55'
            }
        }

        context_updates = action._extract_context(json_result)

        assert context_updates == {'vm_ip': '198.51.100.55'}
        assert 'nonexistent' not in context_updates

    def test_empty_context_keys(self):
        """Should return empty dict when no keys specified."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            context_keys=[]
        )

        json_result = {
            'context': {
                'vm_ip': '198.51.100.55'
            }
        }

        context_updates = action._extract_context(json_result)

        assert context_updates == {}

    def test_none_result(self):
        """Should return empty dict for None result."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            context_keys=['vm_ip']
        )

        assert action._extract_context(None) == {}


class TestExtractErrorMessage:
    """Test _extract_error_message method."""

    def test_error_from_json(self):
        """Should extract error from JSON result."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        json_result = {'error': 'Specific error message'}

        error = action._extract_error_message(json_result, '', '')

        assert error == 'Specific error message'

    def test_error_from_failed_phase(self):
        """Should extract error from failed phase."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        json_result = {
            'phases': [
                {'name': 'provision', 'status': 'passed'},
                {'name': 'start', 'status': 'failed'}
            ]
        }

        error = action._extract_error_message(json_result, '', '')

        assert 'start' in error
        assert 'failed' in error.lower()

    def test_error_from_stderr(self):
        """Should extract error from stderr when JSON has no error."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        error = action._extract_error_message(None, 'Connection refused', '')

        assert 'Connection refused' in error

    def test_error_from_stdout_patterns(self):
        """Should detect error patterns in stdout."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        stdout = "Some log\nError: Something went wrong\nMore logs"

        error = action._extract_error_message(None, '', stdout)

        assert 'Error' in error or 'wrong' in error

    def test_fallback_message(self):
        """Should return fallback message when no error found."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(name='test', scenario_name='vm-roundtrip')

        error = action._extract_error_message(None, '', '')

        assert 'Unknown error' in error


class TestTimeoutHandling:
    """Test timeout behavior."""

    def test_timeout_returns_failure(self):
        """Should return failure on timeout."""
        from actions.recursive import RecursiveScenarioAction
        import subprocess

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            host_attr='node_ip',
            timeout=5
        )
        config = MockHostConfig()
        context = {'node_ip': '198.51.100.52'}

        with patch.object(action, '_run_with_pty', side_effect=subprocess.TimeoutExpired('cmd', 5)):
            result = action.run(config, context)

        assert result.success is False
        assert 'Timeout' in result.message
        assert '5' in result.message


class TestNonPTYMode:
    """Test non-PTY execution mode."""

    def test_run_without_pty(self):
        """Should execute successfully without PTY."""
        from actions.recursive import RecursiveScenarioAction

        action = RecursiveScenarioAction(
            name='test',
            scenario_name='vm-roundtrip',
            host_attr='node_ip',
            use_pty=False
        )
        config = MockHostConfig()
        context = {'node_ip': '198.51.100.52'}

        json_result = {
            'scenario': 'vm-roundtrip',
            'success': True,
            'duration_seconds': 30.0
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps(json_result)
        mock_result.stderr = ''

        with patch('subprocess.run', return_value=mock_result):
            result = action.run(config, context)

        assert result.success is True
