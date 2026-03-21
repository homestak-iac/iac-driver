"""PVE lifecycle actions for nested/recursive deployments.

These actions handle the bootstrapping and configuration of PVE nodes:
- Bootstrap (curl|bash installer)
- Secrets management (copy, inject SSH keys, API tokens)
- Network configuration (vmbr0 bridge)
- Node config generation
- Image management (ensure packer image exists)

Extracted from scenarios/recursive_pve.py for reuse by the operator engine.
"""

import base64
import json
import logging
import tempfile as tmpmod
from pathlib import Path
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

from common import ActionResult, run_ssh
from config import HostConfig

logger = logging.getLogger(__name__)


def _image_to_asset_name(image: str) -> str:
    """Convert manifest image name to packer asset filename.

    Image names map 1:1 to asset filenames:
    - debian-12 → debian-12.qcow2
    - pve-9 → pve-9.qcow2

    Args:
        image: Image name from manifest (e.g., 'debian-12', 'pve-9')

    Returns:
        Packer release asset filename (e.g., 'debian-12.qcow2')
    """
    return f"{image}.qcow2"


@dataclass
class EnsureImageAction:
    """Ensure packer image exists on PVE host, download if missing."""
    name: str

    def run(self, config: HostConfig, _context: dict) -> ActionResult:
        """Check for image, download from release if missing."""
        start = time.time()

        pve_host = config.ssh_host
        ssh_user = config.host_user
        image_name = config.packer_image.replace('.qcow2', '.img')
        image_path = f'/var/lib/vz/template/iso/{image_name}'

        # Use sudo if not root
        sudo = '' if ssh_user == 'root' else 'sudo '

        # Check if image exists
        logger.info(f"[{self.name}] Checking for {image_name} on {pve_host}...")
        rc, out, err = run_ssh(pve_host, f'{sudo}test -f {image_path} && echo exists',
                               user=ssh_user, timeout=30)

        if rc == 0 and 'exists' in out:
            return ActionResult(
                success=True,
                message=f"Image {image_name} already exists",
                duration=time.time() - start
            )

        # Download from release
        repo = config.image_release_repo
        tag = config.image_release
        url = f'https://github.com/{repo}/releases/download/{tag}/{config.packer_image}'

        logger.info(f"[{self.name}] Downloading {config.packer_image} from {repo} {tag}...")

        # Create directory and download
        rc, out, err = run_ssh(pve_host, f'{sudo}mkdir -p /var/lib/vz/template/iso',
                               user=ssh_user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to create directory: {err}",
                duration=time.time() - start
            )

        dl_cmd = f'{sudo}curl -fSL -o {image_path} {url}'
        rc, out, err = run_ssh(pve_host, dl_cmd, user=ssh_user, timeout=300)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to download image: {err}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Downloaded {image_name}",
            duration=time.time() - start
        )


@dataclass
class CreateApiTokenAction:
    """Create API token on PVE node and inject into secrets.yaml.

    This action:
    1. Gets the target hostname (used as token key in secrets.yaml)
    2. Regenerates PVE SSL certificates (IPv6 workaround)
    3. Restarts pveproxy
    4. Creates tofu API token via pveum
    5. Injects token into secrets.yaml using hostname as key
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 120

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Create API token and inject into secrets.yaml."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Creating API token on {host}...")

        # Step 0: Get the hostname - this becomes the token key in secrets.yaml
        # The node config uses hostname as api_token key, so we must match it
        rc, hostname_out, err = run_ssh(host, 'hostname', user=config.vm_user, timeout=10)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to get hostname: {err or hostname_out}",
                duration=time.time() - start
            )
        token_name = hostname_out.strip()
        logger.debug(f"[{self.name}] Using token key: {token_name}")

        # Check if an existing token already works (e.g., pve-setup already created one)
        # This avoids redundant SSL cert regen + pveproxy restart which can invalidate tokens
        existing = self._check_existing_token(host, token_name, config)
        if existing:
            logger.info(f"[{self.name}] Existing API token works on {host} — skipped")
            return ActionResult(
                success=True,
                message=f"API token already valid on {host}",
                duration=time.time() - start
            )

        # Step 1: Regenerate PVE SSL certificates and restart pveproxy
        # IPv6 must be temporarily disabled — pvecm updatecerts generates
        # certificates with IPv6 bindings that break API verification on
        # PVE VMs. Bare-metal hosts work without this, but VMs need it (#228).
        ssl_cmd = '''
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=1
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=1
sudo pvecm updatecerts --force 2>/dev/null || true
sudo sysctl -w net.ipv6.conf.all.disable_ipv6=0
sudo sysctl -w net.ipv6.conf.default.disable_ipv6=0
sudo systemctl restart pveproxy
sleep 2
'''
        rc, out, err = run_ssh(host, ssl_cmd, user=config.vm_user, timeout=60)
        if rc != 0:
            logger.warning(f"[{self.name}] SSL cert regen warning: {err or out}")
            # Continue anyway - this might fail on some systems

        # Step 2: Delete any existing token and create new one
        token_cmd = '''
sudo pveum user token remove root@pam tofu 2>/dev/null || true
sudo pveum user token add root@pam tofu --privsep 0 --output-format json
'''
        rc, out, err = run_ssh(host, token_cmd, user=config.vm_user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to create API token: {err or out}",
                duration=time.time() - start
            )

        # Step 3: Parse the token from JSON output
        try:
            token_data = json.loads(out.strip())
            full_token = f"{token_data['full-tokenid']}={token_data['value']}"
        except (json.JSONDecodeError, KeyError) as e:
            return ActionResult(
                success=False,
                message=f"Failed to parse API token: {e}",
                duration=time.time() - start
            )

        # Step 4: Inject token into secrets.yaml on the target host
        # First try to update existing line, if not found add a new one
        # Use the token_name we retrieved from hostname
        secrets_file = '$HOME/config/secrets.yaml'
        inject_cmd = f'''
# Check if token key exists in secrets.yaml
if grep -q "^\\s*{token_name}:" {secrets_file}; then
    # Update existing line
    sed -i 's|^\\(\\s*\\){token_name}:.*$|\\1{token_name}: {full_token}|' {secrets_file}
else
    # Add new line after api_tokens:
    sed -i '/^api_tokens:/a\\  {token_name}: {full_token}' {secrets_file}
fi
'''
        rc, out, err = run_ssh(host, inject_cmd, user=config.vm_user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to inject token into secrets.yaml: {err or out}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] API token created and injected on {host}")
        return ActionResult(
            success=True,
            message=f"API token created on {host}",
            duration=time.time() - start
        )

    @staticmethod
    def _check_existing_token(host: str, token_name: str, config: HostConfig) -> bool:
        """Check if an existing API token works on the remote PVE host.

        Reads the token from secrets.yaml and verifies it against the PVE API.
        This prevents redundant token recreation (which involves SSL cert regen
        and pveproxy restart that can break working tokens).
        """
        # Read existing token from secrets.yaml on the remote host
        check_cmd = f'''
python3 -c "
import yaml, urllib.request, ssl, json, sys, os
try:
    with open(os.path.expanduser('~/config/secrets.yaml')) as f:
        secrets = yaml.safe_load(f)
    token = secrets.get('api_tokens', {{}}).get('{token_name}', '')
    if not token or '!' not in token:
        sys.exit(1)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        'https://localhost:8006/api2/json/version',
        headers={{'Authorization': f'PVEAPIToken={{token}}'}},
    )
    resp = urllib.request.urlopen(req, context=ctx, timeout=5)
    if resp.status == 200:
        print('valid')
    else:
        sys.exit(1)
except Exception:
    sys.exit(1)
"
'''
        rc, out, _ = run_ssh(host, check_cmd, user=config.vm_user, timeout=15)
        return rc == 0 and 'valid' in out


@dataclass
class BootstrapAction:
    """Bootstrap homestak on a remote host.

    Runs the bootstrap curl|bash installer on a target host. Integrates with
    serve-repos infrastructure when HOMESTAK_SERVER env var is set.

    Environment variables (from --serve-repos):
    - HOMESTAK_SERVER: Server URL for local repo access
    - HOMESTAK_TOKEN: Bearer token for authentication
    - HOMESTAK_REF: Git ref to use (default: _working)
    """
    name: str
    host_attr: str = 'vm_ip'
    source_url: Optional[str] = None  # HTTP server URL for dev workflow
    ref: str = 'master'  # Git ref for bootstrap
    timeout: int = 600

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Run bootstrap on target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        # Check for serve-repos env vars (dev workflow)
        env_source = os.environ.get('HOMESTAK_SERVER')
        env_token = os.environ.get('HOMESTAK_TOKEN')
        env_ref = os.environ.get('HOMESTAK_REF', '_working')

        # Build bootstrap command
        # Note: bootstrap needs sudo for apt/git operations, so we use 'sudo bash'
        if env_source:
            # Dev workflow: use HTTP server from --serve-repos
            # Pass env vars to bash (not curl) so install uses local (uncommitted) code
            # Use 'sudo env VAR=value bash' because 'VAR=value sudo bash' doesn't work -
            # sudo resets the environment by default for security
            env_prefix = f'HOMESTAK_SERVER={env_source}'
            if env_token:
                env_prefix += f' HOMESTAK_TOKEN={env_token}'
            env_prefix += f' HOMESTAK_REF={env_ref}'
            # Serve-repos uses self-signed TLS; pass -k to curl and
            # HOMESTAK_INSECURE=1 so install sets git http.sslVerify=false
            env_prefix += ' HOMESTAK_INSECURE=1'
            # Include Bearer token in curl header (serve-repos requires auth)
            auth_header = f'-H "Authorization: Bearer {env_token}"' if env_token else ''
            # Use 'sudo env' to pass vars through sudo's environment reset
            bootstrap_cmd = f'curl -fsSLk {auth_header} {env_source}/bootstrap.git/install | sudo env {env_prefix} bash'
            logger.info(f"[{self.name}] Using serve-repos source: {env_source} (ref={env_ref})")
        elif self.source_url:
            # Explicit source_url parameter (legacy)
            bootstrap_cmd = f'curl -fsSL {self.source_url}/install | sudo bash'
        else:
            # Production: use GitHub
            bootstrap_url = 'https://raw.githubusercontent.com/homestak/bootstrap'
            bootstrap_cmd = f'curl -fsSL {bootstrap_url}/{self.ref}/install | sudo bash'

        logger.info(f"[{self.name}] Bootstrapping {host}...")

        # Run bootstrap
        rc, out, err = run_ssh(
            host,
            bootstrap_cmd,
            user=config.vm_user,
            timeout=self.timeout
        )

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Bootstrap failed: {err or out}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Bootstrap completed on {host}")
        return ActionResult(
            success=True,
            message=f"Bootstrap completed on {host}",
            duration=time.time() - start
        )


@dataclass
class CopySecretsAction:
    """Copy secrets.yaml from driver host to target PVE node.

    Required for child PVE hosts to have valid API tokens and SSH keys.
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 60

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Copy secrets to target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        # Use scp to copy secrets.yaml
        from config import get_site_config_dir
        secrets_path = get_site_config_dir() / 'secrets.yaml'

        if not secrets_path.exists():
            enc_path = secrets_path.with_suffix('.yaml.enc')
            if enc_path.exists():
                msg = (f"secrets.yaml not decrypted at {secrets_path}\n"
                       f"  Run: cd {secrets_path.parent} && make decrypt")
            else:
                msg = f"secrets.yaml not found at {secrets_path}"
            return ActionResult(
                success=False,
                message=msg,
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Copying secrets to {host}...")

        # Scope secrets: exclude api_tokens (each PVE node generates its own)
        import yaml
        with open(secrets_path, encoding='utf-8') as f:
            secrets = yaml.safe_load(f) or {}
        secrets.pop('api_tokens', None)

        scoped_file = Path(tmpmod.mktemp(suffix='.yaml'))
        with open(scoped_file, 'w', encoding='utf-8') as f:
            yaml.dump(secrets, f, default_flow_style=False)

        # scp directly to ~/config/ (user-owned, no temp file dance needed)
        user = config.vm_user
        cmd = [
            'scp',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            str(scoped_file),
            f'{user}@{host}:config/secrets.yaml'
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )

            if result.returncode != 0:
                return ActionResult(
                    success=False,
                    message=f"Failed to copy secrets: {result.stderr}",
                    duration=time.time() - start
                )

            # Restrict permissions (secrets contain API tokens, SSH keys, signing key)
            rc, out, err = run_ssh(
                host, 'chmod 600 ~/config/secrets.yaml',
                user=config.vm_user, timeout=30
            )
            if rc != 0:
                return ActionResult(
                    success=False,
                    message=f"Failed to set secrets permissions: {err or out}",
                    duration=time.time() - start
                )

            return ActionResult(
                success=True,
                message=f"Secrets copied to {host}",
                duration=time.time() - start
            )

        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                message=f"Timeout copying secrets to {host}",
                duration=time.time() - start
            )
        finally:
            scoped_file.unlink(missing_ok=True)


@dataclass
class CopySiteConfigAction:
    """Copy site.yaml from driver host to target PVE node.

    Required for delegated PVE nodes to have DNS servers, gateway,
    and other site defaults for bridge configuration and child VM
    provisioning.
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 60

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Copy site.yaml to target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        from config import get_site_config_dir
        site_path = get_site_config_dir() / 'site.yaml'

        if not site_path.exists():
            return ActionResult(
                success=False,
                message=f"site.yaml not found at {site_path}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Copying site config to {host}...")

        # scp directly to ~/config/ (user-owned, no temp file dance needed)
        user = config.vm_user
        cmd = [
            'scp',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            str(site_path),
            f'{user}@{host}:config/site.yaml'
        ]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )

            if result.returncode != 0:
                return ActionResult(
                    success=False,
                    message=f"Failed to copy site config: {result.stderr}",
                    duration=time.time() - start
                )

            return ActionResult(
                success=True,
                message=f"Site config copied to {host}",
                duration=time.time() - start
            )

        except subprocess.TimeoutExpired:
            return ActionResult(
                success=False,
                message=f"Timeout copying site config to {host}",
                duration=time.time() - start
            )


@dataclass
class InjectSSHKeyAction:
    """Inject driver host's SSH public key into target PVE node's secrets.yaml.

    This is critical for SSH access to leaf VMs - the driver host's key must
    be in secrets.yaml so ConfigResolver includes it in cloud-init.
    """
    name: str
    host_attr: str = 'vm_ip'
    key_name: str = 'driver'  # Key name in secrets.yaml ssh_keys
    timeout: int = 60

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Inject SSH key into target host's secrets.yaml."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        # Read local SSH public key
        pubkey_path = Path.home() / '.ssh' / 'id_rsa.pub'
        if not pubkey_path.exists():
            pubkey_path = Path.home() / '.ssh' / 'id_ed25519.pub'
        if not pubkey_path.exists():
            return ActionResult(
                success=False,
                message="No SSH public key found (~/.ssh/id_rsa.pub or id_ed25519.pub)",
                duration=time.time() - start
            )

        pubkey = pubkey_path.read_text().strip()
        logger.info(f"[{self.name}] Injecting SSH key ({self.key_name}) to {host}...")

        # Escape the key for sed (forward slashes and ampersands)
        escaped_key = pubkey.replace('/', r'\/').replace('&', r'\&')

        # Inject key into secrets.yaml using sed
        # First check if key already exists
        check_cmd = f"grep -q '^\\s*{self.key_name}:' ~/config/secrets.yaml"
        rc, _, _ = run_ssh(host, check_cmd, user=config.vm_user, timeout=30)

        if rc == 0:
            # Key exists, update it
            inject_cmd = f"sed -i 's|^\\(\\s*\\){self.key_name}:.*$|\\1{self.key_name}: {escaped_key}|' ~/config/secrets.yaml"
        else:
            # Key doesn't exist, add it after ssh_keys:
            inject_cmd = f"sed -i '/^ssh_keys:/a\\  {self.key_name}: {escaped_key}' ~/config/secrets.yaml"

        rc, out, err = run_ssh(host, inject_cmd, user=config.vm_user, timeout=self.timeout)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to inject SSH key: {err or out}",
                duration=time.time() - start
            )

        # Verify the key was injected
        verify_cmd = f"grep -q '{self.key_name}:' ~/config/secrets.yaml"
        rc, _, _ = run_ssh(host, verify_cmd, user=config.vm_user, timeout=30)
        if rc != 0:
            return ActionResult(
                success=False,
                message="SSH key injection verification failed",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] SSH key injected on {host}")
        return ActionResult(
            success=True,
            message=f"SSH key ({self.key_name}) injected on {host}",
            duration=time.time() - start
        )


@dataclass
class CopySSHPrivateKeyAction:
    """Copy driver host's SSH private key to target PVE node.

    This enables the PVE node to SSH to its child VMs. The private key is
    copied to both root and homestak users so that:
    - root: ansible connections work
    - homestak: iac-driver automation_user connections work
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 60

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Copy SSH private key to target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        # Read local SSH private key
        privkey_path = Path.home() / '.ssh' / 'id_rsa'
        pubkey_path = Path.home() / '.ssh' / 'id_rsa.pub'
        if not privkey_path.exists():
            privkey_path = Path.home() / '.ssh' / 'id_ed25519'
            pubkey_path = Path.home() / '.ssh' / 'id_ed25519.pub'
        if not privkey_path.exists():
            return ActionResult(
                success=False,
                message="No SSH private key found (~/.ssh/id_rsa or id_ed25519)",
                duration=time.time() - start
            )

        privkey = privkey_path.read_text()
        pubkey = pubkey_path.read_text().strip() if pubkey_path.exists() else ''

        logger.info(f"[{self.name}] Copying SSH private key to {host}...")

        # Copy private key to both root and homestak users
        # Using base64 encoding to avoid shell escaping issues with the key content
        privkey_b64 = base64.b64encode(privkey.encode()).decode()
        pubkey_b64 = base64.b64encode(pubkey.encode()).decode() if pubkey else ''

        copy_script = f'''
set -e
PRIVKEY=$(echo '{privkey_b64}' | base64 -d)
PUBKEY=$(echo '{pubkey_b64}' | base64 -d)

# Copy to homestak user's ~/.ssh/
mkdir -p ~/.ssh
chmod 700 ~/.ssh
echo "$PRIVKEY" > ~/.ssh/id_rsa
chmod 600 ~/.ssh/id_rsa
[ -n "$PUBKEY" ] && echo "$PUBKEY" > ~/.ssh/id_rsa.pub
[ -f ~/.ssh/id_rsa.pub ] && chmod 644 ~/.ssh/id_rsa.pub

# Ensure pubkey is in authorized_keys (provider SSH-to-self)
if [ -n "$PUBKEY" ]; then
    touch ~/.ssh/authorized_keys
    chmod 600 ~/.ssh/authorized_keys
    grep -qF "$PUBKEY" ~/.ssh/authorized_keys 2>/dev/null || echo "$PUBKEY" >> ~/.ssh/authorized_keys
fi

echo "SSH key copied to ~/.ssh/"
'''

        rc, out, err = run_ssh(host, copy_script, user=config.vm_user, timeout=self.timeout)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to copy SSH key: {err or out}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] SSH private key copied to {host}")
        return ActionResult(
            success=True,
            message=f"SSH private key copied to {host}",
            duration=time.time() - start
        )


@dataclass
class InjectSelfSSHKeyAction:
    """Inject a host's own SSH public key into its secrets.yaml.

    This enables the host to SSH to VMs it provisions - the VM's cloud-init
    will include this key in authorized_keys.
    """
    name: str
    host_attr: str = 'vm_ip'
    key_name: str = 'self'  # Key name in secrets.yaml ssh_keys
    timeout: int = 60

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Inject host's own SSH key into its secrets.yaml."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Injecting {host}'s own SSH key as {self.key_name}...")

        # Inject via Python script encoded in base64 to avoid shell quoting issues
        python_script = '''
import sys, os
key_name = sys.argv[1]
secrets_file = os.path.expanduser("~/config/secrets.yaml")

# Find public key
pubkey = None
home = os.path.expanduser("~")
for keyfile in [f"{home}/.ssh/id_ed25519.pub", f"{home}/.ssh/id_rsa.pub"]:
    try:
        with open(keyfile) as f:
            pubkey = f.read().strip()
            break
    except FileNotFoundError:
        continue

if not pubkey:
    print("No SSH public key found")
    sys.exit(1)

with open(secrets_file, "r") as f:
    lines = f.readlines()

key_exists = any(key_name + ":" in line for line in lines)

with open(secrets_file, "w") as f:
    for line in lines:
        if key_name + ":" in line:
            indent = len(line) - len(line.lstrip())
            f.write(" " * indent + key_name + ": " + pubkey + "\\n")
        else:
            f.write(line)
            if not key_exists and line.strip() == "ssh_keys:":
                f.write("  " + key_name + ": " + pubkey + "\\n")
                key_exists = True

# Verify
with open(secrets_file, "r") as f:
    if key_name + ":" not in f.read():
        print("Verification failed")
        sys.exit(1)
print(f"Injected {key_name}")
'''
        encoded = base64.b64encode(python_script.encode()).decode()
        inject_script = f"echo '{encoded}' | base64 -d | python3 - {self.key_name}"

        rc, out, err = run_ssh(host, inject_script, user=config.vm_user, timeout=self.timeout)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to inject self SSH key: {err or out}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Self SSH key injected on {host}")
        return ActionResult(
            success=True,
            message=f"Self SSH key ({self.key_name}) injected on {host}",
            duration=time.time() - start
        )


@dataclass
class ConfigureNetworkBridgeAction:
    """Configure vmbr0 network bridge on PVE node.

    Creates vmbr0 bridge from eth0 (required for nested VMs to get network).
    Uses a simple shell script rather than ansible for speed.
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 120

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Configure vmbr0 bridge on target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Configuring vmbr0 bridge on {host}...")

        # Check if vmbr0 already exists
        check_cmd = "ip link show vmbr0 2>/dev/null && ip addr show vmbr0 | grep -q 'inet '"
        rc, out, err = run_ssh(host, check_cmd, user=config.vm_user, timeout=30)
        if rc == 0:
            logger.info(f"[{self.name}] vmbr0 already exists on {host}")
            # Ensure DNS is configured on vmbr0 even if bridge already exists (#229)
            if config.dns_servers:
                dns_cmd = f'sudo resolvectl dns vmbr0 {" ".join(config.dns_servers)} 2>/dev/null || true'
                run_ssh(host, dns_cmd, user=config.vm_user, timeout=30)
                logger.info(f"[{self.name}] DNS configured on vmbr0: {config.dns_servers}")
            return ActionResult(
                success=True,
                message=f"vmbr0 already configured on {host}",
                duration=time.time() - start
            )

        # Build DNS config lines (#229)
        dns_line = ''
        dns_resolvectl = ''
        if config.dns_servers:
            dns_line = f'    dns-nameservers {" ".join(config.dns_servers)}'
            dns_resolvectl = f'sudo resolvectl dns vmbr0 {" ".join(config.dns_servers)} 2>/dev/null || true'

        # Script to create vmbr0 bridge from eth0 with DHCP
        # This preserves the current IP during transition
        # Uses sudo for privileged operations
        bridge_script = f'''
set -e

# Get current interface info
IFACE=$(ip -o route get 8.8.8.8 2>/dev/null | grep -oP 'dev \\K\\S+' || echo eth0)
echo "Detected interface: $IFACE"

# Backup interfaces
sudo cp /etc/network/interfaces /etc/network/interfaces.backup.$(date +%s) 2>/dev/null || true

# Create bridge config with DHCP
sudo tee /etc/network/interfaces > /dev/null << 'IFACE_EOF'
auto lo
iface lo inet loopback

iface eth0 inet manual

auto vmbr0
iface vmbr0 inet dhcp
    bridge-ports eth0
    bridge-stp off
    bridge-fd 0
{dns_line}
IFACE_EOF

# Apply network configuration
# Use systemctl to restart networking
sudo systemctl restart networking 2>/dev/null || (sudo ifdown eth0; sudo ifup vmbr0)

# Wait for bridge to get IP
for i in $(seq 1 30); do
    if ip addr show vmbr0 | grep -q 'inet '; then
        echo "vmbr0 configured successfully"
        ip addr show vmbr0 | grep 'inet '
        # Configure DNS on vmbr0 for systemd-resolved (#229)
        {dns_resolvectl}
        exit 0
    fi
    sleep 1
done

echo "Warning: vmbr0 did not get IP within 30s"
exit 0
'''

        rc, out, err = run_ssh(host, bridge_script, user=config.vm_user, timeout=self.timeout)
        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to configure vmbr0: {err or out}",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] vmbr0 configured on {host}")
        return ActionResult(
            success=True,
            message=f"vmbr0 bridge configured on {host}",
            duration=time.time() - start
        )



@dataclass
class GenerateNodeConfigAction:
    """Generate node config on target PVE node.

    Runs 'make node-config' on the target PVE node to generate the
    nodes/{hostname}.yaml file needed for tofu operations.
    """
    name: str
    host_attr: str = 'vm_ip'
    timeout: int = 120

    def run(self, config: HostConfig, context: dict) -> ActionResult:
        """Generate node config on target host."""
        start = time.time()

        host = context.get(self.host_attr)
        if not host:
            return ActionResult(
                success=False,
                message=f"No {self.host_attr} in context",
                duration=time.time() - start
            )

        logger.info(f"[{self.name}] Generating node config on {host}...")

        # Use FORCE=1 in case node config was copied from outer host
        cmd = 'cd ~/config && make node-config FORCE=1'
        rc, out, err = run_ssh(host, cmd, user=config.vm_user, timeout=self.timeout)

        if rc != 0:
            return ActionResult(
                success=False,
                message=f"Failed to generate node config: {err or out}",
                duration=time.time() - start
            )

        return ActionResult(
            success=True,
            message=f"Node config generated on {host}",
            duration=time.time() - start
        )
