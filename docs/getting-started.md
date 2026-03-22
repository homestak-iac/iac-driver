# Developer Setup

This guide covers setting up a development environment for iac-driver.

## Prerequisites

- Python 3.11+ (Debian 12 ships 3.11)
- git
- For integration testing: Ansible 2.15+ (via pipx), OpenTofu, SSH access to a PVE host

## Clone Repositories

iac-driver depends on sibling repos under `$HOMESTAK_ROOT/iac/`. See
`$HOMESTAK_ROOT/dev/meta/docs/getting-started.md` for full workspace clone
instructions, or clone manually:

```bash
export HOMESTAK_ROOT=~/homestak   # dev workstation convention
mkdir -p $HOMESTAK_ROOT/iac && cd $HOMESTAK_ROOT/iac
git clone https://github.com/homestak-iac/iac-driver.git
git clone https://github.com/homestak-iac/ansible.git
git clone https://github.com/homestak-iac/tofu.git
git clone https://github.com/homestak-iac/packer.git
cd $HOMESTAK_ROOT && git clone https://github.com/homestak/config.git
```

iac-driver discovers siblings via `get_sibling_dir()` in `config.py`.

## Install Development Dependencies

```bash
cd $HOMESTAK_ROOT/iac/iac-driver
make install-dev
```

This creates a `.venv/` virtual environment, installs runtime and development
packages (PyYAML, requests, pytest, pylint, mypy, pre-commit), and sets up
pre-commit hooks that run pylint and mypy on staged files at commit time.

No virtual environment activation is needed. `make test` and `make lint` invoke
tools through `.venv/bin/` directly.

## Source Layout

The `src/` directory is flat -- no package install is needed. `run.sh` executes
`python3 src/cli.py` directly, and `PYTHONPATH` is not explicitly set because
`src/` is the working directory for imports.

```
src/
  cli.py              # CLI entry point (noun-action dispatch)
  common.py           # ActionResult, run_command(), run_ssh()
  config.py           # HostConfig, config loading and discovery
  config_resolver.py  # ConfigResolver (resolves YAML for tofu)
  config_apply.py     # Config phase (spec-to-ansible mapping)
  manifest.py         # Manifest schema v2 (ManifestNode)
  validation.py       # Preflight checks
  manifest_opr/       # Manifest operator engine
  actions/            # Reusable action primitives
  scenarios/          # Workflow definitions
  server/             # Spec/repo server daemon
  resolver/           # Spec resolution and HTTP client
  reporting/          # Test report generation
```

## Running Tests

```bash
make test
```

Runs the pytest suite in `tests/`. Tests are pure unit tests with mocked
infrastructure -- no PVE host, network access, or secrets required.

To run a specific test file or test:

```bash
.venv/bin/python -m pytest tests/test_executor.py -v
.venv/bin/python -m pytest tests/test_cli.py::test_manifest_dispatch -v
```

## Running Linters

```bash
make lint
```

Runs pre-commit hooks across all files: pylint for code quality and mypy for
type checking. Type stubs for PyYAML and requests are included in the dev
dependencies.

## Running Scenarios

Scenarios configure hosts or test VM lifecycles. The `--local` flag auto-detects
the hostname and uses local execution.

```bash
./run.sh scenario run pve-setup --local          # Local (on the PVE host)
./run.sh scenario run pve-setup -H srv1          # Remote (via SSH)
./run.sh scenario --help                         # List available scenarios
```

## Running Manifest Operations

Manifests define multi-node deployment topologies. `-M` references a manifest in
`config/manifests/`; `-H` specifies the target PVE host from `config/nodes/`.

```bash
./run.sh manifest apply -M n1-push -H srv1               # Create infrastructure
./run.sh manifest test -M n1-push -H srv1                 # Create, verify, destroy
./run.sh manifest destroy -M n1-push -H srv1 --yes        # Tear down
./run.sh manifest validate -M n1-push                     # Validate structure only
./run.sh manifest apply -M n2-tiered -H srv1 --dry-run    # Preview
```

## Config Setup

For operations that touch real infrastructure, decrypt secrets first:

```bash
cd $HOMESTAK_ROOT/config
make setup     # Configure git hooks, check dependencies
make decrypt   # Decrypt secrets.yaml (requires age key)
```

## Useful Flags

| Flag | Purpose |
|------|---------|
| `--dry-run` | Preview operations without executing |
| `--verbose` | Enable debug-level logging |
| `--json-output` | Structured JSON to stdout (logs to stderr) |
| `--skip-preflight` | Bypass pre-flight validation checks |
| `--depth N` | Limit manifest to first N levels |
| `--timeout N` | Overall timeout in seconds (scenarios only) |
