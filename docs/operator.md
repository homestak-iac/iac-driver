# Operator Engine


The operator engine (`manifest_opr/`) walks a manifest graph to execute create/destroy/test lifecycle operations. See `$HOMESTAK_ROOT/dev/meta/docs/arch/node-orchestration.md` for topology patterns and execution model comparison.

## Manifest Schema

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

## Noun-Action Commands

```bash
./run.sh manifest apply -M n2-push -H srv1 [--dry-run] [--json-output] [--verbose]
./run.sh manifest destroy -M n2-push -H srv1 [--dry-run] [--yes]
./run.sh manifest test -M n2-push -H srv1 [--dry-run] [--json-output]
./run.sh manifest validate -M n2-push -H srv1 [--json-output]
./run.sh config fetch [--insecure]
./run.sh config apply [--spec /path.yaml] [--dry-run]
```

## Error Handling

Set via manifest `settings.on_error`:

| Mode | Behavior |
|------|----------|
| `stop` | Halt immediately (default) |
| `rollback` | Destroy already-created nodes, then halt |
| `continue` | Skip failed node, continue with independent nodes |

## Delegation Model

Root nodes (depth 0) are handled locally. PVE nodes with children trigger:
1. PVE lifecycle setup (bootstrap, secrets, site config, bridge + DNS, API token, image download)
2. Subtree delegation via SSH — `./run.sh manifest apply --manifest-json` on the PVE node

This recursion handles arbitrary depth without limits.

**Config distribution phases:** The PVE lifecycle includes `copy_secrets` (scoped secrets excluding `api_tokens`) and `copy_site_config` (site.yaml with DNS, gateway, timezone). These push configuration to delegated PVE nodes so they can resolve config for their own children.

**Delegate logging:** Delegated action logs use the node name as prefix (e.g., `[delegate-root-pve]`) instead of `[inner]`. JSON output from delegated commands is suppressed from INFO logs to reduce noise.

## Execution Modes

Nodes use **push** (default) or **pull** for config phase. See [config-phase.md](config-phase.md) for spec-to-ansible mapping and implementation details.

| Mode | How Config Runs | Operator Behavior |
|------|----------------|-------------------|
| `push` | Operator runs ansible from controller over SSH | Default; no spec injection in cloud-init |
| `pull` | VM self-configures via cloud-init | Operator polls for complete.json |

PVE nodes default to **2-phase self-configure**: cloud-init bootstraps and starts a systemd oneshot service (`pve-config.service`) that runs `./run.sh scenario run pve-config --local`. The operator polls for success/failure markers via `WaitForFileAction`. Set `execution.mode: push` on a PVE node to use the legacy 11-phase SSH push lifecycle. See [pve-self-configure.md](pve-self-configure.md) for design rationale. Push-mode VM nodes skip spec injection in cloud-init to avoid bootstrap race conditions.

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
