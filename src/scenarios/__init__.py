"""Scenario definitions and orchestration."""

import logging
import time
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable

from config import HostConfig
from reporting import TestReport

logger = logging.getLogger(__name__)


@runtime_checkable
class Scenario(Protocol):
    """Protocol for scenario definitions.

    Class attributes:
        name: Scenario identifier (e.g., 'pve-setup')
        description: Human-readable description
        requires_root: If True, --local mode requires root privileges (default: False)
        requires_host_config: If True, --host must resolve to valid node config (default: True)
        expected_runtime: Expected runtime in seconds for --list-scenarios display (default: None)
    """
    name: str
    description: str
    # Optional attributes with defaults checked in CLI
    # requires_root: bool = False
    # requires_host_config: bool = True
    # expected_runtime: int = None  # seconds

    def get_phases(self, config: HostConfig) -> list[tuple[str, Any, str]]:
        """Return list of (phase_name, action, description) tuples."""
        ...  # pylint: disable=unnecessary-ellipsis


class Orchestrator:
    """Coordinates scenario execution."""

    def __init__(
        self,
        scenario: Scenario,
        config: HostConfig,
        report_dir: Path,
        skip_phases: Optional[list[str]] = None,
        timeout: Optional[int] = None,
        dry_run: bool = False
    ):
        self.scenario = scenario
        self.config = config
        self.report_dir = report_dir
        self.skip_phases = skip_phases or []
        self.timeout = timeout  # Overall scenario timeout in seconds
        self.dry_run = dry_run
        self.report = TestReport(host=config.name, report_dir=report_dir, scenario=scenario.name)
        self.context: dict[str, Any] = {}

    def preview(self) -> bool:
        """Show what would be executed without running. Returns True."""
        phases = self.scenario.get_phases(self.config)

        print("")
        print("═══════════════════════════════════════════════════════════════")
        print(f"  DRY-RUN: {self.scenario.name}")
        print(f"  Host: {self.config.name}")
        print("═══════════════════════════════════════════════════════════════")
        print("")

        print("Phases to execute:")
        phase_count = 0
        skip_count = 0

        for phase_name, action, description in phases:
            action_type = type(action).__name__
            if phase_name in self.skip_phases:
                print(f"  [SKIP] {phase_name}: {description}")
                print(f"         Action: {action_type}")
                skip_count += 1
            else:
                print(f"  [ OK ] {phase_name}: {description}")
                print(f"         Action: {action_type}")

                # Show action details if available
                if hasattr(action, 'name'):
                    print(f"         Name: {action.name}")
                if hasattr(action, 'playbook'):
                    print(f"         Playbook: {action.playbook}")
                if hasattr(action, 'env_name'):
                    print(f"         Environment: {action.env_name}")
                if hasattr(action, 'timeout'):
                    print(f"         Timeout: {action.timeout}s")
                phase_count += 1
            print("")

        print("═══════════════════════════════════════════════════════════════")
        print(f"  Summary: {phase_count} phases to execute, {skip_count} to skip")
        if self.timeout:
            print(f"  Timeout: {self.timeout}s")
        print("  Mode: DRY-RUN (no changes made)")
        print("═══════════════════════════════════════════════════════════════")
        print("")
        print("Remove --dry-run to execute the scenario.")
        print("")

        return True

    def run(self) -> bool:
        """Run all phases. Returns True if all passed."""
        # Handle dry-run mode
        if self.dry_run:
            return self.preview()

        timeout_msg = f" (timeout: {self.timeout}s)" if self.timeout else ""
        logger.info(f"Starting scenario '{self.scenario.name}' on host: {self.config.name}{timeout_msg}")
        self.report.start()

        phases = self.scenario.get_phases(self.config)
        all_passed = True
        start_time = time.time()
        last_failed_phase = ''
        last_failed_message = ''

        for phase_name, action, description in phases:
            # Check timeout before starting each phase
            if self.timeout:
                elapsed = time.time() - start_time
                if elapsed >= self.timeout:
                    logger.error(f"Scenario timeout ({self.timeout}s) exceeded after {elapsed:.1f}s")
                    self.report.fail_phase(phase_name, f"Timeout exceeded ({elapsed:.1f}s >= {self.timeout}s)", 0)
                    all_passed = False
                    last_failed_phase = phase_name
                    last_failed_message = f"Timeout exceeded ({elapsed:.1f}s)"
                    break

            if phase_name in self.skip_phases:
                logger.info(f"Skipping phase: {phase_name}")
                self.report.skip_phase(phase_name, description)
                continue

            logger.info(f"Running phase: {phase_name} - {description}")
            self.report.start_phase(phase_name, description)

            try:
                result = action.run(self.config, self.context)
                if result.success:
                    logger.info(f"Phase {phase_name} passed")
                    self.report.pass_phase(phase_name, result.message, result.duration)
                    self.context.update(result.context_updates or {})
                else:
                    logger.error(f"Phase {phase_name} failed: {result.message}")
                    self.report.fail_phase(phase_name, result.message, result.duration)
                    all_passed = False
                    last_failed_phase = phase_name
                    last_failed_message = result.message
                    if not result.continue_on_failure:
                        break
            except Exception as e:
                logger.exception(f"Phase {phase_name} raised exception")
                self.report.fail_phase(phase_name, str(e), 0)
                all_passed = False
                last_failed_phase = phase_name
                last_failed_message = str(e)
                break

        total_time = time.time() - start_time
        logger.info(f"Scenario completed in {total_time:.1f}s")
        self.report.finish(all_passed)

        # Call scenario's on_failure callback if present
        if not all_passed and hasattr(self.scenario, 'on_failure'):
            self.context['_failed_phase'] = last_failed_phase
            self.context['_failed_message'] = last_failed_message
            try:
                self.scenario.on_failure(self.config, self.context)
            except Exception:
                logger.exception("Scenario on_failure callback raised exception")

        return all_passed


# Registry of available scenarios
_scenarios: dict[str, type[Scenario]] = {}


def register_scenario(cls: type[Scenario]) -> type[Scenario]:
    """Decorator to register a scenario class."""
    _scenarios[cls.name] = cls
    return cls


def get_scenario(name: str) -> Scenario:
    """Get a scenario instance by name."""
    if name not in _scenarios:
        available = list(_scenarios.keys())
        raise ValueError(f"Unknown scenario: {name}. Available: {available}")
    return _scenarios[name]()


def list_scenarios() -> list[str]:
    """List available scenario names."""
    return sorted(_scenarios.keys())


# Import scenarios to trigger registration
from scenarios import pve_setup  # noqa: E402, F401  # pylint: disable=wrong-import-position
from scenarios import pve_config  # noqa: E402, F401  # pylint: disable=wrong-import-position
from scenarios import user_setup  # noqa: E402, F401  # pylint: disable=wrong-import-position
from scenarios import vm_roundtrip  # noqa: E402, F401  # pylint: disable=wrong-import-position
