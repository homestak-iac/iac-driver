"""Proxmox VE actions.

Local actions execute PVE commands directly via subprocess (operator runs
on the PVE host). Remote actions use SSH for delegated child PVE nodes.
"""

import fnmatch
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

from common import ActionResult, run_command, run_ssh, sudo_prefix, _extract_ipv4
from config import HostConfig

logger = logging.getLogger(__name__)


def _resolve(attr: str, config: HostConfig, context: dict):
    """Resolve attribute from context (preferred) or config."""
    return context.get(attr) or getattr(config, attr, None)


def _get_local_vm_ip(vm_id: int, timeout: int = 30) -> Optional[str]:
    """Get VM IP via local qm guest agent command."""
    rc, out, _ = run_command(
        ['sudo', 'qm', 'guest', 'cmd', str(vm_id), 'network-get-interfaces'],
        timeout=timeout
    )
    if rc != 0:
        return None
    try:
        interfaces = json.loads(out)
        for iface in interfaces:
            ip: Optional[str] = _extract_ipv4(iface)
            if ip:
                return ip
    except (json.JSONDecodeError, KeyError):
        pass
    return None


def _wait_for_local_guest_agent(
    vm_id: int, timeout: int = 300, interval: int = 5
) -> Optional[str]:
    """Poll local guest agent for VM IP."""
    logger.info(f"Waiting for guest agent on VM {vm_id}...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        ip = _get_local_vm_ip(vm_id)
        if ip:
            logger.info(f"VM {vm_id} has IP: {ip}")
            return ip
        logger.debug(f"Guest agent not ready on VM {vm_id}, retrying...")
        time.sleep(interval)
    logger.error(f"Guest agent timeout for VM {vm_id}")
    return None


# ---------------------------------------------------------------------------
# Local actions — execute on the operator's own PVE host via subprocess
# ---------------------------------------------------------------------------

@dataclass
class StartVMAction:
    """Start a VM on the local PVE host."""
    name: str
    vm_id_attr: str = 'inner_vm_id'

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Start a VM via local qm command."""
        start = time.time()
        vm_id = _resolve(self.vm_id_attr, config, context)

        if not vm_id:
            return ActionResult(
                success=False,
                message=f"Missing: {self.vm_id_attr}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Starting VM {vm_id}...")
        rc, _, err = run_command(
            ['sudo', 'qm', 'start', str(vm_id)], timeout=60
        )

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to start VM {vm_id}: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"VM {vm_id} started",
            duration=time.time() - start
        )


@dataclass
class WaitForGuestAgentAction:
    """Wait for QEMU guest agent and get VM IP."""
    name: str
    vm_id_attr: str = 'inner_vm_id'
    ip_context_key: str = 'node_ip'
    timeout: int = 300

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Poll guest agent for VM IP and store in context."""
        start = time.time()
        vm_id = _resolve(self.vm_id_attr, config, context)

        if not vm_id:
            return ActionResult(
                success=False,
                message=f"Missing: {self.vm_id_attr}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Waiting for guest agent on VM {vm_id}...")
        ip = _wait_for_local_guest_agent(vm_id, timeout=self.timeout)

        if not ip:
            return ActionResult(
                success=False,
                message=f"Failed to get IP for VM {vm_id}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"VM {vm_id} has IP: {ip}",
            duration=time.time() - start,
            context_updates={self.ip_context_key: ip}
        )


@dataclass
class LookupVMIPAction:
    """Look up a running VM's IP from the guest agent.

    Used during destroy to find VM IPs when context is not available
    (e.g., when destroy runs as a fresh invocation without prior create context).
    """
    name: str
    vmid: int
    ip_context_key: str
    timeout: int = 30  # Short — VM should already be running

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Look up VM IP from guest agent."""
        start = time.time()

        logger.info(f"[{self.name}] Looking up IP for VM {self.vmid}...")
        ip = _wait_for_local_guest_agent(self.vmid, timeout=self.timeout)

        if not ip:
            return ActionResult(
                success=False,
                message=f"Could not get IP for VM {self.vmid}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] VM {self.vmid} has IP: {ip}")
        return ActionResult(
            success=True,
            message=f"VM {self.vmid} IP: {ip}",
            duration=time.time() - start,
            context_updates={self.ip_context_key: ip}
        )


@dataclass
class StartProvisionedVMsAction:
    """Start all VMs from provisioned_vms context."""
    name: str

    def run(self, _config: HostConfig, context: dict) -> ActionResult:
        """Start all VMs listed in provisioned_vms context."""
        start = time.time()

        provisioned_vms = context.get('provisioned_vms', [])
        if not provisioned_vms:
            return ActionResult(
                success=False,
                message="No provisioned_vms in context",
                duration=time.time() - start
            )

        started = []
        for vm in provisioned_vms:
            vm_name = vm.get('name')
            vm_id = vm.get('vmid')
            logger.info(f"[{self.name}] Starting VM {vm_id} ({vm_name})...")

            rc, _, err = run_command(
                ['sudo', 'qm', 'start', str(vm_id)], timeout=60
            )
            if rc != 0:
                return ActionResult(
                    success=False,
                    message=f"Failed to start VM {vm_id} ({vm_name}): {err}",
                    duration=time.time() - start
                )
            started.append(vm_name)

        return ActionResult(
            success=True,
            message=f"Started {len(started)} VMs: {', '.join(started)}",
            duration=time.time() - start
        )


@dataclass
class WaitForProvisionedVMsAction:
    """Wait for guest agent on all provisioned VMs and collect their IPs."""
    name: str
    timeout: int = 300

    def run(self, _config: HostConfig, context: dict) -> ActionResult:
        """Wait for guest agent on all provisioned VMs and collect IPs."""
        start = time.time()

        provisioned_vms = context.get('provisioned_vms', [])
        if not provisioned_vms:
            return ActionResult(
                success=False,
                message="No provisioned_vms in context",
                duration=time.time() - start
            )

        context_updates = {}
        for vm in provisioned_vms:
            vm_name = vm.get('name')
            vm_id = vm.get('vmid')
            logger.info(f"[{self.name}] Waiting for guest agent on VM {vm_id} ({vm_name})...")

            ip = _wait_for_local_guest_agent(vm_id, timeout=self.timeout)
            if not ip:
                return ActionResult(
                    success=False,
                    message=f"Failed to get IP for VM {vm_id} ({vm_name})",
                    duration=time.time() - start
                )

            context_updates[f'{vm_name}_ip'] = ip
            logger.info(f"[{self.name}] VM {vm_name} has IP: {ip}")

        # First VM's IP as 'vm_ip' for backward compatibility
        if provisioned_vms:
            first_name = provisioned_vms[0]['name']
            context_updates['vm_ip'] = context_updates[f'{first_name}_ip']

        return ActionResult(
            success=True,
            message=f"Got IPs for {len(context_updates) - 1} VMs",
            duration=time.time() - start,
            context_updates=context_updates
        )


# ---------------------------------------------------------------------------
# Remote actions — operate on delegated child PVE nodes via SSH
# ---------------------------------------------------------------------------

@dataclass
class StartVMRemoteAction:
    """Start a VM on a remote PVE host via SSH."""
    name: str
    vm_id_attr: str = 'test_vm_id'
    pve_host_key: str = 'node_ip'

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Start a VM on a remote PVE host via SSH."""
        start = time.time()

        vm_id = _resolve(self.vm_id_attr, config, context)
        pve_host = context.get(self.pve_host_key)

        if not vm_id:
            return ActionResult(
                success=False,
                message=f"Missing: {self.vm_id_attr}",
                duration=time.time() - start
            )

        if not pve_host:
            return ActionResult(
                success=False,
                message=f"No {self.pve_host_key} in context",
                duration=time.time() - start
            )

        ssh_user = config.automation_user
        logger.info(f"[{self.name}] Starting VM {vm_id} on {pve_host}...")
        rc, _out, err = run_ssh(pve_host, f'sudo qm start {vm_id}',
                                user=ssh_user, timeout=60)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to start VM {vm_id}: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"VM {vm_id} started on {pve_host}",
            duration=time.time() - start
        )


@dataclass
class WaitForGuestAgentRemoteAction:
    """Wait for guest agent on a remote PVE and get VM IP."""
    name: str
    vm_id_attr: str = 'test_vm_id'
    pve_host_key: str = 'node_ip'
    ip_context_key: str = 'leaf_ip'
    timeout: int = 300
    interval: int = 5

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Poll remote guest agent for VM IP via SSH."""
        start = time.time()

        vm_id = _resolve(self.vm_id_attr, config, context)
        pve_host = context.get(self.pve_host_key)

        if not vm_id:
            return ActionResult(
                success=False,
                message=f"Missing: {self.vm_id_attr}",
                duration=time.time() - start
            )

        if not pve_host:
            return ActionResult(
                success=False,
                message=f"No {self.pve_host_key} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Waiting for guest agent on VM {vm_id}...")

        ssh_user = config.automation_user
        leaf_ip = None
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            rc, out, _ = run_ssh(
                pve_host,
                f'sudo qm guest cmd {vm_id} network-get-interfaces 2>/dev/null | jq -r \'.[].["ip-addresses"][]? | select(.["ip-address-type"]=="ipv4") | .["ip-address"]\' | grep -v "^127\\." | head -1',
                user=ssh_user, timeout=30
            )
            if rc == 0 and out.strip():
                leaf_ip = out.strip()
                break
            logger.debug(f"Guest agent not ready on VM {vm_id}, retrying...")
            time.sleep(self.interval)

        if not leaf_ip:
            return ActionResult(
                success=False,
                message=f"Failed to get IP for VM {vm_id}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"VM {vm_id} has IP: {leaf_ip}",
            duration=time.time() - start,
            context_updates={self.ip_context_key: leaf_ip}
        )


@dataclass
class DiscoverVMsAction:
    """Discover VMs matching a pattern via PVE API on a remote host.

    Queries the PVE cluster resources API and filters by name pattern
    and optional vmid range. Populates context['discovered_vms'] with
    matching VMs for downstream actions.
    """
    name: str
    pve_host_attr: str = 'ssh_host'
    name_pattern: str = 'child-pve*'
    vmid_range: Optional[tuple[int, int]] = (99900, 99999)

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Query PVE API via SSH and filter VMs by name/vmid."""
        start = time.time()

        pve_host = _resolve(self.pve_host_attr, config, context)
        if not pve_host:
            return ActionResult(
                success=False,
                message=f"Missing: {self.pve_host_attr}",
                duration=time.time() - start
            )

        ssh_user = config.automation_user
        sudo = sudo_prefix(ssh_user)

        logger.info(f"[{self.name}] Discovering VMs matching '{self.name_pattern}' on {pve_host}...")

        cmd = f"{sudo}pvesh get /cluster/resources --type vm --output-format json"
        rc, out, err = run_ssh(pve_host, cmd, user=ssh_user, timeout=30)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to query PVE API: {err}",
                duration=time.time() - start
            )

        try:
            vms = json.loads(out)
        except json.JSONDecodeError as e:
            return ActionResult(
                success=False,
                message=f"Failed to parse PVE API response: {e}",
                duration=time.time() - start
            )

        discovered = []
        for vm in vms:
            vm_name = vm.get('name', '')
            vm_id = vm.get('vmid')

            if not fnmatch.fnmatch(vm_name, self.name_pattern):
                continue

            if self.vmid_range and (vm_id < self.vmid_range[0] or vm_id > self.vmid_range[1]):
                continue

            discovered.append({
                'vmid': vm_id,
                'name': vm_name,
                'status': vm.get('status', 'unknown'),
                'node': vm.get('node', ''),
            })
            logger.info(f"[{self.name}] Found VM: {vm_name} (vmid={vm_id}, status={vm.get('status')})")

        return ActionResult(
            success=True,
            message=f"Discovered {len(discovered)} VMs matching '{self.name_pattern}'",
            duration=time.time() - start,
            context_updates={'discovered_vms': discovered}
        )


@dataclass
class DestroyDiscoveredVMsAction:
    """Destroy all VMs in context['discovered_vms'] on a remote host.

    Stops running VMs and then destroys them. Used for cleanup
    of VMs discovered by pattern matching.
    """
    name: str
    pve_host_attr: str = 'ssh_host'
    context_key: str = 'discovered_vms'
    force_stop: bool = True
    stop_timeout: int = 30

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Stop and destroy all discovered VMs via SSH."""
        start = time.time()

        vms = context.get(self.context_key, [])
        if not vms:
            return ActionResult(
                success=True,
                message="No VMs to destroy",
                duration=time.time() - start
            )

        pve_host = _resolve(self.pve_host_attr, config, context)
        if not pve_host:
            return ActionResult(
                success=False,
                message=f"Missing: {self.pve_host_attr}",
                duration=time.time() - start
            )

        ssh_user = config.automation_user
        sudo = sudo_prefix(ssh_user)

        destroyed = []
        for vm in vms:
            vm_id = vm['vmid']
            vm_name = vm.get('name', str(vm_id))
            vm_status = vm.get('status', 'unknown')

            logger.info(f"[{self.name}] Destroying VM {vm_id} ({vm_name})...")

            # Stop if running
            if vm_status == 'running' and self.force_stop:
                logger.info(f"[{self.name}] Stopping VM {vm_id}...")
                rc, _, err = run_ssh(
                    pve_host,
                    f"{sudo}qm stop {vm_id} --timeout {self.stop_timeout}",
                    user=ssh_user,
                    timeout=self.stop_timeout + 30
                )
                if rc != 0:
                    logger.warning(f"[{self.name}] Failed to stop VM {vm_id}: {err}")

            # Destroy
            rc, _, err = run_ssh(
                pve_host,
                f"{sudo}qm destroy {vm_id} --purge",
                user=ssh_user,
                timeout=60
            )
            if rc != 0:
                return ActionResult(
                    success=False,
                    message=f"Failed to destroy VM {vm_id}: {err}",
                    duration=time.time() - start
                )

            destroyed.append(vm_name)
            logger.info(f"[{self.name}] Destroyed VM {vm_id} ({vm_name})")

        return ActionResult(
            success=True,
            message=f"Destroyed {len(destroyed)} VMs: {', '.join(destroyed)}",
            duration=time.time() - start
        )
