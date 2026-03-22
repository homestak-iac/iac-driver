# Config Distribution to Delegated PVE Nodes

**Epic:** [iac-driver#125](https://github.com/homestak-iac/iac-driver/issues/125) (Node Lifecycle Architecture)

How site configuration and secrets reach PVE nodes in multi-level deployments.

## Problem

Delegated PVE nodes (e.g., root-pve, leaf-pve in n2/n3 manifests) need site-specific configuration from their parent to function:

| Need | Why |
|------|-----|
| `dns_servers` | Bridge DNS config, image downloads from GitHub |
| `gateway` | Network routing for child VMs |
| `signing_key` | Mint provisioning tokens for child VMs |
| `ssh_keys` | Inject into child VMs for SSH access |
| `vm_root` password | Child VM root access |
| Private key | SSH to child VMs |

These values are local to each deployment (gitignored) and must be distributed to each PVE level in the propagation chain.

## Current State

```
Driver                          root-pve                        leaf-pve
┌─────────────┐                 ┌─────────────┐                 ┌─────────────┐
│ site.yaml   │                 │ site.yaml   │                 │ site.yaml   │
│ secrets.yaml│                 │ secrets.yaml│                 │ secrets.yaml│
│ ~/.ssh/key  │                 │ ~/.ssh/key  │                 │ ~/.ssh/key  │
└─────────────┘                 └─────────────┘                 └─────────────┘
      │                               │                               │
      │ PVE lifecycle phases:         │ Delegated PVE lifecycle:      │
      │  1. bootstrap (pull)          │  Same phases, one level       │
      │  2. copy_secrets (push)       │  deeper                      │
      │  3. copy_site_config (push)   │                               │
      │  4. inject_ssh_key (push)     │                               │
      │  5. copy_private_key (push)   │                               │
      │  6. pve-setup (push)          │                               │
      │  7. configure_bridge (push)   │                               │
      │  ...                          │                               │
```

### What works

- **Code repos**: Served via `_working` branch on the parent's server. Bootstrapped nodes clone from `HOMESTAK_SERVER`. Pull model.
- **Specs**: Served via `/spec/{identity}` endpoint, authenticated by provisioning token. Pull model.
- **Secrets**: Pushed via SCP (`copy_secrets` phase). Entire `secrets.yaml` including all hosts' API tokens (over-sharing).
- **Private key**: Pushed via SCP (`copy_private_key` phase). Shared key model — same key at every depth.

### What's broken

- **site.yaml**: Was git-tracked, so it arrived via `_working` branch during bootstrap. After config#84 (gitignored for new-user onboarding), delegated PVE nodes get the blank template. DNS breaks at depth 2+ (n3-deep failure: `Could not resolve host: github.com` on leaf-pve).

## Progression

### Short-term: Push site config + scope secrets

**Add `copy_site_config` phase** to the PVE lifecycle. Same SCP pattern as `copy_secrets` — pushes `site.yaml` from the driver to the target PVE node after bootstrap. Not a hack; uses the established push mechanism.

**Scope `copy_secrets`.** Exclude `api_tokens` before copying — each PVE node generates its own via pve-setup. Reduces over-sharing without changing the push mechanism.

| Change | Component |
|--------|-----------|
| Add `CopySiteConfigAction` | `actions/pve_lifecycle.py` |
| Add `copy_site_config` phase (after `copy_secrets`) | `manifest_opr/executor.py` |
| Filter `api_tokens` from `copy_secrets` | `actions/pve_lifecycle.py` — `CopySecretsAction` |

### Mid-term: Pull-mode config distribution

**Add `/config/{identity}` endpoint** to the server. Serves scoped site config + secrets, authenticated by provisioning token. Replaces `copy_secrets` and `copy_site_config` (push) with pull.

```
GET /config/{identity}
Authorization: Bearer <provisioning-token>

Response:
{
  "site": { ... site.yaml defaults ... },
  "secrets": {
    "signing_key": "...",
    "ssh_keys": { ... },
    "passwords": { "vm_root": "..." }
  }
}
```

PVE lifecycle changes:
- `copy_secrets` phase replaced by `fetch_config` — target pulls from parent's server
- `inject_ssh_key` folded into scoped secrets response (driver's key in `ssh_keys`)
- `copy_private_key` folded into `/config` response (see [pve-self-configure.md — Private Key Decision](pve-self-configure.md#private-key-decision))
- Force-adding `site.yaml` to `_working` no longer needed (served via endpoint)

Client side: extend `./run.sh config fetch` to call `/config/{identity}` using `HOMESTAK_TOKEN`, write `site.yaml` and `secrets.yaml` locally.

| Change | Component |
|--------|-----------|
| `/config/{identity}` endpoint | `server/httpd.py`, new `server/config_endpoint.py` |
| Scoped secrets builder | `server/config_endpoint.py` |
| `fetch_config` lifecycle phase | `actions/pve_lifecycle.py` |
| Config fetch client | `src/config_apply.py` or `src/cli.py` |

### Long-term: Per-node keys with posture-driven model

**Each PVE node uses its own generated SSH key** instead of sharing the driver's key. Posture controls the model:

| Posture | Key model | Rationale |
|---------|-----------|-----------|
| `dev` | Shared key | Convenience — direct access to any depth |
| `prod` | Per-node keys | Security — compromise of one node doesn't expose others |

Jump chain observability is preserved because `secrets.ssh_keys` accumulates keys at each level:

```
Driver's secrets.ssh_keys:
  [driver_key]

Root-pve's secrets.ssh_keys:
  [driver_key, root-pve_key]      ← driver's key propagated + own key added

Leaf-pve's secrets.ssh_keys:
  [driver_key, root-pve_key, leaf-pve_key]   ← full chain accumulated
```

Since specs default to `ssh_keys: all`, every VM authorizes the full accumulated set. The driver can jump-chain to any depth using its own key.

`copy_private_key` eliminated in prod mode — each node SSHes to its children with its own generated key.

| Change | Component |
|--------|-----------|
| Posture-driven key model | `actions/pve_lifecycle.py` |
| Skip `copy_private_key` for prod | `manifest_opr/executor.py` |
| Key accumulation in secrets | `actions/pve_lifecycle.py` — `InjectSelfSSHKeyAction` |

## PVE Lifecycle Phase Evolution

| Phase | Short-term | Mid-term | Long-term |
|-------|-----------|----------|-----------|
| bootstrap | Pull (server) | Pull (server) | Pull (server) |
| copy_secrets | **Push (SCP, scoped)** | **Pull** (`/config` endpoint) | Pull (`/config` endpoint) |
| copy_site_config | **Push (SCP)** — new | **Pull** (`/config` endpoint) | Pull (`/config` endpoint) |
| inject_ssh_key | Push (SCP) | **Pull** (in `/config` response) | Pull (in `/config` response) |
| copy_private_key | Push (SCP, shared key) | **Pull** (in `/config` response) | **Posture-driven** (dev: pull, prod: self-generated) |
| pve-setup | Push (SSH) | Push (SSH) | Push (SSH) |
| configure_bridge | Push (SSH) | Push (SSH) | Push (SSH) |
| generate_node_config | Push (SSH) | Push (SSH) | Push (SSH) |
| create_api_token | Push (SSH) | Push (SSH) | Push (SSH) |
| inject_self_ssh_key | Push (SSH) | Push (SSH) | Push (SSH) |
| download_image | Pull (GitHub) | Pull (GitHub) | Pull (GitHub) |

## Tracking

| Issue | Scope | Status |
|-------|-------|--------|
| [iac-driver#245](https://github.com/homestak-iac/iac-driver/issues/245) | Short-term: push site config + scope secrets | Complete |
| [iac-driver#248](https://github.com/homestak-iac/iac-driver/issues/248) | Mid-term: `/config` endpoint for pull-mode distribution | Closed/Complete |

## Related Documents

- [config-phase.md](config-phase.md) — How a node applies its config (spec → ansible)
- [server-daemon.md](server-daemon.md) — Server architecture, repo serving, propagation chain
- [provisioning-token.md](provisioning-token.md) — Token minting, signing, verification
- [node-orchestration.md](node-orchestration.md) — Manifest-driven orchestration, delegation model
