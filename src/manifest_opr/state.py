"""Execution state management for manifest-based orchestration.

Tracks per-node status (pending, running, completed, failed) and persists
state to disk so that destroy can find IPs/IDs without create context.
"""

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from common import get_state_dir

logger = logging.getLogger(__name__)


@dataclass
class NodeState:
    """Per-node execution state.

    Attributes:
        name: Node name (matches ManifestNode.name)
        status: Current status (pending, running, completed, failed, destroyed)
        vm_id: VM ID once provisioned
        ip: IP address once discovered
        started_at: Timestamp when execution started
        completed_at: Timestamp when execution completed
        error: Error message if failed
    """
    name: str
    status: str = 'pending'
    vm_id: Optional[int] = None
    ip: Optional[str] = None
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    error: Optional[str] = None

    def start(self) -> None:
        """Mark node as running."""
        self.status = 'running'
        self.started_at = time.time()

    def complete(self, vm_id: Optional[int] = None, ip: Optional[str] = None) -> None:
        """Mark node as completed with optional VM ID and IP."""
        self.status = 'completed'
        self.completed_at = time.time()
        if vm_id is not None:
            self.vm_id = vm_id
        if ip is not None:
            self.ip = ip

    def fail(self, error: str) -> None:
        """Mark node as failed with error message."""
        self.status = 'failed'
        self.completed_at = time.time()
        self.error = error

    def mark_destroyed(self) -> None:
        """Mark node as destroyed."""
        self.status = 'destroyed'
        self.completed_at = time.time()

    @property
    def duration(self) -> Optional[float]:
        """Return duration in seconds, or None if not completed."""
        if self.started_at and self.completed_at:
            return self.completed_at - self.started_at
        return None

    def to_dict(self) -> dict:
        """Serialize node state to dict."""
        d: dict[str, Any] = {
            'name': self.name,
            'status': self.status,
        }
        if self.vm_id is not None:
            d['vm_id'] = self.vm_id
        if self.ip is not None:
            d['ip'] = self.ip
        if self.started_at is not None:
            d['started_at'] = self.started_at
        if self.completed_at is not None:
            d['completed_at'] = self.completed_at
        if self.error is not None:
            d['error'] = self.error
        return d

    @classmethod
    def from_dict(cls, data: dict) -> 'NodeState':
        """Create NodeState from dictionary."""
        return cls(
            name=data['name'],
            status=data.get('status', 'pending'),
            vm_id=data.get('vm_id'),
            ip=data.get('ip'),
            started_at=data.get('started_at'),
            completed_at=data.get('completed_at'),
            error=data.get('error'),
        )


class ExecutionState:
    """Manifest-level execution state with save/load.

    Tracks all node states and provides context propagation keys
    ({name}_vm_id, {name}_ip) so destroy can locate resources.

    State is persisted to .states/{manifest}/execution.json.
    """

    def __init__(self, manifest_name: str, host_name: str):
        """Initialize execution state.

        Args:
            manifest_name: Manifest identifier
            host_name: Target host name
        """
        self.manifest_name = manifest_name
        self.host_name = host_name
        self._nodes: dict[str, NodeState] = {}
        self.started_at: Optional[float] = None
        self.completed_at: Optional[float] = None

    def add_node(self, name: str) -> NodeState:
        """Register a node for tracking."""
        state = NodeState(name=name)
        self._nodes[name] = state
        return state

    def get_node(self, name: str) -> NodeState:
        """Get node state by name.

        Raises:
            KeyError: If node not registered
        """
        return self._nodes[name]

    @property
    def nodes(self) -> dict[str, NodeState]:
        """Return copy of node state dictionary."""
        return dict(self._nodes)

    def start(self) -> None:
        """Mark execution as started."""
        self.started_at = time.time()

    def finish(self) -> None:
        """Mark execution as finished."""
        self.completed_at = time.time()

    def to_context(self) -> dict[str, Any]:
        """Generate context keys from node states.

        Produces {name}_vm_id and {name}_ip for each completed node.
        """
        ctx: dict[str, Any] = {}
        for name, state in self._nodes.items():
            if state.vm_id is not None:
                ctx[f'{name}_vm_id'] = state.vm_id
            if state.ip is not None:
                ctx[f'{name}_ip'] = state.ip
        return ctx

    def _state_dir(self) -> Path:
        """Return state directory path for this manifest."""
        result: Path = get_state_dir() / 'tofu' / self.manifest_name
        return result

    def save(self, path: Optional[Path] = None) -> Path:
        """Save state to JSON file.

        Args:
            path: Optional override path. Default: .states/{manifest}/execution.json

        Returns:
            Path where state was saved
        """
        if path is None:
            path = self._state_dir() / 'execution.json'
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            'manifest_name': self.manifest_name,
            'host_name': self.host_name,
            'started_at': self.started_at,
            'completed_at': self.completed_at,
            'nodes': {name: state.to_dict() for name, state in self._nodes.items()},
        }
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
        logger.debug(f"Saved execution state to {path}")
        return path

    @classmethod
    def load(cls, manifest_name: str, host_name: str, path: Optional[Path] = None) -> 'ExecutionState':
        """Load state from JSON file.

        Args:
            manifest_name: Manifest identifier
            host_name: Target host name
            path: Optional override path

        Returns:
            ExecutionState instance

        Raises:
            FileNotFoundError: If state file doesn't exist
        """
        state = cls(manifest_name, host_name)
        if path is None:
            path = state._state_dir() / 'execution.json'

        with open(path, encoding='utf-8') as f:
            data = json.load(f)

        state.started_at = data.get('started_at')
        state.completed_at = data.get('completed_at')

        for name, node_data in data.get('nodes', {}).items():
            node_state = NodeState.from_dict(node_data)
            state._nodes[name] = node_state

        logger.debug(f"Loaded execution state from {path}")
        return state
