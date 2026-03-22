# Architecture

iac-driver is the orchestration engine for the homestak platform. It coordinates
provisioning (tofu), configuration (ansible), and lifecycle management of VMs and
PVE nodes across physical hosts.

## Component Topology

The CLI entry point (`src/cli.py`) routes to four noun commands, each with its own
action handlers:

```
./run.sh <noun> <action> [options]

  manifest   apply | destroy | test | validate    Manifest-based infrastructure lifecycle
  scenario   run <name>                           Standalone scenario workflows
  config     fetch | apply                        Pull-mode self-configuration
  server     start | stop | status                Spec/repo server daemon
  token      inspect                              Provisioning token utilities
```

Nouns are dispatched in `dispatch_noun()`. The `manifest` noun delegates to
`manifest_opr/cli.py`, which builds a `ManifestGraph` and passes it to
`NodeExecutor`. The `scenario` noun rewrites `sys.argv` and falls through to the
legacy `argparse`-based scenario runner in `main()`.

## Data Flow: Manifest to Infrastructure

A manifest lifecycle operation follows this path:

```
manifest YAML          ManifestGraph            NodeExecutor           Actions
(config/manifests/)    (graph.py)               (executor.py)          (actions/*.py)

  nodes:               ExecutionNode tree        Walk create_order()    TofuApplyAction
  - name: root-pve     with parent/child         or destroy_order()     StartVMAction
    type: pve           edges and depth                                  WaitForGuestAgentAction
    children: [...]     annotations              Per-node:              WaitForSSHAction
                                                 1. Provision (tofu)    PVE lifecycle actions
                        create_order() = BFS     2. Start VM            RecursiveScenarioAction
                        destroy_order() = rev    3. Wait for IP/SSH
                                                 4. Post-SSH config
```

`ManifestGraph` wraps `ManifestNode` entries in `ExecutionNode` dataclasses that
carry parent/child references and depth. `create_order()` returns BFS traversal
(parents first); `destroy_order()` reverses it.

`NodeExecutor` walks the graph and runs the appropriate action sequence for each
node. It only handles root nodes (depth 0) locally. Children of PVE nodes are
delegated to the PVE host via SSH.

## Actions vs Scenarios

**Actions** (`src/actions/`) are reusable primitives that implement a single
operation and return an `ActionResult`. Each action class has a `run(config, context)`
method. Examples:

| Module | Actions | Purpose |
|--------|---------|---------|
| `tofu.py` | `TofuApplyAction`, `TofuDestroyAction` | VM provisioning via OpenTofu |
| `ansible.py` | `AnsiblePlaybookAction`, `AnsibleLocalPlaybookAction` | Configuration via ansible |
| `ssh.py` | `SSHCommandAction`, `WaitForSSHAction`, `WaitForFileAction` | SSH operations and polling |
| `proxmox.py` | `StartVMAction`, `WaitForGuestAgentAction` | PVE API operations |
| `pve_lifecycle.py` | `BootstrapAction`, `CopySecretsAction`, etc. | PVE node lifecycle steps |
| `recursive.py` | `RecursiveScenarioAction` | SSH delegation to remote hosts |
| `config_pull.py` | `ConfigFetchAction`, `WriteMarkerAction` | Pull-mode config and markers |
| `file.py` | `DownloadFileAction`, `DownloadGitHubReleaseAction` | File transfer operations |

**Scenarios** (`src/scenarios/`) are workflow definitions that compose actions into
ordered phase lists. A scenario class implements the `Scenario` protocol and returns
a list of `(phase_name, action, description)` tuples from `get_phases()`. The
`Orchestrator` runs phases sequentially, checking timeouts and skip lists.

Registered scenarios: `pve-setup`, `pve-config`, `user-setup`,
`push-vm-roundtrip`, `pull-vm-roundtrip`.

The separation exists because actions are composed differently depending on context.
For example, `AnsibleLocalPlaybookAction` is used in both `pve-setup` (local host
configuration) and `pve-config` (self-configure after bootstrap). The same
`WaitForSSHAction` appears in both scenarios and the manifest executor.

## Delegation Model

A manifest like `n2-push` defines a PVE node (`root-pve`) with a child VM (`edge`).
The orchestrator host can create the PVE node via its own tofu + PVE API, but it
can't create the child VM — that requires the PVE node's API, which doesn't exist
until the PVE node is fully configured. The solution: once the PVE node is ready,
SSH into it and run iac-driver *there* to create the child.

The executor handles root nodes (depth 0) directly. When a root PVE node has
children, it delegates:

1. **Create root-pve** locally (tofu apply, PVE lifecycle: bootstrap, secrets,
   bridge, DNS, API token, SSH keys, image download)
2. **Extract subtree** — `ManifestGraph.extract_subtree()` builds a new manifest
   containing only root-pve's children, with the children promoted to root nodes
3. **Delegate via SSH** — `RecursiveScenarioAction` SSHes to root-pve and runs
   `./run.sh manifest apply --manifest-json '<subtree>' -H root-pve --json-output`
4. **Delegated executor repeats** — on root-pve, a new NodeExecutor treats the
   former children as its own roots. If any of *those* are PVE nodes with children,
   it delegates again — enabling arbitrary depth (n3-deep exercises 3 levels)

```
Orchestrator (srv1)                 root-pve (VM on srv1)            edge (VM on root-pve)
┌─────────────────┐                 ┌─────────────────┐              ┌──────────┐
│ 1. tofu apply   │                 │                 │              │          │
│ 2. PVE lifecycle│ ──SSH──────────>│ 3. tofu apply   │──PVE API───>│ created  │
│    (bootstrap,  │   manifest-json │    (edge VM)    │              │          │
│     bridge,     │                 │ 4. push/pull    │              │          │
│     tokens...)  │ <──JSON result──│    config       │              │          │
└─────────────────┘                 └─────────────────┘              └──────────┘
```

**Why delegation, not remote API calls?** The PVE node needs its own iac-driver
instance because tofu requires local filesystem access (state files, provider
plugins, cloud-init snippets via SSH-to-self). Running tofu remotely would require
syncing state and plugins — delegation avoids this by running everything locally
on the PVE node.

Destroy follows the same pattern in reverse: subtree destruction is delegated
first, then the root node is destroyed locally.

## Key Abstractions

**`ActionResult`** (`common.py`): Every action returns this dataclass with `success`,
`message`, `duration`, and `context_updates`. The `context_updates` dict propagates
state between actions (e.g., `{name}_ip`, `{name}_vm_id`).

**`HostConfig`** (`config.py`): Loaded from `config/nodes/*.yaml` or
`config/hosts/*.yaml`. Carries API endpoint, SSH host, credentials, image settings,
and server URL. `is_host_only=True` when loaded from `hosts/` (pre-PVE, no API).

**`Manifest`** (`manifest.py`): Manifest definition with a list of `ManifestNode`
entries. Each node has a name, type (`vm`/`ct`/`pve`), spec/preset FKs, and an
optional parent reference forming a deployment tree.

**`ManifestGraph`** (`manifest_opr/graph.py`): Wraps manifest nodes in
`ExecutionNode` instances with parent/child edges and depth. Provides `create_order()`
and `destroy_order()` traversals, plus `extract_subtree()` for delegation.

**`ExecutionState`** (`manifest_opr/state.py`): Tracks per-node status
(`pending`/`running`/`completed`/`failed`/`destroyed`) with VM IDs and IPs.
Persisted to disk so destroy operations can recover state from a prior create.

**`Orchestrator`** (`scenarios/__init__.py`): Runs scenario phases sequentially with
timeout checking, skip lists, dry-run preview, and `TestReport` generation.
