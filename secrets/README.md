# Secrets

**This directory is deprecated.**

Secrets are now managed in the [config](https://github.com/homestak/config) repository.

## Migration

Host credentials have moved to:
```
config/hosts/{hostname}.tfvars
```

## Setup

```bash
# Clone config
cd ..
git clone https://github.com/homestak/config.git

# Setup and decrypt
cd config
make setup
make decrypt
```

The iac-driver will automatically discover config via `$HOMESTAK_ROOT/config`.
