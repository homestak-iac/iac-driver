"""PVE self-configure scenario.

Runs locally on a PVE node to complete self-configuration after
bootstrap + config pull (Phase 1). This is Phase 2 of the 2-phase
PVE self-configure model.

Phases:
1. fetch_config       — Pull site.yaml, secrets.yaml, private key from parent
2. ensure_pve         — Install PVE if needed (handles reboot re-entry)
3. setup_pve          — Configure PVE (repos, nag removal, packages)
4. configure_bridge   — Create vmbr0 bridge from primary interface
5. generate_node_config — Generate nodes/{hostname}.yaml
6. create_api_token   — Create pveum API token, inject into secrets.yaml
7. inject_self_ssh_key — Add own pubkey to secrets.yaml for child VMs
8. write_marker       — Write completion marker for parent polling

See docs/designs/pve-self-configure.md for design rationale.
"""

import logging
import re
import time
from pathlib import Path

from actions.ansible import AnsibleLocalPlaybookAction
from actions.config_pull import ConfigFetchAction, WriteMarkerAction
from common import ActionResult
from config import HostConfig, get_site_config_dir
from scenarios import register_scenario
from scenarios.pve_setup import (
    _CreateApiTokenPhase,
    _EnsurePVEPhase,
    _GenerateNodeConfigPhase,
    _PVESetupPhase,
)

logger = logging.getLogger(__name__)


@register_scenario
class PVEConfig:
    """PVE self-configure scenario (2-phase model, Phase 2)."""

    name = 'pve-config'
    description = 'Fetch config and self-configure PVE node'
    requires_root = False
    requires_host_config = False
    requires_api = False
    expected_runtime = 600  # ~5-10 min (pve-9 image), ~20 min (debian-13)

    def get_phases(self, _config: HostConfig) -> list[tuple[str, object, str]]:
        """Return phases for PVE self-configure."""
        return [
            ('fetch_config', ConfigFetchAction(name='fetch-config'),
             'Fetch config from parent server'),
            ('ensure_pve', _EnsurePVEPhase(),
             'Ensure PVE installed'),
            ('setup_pve', _PVESetupPhase(),
             'Run pve-setup.yml'),
            ('configure_bridge', _ConfigureBridgePhase(),
             'Configure vmbr0 bridge'),
            ('generate_node_config', _GenerateNodeConfigPhase(),
             'Generate node config'),
            ('create_api_token', _CreateApiTokenPhase(),
             'Create API token'),
            ('inject_self_ssh_key', _InjectSelfSSHKeyPhase(),
             'Inject own SSH key into secrets'),
            ('write_marker', WriteMarkerAction(name='write-marker'),
             'Write completion marker'),
        ]

    def on_failure(self, _config: HostConfig, context: dict):
        """Write failure marker for parent polling."""
        failed_phase = context.get('_failed_phase', 'unknown')
        error = context.get('_failed_message', 'unknown error')
        WriteMarkerAction.write_failure_marker('pve-config', failed_phase, error)


class _ConfigureBridgePhase:
    """Configure vmbr0 network bridge locally.

    Runs the ansible pve-network.yml playbook with bridge task.
    The networking role auto-detects the primary interface and creates
    vmbr0. Idempotent — skips if vmbr0 already exists.
    """

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Configure bridge locally via ansible."""
        action = AnsibleLocalPlaybookAction(
            name='configure-bridge-local',
            playbook='playbooks/pve-network.yml',
            extra_vars={'pve_network_tasks': '["bridge"]'},
        )
        return action.run(config, context)


class _InjectSelfSSHKeyPhase:
    """Inject this host's own SSH public key into its secrets.yaml.

    Reads ~/.ssh/id_rsa.pub (or id_ed25519.pub) and adds it to
    secrets.yaml under ssh_keys as 'self'. This ensures child VMs
    created by this host authorize its SSH key.
    """

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Inject own SSH public key into local secrets.yaml."""
        start = time.time()

        # Find public key
        pubkey = None
        home = Path.home()
        for keyfile in ['id_ed25519.pub', 'id_rsa.pub']:
            path = home / '.ssh' / keyfile
            if path.exists():
                pubkey = path.read_text(encoding='utf-8').strip()
                break

        if not pubkey:
            return ActionResult(
                success=False,
                message="No SSH public key found (~/.ssh/id_ed25519.pub or id_rsa.pub)",
                duration=time.time() - start,
            )

        # Find secrets.yaml
        try:
            site_config_dir = get_site_config_dir()
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Cannot find config: {e}",
                duration=time.time() - start,
            )

        secrets_file = site_config_dir / 'secrets.yaml'
        if not secrets_file.exists():
            return ActionResult(
                success=False,
                message=f"secrets.yaml not found at {secrets_file}",
                duration=time.time() - start,
            )

        key_name = 'self'
        content = secrets_file.read_text(encoding='utf-8')

        # Check if key already exists with this value
        if f'{key_name}: {pubkey}' in content:
            return ActionResult(
                success=True,
                message=f"SSH key '{key_name}' already present — skipped",
                duration=time.time() - start,
            )

        # Update or add the key
        pattern = re.compile(rf'^(\s*){re.escape(key_name)}:.*$', re.MULTILINE)
        if pattern.search(content):
            # Update existing line
            content = pattern.sub(
                lambda m: f'{m.group(1)}{key_name}: {pubkey}', content
            )
        elif 'ssh_keys:' in content:
            # Add after ssh_keys:
            content = re.sub(
                r'^(ssh_keys:)\s*(\{\})?\s*$',
                rf'\1\n  {key_name}: {pubkey}',
                content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            # Add new section
            content += f'\nssh_keys:\n  {key_name}: {pubkey}\n'

        secrets_file.write_text(content, encoding='utf-8')
        logger.info("Injected SSH key '%s' into %s", key_name, secrets_file)

        return ActionResult(
            success=True,
            message=f"SSH key '{key_name}' injected into secrets.yaml",
            duration=time.time() - start,
        )
