"""Recursive scenario execution actions.

Enables running scenarios on remote bootstrapped hosts via SSH with real-time streaming.
"""

import json
import logging
import os
import re
import select
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from common import ActionResult
from config import HostConfig

logger = logging.getLogger(__name__)


@dataclass
class RecursiveScenarioAction:
    """Execute a scenario on a remote bootstrapped host via SSH with real-time streaming.

    This action SSHs to a target host that has been bootstrapped with the homestak CLI,
    runs a scenario using 'homestak scenario <name> --json-output', streams the output
    in real-time, and parses the JSON result at completion.

    The target host must have:
    - homestak CLI installed at ~/bootstrap/homestak
    - Valid config at ~/config/
    - Decrypted secrets

    Attributes:
        name: Action identifier for logging/reporting
        scenario_name: Scenario to execute (e.g., 'vm-roundtrip')
        host_attr: Context key containing target host IP (default: 'node_ip')
        timeout: Overall timeout in seconds (default: 600)
        scenario_args: Additional CLI arguments to pass (e.g., ['--host', 'child-pve'])
        context_keys: Keys to extract from JSON result into context_updates
        use_pty: Whether to use PTY allocation for real-time streaming (default: True)
        ssh_user: SSH username (default: 'root')
    """
    name: str
    scenario_name: str = ''
    host_attr: str = 'node_ip'
    timeout: int = 600
    scenario_args: list[str] = field(default_factory=list)
    context_keys: list[str] = field(default_factory=list)
    use_pty: bool = True
    ssh_user: str = 'root'
    raw_command: str = ''  # When set, replaces the default homestak scenario command

    def run(self, _config: HostConfig, context: dict) -> ActionResult:
        """Execute scenario via SSH with streaming output.

        1. SSH to target host with optional PTY allocation
        2. Run: homestak scenario <name> --json-output [args...]
        3. Stream stdout/stderr to logger in real-time with [action-name] prefix
        4. Parse final JSON result from stdout
        5. Extract context_keys from result into context_updates

        Returns:
            ActionResult with success status, message, duration, and context_updates
        """
        start = time.time()

        # Get target host from context
        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        # Build the remote command
        remote_cmd = self._build_remote_command()

        label = self.scenario_name or self.name
        logger.info(f"[{self.name}] Starting {label} on {host}")
        logger.debug(f"[{self.name}] Command: {remote_cmd}")

        try:
            if self.use_pty:
                result = self._run_with_pty(host, remote_cmd)
            else:
                result = self._run_without_pty(host, remote_cmd)

            rc, output, stderr = result

            # Parse JSON result from output
            json_result = self._parse_json_result(output)

            if rc != 0:
                error_msg = self._extract_error_message(json_result, stderr, output)
                return ActionResult(
                    success=False,
                    message=f"Scenario {self.scenario_name} failed: {error_msg}",
                    duration=time.time() - start
                )

            # Extract context keys
            context_updates = self._extract_context(json_result)

            # Get duration from inner scenario if available
            inner_duration = json_result.get('duration_seconds', 0) if json_result else 0

            return ActionResult(
                success=True,
                message=f"Scenario {self.scenario_name} completed ({inner_duration}s)",
                duration=time.time() - start,
                context_updates=context_updates
            )

        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                message=f"Timeout ({self.timeout}s) waiting for {self.scenario_name}",
                duration=time.time() - start
            )
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Error executing {self.scenario_name}: {e}",
                duration=time.time() - start
            )

    def _build_remote_command(self) -> str:
        """Build the command to run on the remote host.

        When raw_command is set, uses it directly (with serve-repos prefix if active).
        Otherwise builds the default homestak scenario command.

        All arguments are shell-quoted to handle JSON and other special characters
        that may be passed via scenario_args.

        If serve-repos environment variables are set (HOMESTAK_SERVER, HOMESTAK_TOKEN,
        HOMESTAK_REF), they are propagated to the remote command so that child hosts
        can download from the same serve-repos server instead of GitHub.
        """
        # Build env var prefix if serve-repos is active
        env_prefix = self._build_serve_repos_prefix()

        if self.raw_command:
            # Use raw command directly (caller is responsible for quoting)
            cmd = self.raw_command
            if env_prefix:
                return f'{env_prefix} {cmd}'
            return cmd

        cmd_parts = [
            'homestak', 'scenario', self.scenario_name,
            '--json-output'
        ]
        cmd_parts.extend(self.scenario_args)
        # Quote each part to handle special characters (especially JSON with spaces)
        homestak_cmd = ' '.join(shlex.quote(p) for p in cmd_parts)

        if env_prefix:
            return f'{env_prefix} {homestak_cmd}'
        return homestak_cmd

    def _build_serve_repos_prefix(self) -> str:
        """Build environment variable prefix for serve-repos propagation.

        When running with --serve-repos (or _working ref), the outer host has
        HOMESTAK_SERVER, HOMESTAK_TOKEN, and HOMESTAK_REF set. This method
        builds a prefix to pass these to the remote command so that nested
        bootstrap operations use serve-repos instead of GitHub.

        Returns:
            Shell-safe env var assignments (e.g., 'HOMESTAK_SERVER="https://..." ...')
            or empty string if serve-repos is not active.
        """
        serve_repos_vars = ['HOMESTAK_SERVER', 'HOMESTAK_TOKEN', 'HOMESTAK_REF']
        env_parts = []

        for var in serve_repos_vars:
            value = os.environ.get(var)
            if value:
                # Shell-quote the value to handle special characters
                env_parts.append(f'{var}={shlex.quote(value)}')

        if env_parts:
            logger.debug(f"[{self.name}] Propagating serve-repos env vars: {', '.join(serve_repos_vars)}")

        return ' '.join(env_parts)

    def _build_ssh_command(self, host: str, remote_cmd: str) -> list[str]:
        """Build SSH command with appropriate options."""
        ssh_opts = [
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'LogLevel=ERROR',
            '-o', 'ConnectTimeout=30',
        ]

        if self.use_pty:
            ssh_opts.append('-t')  # Force PTY allocation for streaming

        return [
            'ssh',
            *ssh_opts,
            f'{self.ssh_user}@{host}',
            remote_cmd
        ]

    def _run_with_pty(self, host: str, remote_cmd: str) -> tuple[int, str, str]:
        """Execute SSH command with PTY for real-time streaming.

        Uses subprocess with line-by-line reading to stream output as it arrives.
        Output is logged with [action-name] prefix for visibility.
        """
        cmd = self._build_ssh_command(host, remote_cmd)

        # Use unbuffered output for real-time streaming
        with subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
        ) as process:
            output_lines: list[str] = []
            stderr_lines: list[str] = []
            deadline = time.time() + self.timeout

            try:
                # Read output line by line with timeout checking
                while True:
                    # Check timeout
                    if time.time() > deadline:
                        process.kill()
                        raise subprocess.TimeoutExpired(cmd, self.timeout)

                    # Check if process finished
                    if process.poll() is not None:
                        # Read any remaining output
                        remaining_out, remaining_err = process.communicate(timeout=5)
                        if remaining_out:
                            for line in remaining_out.splitlines():
                                self._log_delegate_line(line)
                                output_lines.append(line)
                        if remaining_err:
                            stderr_lines.extend(remaining_err.splitlines())
                        break

                    # Use select for non-blocking read with timeout
                    readable, _, _ = select.select(
                        [process.stdout, process.stderr],
                        [], [],
                        1.0  # 1 second timeout for select
                    )

                    self._read_streams(
                        readable, process.stdout, output_lines, stderr_lines
                    )

                rc = process.returncode

            except Exception:
                process.kill()
                raise

        return rc, '\n'.join(output_lines), '\n'.join(stderr_lines)

    def _run_without_pty(self, host: str, remote_cmd: str) -> tuple[int, str, str]:
        """Execute SSH command without PTY (fallback mode)."""
        cmd = self._build_ssh_command(host, remote_cmd)
        # Remove -t flag if present
        if '-t' in cmd:
            cmd.remove('-t')

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=False,
        )

        # Log output after completion (no streaming)
        for line in result.stdout.splitlines():
            self._log_delegate_line(line)

        return result.returncode, result.stdout, result.stderr

    def _read_streams(self, readable, stdout, output_lines, stderr_lines):
        """Read available data from streams and route to appropriate buffers."""
        for stream in readable:
            if stream is None:
                continue
            line = stream.readline()
            if not line:
                continue
            line = line.rstrip('\n')
            if stream == stdout:
                self._log_delegate_line(line)
                output_lines.append(line)
            else:
                stderr_lines.append(line)

    _json_depth: int = 0

    def _log_delegate_line(self, line: str):
        """Log a line from the delegated scenario with action name prefix.

        JSON output is logged at debug level, phase progress at info level.
        Tracks brace depth so nested JSON objects don't leak to INFO.
        """
        # Skip empty lines
        if not line.strip():
            return

        stripped = line.strip()

        # Track JSON brace depth
        if stripped.startswith('{') and self._json_depth == 0:
            self._json_depth = 1
        elif self._json_depth > 0:
            self._json_depth += stripped.count('{') - stripped.count('}')

        if self._json_depth > 0 or (self._json_depth == 0 and stripped == '}'):
            logger.debug(f"[{self.name}] {line}")
            return

        # Log phase progress and other output at info level
        logger.info(f"[{self.name}] {line}")

    def _parse_json_result(self, output: str) -> dict | None:
        """Parse JSON result from scenario output.

        The JSON is expected to be at the end of the output, possibly after
        log messages. We search for the last valid JSON object.
        """
        if not output:
            return None

        # First, try the entire output as JSON (if it's just JSON)
        try:
            parsed: dict = json.loads(output.strip())
            return parsed
        except json.JSONDecodeError:
            pass

        # Look for JSON starting from the end of output
        # Try each line from the end to find JSON
        lines = output.strip().split('\n')

        # Strategy 1: Try each line from the end as potential single-line JSON
        for line in reversed(lines):
            stripped = line.strip()
            if stripped.startswith('{') and stripped.endswith('}'):
                try:
                    parsed = json.loads(stripped)
                    return parsed
                except json.JSONDecodeError:
                    continue

        # Strategy 2: Look for multi-line JSON block
        json_lines: list[str] = []
        brace_count = 0
        in_json = False

        for line in reversed(lines):
            stripped = line.strip()

            if not in_json:
                if stripped.endswith('}'):
                    in_json = True
                    brace_count = stripped.count('}') - stripped.count('{')
                    json_lines.insert(0, line)
                    # Check if this single line is complete JSON
                    if brace_count == 0 and stripped.startswith('{'):
                        try:
                            parsed = json.loads(stripped)
                            return parsed
                        except json.JSONDecodeError:
                            pass
            else:
                json_lines.insert(0, line)
                brace_count += stripped.count('}') - stripped.count('{')

                if brace_count <= 0 and stripped.startswith('{'):
                    break

        if json_lines:
            json_str = '\n'.join(json_lines)
            try:
                parsed = json.loads(json_str)
                return parsed
            except json.JSONDecodeError:
                logger.debug(f"Failed to parse JSON: {json_str[:200]}")

        return None

    def _extract_error_message(
        self,
        json_result: dict | None,
        stderr: str,
        stdout: str
    ) -> str:
        """Extract a meaningful error message from available output."""
        # First priority: error from JSON result
        if json_result and 'error' in json_result:
            # Strip ANSI escape codes from tofu/other tool output
            return re.sub(r'\x1b\[[0-9;]*m', '', str(json_result['error']))

        # Second: look for failed phase in JSON
        if json_result and 'phases' in json_result:
            for phase in json_result['phases']:
                if phase.get('status') == 'failed':
                    return f"Phase '{phase['name']}' failed"

        # Third: stderr content
        if stderr and stderr.strip():
            # Return last non-empty line of stderr
            lines = [l for l in stderr.strip().split('\n') if l.strip()]
            if lines:
                return lines[-1][:200]

        # Fourth: look for error patterns in stdout
        for line in reversed(stdout.split('\n')):
            if 'error' in line.lower() or 'failed' in line.lower():
                return line.strip()[:200]

        return "Unknown error (no details available)"

    def _extract_context(self, json_result: dict | None) -> dict:
        """Extract specified context keys from JSON result.

        Supports two JSON output formats:
        1. Scenario format: {"context": {"key": "value"}}
        2. Verb command format: {"nodes": [{"name": "x", "ip": "...", "vm_id": N}]}

        For verb format, builds context keys as {name}_ip and {name}_vm_id.
        """
        context_updates: dict[str, object] = {}

        if not json_result:
            return context_updates

        # Extract from context field if present (scenario format)
        result_context = json_result.get('context', {})

        # Also extract from nodes[] array (verb command format)
        for node in json_result.get('nodes', []):
            name = node.get('name')
            if not name:
                continue
            if node.get('ip'):
                result_context[f'{name}_ip'] = node['ip']
            if node.get('vm_id'):
                result_context[f'{name}_vm_id'] = node['vm_id']

        for key in self.context_keys:
            if key in result_context:
                context_updates[key] = result_context[key]
                logger.debug(f"Extracted context key '{key}': {result_context[key]}")
            else:
                logger.debug(f"Context key '{key}' not found in result")

        return context_updates
