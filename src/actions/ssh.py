"""SSH-related actions."""

import logging
import time
from dataclasses import dataclass
from typing import Optional

from common import ActionResult, run_ssh, wait_for_ping
from config import HostConfig

logger = logging.getLogger(__name__)


@dataclass
class SSHCommandAction:
    """Run a command over SSH."""
    name: str
    command: str
    host_key: str = 'node_ip'  # context key for target host
    jump_host_key: Optional[str] = None  # context key for jump host
    timeout: int = 60
    output_context_key: Optional[str] = None  # store output in context

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Execute SSH command."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        jump_host = context.get(self.jump_host_key) if self.jump_host_key else None

        logger.info(f"[{self.name}] Running command on {host}...")
        # Use automation_user for SSH to VMs (created via cloud-init)
        rc, out, err = run_ssh(host, self.command, user=config.automation_user, timeout=self.timeout, jump_host=jump_host)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Command failed: {err}",
                duration=time.time() - start
            )

        context_updates = {}
        if self.output_context_key:
            context_updates[self.output_context_key] = out.strip()

        return ActionResult(
            success=True,
            message=f"Command completed: {out.strip()[:100]}",
            duration=time.time() - start,
            context_updates=context_updates
        )


@dataclass
class WaitForSSHAction:
    """Wait for SSH to become available."""
    name: str
    host_key: str = 'node_ip'
    jump_host_key: Optional[str] = None
    timeout: int = 60
    interval: int = 5

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Wait for SSH connectivity. Uses ping first for faster detection."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        jump_host = context.get(self.jump_host_key) if self.jump_host_key else None

        logger.info(f"[{self.name}] Waiting for SSH on {host}...")

        # First wait for ping (faster than SSH timeout) - skip if using jump host
        if not jump_host:
            if not wait_for_ping(host, timeout=min(30, self.timeout), interval=2):
                logger.debug(f"Host {host} not pingable yet, continuing...")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            # Use automation_user for SSH to VMs (created via cloud-init)
            rc, out, _ = run_ssh(host, 'echo ready', user=config.automation_user, timeout=5, jump_host=jump_host)
            if rc == 0 and 'ready' in out:
                logger.info(f"[{self.name}] SSH available on {host}")
                return ActionResult(
                    success=True,
                    message=f"SSH available on {host}",
                    duration=time.time() - start
                )
            logger.debug(f"SSH not ready on {host}, retrying...")
            time.sleep(self.interval)

        return ActionResult(
            success=False,
            message=f"Timeout waiting for SSH on {host}",
            duration=time.time() - start
        )


@dataclass
class WaitForFileAction:
    """Poll for a file to exist on a remote host via SSH.

    When failure_path is set, also checks for a failure marker each
    interval. If the failure file appears first, returns failure
    immediately instead of waiting for the full timeout.
    """
    name: str
    host_key: str
    file_path: str
    failure_path: Optional[str] = None
    timeout: int = 300
    interval: int = 10

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Poll until file exists, failure detected, or timeout."""
        start = time.time()

        host = context.get(self.host_key)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_key} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Waiting for {self.file_path} on {host}...")

        deadline = time.time() + self.timeout
        while time.time() < deadline:
            # Check for success file
            rc, out, _ = run_ssh(
                host,
                f'test -f {self.file_path} && echo EXISTS',
                user=config.automation_user,
                timeout=10,
            )
            if rc == 0 and 'EXISTS' in out:
                logger.info(f"[{self.name}] Found {self.file_path} on {host}")
                return ActionResult(
                    success=True,
                    message=f"File {self.file_path} found on {host}",
                    duration=time.time() - start,
                )

            # Check for failure file (early exit)
            if self.failure_path:
                rc, out, _ = run_ssh(
                    host,
                    f'test -f {self.failure_path} && cat {self.failure_path}',
                    user=config.automation_user,
                    timeout=10,
                )
                if rc == 0 and out.strip():
                    logger.error(f"[{self.name}] Failure marker found: {self.failure_path}")
                    return ActionResult(
                        success=False,
                        message=f"Failure marker found on {host}: {out.strip()[:200]}",
                        duration=time.time() - start,
                    )

            logger.debug(f"File not found yet, retrying in {self.interval}s...")
            time.sleep(self.interval)

        return ActionResult(
            success=False,
            message=f"Timeout ({self.timeout}s) waiting for {self.file_path} on {host}",
            duration=time.time() - start,
        )


@dataclass
class VerifySSHChainAction:
    """Verify SSH connectivity through a jump host chain."""
    name: str
    target_host_key: str = 'leaf_ip'
    jump_host_key: str = 'node_ip'
    timeout: int = 60
    interval: int = 5

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Verify SSH chain connectivity."""
        start = time.time()

        target = context.get(self.target_host_key)
        jump = context.get(self.jump_host_key)

        if not target or not jump:
            return ActionResult(
                success=False,
                message=f"Missing context: {self.target_host_key}={target}, {self.jump_host_key}={jump}",
                duration=time.time() - start
            )

        # Wait for SSH on target via jump host
        # Use automation_user for SSH to VMs (created via cloud-init)
        user = config.automation_user
        logger.info(f"[{self.name}] Waiting for SSH on {target} via {jump}...")
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            rc, out, err = run_ssh(target, 'echo ready', user=user, jump_host=jump, timeout=5)
            if rc == 0 and 'ready' in out:
                break
            logger.debug(f"SSH not ready on {target}, retrying...")
            time.sleep(self.interval)
        else:
            return ActionResult(
                success=False,
                message=f"Timeout waiting for SSH on {target}",
                duration=time.time() - start
            )

        # Verify chain with hostname command
        logger.info(f"[{self.name}] Verifying SSH chain: outer -> {jump} -> {target}")
        rc, out, err = run_ssh(target, 'hostname && uname -a', user=user, jump_host=jump, timeout=30)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"SSH chain verification failed: {err}",
                duration=time.time() - start
            )

        hostname = out.strip().split('\n')[0] if out else 'unknown'

        return ActionResult(
            success=True,
            message=f"SSH chain verified: {hostname}",
            duration=time.time() - start,
            context_updates={'test_hostname': hostname, 'ssh_output': out.strip()}
        )
