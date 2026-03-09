"""Manifest loading and validation for infrastructure orchestration.

Manifests define deployment topologies for VM/PVE provisioning.
They reference config entities (presets, specs) via foreign keys.

Schema v2: Graph-based nodes with parent references (#143) - used by operator engine.
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]

from config import ConfigError, get_site_config_dir

logger = logging.getLogger(__name__)

# Supported schema versions
SUPPORTED_SCHEMA_VERSIONS = {2}


@dataclass
class ManifestNode:
    """A node in a v2 graph-based manifest.

    Nodes define VMs/CTs with parent references forming a deployment tree.
    parent=None means the node is deployed on the target host (root node).

    Attributes:
        name: Node identifier (VM hostname and context key prefix)
        type: Node type (vm, ct, pve)
        spec: FK to specs/{value}.yaml
        preset: FK to presets/{value}.yaml (vm- prefixed)
        image: Cloud image name
        vmid: Explicit VM ID
        disk: Disk size override in GB
        parent: FK to another node name (None = root node)
        execution_mode: Per-node execution mode override (push/pull)
    """
    name: str
    type: str
    spec: Optional[str] = None
    preset: Optional[str] = None
    image: Optional[str] = None
    vmid: Optional[int] = None
    disk: Optional[int] = None
    parent: Optional[str] = None
    execution_mode: Optional[str] = None

    @classmethod
    def from_dict(cls, data: dict) -> 'ManifestNode':
        """Create ManifestNode from dictionary."""
        execution = data.get('execution', {})
        return cls(
            name=data['name'],
            type=data['type'],
            spec=data.get('spec'),
            preset=data.get('preset'),
            image=data.get('image'),
            vmid=data.get('vmid'),
            disk=data.get('disk'),
            parent=data.get('parent'),
            execution_mode=execution.get('mode') if execution else None,
        )

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        d: dict[str, Any] = {
            'name': self.name,
            'type': self.type,
        }
        if self.spec is not None:
            d['spec'] = self.spec
        if self.preset is not None:
            d['preset'] = self.preset
        if self.image is not None:
            d['image'] = self.image
        if self.vmid is not None:
            d['vmid'] = self.vmid
        if self.disk is not None:
            d['disk'] = self.disk
        if self.parent is not None:
            d['parent'] = self.parent
        if self.execution_mode is not None:
            d['execution'] = {'mode': self.execution_mode}
        return d


@dataclass
class ManifestSettings:
    """Optional settings for manifest execution.

    Attributes:
        verify_ssh: Verify SSH at each level (default: True)
        cleanup_on_failure: Destroy levels on failure (default: True)
        timeout_buffer: Seconds to subtract per level (default: 60)
        on_error: Error handling strategy (default: 'stop')
    """
    verify_ssh: bool = True
    cleanup_on_failure: bool = True
    timeout_buffer: int = 60
    on_error: str = 'stop'

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> 'ManifestSettings':
        """Create ManifestSettings from dictionary."""
        if not data:
            return cls()
        return cls(
            verify_ssh=data.get('verify_ssh', True),
            cleanup_on_failure=data.get('cleanup_on_failure', True),
            timeout_buffer=data.get('timeout_buffer', 60),
            on_error=data.get('on_error', 'stop'),
        )


@dataclass
class Manifest:
    """Infrastructure deployment manifest.

    Defines a graph of nodes with parent references for the operator engine.

    Attributes:
        schema_version: Manifest schema version (must be 2)
        name: Human-readable manifest name
        description: Optional description
        settings: Optional execution settings
        source_path: Path where manifest was loaded from (for debugging)
        pattern: Topology shape ('flat' or 'tiered')
        execution_mode: Default execution mode ('push' or 'pull')
        nodes: Graph-based node definitions
    """
    schema_version: int
    name: str
    description: str = ''
    settings: ManifestSettings = field(default_factory=ManifestSettings)
    source_path: Optional[Path] = None
    pattern: Optional[str] = None
    execution_mode: str = 'push'
    nodes: list[ManifestNode] = field(default_factory=list)

    @property
    def depth(self) -> int:
        """Number of nodes in the manifest."""
        return len(self.nodes)

    def to_dict(self) -> dict:
        """Convert manifest to dictionary (for JSON serialization)."""
        result: dict[str, Any] = {
            'schema_version': 2,
            'name': self.name,
            'description': self.description,
            'pattern': self.pattern or 'flat',
            'nodes': [n.to_dict() for n in self.nodes],
            'settings': {
                'verify_ssh': self.settings.verify_ssh,
                'cleanup_on_failure': self.settings.cleanup_on_failure,
                'timeout_buffer': self.settings.timeout_buffer,
                'on_error': self.settings.on_error,
            }
        }
        if self.execution_mode != 'push':
            result['execution'] = {'default_mode': self.execution_mode}
        return result

    def to_json(self) -> str:
        """Serialize manifest to JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: dict, source_path: Optional[Path] = None) -> 'Manifest':
        """Create Manifest from dictionary.

        Args:
            data: Manifest data dictionary
            source_path: Optional source path for error messages

        Returns:
            Validated Manifest instance

        Raises:
            ConfigError: If manifest is invalid
        """
        # Validate schema version
        schema_version = data.get('schema_version')
        if schema_version is None:
            raise ConfigError(
                "Manifest missing required field: schema_version. "
                "v1 manifests (levels-based) are no longer supported; use schema_version: 2 with nodes[]."
            )
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ConfigError(
                f"Unsupported manifest schema version: {schema_version}. "
                f"Supported versions: {sorted(SUPPORTED_SCHEMA_VERSIONS)}"
            )

        # Validate required fields
        if 'name' not in data:
            raise ConfigError("Manifest missing required field: name")

        return cls._from_dict_v2(data, source_path)

    @classmethod
    def _from_dict_v2(cls, data: dict, source_path: Optional[Path] = None) -> 'Manifest':
        """Parse v2 manifest (graph-based nodes with parent references)."""
        if 'nodes' not in data:
            raise ConfigError("Manifest v2 missing required field: nodes")
        if not data['nodes']:
            raise ConfigError("Manifest v2 must have at least one node")

        # Parse nodes
        nodes = []
        for i, node_data in enumerate(data['nodes']):
            if 'name' not in node_data:
                raise ConfigError(f"Node {i} missing required field: name")
            if 'type' not in node_data:
                raise ConfigError(f"Node {i} ({node_data.get('name', 'unnamed')}) missing required field: type")
            nodes.append(ManifestNode.from_dict(node_data))

        # Validate graph structure
        _validate_graph(nodes)

        execution = data.get('execution', {})

        return cls(
            schema_version=2,
            name=data['name'],
            description=data.get('description', ''),
            settings=ManifestSettings.from_dict(data.get('settings')),
            source_path=source_path,
            pattern=data.get('pattern', 'flat'),
            execution_mode=execution.get('default_mode', 'push'),
            nodes=nodes,
        )

    @classmethod
    def from_json(cls, json_str: str) -> 'Manifest':
        """Create Manifest from JSON string."""
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            raise ConfigError(f"Invalid manifest JSON: {e}") from e
        return cls.from_dict(data)


def _validate_graph(nodes: list[ManifestNode]) -> None:
    """Validate the graph structure of v2 manifest nodes.

    Checks for:
    - Duplicate node names
    - Dangling parent references
    - Cycles in the parent graph

    Raises:
        ConfigError: If validation fails
    """
    # Check for duplicate names
    names = [n.name for n in nodes]
    seen: set[str] = set()
    for name in names:
        if name in seen:
            raise ConfigError(f"Duplicate node name: '{name}'")
        seen.add(name)

    name_set = set(names)

    # Check for dangling parent references
    for node in nodes:
        if node.parent is not None and node.parent not in name_set:
            raise ConfigError(
                f"Node '{node.name}' references unknown parent '{node.parent}'"
            )

    # Check for cycles using DFS
    # Build adjacency: child -> parent
    visited: set[str] = set()
    in_stack: set[str] = set()
    parent_map = {n.name: n.parent for n in nodes}

    def _has_cycle(name: str) -> bool:
        if name in in_stack:
            return True
        if name in visited:
            return False
        visited.add(name)
        in_stack.add(name)
        parent = parent_map.get(name)
        if parent is not None and _has_cycle(parent):
            return True
        in_stack.discard(name)
        return False

    for node in nodes:
        if _has_cycle(node.name):
            raise ConfigError(f"Cycle detected in node graph involving '{node.name}'")


class ManifestLoader:
    """Loads manifests from config/manifests/ directory."""

    def __init__(self, site_config_path: Optional[str] = None):
        """Initialize loader with config path.

        Args:
            site_config_path: Path to config directory. If None, uses
                              auto-discovery (env var, sibling, ~/config).
        """
        if yaml is None:
            raise ConfigError("PyYAML not installed. Run: apt install python3-yaml")

        if site_config_path:
            self.site_config_dir = Path(site_config_path)
        else:
            self.site_config_dir = get_site_config_dir()

        self.manifests_dir = self.site_config_dir / 'manifests'

    def list_manifests(self) -> list[str]:
        """List available manifest names."""
        if not self.manifests_dir.exists():
            return []
        return sorted([
            f.stem for f in self.manifests_dir.glob('*.yaml')
            if f.is_file()
        ])

    def load(self, name: str) -> Manifest:
        """Load manifest by name.

        Args:
            name: Manifest name (without .yaml extension)

        Returns:
            Manifest instance

        Raises:
            ConfigError: If manifest not found or invalid
        """
        path = self.manifests_dir / f'{name}.yaml'
        if not path.exists():
            available = self.list_manifests()
            raise ConfigError(
                f"Manifest '{name}' not found at {path}. "
                f"Available: {', '.join(available) if available else 'none'}"
            )

        return self.load_file(path)

    def load_file(self, path: Path) -> Manifest:
        """Load manifest from specific file path.

        Args:
            path: Path to manifest YAML file

        Returns:
            Manifest instance

        Raises:
            ConfigError: If file not found or invalid
        """
        if not path.exists():
            raise ConfigError(f"Manifest file not found: {path}")

        try:
            with open(path, encoding='utf-8') as f:
                data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in manifest {path}: {e}") from e

        if not isinstance(data, dict):
            raise ConfigError(f"Manifest {path} must be a YAML object (dict)")

        return Manifest.from_dict(data, source_path=path)

def load_manifest(
    name: Optional[str] = None,
    file_path: Optional[str] = None,
    json_str: Optional[str] = None,
    depth: Optional[int] = None
) -> Manifest:
    """Load manifest from various sources.

    Priority:
    1. json_str - Inline JSON (for recursion)
    2. file_path - Specific file path
    3. name - Named manifest from config/manifests/

    Args:
        name: Manifest name (without .yaml extension)
        file_path: Path to manifest file
        json_str: Inline JSON manifest string
        depth: Optional depth limit (use first N nodes)

    Returns:
        Manifest instance

    Raises:
        ConfigError: If manifest not found or invalid
        ValueError: If no manifest source specified
    """
    if json_str:
        manifest = Manifest.from_json(json_str)
    elif file_path:
        loader = ManifestLoader()
        manifest = loader.load_file(Path(file_path))
    elif name:
        loader = ManifestLoader()
        manifest = loader.load(name)
    else:
        raise ValueError("No manifest specified — use -M, --manifest-file, or --manifest-json")

    # Apply depth limit if specified
    if depth is not None and depth > 0:
        if depth < len(manifest.nodes):
            manifest = Manifest(
                schema_version=manifest.schema_version,
                name=f"{manifest.name}[:{depth}]",
                description=manifest.description,
                nodes=manifest.nodes[:depth],
                settings=manifest.settings,
                source_path=manifest.source_path,
                pattern=manifest.pattern,
                execution_mode=manifest.execution_mode,
            )

    return manifest
