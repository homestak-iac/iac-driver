"""Config resolution for site-config YAML files.

Resolves site-config entities (site, secrets, nodes, presets, postures) into
flat configurations suitable for tofu and ansible. All preset
inheritance is resolved here, so consumers receive fully-computed values.

Resolution order (tofu):
1. presets/{vm_preset}.yaml (VM size: cores, memory, disk)
2. Inline VM overrides (name, vmid, image) from manifest nodes or CLI
3. Provisioning token minted with spec FK (#231)

Resolution order (ansible):
1. site.yaml defaults (timezone, packages, pve settings)
2. postures/{posture}.yaml (security settings from env's posture FK)
3. Packages merged: site packages + posture packages (deduplicated)

Provisioning token (#231):
HMAC-SHA256 signed token carrying node name and spec FK.
Replaces posture-based auth (network/site_token/node_token).
"""

import base64
import hashlib
import hmac
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from config import ConfigError, get_site_config_dir, _parse_yaml, _load_secrets


class ConfigResolver:
    """Resolves site-config YAML into flat VM specs for tofu."""

    def __init__(self, site_config_path: Optional[str] = None):
        """Initialize resolver with site-config path.

        Args:
            site_config_path: Path to site-config directory. If None, uses
                              auto-discovery (env var, sibling, ~/etc).
        """
        if yaml is None:
            raise ConfigError("PyYAML not installed. Run: apt install python3-yaml")

        if site_config_path:
            self.path = Path(site_config_path)
        else:
            self.path = get_site_config_dir()

        self.site = self._load_yaml("site.yaml")
        self.secrets = _load_secrets(self.path) or {}
        self.vm_presets = self._load_dir("presets")
        self.postures = self._load_dir("postures")

    def _load_yaml(self, relative_path: str) -> dict:
        """Load a YAML file from site-config directory."""
        path = self.path / relative_path
        if not path.exists():
            return {}
        result: dict = _parse_yaml(path)
        return result

    def _load_dir(self, relative_path: str) -> dict:
        """Load all YAML files in a directory as dict keyed by filename stem."""
        path = self.path / relative_path
        if not path.exists():
            return {}
        result = {}
        for f in path.glob("*.yaml"):
            if f.is_file():
                result[f.stem] = _parse_yaml(f)
        return result

    def _get_signing_key(self) -> str:
        """Get the provisioning token signing key from secrets.

        Returns:
            Hex-encoded 256-bit signing key

        Raises:
            ConfigError: If signing key is not configured
        """
        auth = self.secrets.get("auth", {})
        key = auth.get("signing_key", "")
        if not key:
            raise ConfigError(
                "auth.signing_key not found in secrets.yaml. "
                "Generate with: python3 -c \"import secrets; print(secrets.token_hex(32))\""
            )
        return str(key)

    def _mint_provisioning_token(self, node_name: str, spec_name: str) -> str:
        """Mint a signed provisioning token for a VM.

        Creates an HMAC-SHA256 signed token carrying the node identity
        and spec FK. The token is the sole auth artifact for spec fetching.

        Args:
            node_name: VM hostname (becomes 'n' claim)
            spec_name: Spec FK (becomes 's' claim, resolves to specs/{s}.yaml)

        Returns:
            Signed token: base64url(payload).base64url(signature)

        Raises:
            ConfigError: If signing key is not configured
        """
        signing_key = self._get_signing_key()

        payload = {
            "v": 1,
            "n": node_name,
            "s": spec_name,
            "iat": int(time.time()),
        }

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

    def resolve_inline_vm(
        self,
        node: str,
        vm_name: str,
        vmid: int,
        vm_preset: Optional[str] = None,
        image: Optional[str] = None,
        spec: Optional[str] = None,
    ) -> dict:
        """Resolve inline VM definition.

        VM is defined by direct parameters (vm_name, vmid, preset)
        from manifest nodes or CLI flags.

        Args:
            node: Target PVE node name (matches nodes/{node}.yaml)
            vm_name: VM hostname
            vmid: Explicit VM ID
            vm_preset: Preset name (matches presets/{vm_preset}.yaml)
            image: Image name (required for vm_preset mode)
            spec: Spec FK for provisioning token (specs/{spec}.yaml)

        Returns:
            Dict with all resolved config ready for tfvars.json
        """
        if not vm_preset:
            raise ConfigError("resolve_inline_vm requires vm_preset")

        node_config = self._load_yaml(f"nodes/{node}.yaml")

        if not node_config:
            raise ConfigError(f"Node config not found: nodes/{node}.yaml")

        if 'datastore' not in node_config:
            raise ConfigError(
                f"Node '{node}' missing required 'datastore' in nodes/{node}.yaml. "
                f"Run 'make node-config FORCE=1' in site-config to regenerate."
            )

        # Resolve API token from secrets
        api_token_key = node_config.get("api_token", node)
        api_token = self.secrets.get("api_tokens", {}).get(api_token_key, "")

        # Site defaults
        defaults = self.site.get("defaults", {})

        # Spec server for Create → Config flow (#231: HOMESTAK_SERVER)
        # In nested deployments, the inner operator sets HOMESTAK_SOURCE to
        # the local server URL. Use it as override so VMs reach the nearest
        # server, not the outer host from site.yaml.
        spec_server = os.environ.get("HOMESTAK_SOURCE") or defaults.get("spec_server", "")

        # Build VM instance dict for _resolve_vm
        vm_instance = {
            "name": vm_name,
            "vmid": vmid,
        }
        if vm_preset:
            vm_instance["vm_preset"] = vm_preset
        if image:
            vm_instance["image"] = image

        # Resolve the single VM
        resolved_vm = self._resolve_vm(vm_instance, vmid, defaults)
        # Mint provisioning token if spec server and spec FK are configured (#231)
        if spec_server and spec:
            resolved_vm["auth_token"] = self._mint_provisioning_token(vm_name, spec)
        else:
            resolved_vm["auth_token"] = ""

        # Resolve passwords and SSH keys from secrets
        passwords = self.secrets.get("passwords", {})
        ssh_keys_dict = self.secrets.get("ssh_keys", {})
        ssh_keys_list = list(ssh_keys_dict.values())

        # Determine SSH host for file uploads
        api_endpoint = node_config.get("api_endpoint", "")
        if "localhost" in api_endpoint or "127.0.0.1" in api_endpoint:
            ssh_host = "127.0.0.1"
        else:
            ssh_host = node_config.get("ip", "")

        return {
            "node": node_config.get("node", node),
            "api_endpoint": api_endpoint,
            "api_token": api_token,
            "ssh_private_key_file": self._find_ssh_private_key(),
            "automation_user": defaults.get("automation_user", "homestak"),
            "ssh_host": ssh_host,
            "datastore": node_config["datastore"],
            "root_password": passwords.get("vm_root", ""),
            "ssh_keys": ssh_keys_list,
            # Spec server for Create → Specify flow (v0.45+)
            "spec_server": spec_server,
            # DNS servers for cloud-init (v0.51+, #229)
            "dns_servers": defaults.get("dns_servers", []),
            "vms": [resolved_vm],
        }

    @staticmethod
    def _find_ssh_private_key() -> str:
        """Find RSA SSH private key for the bpg/proxmox provider.

        Only RSA is supported — the provider's Go SSH library cannot
        parse OpenSSH-format ed25519 keys.
        """
        key_path = Path.home() / '.ssh' / 'id_rsa'
        return str(key_path)

    def _resolve_vm(self, vm_instance: dict, default_vmid: Optional[int], defaults: dict) -> dict:
        """Resolve VM instance with vm_preset inheritance.

        Merge order: vm_preset → instance overrides

        Args:
            vm_instance: VM instance from manifest nodes[] or CLI parameters
            default_vmid: Auto-computed vmid (base + index), or None for PVE auto-assign
            defaults: Site defaults from site.yaml

        Returns:
            Fully resolved VM configuration
        """
        direct_vm_preset_name = vm_instance.get("vm_preset")

        if direct_vm_preset_name:
            # Preset mode: vm_preset → instance (no template)
            base: dict = self.vm_presets.get(direct_vm_preset_name, {}).copy()
            if not base:
                raise ConfigError(f"Preset not found: presets/{direct_vm_preset_name}.yaml")
        else:
            # No template or vm_preset - start with empty base
            base = {}

        # Layer 3: Instance overrides
        for key, value in vm_instance.items():
            if key != "vm_preset":  # Don't include meta key in final output
                base[key] = value

        # Layer 4: Default vmid if not specified
        if "vmid" not in base and default_vmid is not None:
            base["vmid"] = default_vmid

        # Apply site defaults for optional fields
        if "bridge" not in base:
            base["bridge"] = defaults.get("bridge", "vmbr0")

        # Apply gateway default for static IPs
        if "gateway" not in base and "gateway" in defaults:
            base["gateway"] = defaults.get("gateway")

        # Validate IP format
        if "ip" in base:
            self._validate_ip(base["ip"], base.get("name", "unknown"))

        return base

    def _validate_ip(self, ip: Any, vm_name: str) -> None:
        """Validate IP is 'dhcp', None, or valid CIDR notation.

        Args:
            ip: IP value from config
            vm_name: VM name for error context

        Raises:
            ConfigError: If IP format is invalid
        """
        if ip is None or ip == "dhcp":
            return

        # Must be a string at this point
        if not isinstance(ip, str):
            raise ConfigError(
                f"Invalid IP type for VM '{vm_name}': expected string, got {type(ip).__name__}"
            )

        # IPv4 CIDR: x.x.x.x/y where y is 0-32
        ipv4_cidr = r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})/(\d{1,2})$'
        match = re.match(ipv4_cidr, ip)

        if not match:
            raise ConfigError(
                f"Invalid IP format for VM '{vm_name}': '{ip}'. "
                f"Static IPs must use CIDR notation (e.g., '198.51.100.124/24'). "
                f"Use 'dhcp' for dynamic assignment."
            )

        # Validate CIDR prefix (0-32)
        prefix = int(match.group(5))
        if prefix > 32:
            raise ConfigError(
                f"Invalid CIDR prefix for VM '{vm_name}': /{prefix}. "
                f"Must be between 0 and 32."
            )

    def write_tfvars(self, config: dict, output_path: str) -> None:
        """Write resolved config as tfvars.json.

        Args:
            config: Resolved configuration from resolve_env()
            output_path: Path to write tfvars.json
        """
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def resolve_ansible_vars(self, posture_name: str = 'dev') -> dict:
        """Resolve ansible variables from site defaults and posture.

        Merges site defaults with security posture settings.
        Packages are merged (union of site + posture, deduplicated).

        Args:
            posture_name: Posture name (matches postures/{posture}.yaml, default: dev)

        Returns:
            Dict with all resolved ansible variables
        """
        defaults = self.site.get("defaults", {})
        posture = self.postures.get(posture_name, {})

        # Merge packages: site defaults + posture additions (deduplicated)
        site_packages = defaults.get("packages", [])
        posture_packages = posture.get("packages", [])
        merged_packages = list(dict.fromkeys(site_packages + posture_packages))

        # Resolve SSH keys from secrets
        ssh_keys_dict = self.secrets.get("ssh_keys", {})
        ssh_keys_list = list(ssh_keys_dict.values())

        # Read posture settings from nested structure
        ssh_config = posture.get("ssh", {})
        sudo_config = posture.get("sudo", {})
        fail2ban_config = posture.get("fail2ban", {})

        return {
            # System config from site defaults
            "timezone": defaults.get("timezone", "UTC"),
            "pve_remove_subscription_nag": defaults.get("pve_remove_subscription_nag", True),

            # Merged packages
            "packages": merged_packages,

            # Security settings from posture (nested keys)
            "ssh_port": ssh_config.get("port", 22),
            "ssh_permit_root_login": ssh_config.get("permit_root_login", "prohibit-password"),
            "ssh_password_authentication": ssh_config.get("password_authentication", "no"),
            "sudo_nopasswd": sudo_config.get("nopasswd", False),
            "fail2ban_enabled": fail2ban_config.get("enabled", False),

            # Metadata
            "posture_name": posture_name,

            # SSH keys for authorized_keys
            "ssh_authorized_keys": ssh_keys_list,
        }

    def write_ansible_vars(self, config: dict, output_path: str) -> None:
        """Write resolved ansible vars as JSON.

        Args:
            config: Resolved configuration from resolve_ansible_vars()
            output_path: Path to write ansible-vars.json
        """
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    def list_postures(self) -> list[str]:
        """List available posture names."""
        return sorted(self.postures.keys())

    def list_vm_presets(self) -> list[str]:
        """List available vm_preset names."""
        return sorted(self.vm_presets.keys())

    def list_presets(self) -> list[str]:
        """List available preset names (alias for list_vm_presets)."""
        return self.list_vm_presets()
