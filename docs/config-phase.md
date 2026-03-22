# Design Summary: Config Phase + Pull Execution Mode

**Issue:** iac-driver#147
**Sprint:** homestak-dev#201
**Epic:** iac-driver#125 (Node Lifecycle Architecture)
**Date:** 2026-02-06

## Problem Statement

The node lifecycle is: **create -> config -> run -> destroy**. Today, only create and destroy are implemented. The config phase — applying a specification (packages, services, users, SSH keys) to a provisioned VM — is done ad-hoc via ansible push.

This issue formalizes the config phase:
1. `./run.sh config` verb in iac-driver applies a spec to the local host
2. Operator respects `execution.mode: pull` — VMs self-configure instead of being pushed

**Success criteria:**
- `./run.sh config` reads spec.yaml and reaches "platform ready" (packages installed, services configured, users created)
- Operator handles pull nodes: provision, then poll for completion markers
- Both push and pull modes use the same config implementation
- Platform-ready marker emitted on successful config

## Proposed Solution

**Summary:** Add `config` verb to iac-driver that maps spec fields to ansible vars and runs existing roles; extend the operator to check `execution_mode` per node and poll for completion on pull nodes. Bootstrap is not involved — iac-driver is called directly from cloud-init.

### Architecture

```
                   Push Mode                              Pull Mode
                   ─────────                              ─────────
Driver             1. tofu apply (no spec injection)      1. tofu apply (with HOMESTAK_TOKEN)
                   2. start VM                            2. start VM
                   3. wait IP                             3. wait IP
                   4. wait SSH                            4. wait SSH
                   5. refresh apt cache                   5. poll for config complete
                   6. ansible-playbook over SSH
                   7. write config complete marker
                      ↓                                      ↓
VM                                                        5a. cloud-init runcmd:
                                                              ./run.sh config fetch --insecure
                                                          5b. fetches spec, applies config
                                                          5c. writes config complete
                      ↓                                      ↓
Driver             8. verify marker                       6. marker found → done
```

Both paths apply the same ansible roles (base, users, security). Push mode runs ansible from the controller over SSH; pull mode runs it locally on the VM via cloud-init.

### Key Components Affected

| Repo | File | Change |
|------|------|--------|
| iac-driver | `src/config_apply.py` | NEW — config command implementation |
| iac-driver | `src/cli.py` | MOD — add `config` verb |
| iac-driver | `src/manifest_opr/executor.py` | MOD — pull mode logic |
| iac-driver | `src/actions/ssh.py` | MOD — add `WaitForFileAction` |
| tofu | `envs/generic/main.tf` | MOD — add `./run.sh config` to runcmd |
| ansible | `playbooks/config-apply.yml` | NEW — config phase playbook |
| ansible | (roles) | KEEP — existing roles reused as-is |

**Not affected:** bootstrap — no changes needed. Cloud-init calls iac-driver directly.

## Interface Design

### CLI: `./run.sh config`

```bash
# Apply spec from state directory (default)
./run.sh config

# Apply spec from explicit path
./run.sh config --spec /path/to/spec.yaml

# Dry-run: show what would be applied
./run.sh config --dry-run

# JSON output for scripting
./run.sh config --json-output
```

**Spec source resolution:**
1. `--spec /path/to/spec.yaml` (explicit)
2. `$HOMESTAK_ROOT/.state/config/spec.yaml` (default, from `homestak spec get`)

**Exit codes:**
- `0` — Success (platform ready)
- `1` — Spec not found or invalid
- `2` — Apply failed (partial application)

### Config Command Implementation (iac-driver/src/config_apply.py)

```python
class ConfigApply:
    """Apply a spec to the local host via ansible roles."""

    def __init__(self, spec_path: Path, dry_run: bool = False):
        self.spec = self._load_spec(spec_path)
        self.dry_run = dry_run

    def apply(self) -> ConfigResult:
        """Apply spec in order: packages, timezone, users, services, marker."""
        # 1. Map spec to ansible vars
        vars_dict = self._spec_to_ansible_vars()

        # 2. Write temp vars file
        vars_file = self._write_vars(vars_dict)

        # 3. Run ansible-playbook with existing roles
        result = self._run_playbook(vars_file)

        # 4. Write platform-ready marker
        if result.success:
            self._write_marker(vars_dict)

        return result
```

**Path discovery:** `config_apply.py` discovers paths via environment variables or user-owned defaults:

- **State dir:** `$HOMESTAK_ROOT/.state/config/`
- **Ansible dir:** `$HOMESTAK_ROOT/iac/ansible/`

**Dev environment:** Set `HOMESTAK_ROOT` to point to your workspace (e.g., `HOMESTAK_ROOT=~/homestak`). There is no sibling directory discovery — the config command is designed for bootstrapped hosts where user-owned paths exist.

### Spec-to-Ansible Vars Mapping

| Spec Field | Ansible Var | Role |
|------------|-------------|------|
| `platform.packages` | `packages` | `homestak.debian.base` |
| `config.timezone` | `timezone` | `homestak.debian.base` |
| `access.users[].name` | `local_user` | `homestak.debian.users` |
| `access.users[].sudo` | `user_sudo` | `homestak.debian.users` |
| `access.users[].ssh_keys` | `ssh_authorized_keys` | `homestak.debian.users` |
| `access.posture` → `.ssh.*` | `ssh_permit_root_login`, etc. | `homestak.debian.security` |
| `access.posture` → `.sudo.*` | `sudo_nopasswd` | `homestak.debian.security` |
| `access.posture` → `.fail2ban.*` | `fail2ban_enabled` | `homestak.debian.security` |

**FK resolution:** By the time spec.yaml is on disk (fetched via `homestak spec get`), FKs are already resolved. SSH keys contain actual public key values, not references.

### Ansible Playbook

New playbook `ansible/playbooks/config-apply.yml`:

```yaml
---
- name: Apply specification
  hosts: all
  roles:
    - homestak.debian.base
    - homestak.debian.users
    - homestak.debian.security
  tasks:
    # Service management (not covered by existing roles)
    - name: Enable services
      ansible.builtin.systemd:
        name: "{{ item }}"
        enabled: true
        state: started
      loop: "{{ services_enable | default([]) }}"

    - name: Disable services
      ansible.builtin.systemd:
        name: "{{ item }}"
        enabled: false
        state: stopped
      loop: "{{ services_disable | default([]) }}"
```

### Platform-Ready Marker

Path: `$HOMESTAK_ROOT/.state/config/complete.json`

```json
{
  "phase": "config",
  "status": "complete",
  "timestamp": "2026-02-06T12:34:56Z",
  "spec": "base",
  "packages": 5,
  "services_enabled": 2,
  "services_disabled": 1,
  "users": 1
}
```

### Operator Pull Mode (executor.py)

Changes to `_create_node()` after the SSH wait phase:

```python
# After step 4 (wait SSH), check execution mode
exec_mode = mn.execution_mode or self.manifest.execution_mode

if mn.type == 'pve':
    # PVE lifecycle is always push (complex multi-step orchestration)
    self._run_pve_lifecycle(exec_node, ip, context)
elif exec_mode == 'pull':
    # Pull: VM self-configures, just poll for completion
    self._wait_for_config_complete(exec_node, ip, context)
else:
    # Push (default): driver runs ansible from controller over SSH
    self._push_config(exec_node, ip, context)
```

New method `_wait_for_config_complete()`:

```python
def _wait_for_config_complete(self, exec_node, ip, context, timeout=300):
    """Poll for spec fetch + config completion on a pull-mode node."""
    # 1. Wait for spec.yaml to appear
    wait_spec = WaitForFileAction(
        name=f'wait-spec-{exec_node.name}',
        host_key=f'{exec_node.name}_ip',
        file_path='$HOMESTAK_ROOT/.state/config/spec.yaml',
        timeout=timeout,
        interval=10,
    )
    result = wait_spec.run(self.config, context)
    if not result.success:
        return result

    # 2. Wait for config complete marker
    wait_config = WaitForFileAction(
        name=f'wait-config-{exec_node.name}',
        host_key=f'{exec_node.name}_ip',
        file_path='$HOMESTAK_ROOT/.state/config/complete.json',
        timeout=timeout,
        interval=10,
    )
    return wait_config.run(self.config, context)
```

### New Action: WaitForFileAction (actions/ssh.py)

```python
@dataclass
class WaitForFileAction:
    """Poll for a file to exist on a remote host via SSH."""
    name: str
    host_key: str
    file_path: str
    timeout: int = 300
    interval: int = 10

    def run(self, config, context) -> ActionResult:
        """Poll until file exists or timeout."""
        host = context.get(self.host_key)
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            rc, out, _ = run_ssh(host, f'test -f {self.file_path} && echo EXISTS',
                                 user=config.vm_user, timeout=10)
            if 'EXISTS' in out:
                return ActionResult(success=True, ...)
            time.sleep(self.interval)
        return ActionResult(success=False, message=f"Timeout waiting for {self.file_path}")
```

### Manifest Schema: execution_mode

Already parsed (manifest.py:131, 225). No schema changes needed.

```yaml
# Per-node execution mode
nodes:
  - name: web-server
    type: vm
    preset: vm-medium
    image: debian-12
    vmid: 99900
    execution:
      mode: pull    # Self-configures via cloud-init

# Manifest-level default
execution:
  default_mode: push   # Default (backward compatible)
```

### Cloud-Init Extension (tofu/envs/generic/main.tf)

Add `./run.sh config` to the existing runcmd block:

```hcl
    runcmd:
      - systemctl enable qemu-guest-agent
      - systemctl start qemu-guest-agent
%{if var.server_url != ""}
      - |
        # Bootstrap from server + config on first boot (v0.48+)
        if [ ! -f $HOMESTAK_ROOT/.state/config/complete.json ]; then
          . /etc/profile.d/homestak.sh
          curl -fsSk "$HOMESTAK_SERVER/bootstrap.git/install.sh" | \
            HOMESTAK_SERVER="$HOMESTAK_SERVER" HOMESTAK_REF=_working HOMESTAK_INSECURE=1 SKIP_SITE_CONFIG=1 bash
          $HOMESTAK_ROOT/iac/iac-driver/run.sh config --fetch --insecure \
            >>/var/log/homestak/config.log 2>&1 || true
        fi
%{endif}
```

**Note:** Cloud-init sources `/etc/profile.d/homestak.sh` which provides `HOMESTAK_SERVER` and `HOMESTAK_TOKEN` (a provisioning token minted at create time — see [provisioning-token.md](provisioning-token.md)). The runcmd bootstraps from the server (curls `install.sh`, clones repos via HTTPS with `HOMESTAK_REF=_working`). `SKIP_SITE_CONFIG=1` skips config clone since VMs receive pre-resolved specs via token. Then `./run.sh config --fetch --insecure` presents the token, fetches the spec, and applies config locally.

**Depth 2+ override ([iac-driver#200](https://github.com/homestak-iac/iac-driver/issues/200)):** At depth 2+, `server_url` in tfvars must point to the immediate parent's server, not the root host from site.yaml. `TofuApplyAction` overrides `server_url` with `HOMESTAK_SERVER` when set, so cloud-init bootstraps from the propagation chain (e.g., root-pve:44443 instead of srv1:44443).

## Integration Points

### Cross-Repo Data Flow

```
config                       iac-driver                     ansible
┌──────────────┐             ┌────────────────┐             ┌────────────────┐
│ specs/       │──resolve──▶ │ server         │             │ roles:         │
│ postures/    │             │                │             │ base, users,   │
│ secrets.yaml │             │                │             │ security       │
└──────────────┘             └───────┬────────┘             └───────▲────────┘
                                     │ serve                        │
                             ┌───────▼────────┐                     │
                             │ ./run.sh config │                    │
                             │   --fetch       │                    │
                             │                 │                    │
                             │ 1. spec_client  │                    │
                             │    → spec.yaml  │                    │
                             │ 2. config_apply │── ansible-playbook─┘
                             │    → roles      │
                             └─────────────────┘
```

`./run.sh config --fetch` runs ON the VM. The `--fetch` flag uses `spec_client.py` to fetch the spec from the server, then `config_apply.py` maps spec to ansible vars and runs roles locally.

### Integration Boundaries

| Boundary | Data | Format |
|----------|------|--------|
| Controller → spec_client | Resolved spec | JSON over HTTPS |
| spec_client → disk | spec.yaml | YAML file |
| config_apply.py → ansible | Generated vars | JSON vars file |
| config_apply.py → marker | config complete | JSON file |
| Operator → VM | File existence check | SSH + `test -f` |

### Path Mode Verification

Config phase runs on bootstrapped VMs at user-owned paths (`$HOMESTAK_ROOT`):
- Spec: `$HOMESTAK_ROOT/.state/config/spec.yaml`
- Marker: `$HOMESTAK_ROOT/.state/config/complete.json`
- iac-driver: `$HOMESTAK_ROOT/iac/iac-driver/`
- Ansible: `$HOMESTAK_ROOT/iac/ansible/`
- Playbook: `$HOMESTAK_ROOT/iac/ansible/playbooks/config-apply.yml`

Dev environment: set `$HOMESTAK_ROOT` to point to your workspace. User-owned paths (`$HOMESTAK_ROOT/iac/`) are the default.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Ansible not installed on fresh VM | Low | High | Bootstrap installs ansible; config requires bootstrap first |
| Spec FKs not resolved | Low | High | Server resolves FKs before serving; client receives flat values |
| Pull timeout too short | Medium | Medium | Configurable timeout (default 300s); cloud-init config can take 2-3 min |
| Self-signed TLS cert blocks spec get | Medium | Medium | Add `--insecure` to cloud-init runcmd and spec_client env var |
| Partial config apply on failure | Low | Medium | Marker only written on full success; re-run is idempotent |
| PVE nodes marked as pull | Low | Low | Validate: `pull` + `type: pve` = error at manifest parse time |

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| Pure Python config (no ansible) | Fewer deps, faster | Reimplements role logic | Premature; ansible roles are tested, correct |
| Config in bootstrap (`homestak config`) | Porcelain UX | Duplicates resolution logic; bootstrap is porcelain, not engine | iac-driver already has ConfigResolver, ansible integration |
| Shell script (no Python) | Simpler | Harder to parse YAML, map vars | Python already available (iac-driver dep) |

## Test Plan

### Unit Tests (iac-driver)

- `config_apply.py`: spec loading, vars mapping, marker generation
- Edge cases: empty spec, missing sections, already-applied
- `test_operator_executor.py`: pull mode skips PVE lifecycle, polls for marker
- `WaitForFileAction`: timeout, found, not found

### Integration Test

**Scenario:** `pull-vm-roundtrip` (iac-driver#156)

```bash
./run.sh scenario pull-vm-roundtrip -H srv1
```

**Steps:**
1. Start server
2. Provision VM with `execution.mode: pull` manifest
3. Wait for cloud-init to run `spec get` + `./run.sh config`
4. Verify spec.yaml exists on VM
5. Verify complete.json exists on VM
6. Verify packages installed (spot check)
7. Verify user created with SSH key
8. Destroy VM and stop server

**Fallback validation:**
```bash
./run.sh test -M n1-push -H srv1
```
Push mode regression — must still work.

### Operator Pull Mode Test (via manifest)

```yaml
# config/manifests/n1-pull.yaml
schema_version: 2
name: n1-pull
description: Single VM with pull execution mode
pattern: flat
nodes:
  - name: edge
    type: vm
    preset: vm-small
    image: debian-12
    vmid: 99950
    execution:
      mode: pull
```

```bash
./run.sh test -M n1-pull -H srv1
```

## Implementation Order

1. **iac-driver: `config_apply.py` + `config` verb** — can be developed and tested independently
2. **iac-driver: `WaitForFileAction`** — generic utility, no deps
3. **iac-driver: Executor pull mode** — wires 1 + 2 together
4. **ansible: `config-apply.yml` playbook** — used by config_apply.py
5. **tofu: Cloud-init runcmd extension** — adds `./run.sh config` to first boot
6. **config: n1-pull manifest** — test fixture

## Deferred

- **Push-mode config** (driver SSHes in and runs `./run.sh config`) — useful but not required this sprint. Current push mode provisions via tofu + ansible; config command is additive.
- **`spec.config` type-specific section** (PVE-specific config like subscription nag removal) — future enhancement.
- **Run phase triggers** — separate issue, not config phase.
- **Rollback on failure** — spec says best-effort; idempotent re-run is sufficient.
- **`HOMESTAK_INSECURE` env var** for spec_client — small addition, can be done in this sprint or deferred.

## Open Questions

1. ~~**Should `homestak config` also run `spec get` if spec.yaml is missing?**~~ Recommendation: No. Keep commands separate. `spec get` + `config` is the explicit flow. Cloud-init chains them.

2. **Should we validate the manifest rejects `execution.mode: pull` on PVE nodes?** Recommendation: Yes. Add validation in `manifest.py` — pull mode on `type: pve` is an error.

## Design Decisions

### D1: Config lives in iac-driver, not bootstrap (2026-02-06)

**Decision:** The `config` verb is implemented in iac-driver (`./run.sh config`), not as a `homestak config` command in bootstrap.

**Rationale:**
- iac-driver already has ConfigResolver, ansible integration, and action patterns
- Avoids duplicating YAML→ansible mapping logic in bootstrap
- bootstrap stays as thin porcelain; iac-driver is the engine
- Cloud-init calls iac-driver directly: `$HOMESTAK_ROOT/iac/iac-driver/run.sh config`
- `homestak spec get` (bootstrap) fetches the spec; `./run.sh config` (iac-driver) applies it — clean separation of concerns

## Implementation Status

**Implemented in Sprint #201 (v0.48)**

| Component | Status | Commit |
|-----------|--------|--------|
| `config_apply.py` (config verb) | Done | iac-driver 73a9fc1 |
| `WaitForFileAction` | Done | iac-driver 73a9fc1 |
| Operator pull mode | Done | iac-driver 73a9fc1 |
| `config-apply.yml` playbook | Done | ansible d3407fe |
| Cloud-init runcmd extension | Done | tofu 676f5bc |
| `n1-pull.yaml` test manifest | Done | config 02c4108 |
| `pull-vm-roundtrip` scenario | Done | iac-driver 3c2017b |
| `edge.yaml` spec | Done | config 0b0faed |

**Post-merge fixes (iac-driver#163, v0.48+):**

| Component | Fix | Commit |
|-----------|-----|--------|
| `controller/repos.py` | Bare repo HEAD→_working so `git clone` gets uncommitted changes | iac-driver#165 |
| `config_apply.py` | Set ANSIBLE_CONFIG env var for cloud-init environments | iac-driver#165 |
| `scenarios/vm_roundtrip.py` | Increase wait_spec timeout 90→150s (bootstrap ~100s) | iac-driver#165 |
| `tofu/envs/generic/main.tf` | Add `HOMESTAK_REF=_working` to cloud-init runcmd | tofu#40 |
| `tofu/envs/generic/main.tf` | Fix SSH key indent (6→10) in cloud-init user-data | tofu#40 |
| `users/defaults/main.yml` | Add `local_user_shell` default for users role | ansible#37 |
| `specs/edge.yaml` | Fix SSH key FK (`jderose@father` not `jderose`) | config#55 |

**Known issues:**
- ~~iac-driver#166: `StartSpecServerAction` SSH FD inheritance~~ — **Fixed** (Sprint #209): close inherited FDs > 2 before exec

**Sprint #249 (Config Phase Completion, homestak-dev#249):**

| Component | Status | Notes |
|-----------|--------|-------|
| Push-mode config (`_push_config`) | Done | Operator runs ansible from controller over SSH (iac-driver#206) |
| Manifest validate verb | Done | `./run.sh manifest validate -M <name> -H <host>` (iac-driver#207) |
| n2-pull manifest (ST-5) | Done | Push-mode PVE + pull-mode VM (config#67) |
| Push-mode cloud-init race fix | Done | Skip spec injection for push-mode nodes |
| Packer apt cache fix | Done | Stop removing apt lists in cleanup (packer#47) |

**Deferred:**
- PVE+pull manifest validation (edge case, non-critical)
- `HOMESTAK_INSECURE` env var for spec_client
