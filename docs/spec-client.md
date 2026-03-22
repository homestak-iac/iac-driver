# Design Summary: `homestak spec get`

**Sprint:** #162 (v0.44 Config Client + Integration)
**Release:** #153
**Epic:** iac-driver#125 (Architecture evolution)
**Author:** Claude
**Date:** 2026-02-01

## Problem Statement

Nodes need to fetch their specifications from the server (iac-driver) and persist them locally. The `homestak spec get` command provides the client-side of the config phase.

**Success criteria:**
- Client fetches spec from server via HTTP
- CLI flags work for manual testing: `--server`, `--identity`, `--token`
- Env vars work for automated path: `HOMESTAK_SERVER`, `HOMESTAK_TOKEN`
- Fetched spec persisted to `~/etc/state/`
- Error responses handled with defined codes

> **Note (v0.50):** The automated path now uses provisioning tokens (`HOMESTAK_TOKEN`) which carry the spec FK and identity in HMAC-signed claims. The `--identity` flag and `HOMESTAK_IDENTITY` env var remain for manual testing only. See [provisioning-token.md](provisioning-token.md) for the token design.

## Proposed Solution

**Summary:** Python HTTP client in bootstrap that fetches specs from the server, validates them, and persists to local state.

**High-level approach:**
- HTTP client using Python's `urllib.request` (stdlib, no deps)
- Configuration via CLI flags (manual) or env vars (automated)
- Persist fetched spec to state directory
- Use same error codes as server (E100-E501)

**Key components affected:**
- `bootstrap/lib/spec_client.py` - New Python module for HTTP client
- `bootstrap/homestak.sh` - Add `spec get` subcommand routing

**New components introduced:**
- `bootstrap/lib/spec_client.py` - HTTP client implementation

**Reused from Sprint #161:**
- Error code structure from `spec_resolver.py` (originally in bootstrap, removed in Sprint #199; resolver now in iac-driver)
- Path discovery pattern (`discover_etc_path()`)

## Interface Design

### CLI

```bash
# Manual invocation (for testing/debugging)
homestak spec get --server https://srv1:44443 --identity dev1

# Automated invocation (via env vars, for cloud-init path)
HOMESTAK_SERVER=https://srv1:44443 \
HOMESTAK_TOKEN=<provisioning-token> \
homestak spec get

# Additional flags
--output <path>    # Override output path (default: state dir)
--validate         # Validate against schema before saving (default: true)
--verbose          # Enable verbose output
```

**Flag precedence:** CLI flags override env vars.

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `HOMESTAK_SERVER` | Server URL (e.g., `https://srv1:44443`) | Yes (if no --server) |
| `HOMESTAK_TOKEN` | Provisioning token (HMAC-signed, carries spec FK + identity) | Yes (automated path) |
| `HOMESTAK_IDENTITY` | Node identity for manual testing (e.g., `dev1`) | Manual only (if no --identity) |

### State Directory Structure

```
~/etc/state/
├── spec.yaml           # Current spec (most recent fetch)
├── spec.yaml.prev      # Previous spec (for rollback/diff)
└── fetch.log           # Fetch history (timestamp, server, result)
```

### Output Format (Success)

```
Fetching spec for 'dev1' from https://srv1:44443...
Spec fetched successfully
  Schema version: 1
  Posture: dev
  Packages: 5
Saved to: ~/etc/state/spec.yaml
```

### Output Format (Error)

```
Error fetching spec: E200 - Spec not found: dev1
```

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | Client error (missing args, invalid config) |
| 2 | Server error (network, HTTP error) |
| 3 | Validation error (schema invalid) |

### Error Code Mapping

Map server error codes to client behavior:

| Server Code | HTTP Status | Client Behavior |
|-------------|-------------|-----------------|
| E100 | 400 | Exit 1, show message |
| E101 | 400 | Exit 1, show message |
| E200 | 404 | Exit 2, "Spec not found" |
| E201 | 404 | Exit 2, "Posture not found" |
| E300 | 401 | Exit 2, "Auth required" |
| E301 | 403 | Exit 2, "Invalid token" |
| E400 | 422 | Exit 3, "Schema validation failed" |
| E500 | 500 | Exit 2, "Server error" |

## Integration Points

1. **Server (iac-driver)** - HTTP API, see [server-daemon.md](server-daemon.md)
2. **Path discovery** - Reuse `discover_etc_path()` pattern
3. **State directory** - `~/etc/state/`
4. **Config completion (v0.48)** - `./run.sh config` reads `state/spec.yaml` and applies via ansible (iac-driver#147)

## Data Flow

**Note:** The automated path (cloud-init) uses provisioning tokens — see [provisioning-token.md](provisioning-token.md). The `homestak spec get` CLI retains `--identity` for manual testing/debugging.

```
homestak spec get
       │
       ▼
Parse CLI flags / env vars
       │
       ├── --server / HOMESTAK_SERVER
       ├── --token / HOMESTAK_TOKEN (provisioning token, automated path)
       └── --identity / HOMESTAK_IDENTITY (manual testing only)
       │
       ▼
Build HTTP request
       │
       ├── URL: {server}/spec/{hostname}
       └── Header: Authorization: Bearer {token}
       │
       ▼
Send request to server
       │
       ├── Success (200) → Parse JSON
       │                         │
       │                         ▼
       │                   Validate schema (optional)
       │                         │
       │                         ▼
       │                   Persist to state/spec.yaml
       │                         │
       │                         ▼
       │                   Exit 0
       │
       └── Error (4xx/5xx) → Log error
                                   │
                                   ▼
                             Write fail marker (if permanent)
                             or retry with backoff (if transient)
                                   │
                                   ▼
                             Exit 1/2/3
```


## Related Documents

- [server-daemon.md](server-daemon.md) - Server daemon design (iac-driver#177)
- [iac-driver#125](https://github.com/homestak-iac/iac-driver/issues/125) - Architecture evolution epic
- [homestak-dev#153](https://github.com/homestak-dev/meta/issues/153) - v0.44 Release Planning
