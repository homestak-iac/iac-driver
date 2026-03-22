# Execution Models

iac-driver supports three execution models for configuring VMs after provisioning.
The model determines who runs ansible, where, and how credentials flow.

**Terminology:** A **spec** is a per-node desired state (packages, users, services)
defined in `config/specs/*.yaml`. **Config** is site-wide operational settings
(gateway, DNS, timezone, credentials) in `config/site.yaml` and `config/secrets.yaml`.
The execution model determines how specs and config reach the VM.

## Push Mode

The operator resolves the spec locally and runs ansible-playbook from the controller,
targeting the VM over SSH. No iac-driver or ansible installation is needed on the VM.

```
Controller (operator)                       Target VM

  SpecResolver.resolve(spec)
  spec_to_ansible_vars(resolved)
  ansible-playbook config-apply.yml   -->   apt install, systemd, users...
    -e ansible_host=<ip>
    -e ansible_user=homestak
    --become
  Write config-complete marker        -->   ~/.state/config/complete.json
```

In the executor (`_push_config()`), the sequence is:
1. Resolve the spec FK via `SpecResolver`
2. Map the resolved spec to ansible variables via `spec_to_ansible_vars()`
3. Write vars to a temp file and run `ansible-playbook` from the controller
4. Write a config-complete marker on the VM via SSH
5. Verify the marker exists

Push mode is selected when `execution.mode: push` is set in the manifest node or
at the manifest level, and the node type is not `pve`.

## Pull Mode

The VM self-configures autonomously. Cloud-init injects the server URL and a
provisioning token. After bootstrap, the VM fetches its spec from the server and
applies it locally.

```
Controller                    Server daemon              Target VM

  ConfigResolver              HTTPS :44443               cloud-init runcmd:
  mint_provisioning_token()                                homestak spec get
  write tfvars with:                                       homestak config apply
    server_url
    auth_token
                                                         GET /spec/<name>
                              Resolve spec,        <---  Authorization: Bearer <token>
                              return YAML          --->  Write ~/.state/config/spec.yaml

                                                         config_apply.py:
                                                           spec_to_ansible_vars()
                                                           ansible-playbook locally
                                                         Write complete.json

  NodeExecutor polls:
  WaitForFileAction(spec.yaml)
  WaitForFileAction(complete.json)
```

The executor (`_wait_for_config_complete()`) polls for two marker files via SSH:
1. `~/.state/config/spec.yaml` -- indicates the spec was fetched from the server
2. `~/.state/config/complete.json` -- indicates config apply completed

Pull mode is selected when `execution.mode: pull` is set in the manifest node or at
the manifest level.

## PVE Self-Configure (2-Phase Model)

PVE nodes use a specialized 2-phase model because PVE installation requires a
reboot, which breaks SSH-based push workflows.

### Phase 1: Cloud-init + systemd oneshot

After the VM boots, cloud-init runs the bootstrap installer and injects environment
variables (`HOMESTAK_SERVER`, `HOMESTAK_TOKEN`). A systemd oneshot service triggers
`./run.sh scenario run pve-config --local`, which runs Phase 2.

### Phase 2: pve-config scenario

The `pve-config` scenario runs locally on the PVE node and executes eight phases:

| Phase | Action | Purpose |
|-------|--------|---------|
| `fetch_config` | `ConfigFetchAction` | Pull site.yaml, secrets.yaml, SSH key from `/config` endpoint |
| `ensure_pve` | `_EnsurePVEPhase` | Install PVE kernel + packages (handles reboot re-entry) |
| `setup_pve` | `_PVESetupPhase` | Configure repos, remove nag, install packages |
| `configure_bridge` | `_ConfigureBridgePhase` | Create vmbr0 from primary interface |
| `generate_node_config` | `_GenerateNodeConfigInlinePhase` | Write nodes/{hostname}.yaml |
| `create_api_token` | `_CreateApiTokenPhase` | Create pveum API token, inject into secrets |
| `inject_self_ssh_key` | `_InjectSelfSSHKeyPhase` | Add own pubkey to secrets for child VMs |
| `write_marker` | `WriteMarkerAction` | Write success/failure marker, disable oneshot |

The parent executor polls for the completion marker via `WaitForFileAction`,
checking `~/.state/pve-config/success.json` (or `failure.json`). After success,
the executor downloads packer images needed by child nodes.

On failure, the scenario's `on_failure()` callback writes a failure marker so the
parent can detect the error and report it.

## Mode Selection Logic

The executor determines the mode in `_create_node()` based on node type and the
`execution_mode` field:

```python
# node.execution_mode: per-node override (from manifest YAML)
# self.manifest.execution_mode: manifest-wide default
exec_mode = node.execution_mode or self.manifest.execution_mode

if node.type == 'pve' and node.execution_mode != 'push':
    # PVE 2-phase self-configure: /config endpoint
    boot_scenario = 'pve-config'
elif exec_mode == 'pull':
    # Pull-mode VM: /spec endpoint + vm-config apply
    boot_scenario = 'vm-config'
else:
    # Push mode: no cloud-init config phase
    boot_scenario = None
```

The `boot_scenario` value is passed to `TofuApplyAction`, which sets it in the
tfvars so tofu's cloud-init template activates the appropriate runcmd script.

PVE nodes default to the 2-phase self-configure model. Setting
`execution.mode: push` on a PVE node falls back to the legacy 11-phase SSH
lifecycle (`_run_pve_lifecycle()`), which pushes all configuration over SSH.

## Token Flow

The provisioning token is the sole authentication artifact for the pull model.

1. `ConfigResolver.resolve_vm()` calls `_mint_provisioning_token(node_name, spec)`
2. The token is an HMAC-SHA256 signed JSON payload carrying the node identity and
   spec FK, signed with `secrets.signing_key`
3. The token is written into tfvars as `auth_token`
4. Tofu's cloud-init template injects it as `HOMESTAK_TOKEN` environment variable
5. The VM presents this token to the server's `/spec` or `/config` endpoint via
   `Authorization: Bearer <token>`
6. The server verifies the HMAC signature and serves the appropriate config

For PVE nodes, the spec claim is set to `'config'` (the `/config` endpoint serves
site.yaml, secrets, and SSH key instead of a spec).

## Server Daemon

The executor starts a spec/repo server daemon (`ServerManager`) before manifest
operations and stops it after completion. The server serves specs at `/spec/<name>`
and config bundles at `/config/<identity>` over HTTPS with self-signed TLS.
`ServerManager` uses reference counting so nested operations (test calls create
then destroy) only start/stop the server once.
