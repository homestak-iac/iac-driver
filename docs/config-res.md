# ConfigResolver


The `ConfigResolver` class resolves config YAML files into flat configurations for tofu and ansible. All template, preset, and posture inheritance is resolved in Python, so consumers receive fully-computed values.

## Usage

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

## Resolution Order (Tofu)

1. `presets/{vm_preset}.yaml` - VM size presets (cores, memory, disk)
2. Inline VM overrides (name, vmid, image) from manifest nodes or CLI
3. `postures/{posture}.yaml` - Auth method for spec discovery

## Resolution Order (Ansible)

1. `site.yaml` defaults - timezone, packages, pve settings
2. `postures/{posture}.yaml` - Security settings from env's posture FK
3. Packages merged: site packages + posture packages (deduplicated)

## Output Structure (Tofu)

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

## SSH Key Default Behavior

When a spec's user entry omits the `ssh_keys` field, ALL keys from `secrets.ssh_keys` are injected automatically. This makes specs portable across deployments without listing specific key identifiers. If `ssh_keys` is explicitly listed, only those named keys are resolved via FK.

## Output Structure (Ansible)

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

## vmid Allocation

- If `vmid_base` is defined in env: `vmid = vmid_base + index`
- If `vmid_base` is not defined: `vmid = null` (PVE auto-assigns)
- Per-VM `vmid` override always takes precedence

## Tofu Actions

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
