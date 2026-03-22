# iac-driver

Infrastructure orchestration engine for Proxmox VE.

## Ecosystem Context

This repo is part of the homestak polyrepo workspace. For project architecture,
development lifecycle, sprint/release process, and cross-repo conventions, see:

- `~/homestak/dev/meta/CLAUDE.md` — primary reference
- `docs/process/` in meta — 7-phase development process
- `docs/standards/claude-guidelines.md` in meta — documentation standards

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

## Documentation

Detailed implementation documentation:

@docs/config-res.md
@docs/server.md
@docs/operator.md
@docs/cli.md

### Design Documents

| Document | Covers |
|----------|--------|
| `$HOMESTAK_ROOT/dev/meta/docs/arch/node-orchestration.md` | Topology patterns, execution models, system test catalog |
| [server-daemon.md](docs/server-daemon.md) | Daemon architecture, PID management, operator integration |
| [config-phase.md](docs/config-phase.md) | Push/pull execution, spec-to-ansible mapping |
| [provisioning-token.md](docs/provisioning-token.md) | HMAC token format, signing, verification |
| [pve-self-configure.md](docs/pve-self-configure.md) | 2-phase PVE self-configure model, pve-config scenario |
| [scenario-consolidation.md](docs/scenario-consolidation.md) | Scenario migration, PVE lifecycle phases |
| [node-lifecycle.md](docs/node-lifecycle.md) | Single-node lifecycle (create/config/run/destroy) |
| [test-strategy.md](docs/test-strategy.md) | Test hierarchy, system test catalog (ST-1 through ST-8) |
