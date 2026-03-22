# Design Summary: Server Daemon Robustness

**Issue:** iac-driver#177
**Author:** Claude (with john-derose)
**Date:** 2026-02-08
**Release:** v0.50 (#217)

## Problem Statement

The server daemon is central to manifest orchestration — spec discovery, pull-mode config, and repo serving all depend on it. But the current implementation has no daemon mode, fragile process lifecycle management, and gaps in orchestrator integration.

**Three problems:**

1. **Zombie wrapper** — `StopSpecServerAction` kills the python3 server (found via `ss -tlnp` port lookup) but the bash wrapper (`nohup ./run.sh serve ... &`) survives. The PID file contains the wrapper PID, not python3. After stop: wrapper alive, server dead, port unbound, health check fails — but `kill -0 $(cat pidfile)` says "ALIVE."

2. **No startup gate** — Orchestrator can't reliably know when the server is ready to accept connections. Current approach: `Popen` fire-and-forget, 3s sleep, poll PID file, `kill -0`. No health-check verification. Race between "process exists" and "port is listening."

3. **Operator doesn't manage server lifecycle** — Verb commands (`./run.sh test -M n1-pull`) assume an externally-running server. If stale/dead, pull-mode tests fail silently. Sprint #219 validation: n1-pull failed because a zombie wrapper was masking a dead server.

**Additional scope:**

4. **Terminology correction** — The package `src/controller/` is misnamed. The component is a passive server (nodes pull from it); the actual controller is the operator (`manifest_opr/`). Rename `src/controller/` to `src/server/` throughout.

5. **CLI restructure (server commands)** — Replace `./run.sh serve --daemon/--stop/--status` flag-based design with noun-action subcommands: `./run.sh server start/stop/status`. Foreground mode (`./run.sh serve`) is retained for development.

**Success criteria:**
- `./run.sh server start` starts reliably, returns only after health check passes
- `./run.sh server stop` cleanly kills the server process (no zombies)
- Works over SSH without FD inheritance issues
- Verb commands for pull-mode manifests auto-manage server lifecycle
- PID file accurately tracks the killable process
- `src/controller/` renamed to `src/server/`

## Scope

**In scope (dev + stage):** Package rename, `server` CLI noun with start/stop/status actions, built-in daemon mode, PID management, health-check gate, operator lifecycle integration, logging improvements.

**Deferred (prod):** systemd unit/socket activation, watchdog, log rotation, persistent deployment.

**Separate issue:** Broader CLI restructure (`manifest apply/destroy/test`, `config apply`, `scenario run`) — see iac-driver#184.

## Operational Requirements by Maturity

| Requirement | Dev | Stage | Prod (deferred) |
|-------------|-----|-------|------------------|
| Start/stop from CLI | Yes | Yes | Yes |
| Start/stop via SSH (remote) | Nice-to-have | Yes | Yes |
| Health-check startup gate | Yes | Yes | Yes |
| Zombie-free stop | Yes | Yes | Yes |
| Operator auto-lifecycle | No | Yes | Yes |
| Survives SSH disconnect | Yes | Yes | Yes |
| Survives reboot | No | No | Yes (systemd) |
| Restart on crash | No | No | Yes (systemd) |
| Structured logging | `/var/log/` path | `/var/log/` path | journald |
| Log rotation | No | No | Yes (logrotate) |

## Proposed Solution

**Summary:** Rename `src/controller/` to `src/server/`, add `./run.sh server start/stop/status` noun-action CLI, fix the exec chain to eliminate the wrapper process, add a health-check startup gate, and integrate server lifecycle into the operator for all manifest verbs and pull-mode scenarios.

### 1. Package Rename: `src/controller/` → `src/server/`

**Rationale:** The component is a passive HTTPS server — nodes pull specs and repos from it. It doesn't control or orchestrate anything. The operator (`manifest_opr/`) is the actual controller. "Server" accurately describes the component.

**Files renamed:**

| Old Path | New Path |
|----------|----------|
| `src/controller/__init__.py` | `src/server/__init__.py` |
| `src/controller/cli.py` | `src/server/cli.py` |
| `src/controller/server.py` | `src/server/server.py` |
| `src/controller/tls.py` | `src/server/tls.py` |
| `src/controller/auth.py` | `src/server/auth.py` (auth model updated: posture-based → HMAC provisioning token per [provisioning-token.md](provisioning-token.md)) |
| `src/controller/specs.py` | `src/server/specs.py` |
| `src/controller/repos.py` | `src/server/repos.py` |

**Import updates:** All files that reference `src.controller.*` updated to `src.server.*`.

**Log/status output:** All user-facing strings changed from "controller" to "server" (e.g., "Server: running (PID 12345, port 44443)").

### 2. Fix the Exec Chain (Zombie Wrapper Problem)

**Root cause:** `run.sh` uses `nohup ./run.sh serve ... &` which creates a bash wrapper process. The PID file records the wrapper PID, but `ss -tlnp` shows the python3 PID. These are different processes with different lifetimes.

**Fix:** When `run.sh` receives the `serve` or `server` verb, use `exec` to replace the bash process with python3. No wrapper survives.

```bash
# run.sh — serve/server verb handling
if [ "$1" = "serve" ] || [ "$1" = "server" ]; then
    exec python3 -m src.cli "$@"
fi
```

This is already partially done for non-`--serve-repos` invocations (line 120: `exec python3 ...`). The fix ensures the serve path always execs, so PID file = python3 PID = the process that owns the port.

**Impact on remote start:** The SSH command becomes:
```bash
ssh $USER@srv1 "./run.sh server start --port 44443"
# Returns after health check passes — no nohup needed
```
The double-fork daemonization handles detachment from the SSH session.

### 3. Built-in Daemon Mode (`server start`)

`./run.sh server start` daemonizes the server:

1. **Double-forks** to detach from terminal/SSH session
2. **Writes PID file** after successful `server.start()` (not before)
3. **Blocks until health check passes**, then returns exit 0 to caller
4. **Redirects I/O** — stdout/stderr to log file, stdin from /dev/null

**Process model:**

```
Parent (CLI caller)
  │
  ├── fork() ──→ Child 1
  │                │
  │                ├── setsid()    # New session leader
  │                ├── fork() ──→ Child 2 (daemon)
  │                │                │
  │                │                ├── chdir("/")
  │                │                ├── Redirect I/O to log file
  │                │                ├── server.start()
  │                │                ├── Write PID file
  │                │                ├── Signal parent: "ready"
  │                │                └── server.serve_forever()
  │                │
  │                └── exit(0)
  │
  ├── Wait for "ready" signal (pipe or file)
  ├── Verify health check: curl /health
  └── exit(0)  # Success — daemon is running
```

**Parent-child coordination:** Use a pipe. Parent blocks on `read()`. Daemon writes "ready\n" after `server.start()` succeeds. Parent verifies with health check, then exits. If pipe closes without "ready" (daemon crashed), parent exits with error.

**CLI interface:**

```bash
# Start as daemon (blocks until ready, then returns)
./run.sh server start [--port 44443] [--log /var/log/homestak/server.log]

# Start in foreground (existing behavior, unchanged — for development)
./run.sh serve [--port 44443]

# Stop a running daemon
./run.sh server stop [--port 44443]

# Check if running
./run.sh server status [--port 44443] [--json]
```

### 4. PID File Management

**Location:** `$HOMESTAK_ROOT/.run/server-{port}.pid`. No fallback — if the directory doesn't exist, fail with "host not bootstrapped."

Port-qualified filename supports multiple servers on different ports (testing, delegated PVE nodes).

**Lifecycle:**
- Written by daemon **after** `server.start()` succeeds (not before)
- Contains the python3 daemon PID (no wrapper)
- Removed on clean shutdown (SIGTERM handler)
- Stale PID detection: if PID file exists but process is dead, remove and proceed

**Startup check:**
```python
def _check_existing(self, pid_file: str, port: int) -> str:
    """Check for existing server. Returns: 'none', 'healthy', 'stale'."""
    if not os.path.exists(pid_file):
        return 'none'
    pid = int(open(pid_file).read().strip())
    try:
        os.kill(pid, 0)  # Process exists?
    except ProcessLookupError:
        os.unlink(pid_file)  # Stale PID file
        return 'none'
    # Process exists — check health
    if self._health_check(port):
        return 'healthy'
    return 'stale'  # Process alive but not responding
```

**On startup:**
- `healthy` → Return success, print "already running (PID N)"
- `stale` → Kill stale process, remove PID file, start fresh
- `none` → Normal startup

### 5. `server stop`

```bash
./run.sh server stop [--port 44443]
```

**Behavior:**
1. Read PID from PID file
2. Send SIGTERM (triggers graceful shutdown in `handle_sigterm`)
3. Wait up to 5s for process to exit (poll `kill -0`)
4. If still alive after 5s, SIGKILL
5. Remove PID file
6. Verify port is unbound

This replaces `StopSpecServerAction`'s fragile `ss -tlnp` + `grep -oP` approach. The action calls `./run.sh server stop` instead of manual PID hunting.

### 6. `server status`

```bash
./run.sh server status [--port 44443] [--json]
```

**Output (human):**
```
Server: running (PID 12345, port 44443)
Uptime: 2h 15m
Specs: 3 available
```

**Output (--json):**
```json
{"running": true, "pid": 12345, "port": 44443, "healthy": true}
```

**Exit codes:** 0 = running and healthy, 1 = not running, 2 = running but unhealthy.

### 7. Logging

**Location:** `/var/log/homestak/server.log`. Override with `--log <path>`. No fallback — daemon mode requires a bootstrapped host.

**Format:** Same as current (`%(asctime)s [%(levelname)s] %(message)s`). No change needed for dev+stage.

**Daemon mode:** stdout/stderr redirected to log file. Foreground mode: unchanged (logs to stderr).

### 8. Operator Lifecycle Integration

The operator always ensures a server is running for manifest verbs (`create`, `destroy`, `test`) and `pull-vm-*` scenarios. No per-manifest detection logic — the overhead is negligible (~1.5s total) and this avoids edge cases with mixed push/pull manifests or future features that may need the server for push-mode too.

**Verb integration:**

```python
# In executor.py, before graph walk (always)
self._ensure_server(host_config)

# After graph walk (in finally block)
if self._started_server:
    self._stop_server(host_config)
```

**`_ensure_server`:**
1. SSH to host: `./run.sh server status --json`
2. If running and healthy: reuse (set `_started_server = False`)
3. If not running: `./run.sh server start --port 44443` (set `_started_server = True`)

**`_stop_server`:**
1. Only if we started it (don't kill user-managed servers)
2. SSH to host: `./run.sh server stop --port 44443`

This means `./run.sh test -M n1-pull -H srv1` auto-starts/stops the server, while a manually-started server for development remains untouched.

## Key Components Affected

| File | Change |
|------|--------|
| `src/controller/` → `src/server/` | Package rename (all modules) |
| `run.sh` | Exec python3 directly for serve/server verbs |
| `src/server/cli.py` | Add `server start/stop/status` subcommands |
| `src/server/server.py` | Add PID file management, startup coordination |
| `src/server/daemon.py` | **New** — double-fork, PID file, health-check gate |
| `src/cli.py` | Route `server` noun to `src/server/cli.py` |
| `src/scenarios/vm_roundtrip.py` | Simplify `StartSpecServerAction` / `StopSpecServerAction` to use `server start` / `server stop` |
| `src/manifest_opr/executor.py` | Add server lifecycle hooks |
| All imports of `src.controller.*` | Update to `src.server.*` |

## Interface Design

### CLI Changes

```
./run.sh server start  [--port PORT] [--log PATH]  # Daemonize
./run.sh server stop   [--port PORT]                # Stop daemon
./run.sh server status [--port PORT] [--json]       # Check health

./run.sh serve [existing flags]                     # Foreground (unchanged)
```

The `server` noun uses action subcommands. The `serve` verb is retained as-is for foreground development use.

### Daemon Module

```python
# src/server/daemon.py

def daemonize(server: ControllerServer, pid_file: str, log_file: str) -> None:
    """Double-fork daemonization with health-check gate."""

def stop_daemon(pid_file: str, port: int, timeout: int = 5) -> bool:
    """Stop daemon by PID file. Returns True if stopped."""

def check_status(pid_file: str, port: int) -> dict:
    """Check daemon status. Returns {running, pid, healthy}."""

def get_pid_file(port: int) -> str:
    """Return PID file path for given port."""
```

### Updated Action Interface

```python
# StartServerAction (renamed from StartSpecServerAction)
class StartServerAction:
    def run(self, config, context):
        # SSH: ./run.sh server status --json
        # If healthy: return success
        # If not running: SSH: ./run.sh server start --port {port}
        # Verify health check
        # Return success with PID in context

# StopServerAction (renamed from StopSpecServerAction)
class StopServerAction:
    def run(self, config, context):
        # SSH: ./run.sh server stop --port {port}
        # Return success
```

No more `Popen` fire-and-forget. No more `ss -tlnp | grep`. No more 3s sleep.

## Integration Points

### Existing Scenarios

`push-vm-roundtrip` and `pull-vm-roundtrip` scenarios use `StartServerAction` / `StopServerAction`. These actions become thin wrappers around SSH calls to `./run.sh server start` and `./run.sh server stop`. No scenario-level changes needed.

### Operator Engine

The operator gains server lifecycle awareness for all manifest verbs. This is additive — the overhead is negligible and ensures the server is always available.

### Tiered PVE and Server Propagation Chain

PVE hosts with children need servers for subtree delegation. The operator starts a server on each PVE level via `_ensure_server()`, creating a propagation chain:

```
srv1:44443 → root-pve:44443 → leaf-pve:44443
```

Each level serves repos and specs to its children, not to the root directly.

**Address resolution (iac-driver#200):** When `_ensure_server()` runs on a delegated PVE node, `self.config.ssh_host` may resolve to `localhost` (from the node's `api_endpoint: https://localhost:8006`). `_set_source_env()` detects loopback addresses and uses `_detect_external_ip()` (Python socket) to determine the host's network-facing IP. This ensures `HOMESTAK_SERVER` contains a routable address for child hosts.

**server_url override (iac-driver#200):** At depth 2+, `TofuApplyAction` overrides `server_url` in the resolved tfvars with `HOMESTAK_SERVER` when set. This ensures cloud-init runcmd on child VMs bootstraps from the immediate parent's server, not the hardcoded site.yaml value.

### Bootstrap / Cloud-Init

No changes. Cloud-init VMs call `./run.sh config --fetch` which talks to the server over HTTPS. Authentication uses the provisioning token (`HOMESTAK_TOKEN`) injected via cloud-init — see [provisioning-token.md](provisioning-token.md). The server's daemonization is transparent to clients.

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Double-fork breaks on some Python builds | Low | High | Test on Debian 12 + 13; fall back to single-fork if needed |
| Pipe-based coordination has edge cases | Medium | Medium | Timeout on parent read; fall back to poll-based health check |
| Operator auto-start conflicts with user-started server | Low | Low | Only stop if we started it; `server status` detects existing |
| PID file directory not writable | Low | Low | Fail with clear error — host not bootstrapped |

## Alternatives Considered

| Alternative | Pros | Cons | Why Not |
|-------------|------|------|---------|
| systemd-only | Clean lifecycle, restart on crash, journald | Not available on delegated PVE nodes, heavyweight for dev | Deferred to prod maturity |
| `start-stop-daemon` | Debian standard, handles daemonization | External tool dependency, less control | Double-fork gives us more control over health-check gate |
| Keep Popen workaround, fix zombie only | Minimal change | Doesn't solve startup gate or operator gap | Insufficient — addresses symptom not root cause |
| `screen`/`tmux` session | Easy to attach for debugging | Extra dependency, not a real daemon | Not appropriate for automated lifecycle |
| Flag-based CLI (`--daemon`, `--stop`, `--status`) | Minimal CLI change | Mutually exclusive flags = code smell | Noun-action subcommands are more idiomatic |

## Test Plan

**Scenario 1: Daemon lifecycle (local)**
```bash
./run.sh server start --port 44443
./run.sh server status --port 44443    # Should show running
curl -sk https://localhost:44443/health  # Should return {"status":"ok"}
./run.sh server stop --port 44443
./run.sh server status --port 44443    # Should show not running
```

**Scenario 2: Remote start/stop via SSH**
```bash
ssh $USER@srv1 "./run.sh server start --port 44443"
# Should return quickly after health check passes
ssh $USER@srv1 "./run.sh server status --json --port 44443"
# {"running": true, "pid": ..., "healthy": true}
ssh $USER@srv1 "./run.sh server stop --port 44443"
```

**Scenario 3: Integration validation**
```bash
./run.sh test -M n1-pull -H srv1
# Should auto-start server, run test, auto-stop server
./run.sh scenario push-vm-roundtrip -H srv1
# Should use server start/stop instead of Popen workaround
```

**Scenario 4: Idempotency**
```bash
./run.sh server start --port 44443
./run.sh server start --port 44443   # Should detect existing, return success
./run.sh server stop --port 44443
./run.sh server stop --port 44443     # Should detect not running, return success
```

**Scenario 5: Stale process recovery**
```bash
./run.sh server start --port 44443
kill -9 $(cat $HOMESTAK_ROOT/.run/server-44443.pid)  # Simulate crash
./run.sh server start --port 44443   # Should detect stale, clean up, start fresh
```

## Implementation Order

1. **Rename `src/controller/` → `src/server/`** — package rename, import updates, user-facing strings
2. **Fix exec chain** in `run.sh` — eliminate wrapper process
3. **Add `daemon.py`** — double-fork, PID file, health-check gate
4. **Add `server start/stop/status`** to `src/server/cli.py` and `src/cli.py` routing
5. **Update actions** — rename and simplify `StartSpecServerAction` / `StopSpecServerAction`
6. **Operator integration** — auto-lifecycle for manifest verbs and pull-mode scenarios
7. **Logging** — `/var/log/` path, no fallback
8. **Validation** — n1-pull and pull-vm-roundtrip must pass reliably

## Deferred to Prod Maturity

- systemd unit file (`/etc/systemd/system/homestak-server.service`)
- Socket activation (start on first connection)
- Watchdog (auto-restart on crash)
- Log rotation (`/etc/logrotate.d/homestak-server`)
- Multi-server coordination (cluster mode)

## Requirements Traceability

| Design Section | Requirement IDs | Notes |
|---|---|---|
| 1. Package rename | REQ-NFR-004 | Names match purpose |
| 2. Exec chain fix | REQ-CTL-015 | New: no bash wrapper in PID chain |
| 3. Daemon mode | REQ-CTL-005, REQ-CTL-016, REQ-CTL-017 | CTL-005 enhanced; new: double-fork, health-check gate |
| 4. PID management | REQ-CTL-005, REQ-CTL-018, REQ-CTL-019 | CTL-005 enhanced; new: port-qualified PID file, stale detection |
| 5. server stop | REQ-CTL-005, REQ-CTL-020 | CTL-005 enhanced; new: SIGTERM→SIGKILL escalation |
| 6. server status | REQ-CTL-021 | New: JSON output, structured exit codes |
| 7. Logging | REQ-CTL-022 | New: `/var/log/` path, no fallback |
| 8. Operator lifecycle | REQ-CTL-023 | New: auto-start/stop for manifest verbs |
| Test plan (idempotency) | REQ-CTL-024 | New: idempotent start/stop |

## Related Documents

- [node-lifecycle.md](node-lifecycle.md) — Single-node lifecycle phases, execution models
- [node-orchestration.md](node-orchestration.md) — Multi-node patterns, operator engine
- [spec-client.md](spec-client.md) — `homestak spec get` client (talks to this server)
- [config-phase.md](config-phase.md) — Config phase (`./run.sh config --fetch` uses server)
- [requirements-catalog.md](requirements-catalog.md) — REQ-CTL-* requirements
- [provisioning-token.md](provisioning-token.md) — Provisioning token design (HMAC auth for spec endpoint)
- [test-strategy.md](test-strategy.md) — Test coverage matrix
- [iac-driver#177](https://github.com/homestak-iac/iac-driver/issues/177) — Implementation issue
- [iac-driver#184](https://github.com/homestak-iac/iac-driver/issues/184) — CLI restructure (depends on #177)

## Changelog

| Date | Change |
|------|--------|
| 2026-02-13 | Sprint #243 (Branch Propagation): Add server propagation chain and address resolution (iac-driver#200); document `_detect_external_ip` and server_url override for depth 2+ |
| 2026-02-11 | Sprint #231 (Provisioning Token): Note auth model shift in auth.py (posture-based → HMAC); add provisioning token reference to Bootstrap/Cloud-Init section; add provisioning-token.md to related docs |
| 2026-02-08 | Initial document |

## Open Questions

1. **Port collision:** If two users on the same host run `./run.sh server start`, port-qualified PID files handle this. But should we warn about port conflicts at startup?
2. **Signal propagation in tiered PVE:** When the parent operator stops a child PVE node's server via SSH, does SIGTERM propagate correctly through the subtree delegation chain?
3. **Test coverage:** Should `daemon.py` have unit tests (mocking fork/exec), or is integration testing sufficient?
