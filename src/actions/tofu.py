"""OpenTofu actions using ConfigResolver."""

import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from common import ActionResult, run_command, get_state_dir
from config import HostConfig, get_sibling_dir
from config_resolver import ConfigResolver

logger = logging.getLogger(__name__)


def create_temp_tfvars(env_name: str, node_name: str) -> Path:
    """Create a unique temporary file for tfvars.

    Uses tempfile to avoid permission issues when different users run commands.
    The file is created in /tmp with a unique name based on PID and timestamp.
    Caller is responsible for cleanup.
    """
    fd, path = tempfile.mkstemp(prefix=f'tfvars-{env_name}-{node_name}-', suffix='.json')
    os.close(fd)  # Close fd since we'll write via ConfigResolver
    return Path(path)


@dataclass
class TofuApplyAction:
    """Run tofu init and apply using ConfigResolver.

    Uses ConfigResolver.resolve_inline_vm() for VM configuration.
    vm_preset references presets/{vm_preset}.yaml (requires image).
    """
    name: str
    vm_name: str      # VM hostname (becomes PVE node name)
    vmid: int         # Explicit VM ID
    vm_preset: Optional[str] = None     # FK to presets/{vm_preset}.yaml
    image: Optional[str] = None      # Image name (required for vm_preset mode)
    spec: Optional[str] = None       # FK to specs/{spec}.yaml (for provisioning token)
    manifest_name: Optional[str] = None  # Manifest name for state isolation
    timeout_init: int = 120
    timeout_apply: int = 300

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Execute tofu init + apply with inline VM config."""
        start = time.time()

        # Resolve inline VM config
        tfvars_path = None
        try:
            resolver = ConfigResolver()
            resolved = resolver.resolve_inline_vm(
                node=config.name,
                vm_name=self.vm_name,
                vmid=self.vmid,
                vm_preset=self.vm_preset,
                image=self.image,
                spec=self.spec,
            )

            tfvars_path = create_temp_tfvars(self.vm_name, config.name)
            resolver.write_tfvars(resolved, str(tfvars_path))
            logger.info(f"[{self.name}] Generated tfvars: {tfvars_path}")
        except Exception as e:
            if tfvars_path and tfvars_path.exists():
                tfvars_path.unlink()
            return ActionResult(
                success=False,
                message=f"ConfigResolver failed: {e}",
                duration=time.time() - start
            )

        # Always use generic env
        tofu_dir = get_sibling_dir('tofu') / 'envs' / 'generic'
        if not tofu_dir.exists():
            return ActionResult(
                success=False,
                message=f"Tofu directory not found: {tofu_dir}",
                duration=time.time() - start
            )

        # State isolation: namespace by manifest to avoid lock contention
        state_subdir = f'{self.vm_name}-{config.name}'
        if self.manifest_name:
            state_dir = get_state_dir() / 'tofu' / self.manifest_name / state_subdir
        else:
            state_dir = get_state_dir() / 'tofu' / state_subdir
        data_dir = state_dir / 'data'
        data_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / 'terraform.tfstate'
        env = {**os.environ, 'TF_DATA_DIR': str(data_dir)}

        # Run tofu init
        logger.info(f"[{self.name}] Running tofu init...")
        rc, _out, err = run_command(['tofu', 'init'], cwd=tofu_dir, timeout=self.timeout_init, env=env)
        if rc != 0:
            if tfvars_path and tfvars_path.exists():
                tfvars_path.unlink()
            return ActionResult(
                success=False,
                message=f"tofu init failed: {err}",
                duration=time.time() - start
            )

        # Run tofu apply with explicit state file
        logger.info(f"[{self.name}] Running tofu apply (state: {state_file})...")
        cmd = ['tofu', 'apply', '-auto-approve', f'-state={state_file}', f'-var-file={tfvars_path}']
        rc, _out, err = run_command(cmd, cwd=tofu_dir, timeout=self.timeout_apply, env=env)

        # Clean up temp tfvars file
        if tfvars_path and tfvars_path.exists():
            tfvars_path.unlink()
            logger.debug(f"[{self.name}] Cleaned up temp tfvars: {tfvars_path}")

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"tofu apply failed: {err}",
                duration=time.time() - start
            )

        # Add VM ID to context for downstream actions
        context_updates = {
            f'{self.vm_name}_vm_id': self.vmid,
            'provisioned_vms': [{'name': self.vm_name, 'vmid': self.vmid}]
        }
        logger.debug(f"[{self.name}] Added {self.vm_name}_vm_id={self.vmid} to context")

        return ActionResult(
            success=True,
            message=f"Tofu apply completed for {self.vm_name} on {config.name}",
            duration=time.time() - start,
            context_updates=context_updates
        )


@dataclass
class TofuDestroyAction:
    """Run tofu destroy using ConfigResolver."""
    name: str
    vm_name: str      # VM hostname
    vmid: int         # VM ID
    vm_preset: Optional[str] = None     # FK to presets/{vm_preset}.yaml
    image: Optional[str] = None      # Image name (for vm_preset mode)
    spec: Optional[str] = None       # FK to specs/{spec}.yaml (for provisioning token)
    manifest_name: Optional[str] = None  # Manifest name for state isolation
    timeout: int = 300

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Execute tofu destroy with inline VM config."""
        start = time.time()

        # Resolve inline VM config
        tfvars_path = None
        try:
            resolver = ConfigResolver()
            resolved = resolver.resolve_inline_vm(
                node=config.name,
                vm_name=self.vm_name,
                vmid=self.vmid,
                vm_preset=self.vm_preset,
                image=self.image,
                spec=self.spec,
            )
            tfvars_path = create_temp_tfvars(self.vm_name, config.name)
            resolver.write_tfvars(resolved, str(tfvars_path))
        except Exception as e:
            if tfvars_path and tfvars_path.exists():
                tfvars_path.unlink()
            return ActionResult(
                success=False,
                message=f"ConfigResolver failed: {e}",
                duration=time.time() - start
            )

        # Always use generic env
        tofu_dir = get_sibling_dir('tofu') / 'envs' / 'generic'
        if not tofu_dir.exists():
            if tfvars_path and tfvars_path.exists():
                tfvars_path.unlink()
            return ActionResult(
                success=False,
                message=f"Tofu directory not found: {tofu_dir}",
                duration=time.time() - start
            )

        # State isolation: namespace by manifest to avoid lock contention
        state_subdir = f'{self.vm_name}-{config.name}'
        if self.manifest_name:
            state_dir = get_state_dir() / 'tofu' / self.manifest_name / state_subdir
        else:
            state_dir = get_state_dir() / 'tofu' / state_subdir
        data_dir = state_dir / 'data'
        state_file = state_dir / 'terraform.tfstate'
        env = {**os.environ, 'TF_DATA_DIR': str(data_dir)}

        if not state_file.exists():
            if tfvars_path and tfvars_path.exists():
                tfvars_path.unlink()
            return ActionResult(
                success=True,
                message=f"No state file found for {self.vm_name}, nothing to destroy",
                duration=time.time() - start
            )

        # Run tofu destroy
        logger.info(f"[{self.name}] Running tofu destroy (state: {state_file})...")
        cmd = ['tofu', 'destroy', '-auto-approve', f'-state={state_file}', f'-var-file={tfvars_path}']
        rc, _out, err = run_command(cmd, cwd=tofu_dir, timeout=self.timeout, env=env)

        # Clean up temp tfvars file
        if tfvars_path and tfvars_path.exists():
            tfvars_path.unlink()

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"tofu destroy failed: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Tofu destroy completed for {self.vm_name}",
            duration=time.time() - start
        )
