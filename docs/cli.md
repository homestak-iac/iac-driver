# CLI Reference


## Architecture

```
PVE Host (srv1)
├── IP: 198.51.100.x
└── VM 99011 (root-pve) - PVE node
    ├── Debian 13 + Proxmox VE
    ├── 4 cores, 8GB RAM, 64GB disk
    └── VM 99021 (edge) - Leaf VM
        └── Debian 12, 2 cores, 2GB RAM
```

## Commands

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

## Available Scenarios

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

## Test Reports

Both `manifest test` and scenario roundtrip tests generate reports in `reports/` with format: `YYYYMMDD-HHMMSS.{scenario}.{passed|failed}.{md|json}`. Manifest test reports track create/verify/destroy phases individually. Use `scripts/parallel-test.sh` to run multiple manifest tests concurrently with a shared server.

## Preflight Validation

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

## Timeouts

Operations use tiered timeouts (Quick: 5-30s through Extended: 1200s). Defaults are defined in `src/actions/*.py` and `src/common.py`. Override per-action in scenario definitions when needed.

## Claude Code Autonomy

For fully autonomous integration test runs, add these to Claude Code allowed tools:
```
Bash(ansible-playbook:*), Bash(ansible:*), Bash(rsync:*)
```
