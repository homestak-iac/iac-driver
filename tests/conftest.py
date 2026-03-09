"""Shared pytest fixtures for iac-driver tests."""

import base64
import hashlib
import hmac
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent / 'src'))


# -- Shared test doubles -----------------------------------------------------

@dataclass
class MockHostConfig:
    """Minimal host config for testing.

    Superset of fields used across action test files.
    Override defaults per-test as needed:
        MockHostConfig(ssh_host='198.51.100.10', vm_id=12345)
    """
    name: str = 'test-host'
    ssh_host: str = '192.0.2.1'
    ssh_user: str = 'root'
    automation_user: str = 'homestak'
    vm_id: int = 99913
    config_file: Path = Path('/tmp/test.yaml')


# Test signing key (256-bit, hex-encoded)
TEST_SIGNING_KEY = "a" * 64  # 32 bytes = 256 bits


def mint_test_token(node: str, spec: str, signing_key: str = TEST_SIGNING_KEY,
                    **overrides) -> str:
    """Mint a valid provisioning token for testing."""
    payload = {"v": 1, "n": node, "s": spec, "iat": int(time.time())}
    payload.update(overrides)
    payload_bytes = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(',', ':')).encode()
    ).rstrip(b'=')
    signature = hmac.new(
        bytes.fromhex(signing_key),
        payload_bytes,
        hashlib.sha256,
    ).digest()
    sig_bytes = base64.urlsafe_b64encode(signature).rstrip(b'=')
    return f"{payload_bytes.decode()}.{sig_bytes.decode()}"


def _has_infrastructure():
    """Check if real config infrastructure is available."""
    try:
        from resolver.base import discover_etc_path
        etc_path = discover_etc_path()
        secrets_file = etc_path / 'secrets.yaml'
        if not secrets_file.exists():
            return False
        # Check secrets.yaml has real content (not just a stub)
        content = secrets_file.read_text()
        return 'api_tokens' in content and len(content) > 50
    except Exception:
        return False


def pytest_collection_modifyitems(config, items):
    """Skip tests marked with requires_infrastructure when infra not available."""
    if _has_infrastructure():
        return
    skip_marker = pytest.mark.skip(reason="requires infrastructure (config with decrypted secrets)")
    for item in items:
        if "requires_infrastructure" in item.keywords:
            item.add_marker(skip_marker)


@pytest.fixture
def site_config_dir(tmp_path):
    """Create temporary config directory structure.

    Creates minimal config with:
    - site.yaml (defaults)
    - secrets.yaml (mock secrets)
    - nodes/test-node.yaml
    - envs/test.yaml
    - presets/vm-small.yaml
    - vms/debian-12.yaml
    - postures/dev.yaml
    - postures/prod.yaml
    """
    # Create directories
    for d in ['nodes', 'envs', 'vms', 'presets', 'postures', 'hosts']:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    # Create site.yaml (v0.13: packages and pve settings, v0.45: spec_server)
    (tmp_path / 'site.yaml').write_text("""
defaults:
  timezone: America/Denver
  bridge: vmbr0
  ssh_user: root
  gateway: 198.51.100.1
  dns_servers:
    - 198.51.100.1
  packages:
    - htop
    - curl
    - wget
  pve_remove_subscription_nag: true
  spec_server: "https://controller:44443"
""")

    # Create secrets.yaml (signing_key for provisioning tokens, #231)
    (tmp_path / 'secrets.yaml').write_text("""
api_tokens:
  test-node: "user@pam!token=secret"
passwords:
  vm_root: "$6$rounds=4096$hash"
ssh_keys:
  key1: "ssh-rsa AAAA... user1"
  key2: "ssh-ed25519 AAAA... user2"
auth:
  signing_key: """ + '"' + 'a' * 64 + '"' + """
""")

    # Create node config (datastore required)
    (tmp_path / 'nodes/test-node.yaml').write_text("""
node: test-node
api_endpoint: https://198.51.100.10:8006
api_token: test-node
datastore: local-zfs
""")

    # Create postures (nested format with auth model)
    (tmp_path / 'postures/dev.yaml').write_text("""
auth:
  method: network
ssh:
  port: 22
  permit_root_login: "yes"
  password_authentication: "yes"
sudo:
  nopasswd: true
fail2ban:
  enabled: false
packages:
  - net-tools
  - strace
""")

    (tmp_path / 'postures/prod.yaml').write_text("""
auth:
  method: node_token
ssh:
  port: 22
  permit_root_login: "no"
  password_authentication: "no"
sudo:
  nopasswd: false
fail2ban:
  enabled: true
packages: []
""")

    (tmp_path / 'postures/stage.yaml').write_text("""
auth:
  method: site_token
ssh:
  port: 22
  permit_root_login: "prohibit-password"
  password_authentication: "no"
sudo:
  nopasswd: false
fail2ban:
  enabled: true
packages: []
""")

    # Create preset
    (tmp_path / 'presets/vm-small.yaml').write_text("""
cores: 1
memory: 2048
disk: 20
""")

    # Create template
    (tmp_path / 'vms/debian-12.yaml').write_text("""
preset: vm-small
image: debian-12.img
packages:
  - qemu-guest-agent
""")

    # Create environment (with posture FK)
    (tmp_path / 'envs/test.yaml').write_text("""
posture: dev
vmid_base: 99900
vms:
  - name: test1
    template: debian-12
    ip: 198.51.100.10/24
  - name: test2
    template: debian-12
    ip: dhcp
  - name: test3
    template: debian-12
    cores: 2
    vmid: 99999
""")

    return tmp_path


@pytest.fixture
def site_config_without_datastore(tmp_path):
    """Site config with node missing datastore (for error tests)."""
    for d in ['nodes', 'envs', 'vms']:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    (tmp_path / 'site.yaml').write_text("""
defaults:
  timezone: UTC
""")

    (tmp_path / 'secrets.yaml').write_text("""
api_tokens: {}
""")

    # Node WITHOUT datastore - should trigger error
    (tmp_path / 'nodes/bad-node.yaml').write_text("""
node: bad-node
api_endpoint: https://198.51.100.10:8006
""")

    (tmp_path / 'envs/test.yaml').write_text("""
vmid_base: 99900
vms: []
""")

    return tmp_path


@pytest.fixture
def site_config_without_posture(tmp_path):
    """Site config with env missing posture FK (for fallback tests)."""
    for d in ['nodes', 'envs', 'vms', 'postures']:
        (tmp_path / d).mkdir(parents=True, exist_ok=True)

    (tmp_path / 'site.yaml').write_text("""
defaults:
  timezone: America/Denver
  packages: []
""")

    (tmp_path / 'secrets.yaml').write_text("""
api_tokens:
  test-node: "token"
ssh_keys: {}
""")

    (tmp_path / 'nodes/test-node.yaml').write_text("""
node: test-node
api_endpoint: https://198.51.100.10:8006
datastore: local-zfs
""")

    # Dev posture for fallback (nested format)
    (tmp_path / 'postures/dev.yaml').write_text("""
auth:
  method: network
ssh:
  port: 22
sudo:
  nopasswd: true
packages: []
""")

    # Env WITHOUT posture - should fall back to dev
    (tmp_path / 'envs/no-posture.yaml').write_text("""
vmid_base: 99900
vms: []
""")

    return tmp_path


@pytest.fixture
def mock_context():
    """Common context dict for action tests."""
    return {
        'node_ip': '198.51.100.10',
        'provisioned_vms': [
            {'name': 'test1', 'vmid': 99900},
        ],
        'test1_vm_id': 99900,
    }
