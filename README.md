# iac-driver

Infrastructure orchestration engine for Proxmox VE.

## Overview

Manifest-driven orchestration that coordinates the homestak tool repositories:

| Repo | Purpose |
|------|---------|
| [bootstrap](https://github.com/homestak/bootstrap) | Entry point - curl\|bash setup |
| [config](https://github.com/homestak/config) | Site-specific secrets and configuration |
| [ansible](https://github.com/homestak-iac/ansible) | Proxmox host configuration, PVE installation |
| [tofu](https://github.com/homestak-iac/tofu) | VM provisioning with OpenTofu |
| [packer](https://github.com/homestak-iac/packer) | Custom Debian cloud images |

## Quick Start

```bash
# Clone iac-driver and config
git clone https://github.com/homestak-iac/iac-driver.git
git clone https://github.com/homestak/config.git

# Setup secrets
cd config
make setup && make decrypt

# Clone sibling tool repos
cd ../iac-driver
./scripts/setup-tools.sh

# Deploy a VM and verify SSH
./run.sh manifest test -M n1-push -H srv1
```

## CLI Usage

```bash
# Manifest commands (infrastructure lifecycle)
./run.sh manifest apply -M <manifest> -H <host>          # Deploy
./run.sh manifest destroy -M <manifest> -H <host> --yes  # Tear down
./run.sh manifest test -M <manifest> -H <host>           # Roundtrip test
./run.sh manifest validate -M <manifest> -H <host>       # Dry validate

# Config commands (node self-configuration)
./run.sh config fetch [--insecure]     # Fetch spec from server
./run.sh config apply                  # Apply spec via ansible

# Scenario commands (standalone workflows)
./run.sh scenario run pve-setup --local     # Configure PVE host
./run.sh scenario run user-setup --local    # Create homestak user

# Preflight checks
./run.sh --preflight --host srv1

# Options
  --dry-run        Preview without executing
  --json-output    Structured JSON to stdout (logs to stderr)
  --verbose        Enable debug logging
```

**Available scenarios:**

| Scenario | Runtime | Description |
|----------|---------|-------------|
| `pve-setup` | ~3m | Install PVE (if needed), configure host, create API token |
| `user-setup` | ~30s | Create homestak user with sudo |
| `push-vm-roundtrip` | ~3m | Push-mode integration test |
| `pull-vm-roundtrip` | ~5m | Pull-mode integration test |

## Secrets Management

Credentials are managed in the [config](https://github.com/homestak/config) repository using SOPS + age.

```bash
cd ../config
make setup    # Configure git hooks, check dependencies
make decrypt  # Decrypt secrets (requires age key)
```

See [config README](https://github.com/homestak/config#readme) for setup instructions.

## Prerequisites

- [config](https://github.com/homestak/config) set up and decrypted
- Ansible 2.15+ (via pipx), OpenTofu
- SSH key at `~/.ssh/id_rsa`
- Proxmox VE host with API access

## Documentation

See [CLAUDE.md](CLAUDE.md) for detailed architecture, manifest schema, operator engine, server daemon, and config phase documentation.

## License

Apache 2.0 - see [LICENSE](LICENSE)
