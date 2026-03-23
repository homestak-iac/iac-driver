# Testing

## Unit Tests

**Location:** `tests/`

**Execution:** `make test` (pytest, no infrastructure required)

**When to run:** Every commit. CI enforces via GitHub Actions.

**Characteristics:**
- Fast (seconds)
- No PVE host, network, or secrets required
- Mocked SSH, HTTP, ansible, tofu
- Must pass before PR merge

```bash
make test                                    # Run all
make lint                                    # pylint + mypy
pytest tests/test_config_resolver.py -v      # Specific file
pytest tests/ -k "test_resolve_inline_vm"    # Specific test
```

## Integration Tests (Manifest Scenarios)

**Execution:** `./run.sh manifest test -M <name> -H <host>`

**When to run:** Before merge for IaC changes. Before release for evidence.

| Manifest | Pattern | Execution | Duration | System Test |
|----------|---------|-----------|----------|-------------|
| n1-push | flat | push | ~1 min | ST-2 |
| n1-pull | flat | pull | ~2.5 min | ST-1 |
| n2-push | tiered | push + PVE self-configure | ~6 min | ST-3 |
| n2-pull | tiered | push PVE + pull leaf | ~8 min | ST-5 |
| n3-deep | tiered | 3-level delegation | ~16 min | ST-4 |

**Standalone scenarios:**

| Scenario | Duration | What it tests |
|----------|----------|---------------|
| pve-setup | ~3 min | Ansible PVE host configuration |
| pve-config | ~10 min | 2-phase PVE self-configure |
| user-setup | ~30s | Ansible user creation |
| push-vm-roundtrip | ~3 min | Server + spec discovery + push verification |
| pull-vm-roundtrip | ~5 min | Server + spec fetch + pull verification |

## Validation Sizing

Match validation effort to change risk:

| Change Type | Minimum Validation |
|-------------|-------------------|
| Documentation only | None (review) |
| CLI argument/help text | Unit tests only |
| Config schema changes | n1-push |
| Tofu/ansible changes | n1-push |
| Manifest/operator code | n2-push |
| PVE lifecycle/delegation | n2-push + n2-pull |
| Cloud-init/bootstrap changes | n1-pull |
| Full release | All 5 manifests |

## Execution Modes

| Mode | Verb | On Failure | Use Case |
|------|------|-----------|----------|
| **UAT** | `manifest test` | Destroy and continue | Release validation, virgin provisioning |
| **Sprint** | `manifest apply` | Stop, leave running | Iterative development, debugging |

UAT mode runs create → verify → destroy for each manifest. Sprint mode runs
create → verify and stops, leaving infrastructure running for inspection.

```bash
# UAT mode (full lifecycle)
./run.sh manifest test -M n2-push -H srv1

# Sprint mode (leave running for debugging)
./run.sh manifest apply -M n2-push -H srv1
# ... inspect ...
./run.sh manifest destroy -M n2-push -H srv1 --yes
```

## System Test Catalog

### ST-1: Single-node Pull Lifecycle

**Manifest:** n1-pull | **Status:** Validated

Validates config phase, spec fetch, pull execution, provisioning token flow.

**Assertions:**
- Token `s` claim resolves to correct spec
- Spec fetched from server (`$HOMESTAK_ROOT/.state/config/spec.yaml`)
- SSH access works with keys from spec
- Config-complete marker written

### ST-2: Single-node Push Lifecycle

**Manifest:** n1-push | **Status:** Validated

Validates push execution — operator runs ansible from controller over SSH.

**Assertions:**
- No spec server required for push path
- Configuration applied via SSH
- Same end state as ST-1

### ST-3: Tiered Topology (2-level)

**Manifest:** n2-push | **Status:** Validated

Validates parent-child ordering, PVE self-configure, subtree delegation.

**Assertions:**
- Parent created before children
- Children destroyed before parent
- PVE 2-phase self-configure completes (8 phases in pve-config)
- Subtree delegation via SSH + `--manifest-json`

### ST-4: Tiered Topology (3-level)

**Manifest:** n3-deep | **Status:** Validated

Validates arbitrary depth, `--self-addr` propagation.

**Assertions:**
- Creation order: level-1 → level-2 → level-3
- Destruction order: level-3 → level-2 → level-1
- `HOMESTAK_SERVER` propagates routable address at each depth

### ST-5: Mixed Execution Modes

**Manifest:** n2-pull | **Status:** Validated

Validates push/pull coexistence — PVE node self-configures, leaf VM pulls
spec autonomously.

**Assertions:**
- PVE node configured via 2-phase self-configure
- Leaf VM fetched spec from server (pull)
- Provisioning token authenticated correctly

### ST-6: Flat Topology (Multiple Peers)

**Status:** Not implemented (future)

Validates parallel creation of peer nodes.

### ST-7: Manifest Validation

**Status:** Validated

`./run.sh manifest validate -M <name> -H <host>` validates manifest FKs
against config entities. No infrastructure required.

### ST-8: Action Idempotency

**Status:** Partial (not formally tested)

Scenarios are mostly idempotent but no formal re-run validation exists.

## Reporting

**Current location:** `$HOMESTAK_ROOT/iac/iac-driver/reports/` (planned move to `$HOMESTAK_ROOT/logs/reports/`)

**Format:** `YYYYMMDD-HHMMSS.{manifest}.{passed|failed}.{md|json}`

Reports are generated by `manifest test` and scenario roundtrip tests.
Use `scripts/parallel-test.sh` to run multiple manifests concurrently
with a shared server.

For the cross-repo test automation vision (UAT pipeline, multi-host testing,
aggregated reporting), see `$HOMESTAK_ROOT/dev/meta/docs/arch/test-strategy.md`.
