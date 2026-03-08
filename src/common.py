"""Common utilities and types for infrastructure automation."""

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def sudo_prefix(user: str) -> str:
    """Return 'sudo ' for non-root users, empty string for root."""
    return '' if user == 'root' else 'sudo '


def get_homestak_root() -> Path:
    """Return the homestak root directory.

    All component paths are derived from this single anchor:
    - config:    $HOMESTAK_ROOT/config
    - iac repos: $HOMESTAK_ROOT/iac/<repo>
    - bootstrap: $HOMESTAK_ROOT/bootstrap

    On installed hosts, $HOME IS the workspace root (default).
    On dev workstations, set HOMESTAK_ROOT=~/homestak.
    """
    return Path(os.environ.get('HOMESTAK_ROOT', Path.home()))


@dataclass
class ActionResult:
    """Result returned by an action."""
    success: bool
    message: str = ''
    duration: float = 0.0
    context_updates: dict = field(default_factory=dict)
    continue_on_failure: bool = False


def run_command(
    cmd: list[str],
    cwd: Optional[Path] = None,
    timeout: int = 600,
    capture: bool = True,
    env: Optional[dict] = None
) -> tuple[int, str, str]:
    """Run a command and return (returncode, stdout, stderr)."""
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=capture,
            text=True,
            timeout=timeout,
            env=env,
            check=False  # We handle return codes explicitly
        )
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return -1, '', f'Command timed out after {timeout}s'
    except Exception as e:
        return -1, '', str(e)


def run_ssh(
    host: str,
    command: str,
    user: str = '',
    timeout: int = 60,
    jump_host: Optional[str] = None
) -> tuple[int, str, str]:
    """Run command over SSH."""
    if not user:
        import getpass
        user = getpass.getuser()
    # Use relaxed host key checking for tests where VMs are recreated
    ssh_opts = '-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR'

    if jump_host:
        # Use nested SSH instead of -J flag because ProxyJump has issues with
        # PVE's /etc/ssh/ssh_known_hosts symlink to /etc/pve/priv/known_hosts
        # which non-root users can't read
        inner_cmd = f"ssh {ssh_opts} -o ConnectTimeout={timeout} {user}@{host} '{command}'"
        cmd = ['ssh'] + ssh_opts.split() + ['-o', f'ConnectTimeout={timeout}', f'{user}@{jump_host}', inner_cmd]
    else:
        cmd = ['ssh'] + ssh_opts.split() + ['-o', f'ConnectTimeout={timeout}', f'{user}@{host}', command]

    return run_command(cmd, timeout=timeout)


def wait_for_ping(host: str, timeout: int = 60, interval: int = 2) -> bool:
    """Wait for host to respond to ping."""
    logger.debug(f"Waiting for ping on {host}...")
    start = time.time()
    while time.time() - start < timeout:
        rc, _, _ = run_command(['ping', '-c', '1', '-W', '1', host], timeout=5)
        if rc == 0:
            logger.debug(f"Host {host} is pingable")
            return True
        time.sleep(interval)
    return False


def wait_for_ssh(host: str, user: str = 'root', timeout: int = 60, interval: int = 3) -> bool:
    """Wait for SSH to become available. Uses ping first for faster detection."""
    logger.info(f"Waiting for SSH on {host}...")
    start = time.time()

    # First wait for ping (faster than SSH timeout)
    if not wait_for_ping(host, timeout=min(60, timeout), interval=2):
        logger.debug(f"Host {host} not pingable, continuing to try SSH...")

    while time.time() - start < timeout:
        rc, out, _ = run_ssh(host, 'echo ready', user=user, timeout=5)
        if rc == 0 and 'ready' in out:
            logger.info(f"SSH available on {host}")
            return True
        logger.debug(f"SSH not ready, retrying in {interval}s...")
        time.sleep(interval)
    logger.error(f"SSH timeout waiting for {host}")
    return False


def get_vm_ip(vm_id: int, pve_host: str, interface: str = 'eth0', user: str = 'root') -> Optional[str]:
    """Get VM IP via qm guest cmd on PVE host."""
    import json as json_module
    sudo = '' if user == 'root' else 'sudo '
    rc, out, _ = run_ssh(pve_host, f'{sudo}qm guest cmd {vm_id} network-get-interfaces', user=user)
    if rc != 0:
        return None

    try:
        interfaces = json_module.loads(out)
        for iface in interfaces:
            if iface.get('name') == interface or interface == '*':
                ip = _extract_ipv4(iface)
                if ip:
                    return ip
    except (json_module.JSONDecodeError, KeyError):
        pass
    return None


def _extract_ipv4(iface: dict) -> Optional[str]:
    """Extract first non-loopback IPv4 address from interface data."""
    for addr in iface.get('ip-addresses', []):
        if addr.get('ip-address-type') == 'ipv4':
            ip: Optional[str] = addr.get('ip-address')
            if ip and not ip.startswith('127.'):
                return ip
    return None


def wait_for_guest_agent(
    vm_id: int,
    pve_host: str,
    timeout: int = 300,
    interval: int = 5,
    user: str = 'root'
) -> Optional[str]:
    """Wait for guest agent and return IP."""
    logger.info(f"Waiting for guest agent on VM {vm_id}...")
    start = time.time()
    while time.time() - start < timeout:
        ip = get_vm_ip(vm_id, pve_host, '*', user=user)
        if ip:
            logger.info(f"VM {vm_id} has IP: {ip}")
            return ip
        logger.debug(f"Guest agent not ready, retrying in {interval}s...")
        time.sleep(interval)
    logger.error(f"Guest agent timeout for VM {vm_id}")
    return None


def start_vm(vm_id: int, pve_host: str, user: str = 'root') -> bool:
    """Start a VM on the PVE host."""
    logger.info(f"Starting VM {vm_id} on {pve_host}...")
    sudo = '' if user == 'root' else 'sudo '
    rc, _, _ = run_ssh(pve_host, f'{sudo}qm start {vm_id}', user=user)
    return rc == 0
