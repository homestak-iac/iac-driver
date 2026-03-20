"""Ansible playbook actions."""

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from common import ActionResult, run_command, run_ssh, wait_for_ssh
from config import HostConfig, get_sibling_dir
from config_resolver import ConfigResolver

logger = logging.getLogger(__name__)


def _append_ansible_vars(cmd: list, vars_dict: dict) -> None:
    """Append variables to ansible-playbook command.

    Uses JSON body format (-e '{"key": [...]}') for list/dict types
    so ansible parses them correctly. The key=value format always
    treats values as strings, which breaks Jinja2 filters like join().
    """
    for key, value in vars_dict.items():
        if isinstance(value, (list, dict)):
            cmd.extend(['-e', json.dumps({key: value})])
        elif isinstance(value, bool):
            cmd.extend(['-e', f'{key}={str(value).lower()}'])
        else:
            cmd.extend(['-e', f'{key}={value}'])


@dataclass
class AnsiblePlaybookAction:
    """Run an ansible playbook."""
    name: str
    playbook: str  # e.g., "playbooks/pve-install.yml"
    inventory: str = "inventory/remote-dev.yml"
    extra_vars: dict = field(default_factory=dict)
    host_key: str = 'node_ip'  # context key for ansible_host
    wait_for_ssh_before: bool = True
    wait_for_ssh_after: bool = False
    ssh_timeout: int = 60
    timeout: int = 600
    # Site-config integration (v0.17+)
    use_site_config: bool = False
    env: Optional[str] = None  # Environment for posture resolution (e.g., 'dev', 'test')

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Execute ansible playbook."""
        start = time.time()

        target_host = context.get(self.host_key)
        if not target_host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        ansible_dir = get_sibling_dir('ansible')
        if not ansible_dir.exists():
            return ActionResult(
                success=False,
                message=f"Ansible directory not found: {ansible_dir}",
                duration=time.time() - start
            )

        # Wait for SSH if requested
        if self.wait_for_ssh_before:
            if not wait_for_ssh(target_host, timeout=self.ssh_timeout):
                return ActionResult(
                    success=False,
                    message=f"SSH not available on {target_host}",
                    duration=time.time() - start
                )

        # Resolve config vars if enabled
        resolved_vars = {}
        if self.use_site_config and self.env:
            try:
                resolver = ConfigResolver()
                resolved_vars = resolver.resolve_ansible_vars(self.env)
                logger.info(f"[{self.name}] Resolved config vars for env '{self.env}'")
            except Exception as e:
                logger.warning(f"[{self.name}] Failed to resolve config: {e}")

        # Build command
        logger.info(f"[{self.name}] Running {self.playbook} on {target_host}...")
        cmd = [
            'ansible-playbook',
            '-i', self.inventory,
            self.playbook,
            '-e', f'ansible_host={target_host}',
            '-e', f'ansible_user={config.ssh_user}'
        ]

        # Add resolved config vars first (extra_vars can override)
        _append_ansible_vars(cmd, resolved_vars)

        # Add extra vars (these override config)
        _append_ansible_vars(cmd, self.extra_vars)

        rc, out, err = run_command(cmd, cwd=ansible_dir, timeout=self.timeout)
        if rc != 0:
            # Truncate error message for readability
            error_msg = err[-500:] if err else out[-500:]
            return ActionResult(
                success=False,
                message=f"{self.playbook} failed: {error_msg}",
                duration=time.time() - start
            )

        # Wait for SSH after reboot if requested
        if self.wait_for_ssh_after:
            logger.info(f"[{self.name}] Waiting for SSH after playbook...")
            if not wait_for_ssh(target_host, timeout=self.ssh_timeout * 2):
                return ActionResult(
                    success=False,
                    message=f"SSH not available after reboot on {target_host}",
                    duration=time.time() - start
                )

        return ActionResult(
            success=True,
            message=f"{self.playbook} completed on {target_host}",
            duration=time.time() - start
        )


@dataclass
class AnsibleLocalPlaybookAction:
    """Run an ansible playbook locally."""
    name: str
    playbook: str  # e.g., "playbooks/pve-setup.yml"
    inventory: str = "inventory/local.yml"
    extra_vars: dict = field(default_factory=dict)
    timeout: int = 600
    # Site-config integration (v0.17+)
    use_site_config: bool = False
    env: Optional[str] = None  # Environment for posture resolution (e.g., 'dev', 'test')

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Execute ansible playbook locally."""
        start = time.time()

        ansible_dir = get_sibling_dir('ansible')
        if not ansible_dir.exists():
            return ActionResult(
                success=False,
                message=f"Ansible directory not found: {ansible_dir}",
                duration=time.time() - start
            )

        # Resolve config vars if enabled
        resolved_vars = {}
        if self.use_site_config and self.env:
            try:
                resolver = ConfigResolver()
                resolved_vars = resolver.resolve_ansible_vars(self.env)
                logger.info(f"[{self.name}] Resolved config vars for env '{self.env}'")
            except Exception as e:
                logger.warning(f"[{self.name}] Failed to resolve config: {e}")

        # Build command
        logger.info(f"[{self.name}] Running {self.playbook} locally...")
        cmd = [
            'ansible-playbook',
            '-i', self.inventory,
            self.playbook,
        ]

        # Add resolved config vars first (extra_vars can override)
        _append_ansible_vars(cmd, resolved_vars)

        # Add extra vars (these override config)
        _append_ansible_vars(cmd, self.extra_vars)

        rc, out, err = run_command(cmd, cwd=ansible_dir, timeout=self.timeout)
        if rc != 0:
            # Truncate error message for readability
            error_msg = err[-500:] if err else out[-500:]
            return ActionResult(
                success=False,
                message=f"{self.playbook} failed: {error_msg}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"{self.playbook} completed locally",
            duration=time.time() - start
        )


@dataclass
class EnsurePVEAction:
    """Idempotent PVE installation - checks if PVE running before installing.

    Checks if pveproxy service is active. If so, skips installation.
    Otherwise, runs pve-install.yml playbook.
    """
    name: str
    host_key: str = 'node_ip'  # context key for target host
    pve_hostname: str = 'child-pve'  # hostname for PVE installation
    ssh_timeout: int = 120
    timeout: int = 1200  # 20 min for PVE install + reboot

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Check if PVE running, install if not."""
        start = time.time()

        target_host = context.get(self.host_key)
        if not target_host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        # Wait for SSH first
        if not wait_for_ssh(target_host, timeout=self.ssh_timeout):
            return ActionResult(
                success=False,
                message=f"SSH not available on {target_host}",
                duration=time.time() - start
            )

        # Check if PVE is already installed (marker file or pveproxy running)
        logger.info(f"[{self.name}] Checking if PVE already installed on {target_host}...")

        # First check for pre-installed marker (debian-13-pve image)
        rc, out, err = run_ssh(target_host, 'test -f /etc/pve-packages-preinstalled', timeout=30)
        if rc == 0:
            logger.info(f"[{self.name}] PVE pre-installed (marker file found) - skipping install")
            return ActionResult(
                success=True,
                message="PVE pre-installed (debian-13-pve image) - skipped installation",
                duration=time.time() - start
            )

        # Fall back to pveproxy check (for manually installed PVE)
        rc, out, err = run_ssh(target_host, 'systemctl is-active pveproxy', timeout=30)
        if rc == 0 and 'active' in out:
            logger.info(f"[{self.name}] PVE already installed and running - skipping")
            return ActionResult(
                success=True,
                message="PVE already installed and running - skipped installation",
                duration=time.time() - start
            )

        # PVE not running, install it
        logger.info(f"[{self.name}] PVE not installed, running pve-install.yml...")
        ansible_dir = get_sibling_dir('ansible')
        if not ansible_dir.exists():
            return ActionResult(
                success=False,
                message=f"Ansible directory not found: {ansible_dir}",
                duration=time.time() - start
            )

        cmd = [
            'ansible-playbook',
            '-i', 'inventory/remote-dev.yml',
            'playbooks/pve-install.yml',
            '-e', f'ansible_host={target_host}',
            '-e', f'pve_hostname={self.pve_hostname}',
            '-e', f'ansible_user={config.ssh_user}',
        ]

        rc, out, err = run_command(cmd, cwd=ansible_dir, timeout=self.timeout)
        if rc != 0:
            error_msg = err[-500:] if err else out[-500:]
            return ActionResult(
                success=False,
                message=f"pve-install.yml failed: {error_msg}",
                duration=time.time() - start
            )

        # Wait for SSH after reboot
        logger.info(f"[{self.name}] Waiting for SSH after PVE installation...")
        if not wait_for_ssh(target_host, timeout=self.ssh_timeout * 2):
            return ActionResult(
                success=False,
                message=f"SSH not available after PVE installation on {target_host}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"PVE installed successfully on {target_host}",
            duration=time.time() - start
        )
