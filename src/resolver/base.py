"""Base resolver with shared FK resolution utilities.

This module provides common functionality for resolving site-config entities:
- Path discovery
- YAML loading with caching
- Secrets and posture loading
- SSH key FK resolution

Used by both ConfigResolver (tofu/ansible) and SpecResolver (server).
"""

import logging
import os
from pathlib import Path
from typing import Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


class ResolverError(Exception):
    """Base exception for resolver errors."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class PostureNotFoundError(ResolverError):
    """Posture file not found."""

    def __init__(self, posture: str):
        super().__init__("E201", f"Posture not found: {posture}")


class SSHKeyNotFoundError(ResolverError):
    """SSH key not found in secrets."""

    def __init__(self, key_id: str):
        super().__init__("E202", f"SSH key not found: {key_id}")


class SecretsNotFoundError(ResolverError):
    """Secrets file not found or not decrypted."""

    def __init__(self, path: Path):
        enc_path = path.with_suffix('.yaml.enc')
        if enc_path.exists():
            msg = (f"Secrets file not decrypted: {path}\n"
                   f"  Run: cd {path.parent} && make decrypt")
        else:
            msg = f"Secrets file not found: {path}"
        super().__init__("E500", msg)


def discover_etc_path() -> Path:
    """Discover the site-config path.

    Derived from $HOMESTAK_ROOT/config. On installed hosts, $HOME is the
    workspace root (default). On dev workstations, set HOMESTAK_ROOT explicitly.

    Returns:
        Path to site-config directory

    Raises:
        ResolverError: If no valid path found
    """
    root = Path(os.environ.get("HOMESTAK_ROOT", str(Path.home())))
    config_dir = root / "config"
    if config_dir.is_dir():
        return config_dir

    raise ResolverError(
        "E500",
        f"Cannot find site-config directory at {config_dir}. "
        "Set HOMESTAK_ROOT to your workspace root directory."
    )


class ResolverBase:
    """Base class for FK resolution with caching.

    Provides common functionality for loading and caching site-config
    entities: site.yaml, secrets.yaml, postures, and SSH key resolution.
    """

    def __init__(self, etc_path: Optional[Path] = None):
        """Initialize resolver.

        Args:
            etc_path: Path to site-config. Auto-discovered if not provided.

        Raises:
            ResolverError: If PyYAML not installed
        """
        if yaml is None:
            raise ResolverError("E500", "PyYAML not installed. Run: apt install python3-yaml")

        self.etc_path = etc_path or discover_etc_path()
        self._secrets: Optional[dict] = None
        self._site: Optional[dict] = None
        self._posture_cache: dict = {}

    def clear_cache(self):
        """Clear all caches (called on SIGHUP for hot reload)."""
        self._secrets = None
        self._site = None
        self._posture_cache.clear()
        logger.info("Cache cleared")

    def _load_yaml(self, path: Path) -> dict:
        """Load YAML file.

        Args:
            path: Path to YAML file

        Returns:
            Parsed YAML content as dict, or empty dict if file missing
        """
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) or {}

    def _load_secrets(self) -> dict:
        """Load secrets.yaml (cached).

        Returns:
            Secrets dict

        Raises:
            SecretsNotFoundError: If secrets.yaml not found
        """
        if self._secrets is None:
            secrets_path = self.etc_path / "secrets.yaml"
            if not secrets_path.exists():
                raise SecretsNotFoundError(secrets_path)
            self._secrets = self._load_yaml(secrets_path)
        return self._secrets

    def _load_site(self) -> dict:
        """Load site.yaml (cached).

        Returns:
            Site config dict (empty if file missing)
        """
        if self._site is None:
            site_path = self.etc_path / "site.yaml"
            self._site = self._load_yaml(site_path)
        return self._site

    def _load_posture(self, name: str) -> dict:
        """Load posture by name (cached).

        Args:
            name: Posture name (e.g., "dev", "prod", "local")

        Returns:
            Posture config dict

        Raises:
            PostureNotFoundError: If posture file not found
        """
        if name not in self._posture_cache:
            posture_path = self.etc_path / "postures" / f"{name}.yaml"

            if not posture_path.exists():
                raise PostureNotFoundError(name)
            self._posture_cache[name] = self._load_yaml(posture_path)
        result: dict = self._posture_cache[name]
        return result

    def _all_ssh_keys(self) -> list:
        """Return all SSH public keys from secrets.ssh_keys.

        Returns:
            List of all public key strings
        """
        secrets = self._load_secrets()
        ssh_keys = secrets.get("ssh_keys", {})
        if not ssh_keys or not isinstance(ssh_keys, dict):
            return []
        return list(ssh_keys.values())

    def _resolve_ssh_keys(self, key_refs: list) -> list:
        """Resolve SSH key references to actual public keys.

        Key refs are identifiers matching secrets.ssh_keys keys
        (e.g., "root@srv2", "user@host").

        Args:
            key_refs: List of SSH key identifier strings

        Returns:
            List of resolved public key strings

        Raises:
            SSHKeyNotFoundError: If a referenced key is not found
        """
        secrets = self._load_secrets()
        ssh_keys = secrets.get("ssh_keys", {})
        resolved = []

        for key_id in key_refs:
            if key_id not in ssh_keys:
                raise SSHKeyNotFoundError(key_id)
            resolved.append(ssh_keys[key_id])

        return resolved

    def _get_site_defaults(self) -> dict:
        """Get site.yaml defaults section.

        Returns:
            Defaults dict from site.yaml, or empty dict
        """
        result: dict = self._load_site().get("defaults", {})
        return result

    def get_signing_key(self) -> Optional[str]:
        """Get the provisioning token signing key from secrets.

        Returns:
            Hex-encoded signing key, or None if not configured
        """
        try:
            secrets = self._load_secrets()
            result: Optional[str] = secrets.get("auth", {}).get("signing_key")
            return result
        except SecretsNotFoundError:
            return None
