"""PVE setup scenario.

Installs PVE (if needed) and configures a Proxmox VE host.
Runs locally on the target host (local-first execution model).

After PVE is installed and configured, generates nodes/{hostname}.yaml
to enable the host for use with vm-constructor and other scenarios.
"""

import json
import logging
import re
import socket
import subprocess
import time

from actions import AnsibleLocalPlaybookAction
from common import ActionResult, run_command
from config import HostConfig, get_sibling_dir, get_site_config_dir
from scenarios import register_scenario

logger = logging.getLogger(__name__)


def _set_bootnext_and_reboot():
    """Reboot with BootNext set to current disk entry.

    Prevents UEFI from auto-discovering USB media on reboot.
    Falls back to bare reboot if efibootmgr is unavailable.
    """
    try:
        result = subprocess.run(
            ['efibootmgr'], capture_output=True, text=True, timeout=10, check=False
        )
        for line in result.stdout.splitlines():
            if line.startswith('BootCurrent:'):
                entry = line.split(':')[1].strip()
                subprocess.run(
                    ['sudo', 'efibootmgr', '-n', entry],
                    check=True, timeout=10
                )
                logger.info("Set BootNext=%s (current disk entry)", entry)
                break
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        logger.debug("efibootmgr unavailable, falling back to bare reboot: %s", exc)

    subprocess.run(['sudo', 'systemctl', 'reboot'], check=False, timeout=30)


@register_scenario
class PVESetup:
    """Install and configure a PVE host."""

    name = 'pve-setup'
    description = 'Install PVE (if needed) and configure host'
    requires_root = False
    requires_host_config = False
    requires_api = False  # pve-setup installs PVE — no API available yet
    expected_runtime = 180  # ~3 min (skip if PVE already installed)

    def get_phases(self, _config: HostConfig) -> list[tuple[str, object, str]]:
        """Return phases for PVE setup."""
        return [
            ('ensure_pve', _EnsurePVEPhase(), 'Ensure PVE installed'),
            ('setup_pve', _PVESetupPhase(), 'Run pve-setup.yml'),
            ('generate_node_config', _GenerateNodeConfigPhase(), 'Generate node config'),
            ('create_api_token', _CreateApiTokenPhase(), 'Create API token'),
        ]


class _EnsurePVEPhase:
    """Phase that ensures PVE is installed locally.

    Uses split playbooks (kernel → reboot → packages) because
    ansible.builtin.reboot does not work with local connection. The scenario
    manages the reboot and re-entry via dpkg state detection.
    """

    def run(self, _config: HostConfig, _context: dict):
        """Ensure PVE is installed."""
        start = time.time()

        return self._run_local(start)

    @staticmethod
    def _run_local_playbook(playbook, hostname, ansible_dir):
        """Run a local ansible-playbook and return (rc, out, err)."""
        cmd = [
            'ansible-playbook', '-i', 'inventory/local.yml',
            playbook, '-e', f'pve_hostname={hostname}',
        ]
        return run_command(cmd, cwd=ansible_dir, timeout=1200)

    def _run_local(self, start: float):
        """Install PVE locally with scenario-managed reboot."""
        result = subprocess.run(
            ['systemctl', 'is-active', 'pveproxy'],
            capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode == 0 and 'active' in result.stdout:
            return ActionResult(
                success=True,
                message="PVE already installed and running - skipped",
                duration=time.time() - start
            )

        ansible_dir = get_sibling_dir('ansible')
        if not ansible_dir.exists():
            return ActionResult(
                success=False,
                message=f"Ansible directory not found: {ansible_dir}",
                duration=time.time() - start
            )

        # Check dpkg state to determine which phase to run
        kernel_check = subprocess.run(
            ['dpkg', '-l', 'proxmox-default-kernel'],
            capture_output=True, text=True, timeout=30, check=False
        )
        kernel_installed = kernel_check.returncode == 0 and 'ii' in kernel_check.stdout

        pve_pkg_check = subprocess.run(
            ['dpkg', '-l', 'proxmox-ve'],
            capture_output=True, text=True, timeout=30, check=False
        )
        pve_installed = pve_pkg_check.returncode == 0 and 'ii' in pve_pkg_check.stdout

        hostname = socket.gethostname()

        if kernel_installed and not pve_installed:
            logger.info("Proxmox kernel installed, running phase 2 (packages)...")
        elif not kernel_installed:
            # Phase 1: Install Proxmox kernel
            logger.info("Phase 1: Installing Proxmox kernel...")
            rc, out, err = self._run_local_playbook(
                'playbooks/pve-install-kernel.yml', hostname, ansible_dir
            )
            if rc != 0:
                return ActionResult(
                    success=False,
                    message=f"pve-install-kernel.yml failed: {(err or out)[-500:]}",
                    duration=time.time() - start
                )

            # Reboot to load Proxmox kernel. On restart, pve-setup will
            # re-enter and resume at phase 2 (kernel_installed=True).
            logger.info("Rebooting to load Proxmox kernel...")
            _set_bootnext_and_reboot()
            time.sleep(300)  # Wait for reboot to kill us
            return ActionResult(
                success=False,
                message="Reboot did not occur within timeout",
                duration=time.time() - start
            )

        # Phase 2: Install PVE packages (after reboot)
        logger.info("Phase 2: Installing PVE packages...")
        rc, out, err = self._run_local_playbook(
            'playbooks/pve-install-packages.yml', hostname, ansible_dir
        )
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"pve-install-packages.yml failed: {(err or out)[-500:]}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message="PVE installed successfully",
            duration=time.time() - start
        )

class _PVESetupPhase:
    """Phase that runs pve-setup.yml locally."""

    def run(self, config: HostConfig, context: dict):
        """Run pve-setup.yml locally."""
        action = AnsibleLocalPlaybookAction(
            name='pve-setup-local',
            playbook='playbooks/pve-setup.yml',
        )
        return action.run(config, context)


class _GenerateNodeConfigPhase:
    """Phase that generates nodes/{hostname}.yaml after PVE setup.

    Creates the node configuration file that enables the host for use
    with vm-constructor and other PVE-dependent scenarios.
    """

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Generate node config locally."""
        start = time.time()

        return self._run_local(start)

    def _run_local(self, start: float) -> ActionResult:
        """Generate node config locally."""
        try:
            site_config_dir = get_site_config_dir()
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Cannot find config: {e}",
                duration=time.time() - start
            )

        logger.info("Generating node config locally...")
        rc, out, err = run_command(
            ['make', 'node-config', 'FORCE=1'],
            cwd=site_config_dir,
            timeout=60
        )

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"make node-config failed: {err or out}",
                duration=time.time() - start
            )

        hostname = socket.gethostname()
        node_file = site_config_dir / 'nodes' / f'{hostname}.yaml'

        return ActionResult(
            success=True,
            message=f"Generated {node_file}",
            duration=time.time() - start,
            context_updates={'generated_node_config': str(node_file)}
        )

class _CreateApiTokenPhase:
    """Phase that creates PVE API token and injects into secrets.yaml.

    Creates a 'tofu' API token via pveum, injects the token value into
    secrets.yaml, and verifies it works against the PVE API.

    Idempotent: if a working token for this hostname already exists
    in secrets.yaml, the phase is skipped.
    """

    def run(self, _config: HostConfig, _context: dict) -> ActionResult:
        """Create API token locally."""
        start = time.time()

        return self._run_local(start)

    def _run_local(self, start: float) -> ActionResult:
        """Create API token on local PVE host."""
        hostname = socket.gethostname()
        api_url = 'https://127.0.0.1:8006'

        try:
            site_config_dir = get_site_config_dir()
        except Exception as e:
            return ActionResult(
                success=False,
                message=f"Cannot find config: {e}",
                duration=time.time() - start
            )

        # Check for existing working token
        existing = self._get_existing_token(site_config_dir, hostname)
        if existing and self._verify_token(api_url, existing):
            return ActionResult(
                success=True,
                message=f"API token for {hostname} already works — skipped",
                duration=time.time() - start
            )

        # Wait for pvedaemon to be ready (pveum talks to it)
        if not self._wait_for_pvedaemon_local():
            return ActionResult(
                success=False,
                message="pvedaemon not running — cannot create API token",
                duration=time.time() - start
            )

        # Regenerate SSL certs and restart pveproxy before token creation
        # IPv6 disable/enable toggle removed — pvecm updatecerts works without
        # it on Debian 13 + PVE 9.1 (tested 2026-03-20, see #228)
        logger.debug("Regenerating PVE SSL certificates...")
        subprocess.run(
            'sudo pvecm updatecerts --force 2>/dev/null; '
            'sudo systemctl restart pveproxy && sleep 2',
            shell=True, capture_output=True, timeout=60, check=False
        )

        # Create token via pveum (remove old if exists, since we can't
        # retrieve the value of an existing token)
        logger.info("Creating API token locally...")
        subprocess.run(
            ['sudo', 'pveum', 'user', 'token', 'remove', 'root@pam', 'tofu'],
            capture_output=True, timeout=30, check=False
        )
        result = subprocess.run(
            ['sudo', 'pveum', 'user', 'token', 'add', 'root@pam', 'tofu',
             '--privsep', '0', '--output-format', 'json'],
            capture_output=True, text=True, timeout=30, check=False
        )
        if result.returncode != 0:
            return ActionResult(
                success=False,
                message=f"pveum token add failed: {result.stderr or result.stdout}",
                duration=time.time() - start
            )

        full_token = self._parse_token(result.stdout)
        if not full_token:
            return ActionResult(
                success=False,
                message="Failed to parse token from pveum output",
                duration=time.time() - start
            )

        # Inject into local secrets.yaml
        if not self._inject_token_local(site_config_dir, hostname, full_token):
            return ActionResult(
                success=False,
                message="Failed to inject token into secrets.yaml",
                duration=time.time() - start
            )

        # Verify token works against PVE API
        if not self._verify_token(api_url, full_token):
            return ActionResult(
                success=False,
                message="Token created but API verification failed after retries",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"API token created and verified for {hostname}",
            duration=time.time() - start,
            context_updates={'api_token_created': hostname}
        )

    @staticmethod
    def _parse_token(pveum_output):
        """Parse full token string from pveum JSON output."""
        try:
            token_data = json.loads(pveum_output.strip())
            return f"{token_data['full-tokenid']}={token_data['value']}"
        except (json.JSONDecodeError, KeyError) as e:
            logger.error(f"Failed to parse pveum token output: {e}")
            return None

    @staticmethod
    def _get_existing_token(site_config_dir, hostname):
        """Read existing token for hostname from local secrets.yaml.

        Scopes the search to the api_tokens: section to avoid matching
        the same hostname under ssh_keys: or other sections.
        """
        secrets_file = site_config_dir / 'secrets.yaml'
        if not secrets_file.exists():
            return None
        content = secrets_file.read_text()
        # Extract the api_tokens section (indented block after "api_tokens:")
        section_match = re.search(
            r'^api_tokens:\s*\n((?:[ \t]+.+\n)*)',
            content, re.MULTILINE
        )
        if not section_match:
            return None
        section = section_match.group(1)
        # Match hostname within the api_tokens section only
        token_match = re.search(
            rf'^\s*{re.escape(hostname)}:\s*"?([^"\n]+)"?\s*$',
            section, re.MULTILINE
        )
        return token_match.group(1).strip() if token_match else None

    @staticmethod
    def _verify_token(api_url, token, retries=3, delay=5):
        """Verify token works against PVE API with retries.

        Uses stdlib urllib (no curl dependency). Retries handle the case
        where pveproxy hasn't fully started after PVE installation.
        """
        import ssl
        import urllib.request

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        req = urllib.request.Request(
            f'{api_url}/api2/json/version',
            headers={'Authorization': f'PVEAPIToken={token}'}
        )
        for attempt in range(retries):
            try:
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    if resp.status == 200:
                        return True
            except (urllib.error.URLError, OSError):
                pass
            if attempt < retries - 1:
                logger.debug("API verification attempt %d/%d failed, "
                             "retrying in %ds...", attempt + 1, retries, delay)
                time.sleep(delay)
        return False

    @staticmethod
    def _wait_for_pvedaemon_local():
        """Wait for pvedaemon to be active (single retry with 10s sleep)."""
        result = subprocess.run(
            ['systemctl', 'is-active', 'pvedaemon'],
            capture_output=True, text=True, timeout=10, check=False
        )
        if result.returncode == 0:
            return True
        logger.debug("pvedaemon not yet active, waiting 10s...")
        time.sleep(10)
        result = subprocess.run(
            ['systemctl', 'is-active', 'pvedaemon'],
            capture_output=True, text=True, timeout=10, check=False
        )
        return result.returncode == 0

    @staticmethod
    def _inject_token_local(site_config_dir, hostname, full_token):
        """Inject token into local secrets.yaml."""
        secrets_file = site_config_dir / 'secrets.yaml'

        # Initialize secrets if needed (decrypt .enc or copy .example)
        if not secrets_file.exists():
            run_command(
                ['make', 'init-secrets'], cwd=site_config_dir, timeout=30
            )
            if not secrets_file.exists():
                logger.error("secrets.yaml not found — no .enc or .example available")
                return False

        content = secrets_file.read_text()
        new_line = f'{hostname}: "{full_token}"'

        # Update existing or add new token entry
        # Scope replacement to api_tokens section by matching indented lines
        pattern = re.compile(
            rf'^(\s*){re.escape(hostname)}:.*$', re.MULTILINE
        )
        if pattern.search(content):
            # Use lambda to avoid regex replacement escaping issues
            # (token value could theoretically contain \, &, etc.)
            content = pattern.sub(
                lambda m: f'{m.group(1)}{new_line}', content
            )
        elif 'api_tokens:' in content:
            # Handle both block style "api_tokens:\n" and inline "api_tokens: {}\n"
            content = re.sub(
                r'^(api_tokens:)\s*(\{\})?\s*$',
                rf'\1\n  {new_line}',
                content,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            content += f'\napi_tokens:\n  {new_line}\n'

        # Auto-generate signing key if empty
        if re.search(r'signing_key:\s*["\']?\s*["\']?\s*$', content, re.MULTILINE):
            import secrets as secrets_mod
            key = secrets_mod.token_hex(32)
            content = re.sub(
                r'(signing_key:)\s*["\']?\s*["\']?',
                rf'\1 "{key}"',
                content,
                count=1,
            )
            logger.info("Auto-generated auth.signing_key")

        secrets_file.write_text(content)
        logger.info(f"Injected API token for {hostname} into {secrets_file}")
        return True
