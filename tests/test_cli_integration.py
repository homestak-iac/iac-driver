#!/usr/bin/env python3
"""Integration tests for CLI scenario attribute handling.

These tests verify the CLI properly handles:
1. requires_root check for --local mode
2. requires_host_config for auto-detect host

Some tests require config with configured hosts and are marked
with @pytest.mark.requires_infrastructure - these are skipped in CI.
"""

import subprocess
import sys
from pathlib import Path

import pytest


# Path to run.sh
RUN_SH = Path(__file__).parent.parent / 'run.sh'

# Marker for tests that require config/infrastructure
requires_infrastructure = pytest.mark.requires_infrastructure


class TestNoRootRequired:
    """Test CLI does not require root for scenarios using as_root/become."""

    def test_pve_setup_local_no_root_error(self):
        """pve-setup --local should not fail with 'requires root' error."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario', 'run', 'pve-setup', '--local', '--dry-run'],
            capture_output=True,
            text=True
        )
        combined = result.stdout + result.stderr
        assert "requires root privileges" not in combined

    def test_user_setup_local_no_root_error(self):
        """user-setup --local should not fail with 'requires root' error."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario', 'run', 'user-setup', '--local', '--dry-run'],
            capture_output=True,
            text=True
        )
        combined = result.stdout + result.stderr
        assert "requires root privileges" not in combined



class TestListScenarios:
    """Test CLI --list-scenarios works correctly."""

    def test_list_scenarios_shows_all(self):
        """--list-scenarios should show active scenarios."""
        result = subprocess.run(
            [str(RUN_SH), '--list-scenarios'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        output = result.stdout
        # Check active scenarios are listed
        assert 'pve-setup' in output
        assert 'user-setup' in output
        # Only active scenarios should appear
        assert 'packer-build' not in output

    def test_list_scenarios_shows_runtime_estimates(self):
        """--list-scenarios should show runtime estimates."""
        result = subprocess.run(
            [str(RUN_SH), '--list-scenarios'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        output = result.stdout
        # Check runtime estimates are shown (format: ~Nm or ~Ns)
        assert '~3m' in output  # pve-setup, push-vm-roundtrip
        assert '~30s' in output  # user-setup, packer-sync



class TestScenarioVerb:
    """Test 'scenario' verb command."""

    @requires_infrastructure
    def test_scenario_verb_lists_phases(self):
        """scenario verb should list phases like legacy --scenario."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario', 'pve-setup', '--list-phases',
             '--host', 'father', '--skip-preflight'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert 'ensure_pve' in result.stdout

    def test_scenario_verb_no_name_shows_usage(self):
        """scenario verb with no name shows usage."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 1
        assert 'Usage' in result.stdout

    def test_scenario_verb_help_lists_scenarios(self):
        """scenario verb --help lists available scenarios."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario', '--help'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert 'pve-setup' in result.stdout

    def test_no_deprecation_warning_with_verb(self):
        """scenario verb should NOT show deprecation warning."""
        result = subprocess.run(
            [str(RUN_SH), 'scenario', 'pve-setup', '--list-phases',
             '--host', 'father', '--skip-preflight'],
            capture_output=True,
            text=True
        )
        combined = result.stdout + result.stderr
        assert 'deprecated' not in combined.lower()


class TestTopLevelUsage:
    """Test top-level usage display."""

    def test_no_args_shows_usage(self):
        """No arguments shows top-level usage."""
        result = subprocess.run(
            [str(RUN_SH)],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert 'scenario' in result.stdout
        assert 'manifest' in result.stdout
        assert 'config' in result.stdout

    def test_unknown_command_shows_error(self):
        """Unknown command shows error and usage."""
        result = subprocess.run(
            [str(RUN_SH), 'foobar'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 1
        assert 'Unknown command' in result.stdout


class TestTimeoutFlag:
    """Test CLI --timeout flag."""

    def test_timeout_flag_accepted(self):
        """--timeout flag should be accepted by CLI."""
        result = subprocess.run(
            [str(RUN_SH), '--help'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert '--timeout' in result.stdout or '-t' in result.stdout

    @requires_infrastructure
    def test_timeout_shown_in_log(self):
        """Timeout should be shown in log when scenario starts."""
        # Use scenario verb with --dry-run and -H to avoid root check
        result = subprocess.run(
            [str(RUN_SH), 'scenario', 'pve-setup', '-H', 'father',
             '--timeout', '5', '--dry-run', '--skip-preflight'],
            capture_output=True,
            text=True,
            timeout=30
        )
        combined = result.stdout + result.stderr
        # Should show timeout in dry-run output
        assert 'timeout' in combined.lower() or 'Timeout' in combined


class TestVmIdFlag:
    """Test CLI --vm-id flag."""

    def test_vm_id_flag_accepted(self):
        """--vm-id flag should be accepted by CLI."""
        result = subprocess.run(
            [str(RUN_SH), '--help'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 0
        assert '--vm-id' in result.stdout

    @requires_infrastructure
    def test_vm_id_invalid_format_no_equals(self):
        """--vm-id without = should fail with clear error."""
        result = subprocess.run(
            [str(RUN_SH), '--scenario', 'pve-setup', '--host', 'father',
             '--skip-preflight', '--vm-id', 'badformat'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "Invalid --vm-id format" in combined
        assert "Expected NAME=VMID" in combined

    @requires_infrastructure
    def test_vm_id_invalid_format_non_numeric(self):
        """--vm-id with non-numeric ID should fail with clear error."""
        result = subprocess.run(
            [str(RUN_SH), '--scenario', 'pve-setup', '--host', 'father',
             '--skip-preflight', '--vm-id', 'test=abc'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "Invalid --vm-id format" in combined
        assert "VMID must be an integer" in combined

    @requires_infrastructure
    def test_vm_id_empty_name_rejected(self):
        """--vm-id with empty name should fail with clear error."""
        result = subprocess.run(
            [str(RUN_SH), '--scenario', 'pve-setup', '--host', 'father',
             '--skip-preflight', '--vm-id', '=99990'],
            capture_output=True,
            text=True
        )
        assert result.returncode == 1
        combined = result.stdout + result.stderr
        assert "Invalid --vm-id format" in combined
        assert "VM name cannot be empty" in combined

    @requires_infrastructure
    def test_vm_id_valid_format_accepted(self):
        """Valid --vm-id should be accepted (though scenario may fail for other reasons)."""
        result = subprocess.run(
            [str(RUN_SH), '--scenario', 'pve-setup', '--host', 'father',
             '--vm-id', 'test=99990', '--list-phases'],
            capture_output=True,
            text=True
        )
        # --list-phases should succeed even with --vm-id
        assert result.returncode == 0
        assert "Phases for scenario" in result.stdout


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
