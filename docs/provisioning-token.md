# Design Summary: Provisioning Token

**Issue:** iac-driver#185
**Epic:** iac-driver#125 (Node Lifecycle Architecture)
**Date:** 2026-02-11
**Status:** Complete

## Problem Statement

Pull-mode VMs cannot fetch their spec because the server resolves identity directly to a spec filename (`specs/{identity}.yaml`), but the manifest decouples node name from spec name (`name: edge`, `spec: base`). When a VM named "edge" requests `GET /spec/edge`, the server looks for `specs/edge.yaml` — which doesn't exist.

**Root cause:** Five design documents and their implementations bake in the assumption that identity = spec name:

| Document | Broken Assumption |
|----------|-------------------|
| `node-lifecycle.md` (L138-144) | Pull model: `GET /spec/{identity}` |
| `phase-interfaces.md` (L33) | Create→Config: "VM identity" is "Spec lookup key" |
| `spec-client.md` (L142-143) | Data flow: `{server}/spec/{identity}` |
| `config-phase.md` (L291-296) | Cloud-init: only `HOMESTAK_IDENTITY`, no spec reference |
| `server/specs.py` | `resolver.resolve(identity)` loads `specs/{identity}.yaml` |

The operator knows the full context (manifest, node, spec FK, posture) at create time, but this context is lost by the time the VM boots and attempts to fetch its spec.

## Concept: Provisioning Token

Instead of threading individual fields through cloud-init, the operator mints a **single cryptographic token** at create time that encodes all context needed for the server to serve the correct spec. The token is the authoritative artifact for "what this VM should become."

```
Operator (create time)              VM (config time)              Server
─────────────────────              ──────────────────             ──────
knows: manifest, node,             knows: hostname               knows: specs, postures,
  spec FK, posture, ...            and the token                   signing key
        │                                                              │
        ├── mint token ─── cloud-init ──► HOMESTAK_TOKEN               │
        │                                     │                        │
        │                                     ├── GET /spec/{hostname} │
        │                                     │   Authorization: Bearer <token>
        │                                     │                        │
        │                                     │         decode ◄───────┤
        │                                     │         verify HMAC    │
        │                                     │         extract spec FK│
        │                                     │         serve spec     │
        │                                     ◄────────────────────────┤
```

### What the Token Replaces

| Current | Token approach |
|---------|---------------|
| `HOMESTAK_IDENTITY` | Embedded as `n` claim |
| `HOMESTAK_AUTH_TOKEN` | Removed — the token itself IS the auth |
| Identity→spec mapping (broken) | Spec FK embedded as `s` claim |
| Posture-based auth dispatch | Unified: valid HMAC = authorized |

Three env vars (`HOMESTAK_SPEC_SERVER`, `HOMESTAK_IDENTITY`, `HOMESTAK_AUTH_TOKEN`) collapse to two: `HOMESTAK_SERVER` + `HOMESTAK_TOKEN`. `HOMESTAK_AUTH_TOKEN` is eliminated entirely — the provisioning token subsumes both identity and authorization.

### Architectural Properties

| Property | Description |
|----------|-------------|
| **Self-describing** | Token carries all context — no server-side mapping tables |
| **Stateless verification** | HMAC check requires only the signing key, no database |
| **Operator-issued** | Only the operator (with signing key) can mint valid tokens |
| **Tamper-evident** | Any modification invalidates the HMAC signature |
| **Transport-safe** | Base64url encoding, safe for env vars, cloud-init, URLs |

## MVP Design (Open Source)

### Token Format

```
base64url(payload) . base64url(hmac-sha256(payload_bytes, signing_key))
```

Two dot-separated segments: payload and signature. The HMAC is computed over the raw base64url-encoded payload bytes (not the decoded JSON) to avoid serialization ambiguity.

Example:
```
eyJ2IjoxLCJuIjoiZWRnZSIsInMiOiJiYXNlIiwiaWF0IjoxNzM4ODAwMDAwfQ.dGhpcyBpcyBhIHNpZ25hdHVyZQ
```

~120-150 characters for typical payloads. Cloud-init user-data supports 16KB+.

### Payload Schema (v1)

```json
{
  "v": 1,
  "n": "edge",
  "s": "base",
  "iat": 1738800000
}
```

| Claim | Type | Required | Description |
|-------|------|----------|-------------|
| `v` | int | Yes | Token schema version (must be `1`) |
| `n` | string | Yes | Node name (VM identity / hostname) |
| `s` | string | Yes | Spec FK — the spec to serve (`specs/{s}.yaml`) |
| `iat` | int | Yes | Issued-at (Unix timestamp, for audit/debugging) |

**Deliberately excluded from v1:**

| Claim | Why excluded |
|-------|-------------|
| `m` (manifest) | Server doesn't need manifest context to serve a spec |
| `p` (posture) | Derivable from the spec's `access.posture` FK |
| `exp` (expiry) | MVP server is ephemeral; expiry adds complexity without clear benefit |
| `jti` (token ID) | One-time tokens require server-side state (commercial feature) |

Claims can be added in future versions (`v: 2`) without breaking v1 verification.

### Signing Key

#### What It Is

A 256-bit symmetric key used for HMAC-SHA256 signing. The same key mints tokens (operator side) and verifies them (server side). This is distinct from the age key used for SOPS encryption — different keys, different purposes:

| Key | Type | Purpose | Used By |
|-----|------|---------|---------|
| age key | Asymmetric (X25519) | Encrypt secrets.yaml at rest | SOPS (git hooks) |
| signing key | Symmetric (256-bit) | Authenticate provisioning tokens in flight | ConfigResolver (mint), server (verify) |

#### Storage

Lives in `secrets.yaml`, encrypted at rest by SOPS like everything else:

```yaml
auth:
  signing_key: "a3f8...64 hex chars...b2c1"   # 32 random bytes, hex-encoded
```

The existing `site_token` and `node_tokens` fields are superseded by the provisioning token and can be removed.

#### Generation

```bash
python3 -c "import secrets; print(secrets.token_hex(32))"
```

One key per site. Generated once during initial config setup (future: `make keygen` target). Stored in `secrets.yaml`, committed encrypted.

#### Distribution Chain

The signing key reaches every host that needs it through the existing config distribution:

```
secrets.yaml (operator workspace, decrypted)
    │
    ├── SOPS encrypted ──► git push ──► all operator machines (git pull + decrypt)
    │
    ├── bootstrap ──► ~/etc/secrets.yaml (decrypted at runtime)
    │   └── iac-driver server loads signing_key at startup ──► verifies tokens
    │
    ├── ConfigResolver loads at `resolve_inline_vm()` time
    │   └── operator mints tokens during `tofu apply`
    │
    └── delegated PVE node: ansible copy-files.yml syncs config to child PVE
        └── child PVE has signing_key ──► can verify tokens for its children
            └── child operator can mint tokens for subtree delegation
```

**Key property:** The recursion works naturally at any depth. Each PVE host in the tree has `secrets.yaml` → has `signing_key` → can both mint and verify tokens for its children.

**Tradeoff:** A single site-wide key means compromise at any depth affects all depths. Per-manifest signing keys (commercial, see [Key Management](#key-management-commercial)) address this with stronger isolation.

#### Lifecycle

| Event | What Happens |
|-------|--------------|
| Site setup | Generate key, add to `secrets.yaml`, encrypt, commit |
| Normal operation | Key is stable — no rotation needed for MVP |
| Key rotation | Generate new key, update `secrets.yaml`, re-encrypt, sync to all hosts. All in-flight tokens signed with the old key become invalid immediately |
| Key compromise | Same as rotation — generate new key, sync. Compromised key can no longer mint valid tokens once the server has the new key |

Rotation invalidates all outstanding tokens. This is acceptable for homelab (operator is at the keyboard, can re-deploy). Commercial key management adds overlap windows and versioned keys for graceful rotation.

### Minting

Tokens are minted during `ConfigResolver.resolve_inline_vm()`, where the operator has full manifest context.

```python
# In config_resolver.py

def _mint_provisioning_token(self, node_name: str, spec_name: str) -> str:
    """Mint a signed provisioning token for a VM."""
    signing_key = self._get_signing_key()
    if not signing_key:
        raise ConfigError("auth.signing_key not found in secrets.yaml")

    payload = {
        "v": 1,
        "n": node_name,
        "s": spec_name,
        "iat": int(time.time()),
    }

    payload_bytes = base64url_encode(json.dumps(payload, separators=(',', ':')))
    signature = hmac.new(
        bytes.fromhex(signing_key),
        payload_bytes,
        hashlib.sha256,
    ).digest()
    sig_bytes = base64url_encode(signature)

    return f"{payload_bytes.decode()}.{sig_bytes.decode()}"
```

**Integration with existing flow:**

The minted token replaces the current `auth_token` field in the resolved VM config and the `_resolve_auth_token()` method. Missing `signing_key` is a hard error — provisioning tokens are the only auth path for spec fetching.

### Distribution (Cloud-Init)

```hcl
# tofu/envs/generic/main.tf — cloud-init write_files

write_files:
  - path: /etc/profile.d/homestak.sh
    content: |
      export HOMESTAK_SERVER=${var.server_url}
      export HOMESTAK_TOKEN=${vm.auth_token}
```

The `auth_token` field always contains a provisioning token (minted by ConfigResolver). The env var is `HOMESTAK_TOKEN` — the previous `HOMESTAK_IDENTITY` and `HOMESTAK_AUTH_TOKEN` variables are removed.

### Presentation (Client)

The VM presents the token when fetching its spec:

```python
# In spec_client.py or iac-driver config --fetch

hostname = socket.gethostname()  # "edge" — from cloud-init
token = os.environ["HOMESTAK_TOKEN"]  # Required — error if missing

url = f"{server_url}/spec/{hostname}"
headers = {"Authorization": f"Bearer {token}"}

response = urllib.request.urlopen(
    urllib.request.Request(url, headers=headers)
)
```

The VM uses its own hostname in the URL (known from cloud-init). The token carries the spec FK. The server reconciles. Missing `HOMESTAK_TOKEN` is a client error (exit 1).

### Verification (Server)

```python
# In server/auth.py

def verify_provisioning_token(token: str, signing_key: str, url_identity: str) -> dict:
    """Verify a provisioning token. Returns decoded claims or raises."""

    # 1. Split token
    parts = token.split(".")
    if len(parts) != 2:
        raise AuthError("E300", "Malformed token")

    payload_b64, sig_b64 = parts

    # 2. Verify HMAC (constant-time comparison)
    expected_sig = hmac.new(
        bytes.fromhex(signing_key),
        payload_b64.encode(),
        hashlib.sha256,
    ).digest()
    actual_sig = base64url_decode(sig_b64)

    if not hmac.compare_digest(expected_sig, actual_sig):
        raise AuthError("E301", "Invalid token signature")

    # 3. Decode payload
    claims = json.loads(base64url_decode(payload_b64))

    # 4. Validate version
    if claims.get("v") != 1:
        raise AuthError("E300", f"Unsupported token version: {claims.get('v')}")

    # 5. Validate identity match (defense in depth)
    if claims.get("n") != url_identity:
        raise AuthError("E301", f"Token identity mismatch: {claims.get('n')} != {url_identity}")

    return claims
```

**Integration with spec endpoint:**

```python
# In server/specs.py — modified handle_spec_request

def handle_spec_request(identity, auth_header, resolver):
    token = extract_bearer_token(auth_header)
    if not token:
        raise AuthError("E300", "Provisioning token required")

    claims = verify_provisioning_token(token, signing_key, identity)
    spec_name = claims["s"]
    return resolver.resolve(spec_name)
```

The spec endpoint requires a provisioning token. No fallback paths, no legacy auth dispatch. The previous posture-based auth (`validate_spec_auth`, `site_token`, `node_tokens`) is removed.

## Error Handling

### Error Categories

| Error | HTTP | Server Response | Client Action | Retryable |
|-------|------|-----------------|---------------|-----------|
| Server unreachable | - | - | Log + retry with backoff | Yes |
| Server timeout | - | - | Log + retry with backoff | Yes |
| TLS handshake failure | - | - | Log + retry with backoff | Yes |
| Token missing | - | (client-side) | Log + fail marker | No |
| Token malformed | 400 | `E300: Malformed token` | Log + fail marker | No |
| Token required | 400 | `E300: Provisioning token required` | Log + fail marker | No |
| Signature invalid | 401 | `E301: Invalid token signature` | Log + fail marker | No |
| Identity mismatch | 401 | `E301: Token identity mismatch` | Log + fail marker | No |
| Token expired | 401 | `E301: Token expired` | Log + fail marker | No |
| Token consumed | 403 | `E302: Token already consumed` | Log + fail marker | No (commercial) |
| Spec not found | 404 | `E200: Spec not found: {s}` | Log + fail marker | No |
| Server error | 500 | `E500: Internal server error` | Log + retry with backoff | Yes |

### Logging

**All errors are logged**, including transient retries. The log is the primary diagnostic tool when a VM fails to configure.

**Client-side log:** `/var/log/homestak/config.log` (written by `./run.sh config --fetch`)

```
2026-02-11T12:00:00Z [INFO]  spec-fetch: starting server=https://srv1:44443 node=edge
2026-02-11T12:00:00Z [INFO]  spec-fetch: token claims v=1 n=edge s=base iat=2026-02-11T11:50:00Z
2026-02-11T12:00:01Z [WARN]  spec-fetch: attempt 1/5 failed: ConnectionRefused (server unreachable)
2026-02-11T12:00:11Z [WARN]  spec-fetch: attempt 2/5 failed: ConnectionRefused (server unreachable)
2026-02-11T12:00:31Z [INFO]  spec-fetch: attempt 3/5 succeeded: 200 OK
2026-02-11T12:00:31Z [INFO]  spec-fetch: spec received spec=base schema_version=1 packages=5 users=1
2026-02-11T12:00:31Z [INFO]  spec-fetch: saved to ~/etc/state/spec.yaml
```

Permanent failure:
```
2026-02-11T12:00:00Z [INFO]  spec-fetch: starting server=https://srv1:44443 node=edge
2026-02-11T12:00:00Z [INFO]  spec-fetch: token claims v=1 n=edge s=base iat=2026-02-11T11:50:00Z
2026-02-11T12:00:01Z [ERROR] spec-fetch: 401 E301 Invalid token signature
2026-02-11T12:00:01Z [ERROR] spec-fetch: permanent error, writing fail marker
```

**Server-side log:** `/var/log/homestak/server.log`

```
2026-02-11T12:00:01Z [INFO]  GET /spec/edge from=198.51.100.155
2026-02-11T12:00:01Z [INFO]  token verified: n=edge s=base iat=2026-02-11T11:50:00Z
2026-02-11T12:00:01Z [INFO]  serving spec=base (resolved, 847 bytes)
```

Failed request:
```
2026-02-11T12:00:01Z [INFO]  GET /spec/edge from=198.51.100.155
2026-02-11T12:00:01Z [WARN]  token verification failed: E301 Invalid token signature node=edge
```

**Log fields:** Every log entry includes enough context to correlate client and server events: timestamp, node name, spec name, error code, server URL (client) or client IP (server).

### Retry Strategy

Transient errors (server unreachable, 5xx) use exponential backoff:

| Attempt | Delay | Cumulative |
|---------|-------|------------|
| 1 | 0s (immediate) | 0s |
| 2 | 10s | 10s |
| 3 | 20s | 30s |
| 4 | 40s | 70s |
| 5 | 80s | 150s |

After 5 failed attempts (~2.5 minutes total): write fail marker and stop. Each attempt is logged at WARN level.

### Fail Marker

Path: `~/etc/state/config-failed.json`

Fields that depend on token decoding (`node`, `spec`) are `null` when the token is malformed or missing. The marker always includes whatever context is available.

Permanent failure (token decodable):
```json
{
  "error": "token_signature_invalid",
  "code": "E301",
  "message": "Invalid token signature",
  "node": "edge",
  "spec": "base",
  "server": "https://srv1:44443",
  "attempts": 1,
  "first_attempt": "2026-02-11T12:00:00Z",
  "last_attempt": "2026-02-11T12:00:01Z",
  "log": "/var/log/homestak/config.log"
}
```

Permanent failure (token malformed or missing):
```json
{
  "error": "token_malformed",
  "code": "E300",
  "message": "Malformed token: expected 2 dot-separated segments, got 1",
  "node": null,
  "spec": null,
  "server": "https://srv1:44443",
  "attempts": 1,
  "first_attempt": "2026-02-11T12:00:00Z",
  "last_attempt": "2026-02-11T12:00:00Z",
  "log": "/var/log/homestak/config.log"
}
```

Transient failure (retries exhausted):
```json
{
  "error": "server_unreachable",
  "code": null,
  "message": "Connection refused after 5 attempts",
  "node": "edge",
  "spec": "base",
  "server": "https://srv1:44443",
  "attempts": 5,
  "first_attempt": "2026-02-11T12:00:00Z",
  "last_attempt": "2026-02-11T12:02:30Z",
  "log": "/var/log/homestak/config.log"
}
```

**Operator visibility:**
- `WaitForFileAction` on `complete.json` times out → node marked failed in operator
- SSH inspection: `cat $HOMESTAK_ROOT/.state/config/failed.json`
- Full log: `cat /var/log/homestak/config.log`
- Server logs: failed requests logged with client IP and error code

### Operator Recovery

```bash
# Inspect failure (structured)
ssh edge cat ~/etc/state/config-failed.json

# Inspect failure (full log)
ssh edge cat /var/log/homestak/config.log

# Option 1: Re-mint and inject fresh token
ssh edge "echo 'export HOMESTAK_TOKEN=<new-token>' > /etc/profile.d/homestak.sh"
ssh edge "rm -f ~/etc/state/config-failed.json"
ssh edge "$HOMESTAK_ROOT/iac/iac-driver/run.sh config --fetch --insecure"

# Option 2: Push config directly (bypass pull)
./run.sh config-push --host edge --spec base

# Option 3: Destroy and recreate (token is fresh on new create)
./run.sh destroy -M n1-pull -H srv1 --yes
./run.sh create -M n1-pull -H srv1
```

## Key Components Affected

### MVP Changes

| Repo | File | Change |
|------|------|--------|
| iac-driver | `src/config_resolver.py` | Add `_mint_provisioning_token()`, replace `_resolve_auth_token()` |
| iac-driver | `src/server/auth.py` | Replace `validate_spec_auth()` with `verify_provisioning_token()` |
| iac-driver | `src/server/specs.py` | Require provisioning token, extract `s` claim for spec lookup |
| iac-driver | `src/config_apply.py` | Update `--fetch` to use `HOMESTAK_TOKEN`, add structured logging, fail marker |
| bootstrap | `lib/spec_client.py` | Read `HOMESTAK_TOKEN`, send as Bearer, add structured logging |
| tofu | `envs/generic/main.tf` | Inject `HOMESTAK_TOKEN` only (remove `HOMESTAK_IDENTITY` and `HOMESTAK_AUTH_TOKEN`) |
| config | `secrets.yaml` | Add `auth.signing_key`, remove `site_token` and `node_tokens` |

### Design Doc Updates

| Document | Update |
|----------|--------|
| `phase-interfaces.md` | Create→Config contract: add token as output, spec FK no longer assumed from identity |
| `node-lifecycle.md` | Pull model diagram: `HOMESTAK_TOKEN` replaces identity-based lookup |
| `spec-client.md` | Data flow: token presentation replaces identity URL construction |
| `config-phase.md` | Cloud-init runcmd: `HOMESTAK_TOKEN` replaces `HOMESTAK_IDENTITY` and `HOMESTAK_AUTH_TOKEN` |

## Design Decisions

### D1: HMAC-SHA256, Not Encryption (MVP)

**Decision:** MVP tokens are signed (HMAC-SHA256) but not encrypted. Claims are visible to anyone with the token.

**Rationale:**
- Claims are not secret — node name and spec name are not sensitive
- The security property needed is **authenticity** (who minted it), not **confidentiality** (who can read it)
- Token travels via cloud-init (local disk) and HTTPS (encrypted transport) — not exposed in plaintext on the network
- HMAC uses only Python stdlib (`hmac`, `hashlib`, `json`, `base64`) — zero dependencies
- Encryption adds a dependency (`cryptography` library or `PyNaCl`) without clear benefit for homelab use

**Future:** Commercial layer adds encryption (see [Encryption Extension](#encryption-extension-commercial)).

### D2: No Expiry in MVP

**Decision:** MVP tokens have `iat` (issued-at) for audit but no `exp` (expiry).

**Rationale:**
- MVP server is ephemeral — runs during orchestration, stops after
- Token is only useful if you can reach the server, and the server is only running briefly
- Signing key rotation IS the expiry mechanism — rotate the key and all tokens become invalid
- Expiry introduces clock-skew concerns on freshly booted VMs (NTP may not be synced yet)

**Future:** Commercial layer adds configurable expiry per posture.

### D3: Hostname in URL, Spec in Token

**Decision:** VM still sends `GET /spec/{hostname}`. The spec FK comes from the token, not the URL.

**Rationale:**
- Defense in depth — server validates URL identity matches token's `n` claim
- Better logging — server sees identity in URL without decoding token
- Clean URL semantics — "I am edge, here is my provisioning token"

## Commercial Extensions

These features build on the MVP token format without breaking it. Each adds server-side state or additional claims.

### Token Expiry

Add `exp` claim (Unix timestamp). Server rejects tokens past expiry.

```json
{
  "v": 1,
  "n": "edge",
  "s": "base",
  "iat": 1738800000,
  "exp": 1738886400
}
```

**Configuration per posture:**

```yaml
# postures/prod.yaml
auth:
  method: token
  token_expiry: 1h
```

**Open question:** Clock skew tolerance on freshly booted VMs. NTP sync may not complete before first spec fetch. A grace window (e.g., 5 minutes) or explicit `nbf` (not-before) claim could address this. TBD.

### One-Time Tokens

Add `jti` (token ID) claim. Server maintains a consumption ledger.

```json
{
  "v": 1,
  "jti": "a7f3b2c1",
  "n": "edge",
  "s": "base",
  "iat": 1738800000
}
```

**Ledger:** SQLite database at `/var/lib/homestak/token-ledger.db`.

```sql
CREATE TABLE consumed_tokens (
    jti TEXT PRIMARY KEY,
    consumed_at TEXT NOT NULL,
    consumed_by TEXT NOT NULL,  -- client IP
    node_name TEXT NOT NULL,
    spec TEXT NOT NULL
);
```

**Server flow:**
1. Verify HMAC (stateless)
2. Check `jti` not in ledger (stateful)
3. Serve spec
4. Record `jti` in ledger (atomic)

**Failure mode:** If server crashes between serving spec and recording `jti`, the token could be replayed. Acceptable for this model — the spec is the same regardless, and the ledger is for audit/billing, not strict security.

**Use cases:**
- Fleet pre-provisioning: mint 50 tokens offline, each used exactly once
- Billing metering: one token = one provisioned node
- Audit trail: clear provenance of which token provisioned which VM

### Encryption Extension (Commercial)

Upgrade from signed-only to signed-and-encrypted. Claims become opaque without the key.

**Format change:**

```
base64url(encrypted_payload) . base64url(tag)
```

Where `encrypted_payload` = AES-256-GCM(payload + HMAC, key, nonce). The GCM authentication tag replaces the separate HMAC — authenticated encryption provides both confidentiality and integrity.

**Implications:**
- Token introspection requires the key: `./run.sh token inspect <token>` works on operator's machine (has secrets.yaml) but not on a naked VM
- Captured tokens reveal nothing about the node or spec
- Adds `cryptography` library dependency (justified for commercial tier)

**Token introspection CLI:**

```bash
# Signed-only (MVP): decode without key
./run.sh token inspect <token>
# Output: {"v": 1, "n": "edge", "s": "base", "iat": 1738800000}
# Note: signature not verified (no --key), claims shown as-is

# Signed-only (MVP): decode and verify
./run.sh token inspect <token> --verify
# Output: {"v": 1, "n": "edge", "s": "base", "iat": 1738800000} (verified)
# Uses signing_key from secrets.yaml auto-discovery

# Encrypted (commercial): requires key
./run.sh token inspect <token>
# Output: Error: token is encrypted, use --key or ensure secrets.yaml is accessible

./run.sh token inspect <token> --verify
# Output: {"v": 1, "n": "edge", "s": "base", "iat": 1738800000} (decrypted, verified)
```

### Token Revocation

Server maintains a revocation list (separate from consumption ledger). Tokens can be revoked before use.

```bash
./run.sh token revoke <jti>         # Revoke by token ID
./run.sh token revoke --node edge   # Revoke all tokens for a node
./run.sh token list --active        # List non-revoked, non-consumed tokens
```

**Use case:** Node returned/decommissioned before provisioning. Operator revokes its token to prevent unauthorized use.

### Key Management (Commercial)

| Feature | Description |
|---------|-------------|
| Key rotation with overlap | Accept tokens signed by previous key during grace period |
| Immediate key disablement | Reject all tokens signed by a specific key (emergency revocation) |
| Per-manifest signing keys | Stronger isolation between deployments |
| Key versioning | `kid` (key ID) claim in token, server selects verification key |

**Open questions for later evaluation:**
- Key overlap window duration (configurable per posture?)
- Key disablement mechanism (revocation list vs key deletion)
- Per-manifest keys: key distribution complexity vs isolation benefit

### Feature Matrix

| Feature | Open Source (MVP) | Commercial |
|---------|-------------------|------------|
| Token minting | Yes | Yes |
| HMAC-SHA256 signing | Yes | Yes |
| Spec FK in token | Yes | Yes |
| `iat` claim (audit) | Yes | Yes |
| Token introspection (decode) | Yes | Yes |
| Token introspection (verify) | Yes | Yes |
| Configurable expiry | No | Yes |
| One-time tokens (`jti`) | No | Yes |
| Consumption ledger | No | Yes |
| Token revocation | No | Yes |
| Encryption (AES-256-GCM) | No | Yes |
| Key rotation / overlap | No | Yes |
| Key disablement | No | Yes |
| Fleet pre-provisioning | No | Yes |
| Billing metering hooks | No | Yes |

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| Signing key missing from secrets.yaml | Low | High | Hard error at mint time with clear message; preflight could check |
| Token too large for cloud-init | Very Low | Low | ~150 bytes; cloud-init supports 16KB+ |
| HMAC implementation bug | Low | High | Use Python stdlib `hmac.compare_digest`; unit tests |
| Clock skew breaks `iat` audit | Low | Low | `iat` is informational in MVP, not enforced |
| Token captured from VM disk | Medium | Low | Signed-only MVP: claims not secret; encrypted commercial: opaque |
| Signing key compromised | Low | High | Key rotation; commercial: key disablement |
| JSON serialization ambiguity | Low | Medium | HMAC over base64url bytes, not raw JSON |

## Test Plan

### Unit Tests (iac-driver)

1. **Token minting**: correct format, valid HMAC, all claims present
2. **Token verification**: valid token accepted
3. **Tampered token**: modified payload → HMAC mismatch → rejected
4. **Wrong signing key**: valid format but wrong key → rejected
5. **Identity mismatch**: token `n` != URL identity → rejected
6. **Version check**: `v: 2` token → rejected by v1 verifier
7. **Missing signing key**: hard error at mint time, clear message
8. **Missing token**: server returns `E300`, client writes fail marker
9. **Base64url encoding**: padding-free, URL-safe characters
10. **Retry logic**: transient errors retry with backoff, permanent errors fail immediately
11. **Logging**: all attempts logged (including transient retries)

### Integration Tests

**Scenario: Pull-mode with provisioning token**
```bash
./run.sh test -M n1-pull -H srv1
# VM boots, presents token, fetches spec, self-configures
```

**Scenario: Token introspection**
```bash
./run.sh token inspect <token>
# Shows decoded claims without verification

./run.sh token inspect <token> --verify
# Shows decoded claims with HMAC verification
```

## Open Questions

1. **Clock skew tolerance for expiry (commercial):** TBD. Freshly booted VMs may not have NTP sync. Grace window? `nbf` claim?

2. **Key rotation overlap policy (commercial):** Key overlap vs immediate disablement — evaluate as a pair of requirements during commercial design. Both are needed; the question is policy configuration.

3. **Encrypted token introspection UX:** If token is encrypted, `inspect` without key shows nothing useful. Should it show metadata (token length, encrypted flag) or refuse entirely?

4. **Manifest-scoped signing keys:** Good isolation property but adds key distribution complexity. Evaluate for "MVP Plus" — may be the natural boundary for commercial key management.

## Implementation Order

1. **`secrets.yaml`: Add `auth.signing_key`** — generate key, add to secrets, encrypt
2. **`config_resolver.py`: Token minting** — `_mint_provisioning_token()` replaces `_resolve_auth_token()`
3. **`server/auth.py`: Token verification** — `verify_provisioning_token()` with HMAC check
4. **`server/specs.py`: Spec endpoint** — require token, extract `s` claim, serve correct spec
5. **`tofu/main.tf`: Cloud-init** — inject `HOMESTAK_TOKEN` only (remove `HOMESTAK_IDENTITY` and `HOMESTAK_AUTH_TOKEN`)
6. **`spec_client.py` / `config_apply.py`**: Read `HOMESTAK_TOKEN`, present as Bearer, structured logging, fail marker
7. **Remove legacy auth** — delete `validate_spec_auth()`, `_resolve_auth_token()`, posture-based auth dispatch
8. **Unit tests** — minting, verification, retry logic, logging, edge cases
9. **Integration test** — `./run.sh test -M n1-pull -H srv1`
10. **Token introspection CLI** — `./run.sh token inspect`
11. **Design doc updates** — phase-interfaces, node-lifecycle, spec-client, config-phase

## Related Documents

- [node-lifecycle.md](node-lifecycle.md) — Single-node lifecycle, pull model
- [phase-interfaces.md](phase-interfaces.md) — Create→Config contract (needs update)
- [spec-client.md](spec-client.md) — `homestak spec get` client
- [config-phase.md](config-phase.md) — Config phase and cloud-init
- [server-daemon.md](server-daemon.md) — Server daemon design
- [requirements-catalog.md](requirements-catalog.md) — REQ-CTL-* requirements
- [iac-driver#185](https://github.com/homestak-iac/iac-driver/issues/185) — Scope issue: identity != spec

## Changelog

| Date | Change |
|------|--------|
| 2026-02-11 | Drop HOMESTAK_AUTH_TOKEN entirely — provisioning token subsumes both identity and authorization |
| 2026-02-11 | Elaborate signing key: lifecycle, distribution chain, nested PVE, relationship to age key |
| 2026-02-11 | Remove backward compat: token required, legacy auth removed, no fallback paths |
| 2026-02-11 | Comprehensive error logging: all errors logged (transient retries + permanent failures), structured log format |
| 2026-02-11 | Initial draft: concept, MVP design, commercial extensions |
