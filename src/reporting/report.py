"""Test reporting and logging."""

import json
import socket
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional


def _get_current_branch() -> str:
    """Get current git branch, or 'unknown' if not in a repo."""
    try:
        result = subprocess.run(
            ['git', 'branch', '--show-current'],
            capture_output=True, text=True, timeout=5, check=False
        )
        return result.stdout.strip() or 'detached'
    except Exception:
        return 'unknown'


@dataclass
class PhaseResult:
    """Result of a test phase."""
    name: str
    description: str
    status: str  # 'passed', 'failed', 'skipped'
    message: str = ''
    duration: float = 0.0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


@dataclass
class TestReport:
    """Collects and generates test reports."""
    host: str
    report_dir: Path
    scenario: str = ''
    phases: list[PhaseResult] = field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    success: bool = False

    _current_phase: Optional[str] = field(default=None, repr=False)
    _phase_start: Optional[datetime] = field(default=None, repr=False)

    def start(self):
        """Mark test run start."""
        self.started_at = datetime.now()
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def start_phase(self, name: str, _description: str):
        """Mark phase start."""
        self._current_phase = name
        self._phase_start = datetime.now()

    def pass_phase(self, name: str, message: str = '', duration: float = 0.0):
        """Record passed phase."""
        self._record_phase(name, 'passed', message, duration)

    def fail_phase(self, name: str, message: str = '', duration: float = 0.0):
        """Record failed phase."""
        self._record_phase(name, 'failed', message, duration)

    def skip_phase(self, name: str, description: str):
        """Record skipped phase."""
        self.phases.append(PhaseResult(
            name=name,
            description=description,
            status='skipped'
        ))

    def _record_phase(self, name: str, status: str, message: str, duration: float):
        """Record phase result."""
        now = datetime.now()
        if duration == 0.0 and self._phase_start:
            duration = (now - self._phase_start).total_seconds()

        # Find phase description from phases list
        desc = name
        for p in self.phases:
            if p.name == name:
                desc = p.description
                break

        self.phases.append(PhaseResult(
            name=name,
            description=desc,
            status=status,
            message=message,
            duration=duration,
            started_at=self._phase_start,
            finished_at=now
        ))
        self._current_phase = None
        self._phase_start = None

    def finish(self, success: bool):
        """Finalize report and write files."""
        self.finished_at = datetime.now()
        self.success = success
        self._write_json()
        self._write_markdown()

    def _write_json(self):
        """Write JSON report."""
        data = {
            'scenario': self.scenario,
            'host': self.host,
            'hostname': socket.gethostname(),
            'branch': _get_current_branch(),
            'success': self.success,
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'finished_at': self.finished_at.isoformat() if self.finished_at else None,
            'duration': (self.finished_at - self.started_at).total_seconds() if self.finished_at and self.started_at else 0,
            'phases': [
                {
                    'name': p.name,
                    'description': p.description,
                    'status': p.status,
                    'message': p.message,
                    'duration': p.duration
                }
                for p in self.phases
            ]
        }
        filename = self._report_filename('json')
        with open(filename, 'w', encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def _write_markdown(self):
        """Write markdown report."""
        status = 'PASSED' if self.success else 'FAILED'
        duration = (self.finished_at - self.started_at).total_seconds() if self.finished_at and self.started_at else 0

        lines = [
            f"# {self.scenario}",
            "",
            f"**Host**: {self.host}",
            f"**Status**: {status}",
            f"**Date**: {self.started_at.strftime('%Y-%m-%d %H:%M:%S') if self.started_at else 'N/A'}",
            f"**Duration**: {duration:.1f}s",
            "",
            "## Phases",
            "",
            "| Phase | Status | Duration | Message |",
            "|-------|--------|----------|---------|",
        ]

        for p in self.phases:
            status_emoji = {'passed': '✅', 'failed': '❌', 'skipped': '⏭️'}.get(p.status, '❓')
            lines.append(f"| {p.name} | {status_emoji} {p.status} | {p.duration:.1f}s | {p.message} |")

        lines.extend(["", "---", f"Generated: {datetime.now().isoformat()}"])

        filename = self._report_filename('md')
        with open(filename, 'w', encoding="utf-8") as f:
            f.write('\n'.join(lines))

    def _report_filename(self, ext: str) -> Path:
        """Generate report filename.

        Format: YYYYMMDD-HHMMSS.{type}.{subject}.{status}.{ext}
        """
        timestamp = self.started_at.strftime('%Y%m%d-%H%M%S') if self.started_at else 'unknown'
        status = 'passed' if self.success else 'failed'
        # Extract manifest name from scenario (e.g., 'manifest-test-n1-push' → 'n1-push')
        subject = self.scenario
        if subject.startswith('manifest-test-'):
            subject = subject[len('manifest-test-'):]
        subject = subject.replace('/', '-') if subject else 'unknown'
        return self.report_dir / f"{timestamp}.manifest.{subject}.{status}.{ext}"

    def to_dict(self, context: Optional[dict] = None) -> dict:
        """Return report as dictionary for JSON output.

        Args:
            context: Optional context dict to include in output.
                     Only JSON-serializable values are included.
        """
        duration = (self.finished_at - self.started_at).total_seconds() if self.finished_at and self.started_at else 0

        result = {
            'scenario': self.scenario,
            'success': self.success,
            'duration_seconds': round(duration, 1),
            'phases': [
                {
                    'name': p.name,
                    'status': p.status,
                    'duration': round(p.duration, 1),
                }
                for p in self.phases
            ]
        }

        # Include error message on failure
        if not self.success:
            for p in self.phases:
                if p.status == 'failed' and p.message:
                    result['error'] = p.message
                    break

        # Include context if provided
        if context:
            # Filter to JSON-serializable values
            serializable_context = {}
            for key, value in context.items():
                # Skip internal/private keys
                if key.startswith('_'):
                    continue
                try:
                    json.dumps(value)
                    serializable_context[key] = value
                except (TypeError, ValueError):
                    # Skip non-serializable values
                    pass
            if serializable_context:
                result['context'] = serializable_context

        return result
