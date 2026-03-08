"""Spec resolver for the server.

Loads specs from config/specs/ and resolves foreign key references
to postures and secrets. Migrated from bootstrap/lib/spec_resolver.py for
unified server architecture.
"""

import logging
from pathlib import Path
from typing import Optional

from resolver.base import (
    ResolverBase,
    ResolverError,
    PostureNotFoundError,
    SSHKeyNotFoundError,
)

logger = logging.getLogger(__name__)


class SpecNotFoundError(ResolverError):
    """Spec file not found."""

    def __init__(self, identity: str):
        super().__init__("E200", f"Spec not found: {identity}")


class SchemaValidationError(ResolverError):
    """Schema validation failed."""

    def __init__(self, message: str):
        super().__init__("E400", f"Schema validation failed: {message}")


class SpecResolver(ResolverBase):
    """Resolves specs from config with FK expansion.

    Extends ResolverBase with spec-specific functionality:
    - Spec loading from specs/
    - Site defaults application
    - Identity/hostname defaulting
    - Posture FK expansion
    - SSH key FK resolution in users
    """

    def __init__(self, etc_path: Optional[Path] = None):
        """Initialize resolver.

        Args:
            etc_path: Path to config. Auto-discovered if not provided.
        """
        super().__init__(etc_path)
        self._spec_cache: dict = {}

    def clear_cache(self):
        """Clear all caches (called on SIGHUP)."""
        super().clear_cache()
        self._spec_cache.clear()

    def _load_spec(self, identity: str) -> dict:
        """Load raw spec by identity.

        Args:
            identity: Spec identifier (filename without .yaml)

        Returns:
            Raw spec dict

        Raises:
            SpecNotFoundError: If spec file not found
        """
        spec_path = self.etc_path / "specs" / f"{identity}.yaml"
        if not spec_path.exists():
            raise SpecNotFoundError(identity)
        result: dict = self._load_yaml(spec_path)
        return result

    def _apply_site_defaults(self, spec: dict) -> dict:
        """Apply site.yaml defaults to spec.

        Applies:
        - identity.domain from defaults.domain
        - config.timezone from defaults.timezone

        Args:
            spec: Spec dict to modify

        Returns:
            Modified spec dict
        """
        defaults = self._get_site_defaults()

        # Apply identity defaults
        if "identity" not in spec:
            spec["identity"] = {}
        if "domain" not in spec.get("identity", {}) and "domain" in defaults:
            spec["identity"]["domain"] = defaults["domain"]

        # Apply config defaults
        if "config" not in spec:
            spec["config"] = {}
        if "timezone" not in spec.get("config", {}) and "timezone" in defaults:
            spec["config"]["timezone"] = defaults["timezone"]

        return spec

    def resolve(self, identity: str) -> dict:
        """Resolve spec by identity with all FK expansion.

        Resolution includes:
        1. Load raw spec from specs/{identity}.yaml
        2. Apply site.yaml defaults
        3. Set identity.hostname if not specified
        4. Load and merge posture from access.posture FK
        5. Resolve SSH key FKs in access.users[].ssh_keys

        Args:
            identity: Spec identifier (e.g., "base", "pve")

        Returns:
            Fully resolved spec with FKs expanded

        Raises:
            SpecNotFoundError: Spec file not found
            PostureNotFoundError: Posture not found
            SSHKeyNotFoundError: SSH key not found
        """
        # Check cache first
        if identity in self._spec_cache:
            cached: dict = self._spec_cache[identity]
            return cached

        # Load raw spec
        spec = self._load_spec(identity)

        # Apply site defaults
        spec = self._apply_site_defaults(spec)

        # Set identity.hostname if not specified
        if "identity" not in spec:
            spec["identity"] = {}
        if "hostname" not in spec["identity"]:
            spec["identity"]["hostname"] = identity

        # Resolve posture FK
        posture_name = spec.get("access", {}).get("posture", "dev")
        posture = self._load_posture(posture_name)

        # Merge posture settings into access (posture values as defaults)
        if "access" not in spec:
            spec["access"] = {}
        spec["access"]["_posture"] = posture  # Include full posture for auth checks

        # Resolve SSH keys for users
        # If ssh_keys is omitted, inject all keys from secrets.ssh_keys
        # If ssh_keys is explicitly listed, resolve only those FKs
        users = spec.get("access", {}).get("users", [])
        for user in users:
            if "ssh_keys" in user:
                user["ssh_keys"] = self._resolve_ssh_keys(user["ssh_keys"])
            else:
                user["ssh_keys"] = self._all_ssh_keys()

        # Cache and return
        self._spec_cache[identity] = spec
        return spec

    def list_specs(self) -> list:
        """List available spec identities.

        Returns:
            List of spec names (filenames without .yaml)
        """
        specs_dir = self.etc_path / "specs"
        if not specs_dir.exists():
            return []
        return sorted([p.stem for p in specs_dir.glob("*.yaml")])


__all__ = [
    "SpecResolver",
    "SpecNotFoundError",
    "SchemaValidationError",
    "PostureNotFoundError",
    "SSHKeyNotFoundError",
]
