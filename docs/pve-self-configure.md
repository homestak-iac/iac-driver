# PVE Self-Configure Lifecycle

**Epic:** [iac-driver#125](https://github.com/homestak-iac/iac-driver/issues/125) (Node Lifecycle Architecture)
**Builds on:** [config-distribution.md](config-distribution.md), [operational-model.md](operational-model.md)
**Date:** 2026-03-20

How the PVE node lifecycle evolves from 11 parent-driven SSH phases to 2 phases with pull-mode config and child self-configuration.

## Problem

The current PVE lifecycle is a monolithic sequence of 11 phases, all driven by the parent via SSH:

```
Parent SSHes to child for every step:
  1. bootstrap            (pull transport, SSH trigger)
  2. copy_secrets          (SCP push)
  3. copy_site_config      (SCP push)
  4. inject_ssh_key        (SCP push)
  5. copy_private_key      (SCP push)
  6. pve-setup             (SSH trigger, runs locally)
  7. configure_bridge      (SSH push)
  8. generate_node_config  (SSH trigger, runs locally)
  9. create_api_token      (SSH trigger, runs locally)
  10. inject_self_ssh_key  (SSH push)
  11. download_images      (SSH trigger, child pulls from GitHub)
```

The parent micro-manages every step. Phases 8-9 are redundant with pve-setup (which already includes them). The push model requires sustained SSH connectivity and creates tight coupling between parent and child.

## Target: 2-Phase Model

```
PHASE 1 — Bootstrap + Config Pull (child-driven, cloud-init)
├── cloud-init fires bootstrap (pull from parent server)
└── child pulls config: GET /config/{identity}
    → site.yaml + scoped secrets + SSH keys + private key

PHASE 2 — Child self-configure (enriched pve-setup)
├── install PVE (if needed)
├── configure bridge + DNS
├── generate node config
├── create API token
├── inject self SSH key
├── download packer images
└── write completion marker → parent resumes delegation
```

The parent's only active role after cloud-init is polling for the Phase 2 completion marker. All configuration data — including the private key — reaches the child via the `/config/{identity}` pull.

## Phase Mapping: 11 → 2

| # | Current Phase | New Bucket | Mechanism | Notes |
|---|--------------|-----------|-----------|-------|
| 1 | bootstrap | Phase 1 | Cloud-init runcmd | Trigger changes; data flow unchanged |
| 2 | copy_secrets | Phase 1 | `GET /config/{identity}` | Push → pull (iac-driver#248) |
| 3 | copy_site_config | Phase 1 | Same `/config` response | Folded with secrets |
| 4 | inject_ssh_key | Phase 1 | Part of scoped secrets | Already in parent's `secrets.ssh_keys` |
| 5 | copy_private_key | Phase 1 | `GET /config/{identity}` | Folded into `/config` response; see [Private Key Decision](#private-key-decision) |
| 6 | pve-setup | Phase 2 | `pve-setup --local` | Already runs locally; trigger changes |
| 7 | configure_bridge | Phase 2 | Folded into enriched pve-setup | Simpler without SSH stability concerns |
| 8 | generate_node_config | Phase 2 | Already in pve-setup | Redundant — eliminated as separate phase |
| 9 | create_api_token | Phase 2 | Already in pve-setup | Redundant — eliminated as separate phase |
| 10 | inject_self_ssh_key | Phase 2 | Local self-operation | Trivial |
| 11 | download_images | Post-Phase 2 | Inner executor preflight | Decoupled from PVE lifecycle |

## Phase Details

### Phase 1: Bootstrap + Config Pull

Both steps are child-driven via cloud-init runcmd — the parent does not intervene.

**1a. Bootstrap (cloud-init → pull)**

Cloud-init runcmd triggers bootstrap from the parent's server. The child curls the install script, creates the `homestak` user, clones all repos from `HOMESTAK_SERVER` using `_working` ref. This is already pull at the transport layer — only the trigger changes from parent-SSH to cloud-init.

**1b. Config Pull**

After bootstrap completes, the child fetches authored config from the parent's server:

```
GET /config/{identity}
Authorization: Bearer <provisioning-token>

Response:
{
  "site": { ... site.yaml defaults (DNS, gateway, timezone) ... },
  "secrets": {
    "signing_key": "...",
    "ssh_keys": { "driver": "ssh-ed25519 ...", ... },
    "passwords": { "vm_root": "$6$..." },
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
  }
}
```

This single pull replaces four push phases (copy_secrets, copy_site_config, inject_ssh_key, copy_private_key). The provisioning token authenticates the request; the server responds with scoped secrets (no `api_tokens`).

The child writes the private key to `~/.ssh/id_rsa` (mode 600), enabling it to SSH to VMs it will create for its subtree.

**Prerequisite:** iac-driver#248 (`/config/{identity}` endpoint).

### Phase 2: Self-Configure (Enriched pve-setup)

The child runs an enriched `pve-setup` scenario locally that consolidates phases 6-11:

1. **install PVE** — `ensure_pve` detects if PVE needs installation. On `pve-9` images, PVE is pre-installed (skip). Handles reboot if kernel install needed (idempotent re-entry via dpkg state detection).
2. **configure PVE** — repos, subscription nag removal, base packages, security posture.
3. **configure bridge** — creates vmbr0 from eth0, sets DNS from `site.yaml` (now local). Simpler than push model: no SSH connection stability concern.
4. **generate node config** — `make node-config FORCE=1`. Currently redundant with pve-setup; consolidated.
5. **create API token** — `pveum user token add`, inject into local `secrets.yaml`. Currently redundant with pve-setup; consolidated.
6. **inject self SSH key** — reads own `~/.ssh/id_rsa.pub`, adds to local `secrets.ssh_keys`. Ensures child VMs authorize this node's key.
7. **download images** — fetches packer images from GitHub. Requires DNS (available after bridge config).
8. **write completion marker** — signals to parent that self-configure is done.

The parent polls for the completion marker via SSH, then proceeds with subtree delegation.

## DNS Chicken-and-Egg (Resolved)

The ordering naturally resolves the DNS dependency:

| Step | Network state | DNS needed? |
|------|--------------|-------------|
| Phase 1: bootstrap | flat eth0, DHCP, parent by IP | No |
| Phase 1: config pull | flat eth0, parent by IP | No |
| Phase 2: bridge config | vmbr0 created, DNS set from site.yaml | Sets DNS |
| Phase 2: image download | vmbr0 with DNS | Yes (GitHub) |

The child reaches the parent's server by IP (injected via cloud-init) for all pre-bridge operations. DNS is only needed after bridge configuration, at which point `site.yaml` DNS servers are already local.

## Image Download Timing

The child doesn't know which images its children need until subtree delegation (when the parent passes the manifest). Two options:

**(a)** Include image list in `/config` response — requires manifest awareness server-side.

**(b)** Let the inner executor's preflight handle it — `EnsureImageAction` already exists, decouple from PVE lifecycle.

**Recommendation:** Option (b). The inner executor (running on the child during delegation) already has preflight checks for image availability. Image download is an executor concern, not a PVE lifecycle concern. However, the enriched pve-setup could download commonly-needed images as an optimization.

## Completion Signaling

The parent needs to know the child is ready for subtree delegation. A PVE-specific completion marker (beyond the generic `complete.json`) should indicate:

```json
{
  "phase": "pve-config",
  "status": "success",
  "timestamp": "2026-03-20T14:30:00Z",
  "pve_installed": true,
  "bridge_configured": true,
  "api_token_created": true,
  "node_config_generated": true
}
```

The parent polls for this marker using the existing `WaitForFileAction` pattern, then initiates subtree delegation.

## Private Key Decision

### The question

The `config-distribution.md` mid-term plan originally stated: "copy_private_key remains as push (private keys don't belong on HTTP endpoints)." This assertion was examined and overturned.

### The analysis

The `/config/{identity}` endpoint already serves:
- `signing_key` — an HMAC-SHA256 secret used to mint provisioning tokens
- `passwords.vm_root` — a root password hash
- `ssh_keys` — SSH public keys

The signing key is equally sensitive to a private SSH key — compromise of either grants unauthorized access. The endpoint is authenticated (provisioning token) and encrypted (TLS). Adding the private key to this response does not materially change the threat surface.

### The decision

**Include the private key in the `/config` response.** This enables the pure 2-phase model where the parent's only post-cloud-init role is polling for the completion marker. Benefits:

- **Eliminates the last SCP push** — no parent intervention between bootstrap and self-configure
- **Simplifies the parent's role** — fire cloud-init, poll for completion, delegate
- **Reduces SSH dependency** — the parent doesn't need SSH access to the child until delegation
- **Consistent security model** — all secrets travel through the same authenticated, encrypted channel

### Dev vs prod key models

| Posture | Key model | Private key in `/config`? |
|---------|-----------|--------------------------|
| `dev` | Shared key | Yes — parent's key served to child |
| `prod` | Per-node keys (self-generated) | No — child generates its own keypair |

In prod mode, each PVE node generates its own SSH keypair during self-configure. The parent's key is NOT distributed. Jump-chain access works because `secrets.ssh_keys` accumulates public keys at each depth:

```
Driver:     [driver_key]
Root-pve:   [driver_key, root-pve_key]
Leaf-pve:   [driver_key, root-pve_key, leaf-pve_key]
```

Since specs default to `ssh_keys: all`, VMs authorize the full accumulated set. The driver can reach any depth using its own key.

## Evolution Path

| Step | Issue | What changes |
|------|-------|-------------|
| 1 | iac-driver#248 | Build `/config/{identity}` endpoint with private key — enables Phase 1 pull |
| 2 | iac-driver#275 | Enrich pve-setup to absorb phases 7, 10, 11; remove redundant executor phases 8, 9 |
| 3 | (new) | Wire Phase 1/2 in executor: cloud-init bootstrap + config pull → wait for self-configure |
| 4 | (new) | Per-node key generation for prod posture — eliminates shared key distribution |

### Phase count progression

```
Current:    11 phases, all parent-SSH-driven
After #248:  2 phases (pull replaces 4 SCP pushes, enriched pve-setup absorbs 6-11)
After #275:  2 phases (cleaner — pve-setup fully consolidated)
Long-term:   2 phases (per-node keys eliminate shared key distribution)
```

## Long-term: SSH Elimination Trajectory

The 2-phase model reduces SSH but doesn't eliminate it — the parent still SSHes to the child for delegation (`RecursiveScenarioAction`) and completion polling (`WaitForFileAction`). The logical endpoint of the pull model eliminates SSH entirely:

```
Current (11-phase):
  Driver ──SSH──▶ root-pve ──SSH──▶ leaf-pve ──SSH──▶ VMs

Mid-term (2-phase + SSH delegation):
  Driver ──API──▶ root-pve (cloud-init creates VM)
  Driver ──HTTPS─▶ root-pve (serves /config)
  Driver ──SSH──▶ root-pve (polls marker, delegates subtree)

Long-term (server-mediated delegation + pull-mode VMs):
  Driver ──API──▶ root-pve (cloud-init)
  Driver ──HTTPS─▶ root-pve (serves /config, /delegate)
  root-pve ──API──▶ own VMs (cloud-init)
  root-pve ──HTTPS─▶ own VMs (serves /config)
  Zero SSH in the entire tree
```

In the server-mediated model, the parent posts the subtree manifest to a `/delegate/{identity}` endpoint. The child pulls and executes it after self-configure completes. Each PVE level becomes a miniature server — config and delegation work flow downward via HTTPS, not SSH.

**Push mode retains lasting value** even as pull becomes the default capability. SSH access provides real-time log streaming, immediate error feedback, and interactive debugging. Push mode should remain available as an execution option — the architecture trends toward pull everywhere from a capability standpoint, not as a mandate.

## Implementation Details

This section fleshes out the mid-term implementation: what needs to change, where, and how the pieces connect.

### Cloud-init runcmd for PVE nodes

Currently, PVE nodes are push-only. The executor's `_run_pve_lifecycle()` handles all 11 phases via SSH. Cloud-init for PVE nodes only starts `qemu-guest-agent` (no bootstrap, no config pull).

Pull-mode VMs use a different path: cloud-init runcmd does bootstrap + `HOMESTAK_BOOT_SCENARIO=vm-config` (currently `config`), which runs `./run.sh config fetch && ./run.sh config apply`. The conditional in `tofu/envs/generic/main.tf` is `server_url != "" && auth_token != ""`.

PVE nodes need a third path. The runcmd must:

1. Start qemu-guest-agent (same as all VMs)
2. Bootstrap from parent server (same as pull-mode VMs)
3. Fetch config from `/config/{identity}` (new — not `/spec`)
4. Run self-configure locally (new scenario)

**Approach:** A new `HOMESTAK_BOOT_SCENARIO` value — `pve-config` — that the bootstrap install script dispatches to:

```bash
# Cloud-init runcmd for PVE nodes (generated by tofu)
if [ ! -f ~homestak/.state/pve-config/success.json ]; then
  . /etc/profile.d/homestak.sh
  curl -fsSk "$HOMESTAK_SERVER/bootstrap.git/install" | \
    HOMESTAK_SERVER="$HOMESTAK_SERVER" HOMESTAK_REF=_working \
    HOMESTAK_INSECURE=1 SKIP_SITE_CONFIG=1 \
    HOMESTAK_BOOT_SCENARIO=pve-config bash
fi
```

**How does tofu know to generate PVE-specific cloud-init?**

The executor already knows node type (`mn.type == 'pve'`). Two changes needed:

1. **ConfigResolver** — when node type is `pve`, mint a config token (not a spec token). The token claims would carry `"t": "config"` (or use a separate minting method) so the server knows to serve `/config` data.

2. **Tofu template** — add a third conditional block for PVE nodes, or generalize `HOMESTAK_BOOT_SCENARIO` as a tofu variable that ConfigResolver sets based on node type (`vm-config` for VMs, `pve-config` for PVE nodes).

Option 2 is cleaner — add a `boot_scenario` variable to the tofu template:

```hcl
# tofu/envs/generic/main.tf (new variable)
variable "vms" {
  type = list(object({
    # ... existing fields ...
    boot_scenario = optional(string, "vm-config")  # "vm-config" or "pve-config"
  }))
}
```

The runcmd becomes:

```hcl
HOMESTAK_BOOT_SCENARIO=${vm.boot_scenario} bash
```

ConfigResolver sets `boot_scenario = "pve-config"` for PVE nodes, `"vm-config"` for VMs.

### pve-config scenario

The cloud-init runcmd needs a single invocable entry point. This is a new scenario that wraps Phase 1b (config pull) and all of Phase 2 (self-configure):

```
./run.sh scenario run pve-config --local
```

The scenario phases:

| # | Phase | Action | Notes |
|---|-------|--------|-------|
| 1 | fetch_config | `ConfigFetchAction` | `GET /config/{identity}` → write site.yaml, secrets.yaml, private key |
| 2 | ensure_pve | `EnsurePVEAction` | Existing — detect/install PVE, handle reboot |
| 3 | setup_pve | `AnsibleLocalPlaybookAction` | Existing — repos, nag removal, packages |
| 4 | configure_bridge | `ConfigureBridgeAction` (local) | Existing logic, adapted for local execution |
| 5 | generate_node_config | `GenerateNodeConfigAction` (local) | `make node-config FORCE=1` |
| 6 | create_api_token | `CreateApiTokenAction` (local) | `pveum user token add`, inject into secrets.yaml |
| 7 | inject_self_ssh_key | `InjectSelfSSHKeyAction` (local) | Read own pubkey, add to secrets.ssh_keys |
| 8 | write_marker | `WriteMarkerAction` | Write completion marker |

**New action: `ConfigFetchAction`** — the glue between Phase 1 and Phase 2:

```python
class ConfigFetchAction:
    """Fetch config from parent's /config endpoint and write local files."""

    def run(self, config, context):
        # 1. Read HOMESTAK_SERVER + HOMESTAK_TOKEN from env
        # 2. GET /config/{identity} with Bearer token
        # 3. Write site.yaml to ~/config/site.yaml
        # 4. Write secrets.yaml to ~/config/secrets.yaml (mode 600)
        # 5. Write private_key to ~/.ssh/id_rsa (mode 600)
        # 6. Derive pubkey, write to ~/.ssh/id_rsa.pub
```

**Existing actions need local variants.** Several PVE lifecycle actions (`ConfigureBridgeAction`, `GenerateNodeConfigAction`, etc.) currently SSH to a remote host. The self-configure scenario runs locally, so these actions need a `local=True` mode or local-specific variants. The pve-setup scenario already uses local ansible — the same pattern applies.

### Executor refactoring

The executor's `_create_node()` method (lines 320-354 in `executor.py`) currently branches on node type:

```python
# Current
if mn.type == 'pve' and ip:
    pve_result = self._run_pve_lifecycle(exec_node, ip, context)
elif exec_mode == 'pull':
    result = self._wait_for_config_complete(ip, mn, context)
else:
    result = self._push_config(ip, mn, exec_node, context)
```

In the 2-phase model, PVE nodes take the pull path:

```python
# New
if mn.type == 'pve' and ip:
    # 2-phase model: PVE node self-configures via cloud-init
    result = self._wait_for_pve_config(exec_node, ip, context)
elif exec_mode == 'pull':
    result = self._wait_for_config_complete(ip, mn, context)
else:
    result = self._push_config(ip, mn, exec_node, context)
```

`_wait_for_pve_config()` polls for the PVE-specific completion marker:

```python
def _wait_for_pve_config(self, exec_node, ip, context):
    # Poll for success or failure marker
    # Timeout: 1200s (PVE install can take 15-20 min)
    # Interval: 30s (longer phases, less frequent polling)
    WaitForFileAction(
        file_path='~/.state/pve-config/success.json',
        failure_path='~/.state/pve-config/failure.json',
        timeout=1200,
        interval=30,
    )
```

**What changes in tofu create:** The executor must pass PVE-specific parameters to `TofuApplyAction` so the cloud-init generates the right runcmd. Currently:

```python
tofu_spec = mn.spec if exec_mode == 'pull' else None
```

New logic:

```python
if mn.type == 'pve':
    tofu_spec = None                        # PVE nodes don't use /spec
    tofu_boot_scenario = 'pve-config'
    tofu_auth_token = self._mint_config_token(mn)  # Token for /config endpoint
elif exec_mode == 'pull':
    tofu_spec = mn.spec
    tofu_boot_scenario = 'vm-config'
    tofu_auth_token = None                  # Minted by ConfigResolver
else:
    tofu_spec = None
    tofu_boot_scenario = None
    tofu_auth_token = None
```

This requires `TofuApplyAction` and `ConfigResolver` to accept the new parameters.

**The 11-phase `_run_pve_lifecycle()` is preserved** — it remains available for push-mode PVE provisioning (debugging, fallback, or explicit `execution.mode: push` in manifests). The executor chooses based on a to-be-determined trigger (default pve-config for PVE nodes, override to push via manifest).

### Error handling and timeout strategy

In the 2-phase model, the parent's failure signals are:

| Signal | Detection | Latency |
|--------|-----------|---------|
| Completion marker appears | WaitForFileAction succeeds | Immediate (next poll) |
| Failure marker appears | WaitForFileAction checks both | Immediate (next poll) |
| Silent failure (crash, hang) | WaitForFileAction timeout | Up to 1200s |

**Marker pattern:** The pve-config scenario writes a success or failure marker:

```
Success: $HOMESTAK_ROOT/.state/pve-config/success.json
Failure: $HOMESTAK_ROOT/.state/pve-config/failure.json
```

Failure marker contents:

```json
{
  "phase": "pve-config",
  "status": "failed",
  "failed_at": "configure_bridge",
  "timestamp": "2026-03-20T14:35:00Z",
  "error": "vmbr0 creation failed: interface eth0 not found"
}
```

The existing `WaitForFileAction` gains an optional `failure_path` parameter. When set, it polls for both files each interval — if the failure file appears first, the action returns failure immediately:

```python
WaitForFileAction(
    file_path='~/.state/pve-config/success.json',
    failure_path='~/.state/pve-config/failure.json',
    timeout=1200,
    interval=30,
)
```

On failure detection, the parent can SSH to the child for diagnosis (logs, state inspection) before returning the error to the operator.

**Timeout values:** PVE self-configure is much longer than VM config apply:

| Operation | Expected duration | Timeout |
|-----------|------------------|---------|
| VM config apply (pull) | ~60s | 300s |
| pve-config (pve-9 image) | ~3-5 min | 600s |
| pve-config (debian-13, installs PVE) | ~15-20 min | 1200s |

### Reboot re-entry during self-configure

**pve-9 images:** PVE is pre-installed. No reboot needed. The `ensure_pve` phase detects `pveproxy` running and skips installation. This is the common case.

**debian-13 images:** PVE installation requires kernel install → reboot → package install. Cloud-init runcmd runs once and does not re-execute after reboot.

**Solution: systemd oneshot service.**

1. Cloud-init runcmd installs and enables the service:
   ```bash
   # In bootstrap's pve-config dispatch
   cat > /etc/systemd/system/pve-config.service << 'UNIT'
   [Unit]
   Description=PVE Config (self-configure)
   After=network-online.target
   Wants=network-online.target

   [Service]
   Type=oneshot
   User=homestak
   ExecStart=/home/homestak/iac/iac-driver/run.sh scenario run pve-config --local
   RemainAfterExit=yes

   [Install]
   WantedBy=multi-user.target
   UNIT

   systemctl daemon-reload
   systemctl enable --now pve-config.service
   ```

2. On first run: `ensure_pve` installs kernel, reboots
3. After reboot: systemd starts service again, `ensure_pve` detects kernel installed (dpkg state), skips to packages phase
4. After completion: service writes marker, disables itself

The existing dpkg state detection in `_EnsurePVEPhase` handles the idempotent re-entry — the systemd service just provides the trigger.

### `/config/{identity}` endpoint scoping

The `/config` endpoint is distinct from `/spec`. Different data, different purpose:

| | `/spec/{identity}` | `/config/{identity}` |
|---|---|---|
| Consumer | VMs (what to become) | PVE nodes (operational config) |
| Auth | Provisioning token (spec claim) | Provisioning token (config claim) |
| Response | Resolved spec (packages, users, services) | Site config + scoped secrets |

**Response structure:**

```json
{
  "site": {
    "timezone": "America/Denver",
    "domain": "",
    "gateway": "192.0.2.1",
    "dns_servers": ["192.0.2.1"],
    "bridge": "vmbr0",
    "packages": ["htop", "curl", "wget", "net-tools"],
    "pve_remove_subscription_nag": true,
    "packer_release": "latest"
  },
  "secrets": {
    "signing_key": "abcdef1234...",
    "ssh_keys": {
      "driver": "ssh-ed25519 AAAA..."
    },
    "passwords": {
      "vm_root": "$6$..."
    },
    "private_key": "-----BEGIN OPENSSH PRIVATE KEY-----\n..."
  }
}
```

**Scoping rules:**

| Field | Included | Rationale |
|-------|----------|-----------|
| `site.*` | All | PVE node needs DNS, gateway, timezone for bridge config and child VMs |
| `secrets.signing_key` | Yes | Needed to mint tokens for child VMs |
| `secrets.ssh_keys` | Yes | Injected into child VMs' authorized_keys |
| `secrets.passwords.vm_root` | Yes | Needed for child VMs |
| `secrets.api_tokens` | **No** | Each PVE node generates its own via `pveum` |
| `secrets.private_key` | Dev only | See [Private Key Decision](#private-key-decision) |

**Server-side implementation:**

```python
# server/config_endpoint.py (new file)
def handle_config_request(identity, signing_key, secrets, site_config, posture):
    """Build scoped /config response."""
    scoped_secrets = dict(secrets)
    scoped_secrets.pop('api_tokens', None)

    if posture == 'dev':
        # Read parent's private key
        key_path = os.path.expanduser('~/.ssh/id_rsa')
        scoped_secrets['private_key'] = Path(key_path).read_text()
    # prod posture: no private_key (child generates its own)

    return {
        "site": site_config,
        "secrets": scoped_secrets,
    }
```

The server reads `~/.ssh/id_rsa` at request time (not cached at startup) to pick up key rotations.

**Token differentiation:** The provisioning token needs to indicate whether the request is for `/spec` or `/config`. Options:

1. **Separate claim:** `"t": "config"` vs `"t": "spec"` in token payload
2. **URL-based routing:** Server routes based on URL path, token just authenticates
3. **Implicit from node type:** Token carries node type, server decides

Option 2 is simplest — the token authenticates the caller, the URL determines what's served. The server validates that the token's identity matches the URL identity (defense-in-depth, already implemented for `/spec`). No token format changes needed.

## Open Questions

1. **pve-config as default or opt-in?** Should PVE nodes default to the 2-phase pve-config model, with `execution.mode: push` as the fallback? Or should it require explicit opt-in via a new execution mode? Default pve-config is cleaner; opt-in is safer for the transition.

2. **Key registration for per-node model:** How does a child's generated public key reach the parent's accumulation chain? This is the enabling mechanism for eliminating shared key distribution in prod mode. Options:
   - Parent reads it from the child via SSH during delegation setup
   - Child registers it with the parent's server (new endpoint)
   - Parent polls for a key-available marker, then reads it

3. **Posture awareness in `/config` response:** How does the server know the deployment posture (dev vs prod) to decide whether to include the private key? Options: posture claim in token, server-side config, or always include and let the client decide. Server-side config (from site.yaml or a server flag) is simplest.

## Related Documents

- [config-distribution.md](config-distribution.md) — Short/mid/long-term config distribution evolution
- [operational-model.md](operational-model.md) — Execution models, state architecture, 3-phase vision
- [node-lifecycle.md](node-lifecycle.md) — Single-node lifecycle phases
- [server-daemon.md](server-daemon.md) — Server architecture, endpoint design
- [config-phase.md](config-phase.md) — Push/pull execution, spec-to-ansible mapping
- [provisioning-token.md](provisioning-token.md) — Token auth for pull endpoints

## Related Issues

| Issue | Relationship |
|-------|-------------|
| [iac-driver#248](https://github.com/homestak-iac/iac-driver/issues/248) | `/config` endpoint — enables Phase 1 pull (delivered) |
| [iac-driver#275](https://github.com/homestak-iac/iac-driver/issues/275) | Operator simplification — enables Phase 2 consolidation |
| [iac-driver#125](https://github.com/homestak-iac/iac-driver/issues/125) | Node Lifecycle Architecture epic |
