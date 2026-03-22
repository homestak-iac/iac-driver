# Server Daemon


The server daemon serves specs and git repos over HTTPS. See [server-daemon.md](server-daemon.md) for architecture, double-fork daemonization, PID management, and operator lifecycle integration.

## Management

```bash
./run.sh server start                    # Start as daemon
./run.sh server start --repos --repo-token <token>  # With repo serving
./run.sh server start --foreground       # Development mode
./run.sh server status [--json]          # Check status
./run.sh server stop                     # Stop daemon
```

PID file: `$HOMESTAK_ROOT/.run/server-{port}.pid` | Log file: `$HOMESTAK_ROOT/logs/server.log`

Operator (executor.py) auto-manages server lifecycle for manifest verbs with reference counting.

## Endpoints

| Method | Endpoint | Auth | Description |
|--------|----------|------|-------------|
| GET | `/health` | None | Health check |
| GET | `/specs` | None | List available specs |
| GET | `/spec/{identity}` | Provisioning token | Fetch resolved spec |
| GET | `/{repo}.git/*` | Bearer | Git dumb HTTP protocol |
| GET | `/{repo}.git/{path}` | Bearer | Raw file extraction |

Spec endpoints authenticate via HMAC-signed provisioning tokens. See [provisioning-token.md](provisioning-token.md).

Auto-generates self-signed TLS certificate if none provided via `--cert`/`--key`.
