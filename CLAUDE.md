# iac-driver

Infrastructure orchestration engine for Proxmox VE.

## Ecosystem Context

This repo is part of the homestak polyrepo workspace. For project architecture,
development lifecycle, sprint/release process, and cross-repo conventions, see:

- `~/homestak/dev/meta/CLAUDE.md` — primary reference
- `docs/lifecycle/` in meta — 7-phase development process
- `docs/CLAUDE-GUIDELINES.md` in meta — documentation standards

When working in a scoped session (this repo only), follow the same sprint/release
process defined in meta. Use `/session save` before context compaction and
`/session resume` to restore state in new sessions.

### Agent Boundaries

This agent operates within the following constraints:

- Opens PRs via `homestak-bot`; never merges without human approval
- Runs lint and validation tools only; never executes infrastructure operations
- Never executes infrastructure operations without explicit human approval

## Overview

This repo provides scenario-based workflows that coordinate the tool repositories:

| Repo | Purpose | URL |
|------|---------|-----|
| bootstrap | Entry point, curl\|bash installer | https://github.com/homestak/bootstrap |
| config | Site-specific secrets and configuration | https://github.com/homestak/config |
| ansible | Proxmox host configuration, PVE installation | https://github.com/homestak-iac/ansible |
| tofu | VM provisioning with OpenTofu | https://github.com/homestak-iac/tofu |
| packer | Custom Debian cloud image building | https://github.com/homestak-iac/packer |

## Execution Models

iac-driver has three execution contexts:

| Context | Runs where | Reaches out via | `-H` flag |
|---------|-----------|-----------------|-----------|
| **Scenario** | Locally on the host being configured | Nothing (local ansible) | Optional — auto-detects from hostname |
| **Manifest** | Anywhere (orchestrator) | PVE API (HTTPS) + SSH to VMs | Required — specifies target PVE host |
| **Config** | Locally on the VM being configured | Server (HTTPS fetch), then local ansible | Not used |

- **Scenarios** (pve-setup, user-setup) configure the local host. Run on the PVE host as the `homestak` user.
- **Manifests** (apply, destroy, test) orchestrate infrastructure. They call the PVE API to provision VMs and SSH to VMs for config push. Can run from any machine with API access.
- **Config** (fetch, apply) is the pull-mode self-configuration path. A VM fetches its spec from the server, then applies it locally.

## Quick Start

```bash
# Clone this repo and tool repos
git clone https://github.com/homestak-iac/iac-driver.git
cd iac-driver
./scripts/setup-tools.sh  # Clones ansible, tofu, packer, config as siblings

# Setup config (secrets management)
cd ../config
make setup
make decrypt
```

## Secrets Management

Credentials are managed in the [config](https://github.com/homestak/config) repository using [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age).

**Discovery:** iac-driver finds config via `$HOMESTAK_ROOT/config` (defaults to `~/config/`).

**Fallback:** If `secrets.yaml` is missing (no `.enc` file to decrypt), iac-driver automatically runs `make init-secrets` in config, which copies from the `.example` template. This enables first-run bootstrap on fresh installations without manual secrets setup.

**Setup:**
```bash
cd ../config
make setup    # Configure git hooks, check dependencies
make decrypt  # Decrypt secrets (requires age key)
```

## Directory Structure

```
<parent>/
├── iac-driver/           # This repo - Infrastructure orchestration
│   ├── run.sh            # CLI entry point (bash wrapper)
│   ├── src/              # Python package
│   │   ├── cli.py        # CLI implementation
│   │   ├── common.py     # ActionResult + shared utilities
│   │   ├── config.py          # Host configuration (auto-discovery from config)
│   │   ├── config_apply.py    # Config phase: spec-to-ansible-vars + apply
│   │   ├── config_resolver.py # ConfigResolver - resolves config for tofu
│   │   ├── validation.py      # Preflight checks (API, SSH, config, images)
│   │   ├── manifest.py        # Manifest schema v2 (nodes graph)
│   │   ├── manifest_opr/ # Operator engine for manifest-based orchestration
│   │   │   ├── graph.py       # ExecutionNode, ManifestGraph, topo sort
│   │   │   ├── state.py       # NodeState, ExecutionState persistence
│   │   │   ├── executor.py    # NodeExecutor - walks graph, runs actions
│   │   │   └── cli.py         # create/destroy/test verb handlers
│   │   ├── resolver/     # Configuration resolution
│   │   │   ├── base.py        # Shared FK resolution utilities
│   │   │   ├── spec_resolver.py # Spec loading and FK resolution
│   │   │   └── spec_client.py   # HTTP client for spec fetching
│   │   ├── server/      # Server daemon
│   │   │   ├── tls.py         # TLS certificate management
│   │   │   ├── auth.py        # Authentication middleware
│   │   │   ├── specs.py       # Spec endpoint handler
│   │   │   ├── repos.py       # Repo endpoint handler
│   │   │   ├── httpd.py       # HTTPS server
│   │   │   ├── daemon.py      # Double-fork daemonization, PID management
│   │   │   └── cli.py         # server start/stop/status CLI
│   │   ├── actions/      # Reusable primitive operations
│   │   │   ├── tofu.py   # TofuApplyAction, TofuDestroyAction
│   │   │   ├── ansible.py# AnsiblePlaybookAction
│   │   │   ├── ssh.py    # SSHCommandAction, WaitForSSHAction, WaitForFileAction
│   │   │   ├── proxmox.py# StartVMAction, WaitForGuestAgentAction
│   │   │   ├── file.py   # DownloadFileAction, RemoveImageAction
│   │   │   ├── recursive.py   # RecursiveScenarioAction
│   │   │   ├── config_pull.py # ConfigFetchAction, WriteMarkerAction
│   │   │   └── pve_lifecycle.py # PVE lifecycle actions (bootstrap, secrets, bridge, etc.)
│   │   ├── scenarios/    # Workflow definitions
│   │   │   ├── pve_setup.py         # pve-setup (local/remote)
│   │   │   ├── pve_config.py        # pve-config (2-phase self-configure)
│   │   │   ├── user_setup.py        # user-setup (local/remote)
│   │   │   └── vm_roundtrip.py       # push-vm-roundtrip, pull-vm-roundtrip
│   │   └── reporting/    # Test report generation (JSON + markdown)
│   ├── reports/          # Generated test reports
│   └── scripts/          # Helper scripts
├── ansible/              # Tool repo (sibling in ~/iac/)
├── tofu/                 # Tool repo (sibling in ~/iac/)
└── packer/               # Tool repo (sibling in ~/iac/)
```

## ConfigResolver

The `ConfigResolver` class resolves config YAML files into flat configurations for tofu and ansible. All template, preset, and posture inheritance is resolved in Python, so consumers receive fully-computed values.

### Usage

```python
from src.config_resolver import ConfigResolver

resolver = ConfigResolver()  # Auto-discover config

# Resolve inline VM for tofu
config = resolver.resolve_inline_vm(
    node='srv1', vm_name='test', vmid=99900,
    vm_preset='vm-small', image='debian-12'
)
resolver.write_tfvars(config, '/tmp/tfvars.json')

# Resolve ansible vars from posture
ansible_vars = resolver.resolve_ansible_vars('dev')
resolver.write_ansible_vars(ansible_vars, '/tmp/ansible-vars.json')
```

### Resolution Order (Tofu)

1. `presets/{vm_preset}.yaml` - VM size presets (cores, memory, disk)
2. Inline VM overrides (name, vmid, image) from manifest nodes or CLI
3. `postures/{posture}.yaml` - Auth method for spec discovery

### Resolution Order (Ansible)

1. `site.yaml` defaults - timezone, packages, pve settings
2. `postures/{posture}.yaml` - Security settings from env's posture FK
3. Packages merged: site packages + posture packages (deduplicated)

### Output Structure (Tofu)

```python
{
    "node": "pve",
    "api_endpoint": "https://localhost:8006",
    "api_token": "root@pam!tofu=...",
    "host_user": "root",
    "vm_user": "homestak",
    "datastore": "local-zfs",
    "root_password": "$6$...",
    "ssh_keys": ["ssh-rsa ...", ...],
    "server_url": "https://srv1:44443",
    "vms": [
        {
            "name": "test",
            "vmid": 99900,
            "image": "debian-12",
            "cores": 1,
            "memory": 2048,
            "disk": 20,
            "bridge": "vmbr0",
            "auth_token": ""  # HMAC-signed provisioning token
        }
    ]
}
```

Per-VM `auth_token` is an HMAC-SHA256 provisioning token minted by `ConfigResolver._mint_provisioning_token()` when both `server_url` and `spec` are set. See [provisioning-token.md](../docs/designs/provisioning-token.md) for token format, signing, and verification.

**Auto-generated signing key:** During `pve-setup`, if `secrets.auth.signing_key` is empty or missing, it is auto-generated (256-bit hex via `secrets.token_hex(32)`) and written to `secrets.yaml`. This eliminates a manual setup step for the create -> config flow.

### SSH Key Default Behavior

When a spec's user entry omits the `ssh_keys` field, ALL keys from `secrets.ssh_keys` are injected automatically. This makes specs portable across deployments without listing specific key identifiers. If `ssh_keys` is explicitly listed, only those named keys are resolved via FK.

### Output Structure (Ansible)

```python
{
    "timezone": "America/Denver",
    "pve_remove_subscription_nag": true,
    "packages": ["htop", "curl", "wget", "net-tools", "strace"],
    "ssh_port": 22,
    "ssh_permit_root_login": "yes",
    "ssh_password_authentication": "yes",
    "sudo_nopasswd": true,
    "fail2ban_enabled": false,
    "env_name": "dev",
    "posture_name": "dev",
    "ssh_authorized_keys": ["ssh-rsa ...", ...]
}
```

### vmid Allocation

- If `vmid_base` is defined in env: `vmid = vmid_base + index`
- If `vmid_base` is not defined: `vmid = null` (PVE auto-assigns)
- Per-VM `vmid` override always takes precedence

### Tofu Actions

Actions in `src/actions/tofu.py` use ConfigResolver to generate tfvars and run tofu:

| Action | Description |
|--------|-------------|
| `TofuApplyAction` | Run tofu apply with ConfigResolver on local host |
| `TofuDestroyAction` | Run tofu destroy with ConfigResolver on local host |

**State Isolation:** Each manifest+node+host gets isolated state via explicit `-state` flag:
```
$HOMESTAK_ROOT/.state/tofu/{manifest}/{node}-{host}/terraform.tfstate   # manifest operator
$HOMESTAK_ROOT/.state/tofu/{node}-{host}/terraform.tfstate              # standalone scenarios
```

The `-state` flag is required because `TF_DATA_DIR` only affects plugin/module caching, not state file location.

**Context Passing:** TofuApplyAction extracts VM IDs from resolved config and adds them to context:
```python
context['test_vm_id'] = 99900
context['provisioned_vms'] = [{'name': 'test', 'vmid': 99900}, ...]
```

**Multi-VM Actions:** `StartProvisionedVMsAction` and `WaitForProvisionedVMsAction` operate on all VMs from `provisioned_vms` context. After completion, context contains `{vm_name}_ip` for each VM and `vm_ip` for backward compatibility.

## Server Daemon

The server daemon serves specs and git repos over HTTPS. See [server-daemon.md](../docs/designs/server-daemon.md) for architecture, double-fork daemonization, PID management, and operator lifecycle integration.

### Management

```bash
./run.sh server start                    # Start as daemon
./run.sh server start --repos --repo-token <token>  # With repo serving
./run.sh server start --foreground       # Development mode
./run.sh server status [--json]          # Check status
./run.sh server stop                     # Stop daemon
```

PID file: `$HOMESTAK_ROOT/.run/server-{port}.pid` | Log file: `$HOMESTAK_ROOT/logs/server.log`

Operator (executor.py) auto-manages server lifecycle for manifest verbs with reference counting.

### Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/health` | None | Health check |
| GET | `/specs` | None | List available specs |
| GET | `/spec/{identity}` | Provisioning token | Fetch resolved spec |
| GET | `/{repo}.git/*` | Bearer | Git dumb HTTP protocol |
| GET | `/{repo}.git/{path}` | Bearer | Raw file extraction |

Spec endpoints authenticate via HMAC-signed provisioning tokens. See [provisioning-token.md](../docs/designs/provisioning-token.md).

Auto-generates self-signed TLS certificate if none provided via `--cert`/`--key`.

## Operator Engine

The operator engine (`manifest_opr/`) walks a v2 manifest graph to execute create/destroy/test lifecycle operations. See [node-orchestration.md](../docs/designs/node-orchestration.md) for topology patterns and execution model comparison.

### Manifest Schema v2

```yaml
schema_version: 2
name: n2-push
pattern: tiered
nodes:
  - name: root-pve
    type: pve
    preset: vm-large
    image: pve-9
    vmid: 99011
    disk: 64
  - name: edge
    type: vm
    preset: vm-small
    image: debian-12
    vmid: 99021
    parent: root-pve
    execution:
      mode: pull  # Default: push
```

### Noun-Action Commands

```bash
./run.sh manifest apply -M n2-push -H srv1 [--dry-run] [--json-output] [--verbose]
./run.sh manifest destroy -M n2-push -H srv1 [--dry-run] [--yes]
./run.sh manifest test -M n2-push -H srv1 [--dry-run] [--json-output]
./run.sh manifest validate -M n2-push -H srv1 [--json-output]
./run.sh config fetch [--insecure]
./run.sh config apply [--spec /path.yaml] [--dry-run]
```

### Error Handling

| Mode | Behavior |
|------|----------|
| `stop` | Halt immediately (default) |
| `rollback` | Destroy already-created nodes, then halt |
| `continue` | Skip failed node, continue with independent nodes |

### Delegation Model

Root nodes (depth 0) are handled locally. PVE nodes with children trigger:
1. PVE lifecycle setup (bootstrap, secrets, site config, bridge + DNS, API token, image download)
2. Subtree delegation via SSH — `./run.sh manifest apply --manifest-json` on the PVE node

This recursion handles arbitrary depth without limits.

**Config distribution phases:** The PVE lifecycle includes `copy_secrets` (scoped secrets excluding `api_tokens`) and `copy_site_config` (site.yaml with DNS, gateway, timezone). These push configuration to delegated PVE nodes so they can resolve config for their own children.

**Delegate logging:** Delegated action logs use the node name as prefix (e.g., `[delegate-root-pve]`) instead of `[inner]`. JSON output from delegated commands is suppressed from INFO logs to reduce noise.

### Execution Modes

Nodes use **push** (default) or **pull** for config phase. See [config-phase.md](../docs/designs/config-phase.md) for spec-to-ansible mapping and implementation details.

| Mode | How Config Runs | Operator Behavior |
|------|----------------|-------------------|
| `push` | Operator runs ansible from controller over SSH | Default; no spec injection in cloud-init |
| `pull` | VM self-configures via cloud-init | Operator polls for complete.json |

PVE nodes default to **2-phase self-configure**: cloud-init bootstraps and starts a systemd oneshot service (`pve-config.service`) that runs `./run.sh scenario run pve-config --local`. The operator polls for success/failure markers via `WaitForFileAction`. Set `execution.mode: push` on a PVE node to use the legacy 11-phase SSH push lifecycle. See [pve-self-configure.md](../docs/designs/pve-self-configure.md) for design rationale. Push-mode VM nodes skip spec injection in cloud-init to avoid bootstrap race conditions.

## Manifest-Driven Orchestration

Manifests define N-level tiered PVE deployments using graph-based schema v2. Manifests are YAML files in `config/manifests/`.

```bash
./run.sh manifest apply -M n2-push -H srv1
./run.sh manifest destroy -M n2-push -H srv1 --yes
./run.sh manifest test -M n2-push -H srv1
./run.sh manifest apply -M n2-push -H srv1 --dry-run
./run.sh manifest test -M n1-push -H srv1 --json-output
```

`RecursiveScenarioAction` executes commands on remote hosts via SSH with PTY streaming. Used by the operator for subtree delegation. Supports `raw_command` for verb delegation and `scenario_name` for legacy scenarios. Extracts context keys from `--json-output` results.

## Naming Conventions

### Scenarios, Phases, and Actions

| Type | Pattern | Examples |
|------|---------|----------|
| **Scenarios** | `noun-verb` | `pve-setup`, `pve-config`, `user-setup`, `push-vm-roundtrip` |
| **Phases** | `verb_noun` | `ensure_pve`, `setup_pve`, `provision_vm`, `create_user` |
| **Actions** | `VerbNounAction` | `EnsurePVEAction`, `StartVMAction`, `WaitForSSHAction` |

### Phase Verb Conventions

| Verb | Meaning | Idempotent? |
|------|---------|-------------|
| `ensure_*` | Make sure X exists/is running | Yes - checks first |
| `setup_*` | Configure X for use | Usually yes |
| `provision_*` | Create new resource | No - creates |
| `start_*` | Start existing resource | Yes - checks state |
| `wait_*` | Wait for condition | Yes |
| `verify_*` | Check/validate | Yes |
| `destroy_*` | Remove resource | Yes - checks exists |
| `sync_*` | Synchronize data | Yes |

## Conventions

- **Remote SSH paths** use `~/iac/iac-driver` (hardcoded). This works because on target hosts `$HOME` is the workspace root. If `$HOMESTAK_ROOT` diverges from `$HOME` in the future, these paths should change to `${HOMESTAK_ROOT:-~}/iac/iac-driver`. Grep for `~/iac/iac-driver` to find all occurrences (server_mgmt.py, executor.py, vm_roundtrip.py, parallel-test.sh).
- **`$HOMESTAK_ROOT`** defaults to `$HOME`. Used for log dir (`$HOMESTAK_ROOT/logs`), config (`$HOMESTAK_ROOT/config`), and repo discovery. On target hosts, `$HOME` is always the workspace root so the default is correct.
- **VM IDs**: 5-digit (10000+ dev, 20000+ k8s)
- **MAC prefix**: BC:24:11:*
- **Hostnames**: `{cluster}{instance}` (dev1, router, kubeadm1)
- **Cloud-init files**: `{hostname}-meta.yaml`, `{hostname}-user.yaml`
- **Environments**: dev (permissive SSH, passwordless sudo) vs prod (strict SSH, fail2ban)

## Host Resolution (v0.36+)

The `--host` flag resolves configuration from config with fallback:

| Priority | Path | Use Case |
|----------|------|----------|
| 1 | `nodes/{host}.yaml` | PVE node with API access |
| 2 | `hosts/{host}.yaml` | Physical machine, SSH-only (pre-PVE) |

**Pre-PVE Host Provisioning:**

1. Create `hosts/{hostname}.yaml` (or run `make host-config` on the target)
2. Run `./run.sh --scenario pve-setup --host {hostname}`
3. After PVE install, `nodes/{hostname}.yaml` is auto-generated
4. Host is now usable for `./run.sh manifest apply` and other PVE operations

`HostConfig.is_host_only` is `True` when loaded from `hosts/*.yaml` (PVE-specific fields are empty).

## Node Configuration

PVE node configuration is stored in `config/nodes/*.yaml`. Filename must match the actual PVE node name (`pvesh get /nodes`).

API tokens are stored separately in `config/secrets.yaml` and resolved by key reference:
```yaml
# nodes/srv1.yaml
host: srv1                      # FK -> hosts/srv1.yaml
api_endpoint: https://198.51.100.61:8006
api_token: srv1                 # FK -> secrets.api_tokens.srv1
```

**Configuration Merge Order:** `site.yaml` → `nodes/{node}.yaml` → `secrets.yaml`

## CLI Reference

### Architecture

```
PVE Host (srv1)
├── IP: 198.51.100.x
└── VM 99011 (root-pve) - PVE node
    ├── Debian 13 + Proxmox VE
    ├── 4 cores, 8GB RAM, 64GB disk
    └── VM 99021 (edge) - Leaf VM
        └── Debian 12, 2 cores, 2GB RAM
```

### Commands

Run `./run.sh` with no arguments for top-level usage, or `./run.sh scenario --help` for scenario list.

```bash
# Manifest commands (infrastructure lifecycle)
./run.sh manifest apply -M n2-push -H srv1
./run.sh manifest destroy -M n2-push -H srv1 --yes
./run.sh manifest test -M n2-push -H srv1
./run.sh manifest validate -M n2-push -H srv1

# Config commands (spec fetch and apply)
./run.sh config fetch --insecure
./run.sh config apply

# Scenario commands (standalone workflows)
./run.sh scenario run pve-setup --local
./run.sh scenario run user-setup --local

# Preflight checks
./run.sh --preflight --host srv1
```

Use `--json-output` for structured JSON to stdout (logs to stderr). Use `--dry-run` to preview without executing. Use `--verbose` for detailed logging. Use `scripts/parallel-test.sh` to run multiple manifest tests concurrently with a shared server.

### Available Scenarios

| Scenario | Runtime | Description |
|----------|---------|-------------|
| `pve-setup` | ~3m | Install PVE (if needed), configure host, create API token, generate node config |
| `pve-config` | ~10m | 2-phase PVE self-configure: fetch config, install PVE, bridge, API token, SSH key |
| `user-setup` | ~30s | Create homestak user |
| `push-vm-roundtrip` | ~3m | Spec discovery integration test (push verification) |
| `pull-vm-roundtrip` | ~5m | Config phase integration test (pull verification) |

**pve-config details:**
- 2-phase model: Phase 1 is cloud-init bootstrap (creates systemd oneshot service), Phase 2 is local execution of the pve-config scenario.
- Fetches site.yaml, secrets.yaml, and private key from parent's `/config/{identity}` endpoint via `ConfigFetchAction`.
- Reuses phases from pve-setup (ensure_pve, setup_pve, generate_node_config, create_api_token).
- Configures vmbr0 bridge locally via ansible `pve-network.yml`.
- Injects own SSH public key into secrets.yaml for child VMs.
- Writes success/failure markers for parent polling; `on_failure` callback writes failure marker.
- `requires_host_config = False` — runs before node config exists.

**pve-setup details:**
- Splits PVE installation into kernel and packages phases with idempotent re-entry. If a reboot is needed after kernel installation, the operator re-enters and continues from the packages phase (detected via `dpkg -l` state).
- Auto-creates the PVE API token (`pveum user token add`), injects it into `secrets.yaml`, and verifies it against the PVE API.
- Auto-generates `auth.signing_key` if missing from `secrets.yaml`.
- Generates `nodes/{hostname}.yaml` after successful installation.
- API preflight is skipped (`requires_api = False`) since PVE is not installed yet on fresh hosts.

### Test Reports

Both `manifest test` and scenario roundtrip tests generate reports in `reports/` with format: `YYYYMMDD-HHMMSS.{scenario}.{passed|failed}.{md|json}`. Manifest test reports track create/verify/destroy phases individually. Use `scripts/parallel-test.sh` to run multiple manifest tests concurrently with a shared server.

### Preflight Validation

Preflight checks run automatically before manifest verbs (`apply`, `destroy`, `test`) and can be run standalone. Use `--skip-preflight` to bypass.

```bash
./run.sh --preflight --host srv1    # Standalone preflight
```

**Checks performed:**
- API token validity (format and PVE API response)
- SSH connectivity to target host
- `gateway` and `dns_servers` configured in `site.yaml` (fail-fast; `domain` is a non-blocking warning)
- SSH keys present in `secrets.yaml` (prevents silent 2-minute SSH timeout)
- Packer images available in PVE storage (`/var/lib/vz/template/iso/`) for root-level manifest nodes (checks local or remote via SSH)
- Nested virtualization enabled (when manifest contains PVE nodes with children)
- Provider lockfile versions match `providers.tf` (auto-fixes stale lockfiles)

### Timeouts

Operations use tiered timeouts (Quick: 5-30s through Extended: 1200s). Defaults are defined in `src/actions/*.py` and `src/common.py`. Override per-action in scenario definitions when needed.

### Claude Code Autonomy

For fully autonomous integration test runs, add these to Claude Code allowed tools:
```
Bash(ansible-playbook:*), Bash(ansible:*), Bash(rsync:*)
```

## Prerequisites

- Ansible 2.15+ (via pipx), OpenTofu, Packer with QEMU/KVM
- Python 3 with `python3-yaml` and `python3-requests` (`make install-deps`)
- SSH key at `~/.ssh/id_rsa`
- age + sops for secrets decryption (see `make setup`)
- age key at `~/.config/sops/age/keys.txt`
- Nested virtualization enabled (`cat /sys/module/kvm_intel/parameters/nested` = Y)

## Development Setup

```bash
make install-dev   # Creates .venv/, installs linters + runtime deps, hooks
make test          # Run unit tests (612 tests)
make lint          # Run pre-commit hooks (pylint, mypy)
```

Uses a `.venv/` virtual environment for PEP 668 compatibility (Debian 12+). Pre-commit hooks run pylint and mypy on staged Python files automatically on `git commit`.

## Design Documents

Detailed architecture and design rationale:

| Document | Covers |
|----------|--------|
| [node-orchestration.md](../docs/designs/node-orchestration.md) | Topology patterns, execution models, system test catalog |
| [server-daemon.md](../docs/designs/server-daemon.md) | Daemon architecture, PID management, operator integration |
| [config-phase.md](../docs/designs/config-phase.md) | Push/pull execution, spec-to-ansible mapping |
| [provisioning-token.md](../docs/designs/provisioning-token.md) | HMAC token format, signing, verification |
| [pve-self-configure.md](../docs/designs/pve-self-configure.md) | 2-phase PVE self-configure model, pve-config scenario |
| [scenario-consolidation.md](../docs/designs/scenario-consolidation.md) | Scenario migration, PVE lifecycle phases |
| [node-lifecycle.md](../docs/designs/node-lifecycle.md) | Single-node lifecycle (create/config/run/destroy) |
| [test-strategy.md](../docs/designs/test-strategy.md) | Test hierarchy, system test catalog (ST-1 through ST-8) |

## Tool Documentation

Each tool repo has its own CLAUDE.md with detailed context:
- `../../bootstrap/CLAUDE.md` - curl|bash installer and homestak CLI
- `../../config/CLAUDE.md` - Secrets management and encryption
- `../ansible/CLAUDE.md` - Ansible-specific commands and structure
- `../tofu/CLAUDE.md` - OpenTofu modules and environment details
