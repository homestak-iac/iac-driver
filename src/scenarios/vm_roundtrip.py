"""Spec VM lifecycle scenarios.

Validates the Create → Specify integration: VMs provisioned via tofu
receive spec server environment variables via cloud-init.

Includes push (verify env vars) and pull (verify autonomous config) modes.
"""

import logging
import time
from dataclasses import dataclass

from actions import (
    TofuApplyAction,
    TofuDestroyAction,
    StartProvisionedVMsAction,
    WaitForProvisionedVMsAction,
    WaitForSSHAction,
    WaitForFileAction,
)
from actions.pve_lifecycle import EnsureImageAction
from common import ActionResult, run_ssh
from config import HostConfig, get_site_config_dir
from scenarios import register_scenario

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


@dataclass
class CheckSpecServerConfigAction:
    """Verify spec_server is configured in site.yaml."""
    name: str

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Check that spec_server is configured."""
        start = time.time()

        try:
            site_file = get_site_config_dir() / 'site.yaml'
            with open(site_file, encoding='utf-8') as f:
                site_config = yaml.safe_load(f) or {}
            spec_server = site_config.get('defaults', {}).get('spec_server', '')

            if not spec_server:
                return ActionResult(
                    success=False,
                    message="spec_server not configured in site.yaml. "
                            "Set defaults.spec_server to enable Create → Specify flow.",
                    duration=time.time() - start
                )

            logger.info(f"[{self.name}] spec_server configured: {spec_server}")
            return ActionResult(
                success=True,
                message=f"spec_server: {spec_server}",
                duration=time.time() - start,
                context_updates={'spec_server_url': spec_server}
            )
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Failed to read site.yaml: {e}",
                duration=time.time() - start
            )


@dataclass
class StartServerAction:
    """Start server daemon on remote host via SSH."""
    name: str
    server_port: int = 44443
    timeout: int = 30
    serve_repos: bool = False
    repo_token: str | None = None  # None = don't pass flag, "" = disable auth

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Start server on PVE host via ./run.sh server start."""
        start = time.time()

        pve_host = config.ssh_host
        ssh_user = config.automation_user
        iac_dir = '~/iac/iac-driver'

        # Check if iac-driver exists on remote host
        check_cmd = f'test -f {iac_dir}/run.sh && echo FOUND || echo NOT_FOUND'
        rc, out, err = run_ssh(pve_host, check_cmd, user=ssh_user, timeout=10)
        if 'NOT_FOUND' in out:
            return ActionResult(
                success=False,
                message=f"iac-driver not found at {iac_dir}. Run bootstrap first.",
                duration=time.time() - start
            )

        # Check if already running via server status
        status_cmd = f'cd {iac_dir} && ./run.sh server status --port {self.server_port} --json 2>/dev/null || true'
        rc, out, _ = run_ssh(pve_host, status_cmd, user=ssh_user, timeout=10)
        try:
            import json as _json
            status = _json.loads(out.strip())
            if status.get('running') and status.get('healthy'):
                pid = status.get('pid', '?')
                logger.info(f"[{self.name}] Server already running and healthy (PID {pid}, port {self.server_port})")
                return ActionResult(
                    success=True,
                    message=f"Server already running (PID {pid}, port {self.server_port})",
                    duration=time.time() - start,
                    context_updates={'spec_server_pid': str(pid)}
                )
        except (ValueError, TypeError):
            pass  # Status check failed, proceed with start

        # Build start command flags
        start_flags = f'--port {self.server_port}'
        if self.serve_repos:
            start_flags += ' --repos'
            if self.repo_token is not None:
                start_flags += f" --repo-token '{self.repo_token}'"

        # Start server daemon — blocks until health check passes, then returns
        start_cmd = f'cd {iac_dir} && ./run.sh server start {start_flags}'
        logger.info(f"[{self.name}] Starting server on {pve_host}:{self.server_port}...")
        rc, out, err = run_ssh(pve_host, start_cmd, user=ssh_user, timeout=self.timeout)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Server failed to start (rc={rc}): {out} {err}",
                duration=time.time() - start
            )

        # Extract PID from output (format: "Server started (PID 12345, port 44443)")
        pid = '?'
        if 'PID' in out:
            try:
                pid = out.split('PID ')[1].split(',')[0].strip()
            except (IndexError, ValueError):
                pass

        return ActionResult(
            success=True,
            message=f"Server started (PID {pid}, port {self.server_port})",
            duration=time.time() - start,
            context_updates={'spec_server_pid': pid}
        )


@dataclass
class VerifyEnvVarsAction:
    """Verify HOMESTAK_* env vars are present in /etc/profile.d/homestak.sh."""
    name: str
    host_key: str = 'vm_ip'
    timeout: int = 30

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Check that env vars were injected by cloud-init."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        # Read the profile.d file
        cmd = 'cat /etc/profile.d/homestak.sh 2>/dev/null || echo "FILE_NOT_FOUND"'
        logger.info(f"[{self.name}] Checking env vars on {host}...")
        _, out, _ = run_ssh(host, cmd, user=config.automation_user, timeout=self.timeout)

        if 'FILE_NOT_FOUND' in out:
            return ActionResult(
                success=False,
                message="/etc/profile.d/homestak.sh not found - cloud-init may not have run",
                duration=time.time() - start
            )

        # Check for required env vars
        required_vars = ['HOMESTAK_SERVER', 'HOMESTAK_TOKEN']
        missing = []
        for var in required_vars:
            if var not in out:
                missing.append(var)

        if missing:
            return ActionResult(
                success=False,
                message=f"Missing env vars: {', '.join(missing)}. Content: {out[:200]}",
                duration=time.time() - start
            )

        # Extract values for logging
        lines = out.strip().split('\n')
        env_summary = '; '.join(l.strip() for l in lines if l.strip() and not l.startswith('#'))

        return ActionResult(
            success=True,
            message=f"Env vars present: {env_summary[:100]}",
            duration=time.time() - start,
            context_updates={'homestak_env_content': out.strip()}
        )


@dataclass
class VerifyServerReachableAction:
    """Verify spec server is reachable from VM."""
    name: str
    host_key: str = 'vm_ip'
    timeout: int = 30

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Check that VM can reach the spec server."""
        start = time.time()

        host = context.get(self.host_key)
        spec_server = context.get('spec_server_url')

        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        if not spec_server:
            return ActionResult(
                success=False,
                message="No spec_server_url in context",
                duration=time.time() - start
            )

        # Curl the health endpoint (allow self-signed cert)
        cmd = f'curl -sk {spec_server}/health 2>&1 || echo "CURL_FAILED"'
        logger.info(f"[{self.name}] Testing connectivity to {spec_server} from {host}...")
        rc, out, _ = run_ssh(host, cmd, user=config.automation_user, timeout=self.timeout)

        if 'CURL_FAILED' in out or rc != 0:
            return ActionResult(
                success=False,
                message=f"Cannot reach spec server from VM: {out}",
                duration=time.time() - start
            )

        # Check for expected health response
        if 'ok' in out.lower() or 'healthy' in out.lower() or '"status"' in out:
            return ActionResult(
                success=True,
                message=f"Spec server reachable: {out.strip()[:50]}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Spec server responded: {out.strip()[:50]}",
            duration=time.time() - start
        )


@dataclass
class StopServerAction:
    """Stop server daemon on remote host via SSH."""
    name: str
    server_port: int = 44443
    timeout: int = 30

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Stop server via ./run.sh server stop."""
        start = time.time()

        pve_host = config.ssh_host
        ssh_user = config.automation_user
        iac_dir = '~/iac/iac-driver'

        stop_cmd = f'cd {iac_dir} && ./run.sh server stop --port {self.server_port}'
        logger.info(f"[{self.name}] Stopping server on {pve_host}:{self.server_port}...")
        rc, out, err = run_ssh(pve_host, stop_cmd, user=ssh_user, timeout=self.timeout)

        if rc != 0:
            logger.warning(f"[{self.name}] server stop returned rc={rc}: {out} {err}")

        return ActionResult(
            success=True,
            message=out.strip() if out.strip() else "Server stopped",
            duration=time.time() - start
        )


@dataclass
class VerifyPackagesAction:
    """Verify expected packages are installed on a VM."""
    name: str
    packages: tuple
    host_key: str = 'vm_ip'
    timeout: int = 30

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Check that packages are installed via dpkg."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        missing = []
        for pkg in self.packages:
            cmd = f'dpkg -s {pkg} 2>/dev/null | grep -q "Status: install ok installed" && echo INSTALLED || echo MISSING'
            rc, out, _ = run_ssh(host, cmd, user=config.automation_user, timeout=self.timeout)
            if 'MISSING' in out or rc != 0:
                missing.append(pkg)

        if missing:
            return ActionResult(
                success=False,
                message=f"Packages not installed: {', '.join(missing)}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"All packages installed: {', '.join(self.packages)}",
            duration=time.time() - start
        )


@dataclass
class VerifyUserAction:
    """Verify expected user exists on a VM."""
    name: str
    username: str
    host_key: str = 'vm_ip'
    timeout: int = 30

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Check that user exists via id command."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        cmd = f'id {self.username} 2>/dev/null && echo USER_EXISTS || echo USER_MISSING'
        rc, out, _ = run_ssh(host, cmd, user=config.automation_user, timeout=self.timeout)

        if 'USER_MISSING' in out or rc != 0:
            return ActionResult(
                success=False,
                message=f"User '{self.username}' not found",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"User '{self.username}' exists: {out.strip().splitlines()[0][:60]}",
            duration=time.time() - start
        )


@register_scenario
class SpecVMPushRoundtrip:
    """Test Create → Specify flow (push): provision VM, verify spec server integration."""

    name = 'push-vm-roundtrip'
    description = 'Deploy VM with spec server vars, verify env injection via SSH, destroy'
    expected_runtime = 180  # ~3 min

    def get_phases(self, _config: HostConfig) -> list[tuple[str, object, str]]:
        """Return phases for spec VM push roundtrip test."""
        return [
            # Prerequisites
            ('check_config', CheckSpecServerConfigAction(
                name='check-spec-config',
            ), 'Verify spec_server configured'),

            ('start_server', StartServerAction(
                name='start-spec-server',
            ), 'Start spec discovery server'),

            # Standard VM provisioning
            ('ensure_image', EnsureImageAction(
                name='ensure-image',
            ), 'Ensure packer image exists'),

            ('provision', TofuApplyAction(
                name='provision-vm',
                vm_name='test',
                vmid=99900,
                vm_preset='vm-small',
                image='debian-12',
                spec='base',
            ), 'Provision VM(s)'),

            ('start', StartProvisionedVMsAction(
                name='start-vms',
            ), 'Start VM(s)'),

            ('wait_ip', WaitForProvisionedVMsAction(
                name='wait-for-ips',
                timeout=180,
            ), 'Wait for VM IP(s)'),

            ('verify_ssh', WaitForSSHAction(
                name='verify-ssh',
                host_key='vm_ip',
                timeout=120,
            ), 'Verify SSH access'),

            # Spec-specific verification
            ('verify_env', VerifyEnvVarsAction(
                name='verify-env-vars',
                host_key='vm_ip',
            ), 'Verify HOMESTAK_* env vars'),

            ('verify_server', VerifyServerReachableAction(
                name='verify-server-reachable',
                host_key='vm_ip',
            ), 'Verify spec server reachable'),

            # Cleanup
            ('destroy', TofuDestroyAction(
                name='destroy-vm',
                vm_name='test',
                vmid=99900,
                vm_preset='vm-small',
                image='debian-12',
            ), 'Destroy VM(s)'),

            ('stop_server', StopServerAction(
                name='stop-spec-server',
            ), 'Stop spec discovery server'),
        ]


@register_scenario
class SpecVMPullRoundtrip:
    """Test Create → Config flow (pull): VM self-configures, driver verifies."""

    name = 'pull-vm-roundtrip'
    description = 'Deploy VM with pull mode, verify autonomous spec fetch + config apply, destroy'
    expected_runtime = 300  # ~5 min (includes waiting for cloud-init config)

    def get_phases(self, _config: HostConfig) -> list[tuple[str, object, str]]:
        """Return phases for spec VM pull roundtrip test."""
        return [
            # Prerequisites
            ('check_config', CheckSpecServerConfigAction(
                name='check-spec-config',
            ), 'Verify spec_server configured'),

            ('start_server', StartServerAction(
                name='start-spec-server',
                serve_repos=True,
                repo_token='',  # Disable auth for dev posture (network trust)
            ), 'Start spec + repo server'),

            # Standard VM provisioning
            ('ensure_image', EnsureImageAction(
                name='ensure-image',
            ), 'Ensure packer image exists'),

            ('provision', TofuApplyAction(
                name='provision-vm',
                vm_name='edge',
                vmid=99950,
                vm_preset='vm-small',
                image='debian-12',
                spec='base',
            ), 'Provision VM(s)'),

            ('start', StartProvisionedVMsAction(
                name='start-vms',
            ), 'Start VM(s)'),

            ('wait_ip', WaitForProvisionedVMsAction(
                name='wait-for-ips',
                timeout=180,
            ), 'Wait for VM IP(s)'),

            ('verify_ssh', WaitForSSHAction(
                name='verify-ssh',
                host_key='vm_ip',
                timeout=120,
            ), 'Verify SSH access'),

            # Pull mode verification: VM autonomously fetches spec and applies config
            ('wait_spec', WaitForFileAction(
                name='wait-spec-file',
                host_key='vm_ip',
                file_path='~/config/state/spec.yaml',
                timeout=150,
                interval=10,
            ), 'Wait for spec fetch (pull)'),

            ('wait_config', WaitForFileAction(
                name='wait-config-complete',
                host_key='vm_ip',
                file_path='~/config/state/config-complete.json',
                timeout=180,
                interval=10,
            ), 'Wait for config complete (pull)'),

            # Verify config was applied correctly
            ('verify_packages', VerifyPackagesAction(
                name='verify-packages',
                host_key='vm_ip',
                packages=('htop', 'curl'),
            ), 'Verify packages installed'),

            ('verify_user', VerifyUserAction(
                name='verify-user',
                host_key='vm_ip',
                username='homestak',
            ), 'Verify user created'),

            # Cleanup
            ('destroy', TofuDestroyAction(
                name='destroy-vm',
                vm_name='edge',
                vmid=99950,
                vm_preset='vm-small',
                image='debian-12',
            ), 'Destroy VM(s)'),

            ('stop_server', StopServerAction(
                name='stop-spec-server',
            ), 'Stop spec discovery server'),
        ]
