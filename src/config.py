"""Host configuration management.

Configuration is loaded from config YAML files:
- site.yaml: Site-wide defaults
- secrets.yaml: All sensitive values (decrypted)
- nodes/*.yaml: PVE instance configuration (post-PVE install)
- hosts/*.yaml: Physical machine configuration (pre-PVE, SSH access only)
- envs/*.yaml: Environment configuration (for tofu)

Resolution order for --host:
1. nodes/{host}.yaml - PVE node with API access (existing behavior)
2. hosts/{host}.yaml - SSH-only access for pre-PVE hosts (fallback)

The merge order is: site → node/host, with secrets resolved by key reference.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


class ConfigError(Exception):
    """Configuration error."""


@dataclass
class HostConfig:
    """Configuration for a target host/node.

    Supports two config sources:
    - nodes/*.yaml: PVE node with API access (has api_endpoint, api_token)
    - hosts/*.yaml: Physical machine with SSH access only (pre-PVE install)

    When loaded from hosts/*.yaml, is_host_only=True and PVE-specific
    fields (api_endpoint, api_token) will be empty.
    """
    name: str
    config_file: Path
    api_endpoint: str = ''
    ssh_host: str = ''
    inner_vm_id: int = 99800  # Match config vmid_base for child PVE nodes
    test_vm_id: int = 99900   # Match config/envs/test.yaml vmid_base
    ssh_user: str = field(default_factory=lambda: os.getenv('USER', ''))
    automation_user: str = 'homestak'  # For SSH to VMs (created via cloud-init)
    ssh_key: Path = field(default_factory=lambda: Path.home() / '.ssh' / 'id_rsa')

    # Packer release settings
    packer_release_repo: str = 'homestak-dev/packer'
    packer_release: str = 'latest'
    packer_image: str = 'debian-12.qcow2'

    # DNS servers from site.yaml (for bridge config, #229)
    dns_servers: list = field(default_factory=list)

    # Spec server URL from site.yaml defaults (e.g., "https://controller:44443")
    spec_server: str = ''

    # Track config source type
    is_host_only: bool = False  # True when loaded from hosts/*.yaml (no PVE)

    # API token (resolved from secrets.yaml at load time)
    _api_token: str = field(default='', init=False, repr=False)

    def __post_init__(self):
        if isinstance(self.config_file, str):
            self.config_file = Path(self.config_file)
        if isinstance(self.ssh_key, str):
            self.ssh_key = Path(self.ssh_key)

        # Read config from file if it exists
        if self.config_file.exists():
            if self.config_file.parent.name == 'hosts':
                self._load_from_host_yaml()
            else:
                self._load_from_yaml()

        # Derive ssh_host from api_endpoint if not set
        if not self.ssh_host and self.api_endpoint:
            self.ssh_host = urlparse(self.api_endpoint).hostname or ''

    def _load_from_yaml(self):
        """Load configuration from YAML file with secrets resolution."""
        if yaml is None:
            raise ConfigError("PyYAML not installed. Run: apt install python3-yaml")

        site_config_dir = self.config_file.parent.parent

        # Load site defaults
        site_file = site_config_dir / 'site.yaml'
        site_defaults = {}
        if site_file.exists():
            site_defaults = _parse_yaml(site_file).get('defaults', {})

        # Load node config
        node_config = _parse_yaml(self.config_file)

        # Load secrets for resolution
        secrets = _load_secrets(site_config_dir)

        # Apply values with merge order: site → node
        if not self.api_endpoint:
            self.api_endpoint = node_config.get('api_endpoint', '')

        # Resolve api_token from secrets
        api_token_key = node_config.get('api_token', self.name)
        if secrets and 'api_tokens' in secrets:
            # Store resolved token for use by scenarios
            self._api_token = secrets['api_tokens'].get(api_token_key, '')

        # SSH user: node > site > default (for PVE host connections)
        if ssh_user := node_config.get('ssh_user', site_defaults.get('ssh_user')):
            self.ssh_user = ssh_user

        # Automation user: for SSH to VMs created via cloud-init
        if automation_user := site_defaults.get('automation_user'):
            self.automation_user = automation_user

        # Packer release: site.yaml > default
        if packer_release := site_defaults.get('packer_release'):
            self.packer_release = packer_release

        # DNS servers: site.yaml (for bridge config, #229)
        if dns_servers := site_defaults.get('dns_servers'):
            self.dns_servers = dns_servers

        # Spec server URL: site.yaml (for server daemon management, #203)
        if spec_server := site_defaults.get('spec_server'):
            self.spec_server = spec_server

    def _load_from_host_yaml(self):
        """Load configuration from hosts/*.yaml (SSH-only, pre-PVE).

        Hosts files contain physical machine info for SSH access before
        PVE is installed. No api_endpoint or api_token available.
        """
        if yaml is None:
            raise ConfigError("PyYAML not installed. Run: apt install python3-yaml")

        self.is_host_only = True
        site_config_dir = self.config_file.parent.parent

        # Load site defaults
        site_file = site_config_dir / 'site.yaml'
        site_defaults = {}
        if site_file.exists():
            site_defaults = _parse_yaml(site_file).get('defaults', {})

        # Load host config
        host_config = _parse_yaml(self.config_file)

        # Extract SSH host IP from network config or explicit ip field
        if not self.ssh_host:
            # Try explicit ip field first
            if ip := host_config.get('ip'):
                self.ssh_host = ip
            # Fall back to vmbr0 address (strip CIDR notation)
            elif network := (host_config.get('network') or {}).get('interfaces') or {}:
                if address := (network.get('vmbr0') or {}).get('address'):
                    # Strip CIDR suffix (e.g., "198.51.100.61/24" -> "198.51.100.61")
                    self.ssh_host = address.split('/')[0]

        # SSH user from access section or site defaults
        if access := host_config.get('access', {}):
            if ssh_user := access.get('ssh_user'):
                self.ssh_user = ssh_user
        elif ssh_user := site_defaults.get('ssh_user'):
            self.ssh_user = ssh_user

        # Automation user: for SSH to VMs created via cloud-init
        if automation_user := site_defaults.get('automation_user'):
            self.automation_user = automation_user

        # Packer release: site.yaml > default
        if packer_release := site_defaults.get('packer_release'):
            self.packer_release = packer_release

        # DNS servers: site.yaml (for bridge config, #229)
        if dns_servers := site_defaults.get('dns_servers'):
            self.dns_servers = dns_servers

        # Spec server URL: site.yaml (for server daemon management, #203)
        if spec_server := site_defaults.get('spec_server'):
            self.spec_server = spec_server

        # No api_endpoint or api_token for host-only configs
        # These remain empty strings (defaults)

    def get_api_token(self) -> str:
        """Get resolved API token (from secrets.yaml)."""
        return getattr(self, '_api_token', '')

    def set_api_token(self, token: str) -> None:
        """Set API token (for local config auto-discovery)."""
        self._api_token = token


def _parse_yaml(path: Path) -> dict:
    """Parse a YAML file and return contents."""
    if yaml is None:
        raise ConfigError("PyYAML not installed. Run: apt install python3-yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_secrets(site_config_dir: Path) -> Optional[dict]:
    """Load decrypted secrets from secrets.yaml."""
    secrets_file = site_config_dir / 'secrets.yaml'
    if not secrets_file.exists():
        return None
    try:
        return _parse_yaml(secrets_file)
    except Exception:
        return None


def get_base_dir() -> Path:
    """Get the iac-driver directory."""
    return Path(__file__).parent.parent  # src/ -> iac-driver/


def get_sibling_dir(name: str) -> Path:
    """Get a sibling repo directory (ansible, tofu, packer)."""
    return get_base_dir().parent / name  # iac-driver/.parent / "ansible" = iac/ansible/


def get_site_config_dir() -> Path:
    """Discover config directory.

    Derived from $HOMESTAK_ROOT/config. On installed hosts, $HOME is the
    workspace root (default). On dev workstations, set HOMESTAK_ROOT explicitly.
    """
    root = Path(os.environ.get('HOMESTAK_ROOT', str(Path.home())))
    config_dir = root / 'config'
    if config_dir.exists():
        return config_dir

    raise ConfigError(
        f"config not found at {config_dir}. "
        "Set HOMESTAK_ROOT to your workspace root directory."
    )



def list_hosts() -> list[str]:
    """List available hosts/nodes from config.

    Combines hosts from multiple sources (deduplicated):
    1. nodes/*.yaml - PVE nodes (have API access)
    2. hosts/*.yaml - Physical machines (SSH access only)
    """
    try:
        site_config = get_site_config_dir()
    except ConfigError:
        return []

    hosts: set[str] = set()

    # nodes/*.yaml - PVE nodes
    nodes_dir = site_config / 'nodes'
    if nodes_dir.exists():
        hosts.update(f.stem for f in nodes_dir.glob('*.yaml') if f.is_file())

    # hosts/*.yaml - Physical machines (pre-PVE)
    hosts_dir = site_config / 'hosts'
    if hosts_dir.exists():
        hosts.update(f.stem for f in hosts_dir.glob('*.yaml') if f.is_file())

    return sorted(hosts)


def load_host_config(host: str) -> HostConfig:
    """Load configuration for a named host/node.

    Resolution order:
    1. nodes/{host}.yaml - PVE node with API access
    2. hosts/{host}.yaml - Physical machine with SSH access (pre-PVE)

    When loaded from hosts/*.yaml, the returned config has is_host_only=True
    and PVE-specific fields (api_endpoint, api_token) are empty.
    """
    site_config = get_site_config_dir()

    # 1. Try nodes/*.yaml (PVE node with API access)
    node_file = site_config / 'nodes' / f'{host}.yaml'
    if node_file.exists():
        return HostConfig(name=host, config_file=node_file)

    # 2. Try hosts/*.yaml (SSH-only, pre-PVE)
    host_file = site_config / 'hosts' / f'{host}.yaml'
    if host_file.exists():
        return HostConfig(name=host, config_file=host_file)

    # Build helpful error message
    available = list_hosts()
    node_path = site_config / 'nodes' / f'{host}.yaml'
    host_path = site_config / 'hosts' / f'{host}.yaml'

    raise ValueError(
        f"Host '{host}' not found.\n"
        f"  - No node config: {node_path} (PVE not installed?)\n"
        f"  - No host config: {host_path} (physical machine)\n\n"
        f"Available hosts: {', '.join(available) if available else 'none configured'}\n\n"
        f"To provision a new host, create {host_path}:\n"
        f"  ssh root@<ip> \"cd ~/config && make host-config\""
    )


def load_secrets() -> dict:
    """Load all secrets from config/secrets.yaml."""
    site_config = get_site_config_dir()
    secrets = _load_secrets(site_config)
    if secrets is None:
        raise ConfigError(
            "secrets.yaml not found or not decrypted. "
            "Run: cd ../config && make decrypt"
        )
    return secrets
