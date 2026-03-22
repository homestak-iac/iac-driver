"""Config pull actions for PVE self-configure.

Actions for fetching config from the parent's /config endpoint and
writing completion markers for the parent to poll.

See docs/pve-self-configure.md for design rationale.
"""

import json
import logging
import os
import socket
import ssl
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen

import yaml

from common import ActionResult, get_homestak_root

logger = logging.getLogger(__name__)


@dataclass
class ConfigFetchAction:
    """Fetch config from parent's /config endpoint and write local files.

    Calls GET /config/{identity}, writes:
    - site.yaml to ~/config/site.yaml
    - secrets.yaml to ~/config/secrets.yaml (mode 600)
    - private_key to ~/.ssh/id_rsa (mode 600) + derived pubkey

    Uses HOMESTAK_SERVER and HOMESTAK_TOKEN from environment.
    """
    name: str
    insecure: bool = True  # Self-signed certs in dev

    def run(self, _config, _context: dict) -> ActionResult:
        """Fetch config and write local files."""
        start = time.time()

        server = os.environ.get('HOMESTAK_SERVER')
        token = os.environ.get('HOMESTAK_TOKEN')
        identity = socket.gethostname()

        if not server:
            return ActionResult(
                success=False,
                message="HOMESTAK_SERVER not set",
                duration=time.time() - start,
            )
        if not token:
            return ActionResult(
                success=False,
                message="HOMESTAK_TOKEN not set",
                duration=time.time() - start,
            )

        url = f"{server.rstrip('/')}/config/{identity}"
        logger.info("[%s] Fetching config from %s", self.name, url)

        try:
            data = self._fetch(url, token)
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Config fetch failed: {e}",
                duration=time.time() - start,
            )

        # Write site.yaml (wrap in defaults: to match expected format)
        config_dir = get_homestak_root() / 'config'
        config_dir.mkdir(parents=True, exist_ok=True)

        site_data = data.get('site', {})
        site_path = config_dir / 'site.yaml'
        try:
            with open(site_path, 'w', encoding='utf-8') as f:
                yaml.dump({'defaults': site_data}, f, default_flow_style=False)
            logger.info("[%s] Wrote %s", self.name, site_path)
        except OSError as e:
            return ActionResult(
                success=False,
                message=f"Failed to write site.yaml: {e}",
                duration=time.time() - start,
            )

        # Write secrets.yaml (extract private_key before writing)
        secrets_data = data.get('secrets', {})
        private_key = secrets_data.pop('private_key', None)

        secrets_path = config_dir / 'secrets.yaml'
        try:
            with open(secrets_path, 'w', encoding='utf-8') as f:
                yaml.dump(secrets_data, f, default_flow_style=False)
            secrets_path.chmod(0o600)
            logger.info("[%s] Wrote %s", self.name, secrets_path)
        except OSError as e:
            return ActionResult(
                success=False,
                message=f"Failed to write secrets.yaml: {e}",
                duration=time.time() - start,
            )

        # Write private key if provided (dev posture — shared key model)
        if private_key:
            ssh_dir = Path.home() / '.ssh'
            ssh_dir.mkdir(mode=0o700, exist_ok=True)

            key_path = ssh_dir / 'id_rsa'
            try:
                key_path.write_text(private_key.rstrip('\n') + '\n', encoding='utf-8')
                key_path.chmod(0o600)
                logger.info("[%s] Wrote private key to %s", self.name, key_path)
                self._derive_pubkey(key_path)
            except OSError as e:
                return ActionResult(
                    success=False,
                    message=f"Failed to write private key: {e}",
                    duration=time.time() - start,
                )

        return ActionResult(
            success=True,
            message=f"Config fetched for {identity}",
            duration=time.time() - start,
            context_updates={
                'config_fetched': True,
                'has_private_key': private_key is not None,
            },
        )

    def _fetch(self, url: str, token: str) -> dict:
        """HTTP GET with Bearer auth, return parsed JSON."""
        request = Request(url)
        request.add_header('Accept', 'application/json')
        request.add_header('Authorization', f'Bearer {token}')

        ctx: Optional[ssl.SSLContext] = None
        if self.insecure:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

        with urlopen(request, context=ctx, timeout=30) as response:
            body = response.read()
            result: dict = json.loads(body.decode('utf-8'))
            return result

    @staticmethod
    def _derive_pubkey(privkey_path: Path):
        """Derive public key from private key using ssh-keygen."""
        pubkey_path = privkey_path.with_suffix('.pub')
        result = subprocess.run(
            ['ssh-keygen', '-y', '-f', str(privkey_path)],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            pubkey_path.write_text(result.stdout.strip() + '\n', encoding='utf-8')
            pubkey_path.chmod(0o644)
            logger.info("Derived public key: %s", pubkey_path)
        else:
            logger.warning("Could not derive public key: %s", result.stderr)


@dataclass
class WriteMarkerAction:
    """Write a completion marker for the parent to poll.

    Writes success marker to $HOMESTAK_ROOT/.state/{scenario_name}/success.json.
    Also disables the systemd oneshot service to prevent re-run on reboot.
    """
    name: str
    scenario_name: str = 'pve-config'

    def run(self, _config, context: dict) -> ActionResult:
        """Write success marker."""
        start = time.time()

        state_dir = get_homestak_root() / '.state' / self.scenario_name
        state_dir.mkdir(parents=True, exist_ok=True)

        marker = {
            'phase': self.scenario_name,
            'status': 'success',
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'pve_installed': True,
            'bridge_configured': True,
            'api_token_created': context.get('api_token_created') is not None,
            'node_config_generated': context.get('generated_node_config') is not None,
        }

        marker_path = state_dir / 'success.json'
        try:
            with open(marker_path, 'w', encoding='utf-8') as f:
                json.dump(marker, f, indent=2)
            logger.info("[%s] Wrote marker to %s", self.name, marker_path)
        except OSError as e:
            return ActionResult(
                success=False,
                message=f"Failed to write marker: {e}",
                duration=time.time() - start,
            )

        # Disable systemd oneshot service (prevents re-run on reboot)
        subprocess.run(
            ['sudo', 'systemctl', 'disable', f'{self.scenario_name}.service'],
            capture_output=True, timeout=10, check=False,
        )

        return ActionResult(
            success=True,
            message=f"Marker written to {marker_path}",
            duration=time.time() - start,
        )

    @staticmethod
    def write_failure_marker(scenario_name: str, failed_phase: str, error: str):
        """Write failure marker (called by orchestrator on_failure).

        Args:
            scenario_name: Scenario identifier for state dir
            failed_phase: Name of the phase that failed
            error: Error message
        """
        state_dir = get_homestak_root() / '.state' / scenario_name
        state_dir.mkdir(parents=True, exist_ok=True)

        marker = {
            'phase': scenario_name,
            'status': 'failed',
            'failed_at': failed_phase,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'error': error[:500],
        }

        marker_path = state_dir / 'failure.json'
        try:
            with open(marker_path, 'w', encoding='utf-8') as f:
                json.dump(marker, f, indent=2)
            logger.info("Wrote failure marker to %s", marker_path)
        except OSError:
            logger.exception("Failed to write failure marker")
