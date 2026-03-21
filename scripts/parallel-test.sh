#!/usr/bin/env bash
# Run multiple manifest tests concurrently with a shared server.
#
# Usage: parallel-test.sh -H <host> <manifest1> [manifest2] ... [--verbose]
#
# Starts a shared spec/repo server on the target host, launches N
# manifest tests in parallel, collects results, and stops the server.
# Each test's ServerManager.ensure() finds the running server and
# reuses it (sets _started=False, so stop() won't kill it).

# shellcheck disable=SC2317  # cleanup() is invoked via trap, not direct call
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
RUN_SH="$BASE_DIR/run.sh"

# Defaults
HOST=""
MANIFESTS=()
VERBOSE=""
SERVER_STARTED="false"
SSH_HOST=""
SSH_USER=""
PORT=""

# Subprocess tracking
declare -a PIDS=()
declare -a MANIFEST_NAMES=()

show_help() {
    cat << 'EOF'
parallel-test.sh - Run multiple manifest tests concurrently

Usage: parallel-test.sh -H <host> <manifest1> [manifest2] ... [--verbose]

Options:
  -H, --host <host>   Target PVE host (required)
  --verbose            Pass --verbose to each manifest test
  -h, --help           Show this help

Examples:
  ./scripts/parallel-test.sh -H mother n1-push n1-pull
  ./scripts/parallel-test.sh -H mother n1-push n1-pull n2-push --verbose

The script starts a shared server, runs tests concurrently, and
stops the server when all tests complete (or on interrupt).
EOF
}

cleanup() {
    # Kill running test subprocesses
    for pid in "${PIDS[@]}"; do
        kill "$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
    # Stop server only if we started it
    if [[ "$SERVER_STARTED" == "true" && -n "$SSH_HOST" ]]; then
        echo ""
        echo "==> Stopping server on $SSH_HOST:$PORT..."
        ssh "$SSH_USER@$SSH_HOST" \
            "cd ~/iac/iac-driver" \
            "&& ./run.sh server stop --port $PORT" \
            2>/dev/null || true
    fi
}
trap cleanup EXIT

# Parse arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        -H|--host)
            HOST="$2"
            shift 2
            ;;
        --verbose)
            VERBOSE="--verbose"
            shift
            ;;
        -h|--help)
            show_help
            exit 0
            ;;
        -*)
            echo "Error: Unknown option '$1'" >&2
            echo "Run with --help for usage." >&2
            exit 2
            ;;
        *)
            MANIFESTS+=("$1")
            shift
            ;;
    esac
done

# Validate arguments
if [[ -z "$HOST" ]]; then
    echo "Error: -H <host> is required" >&2
    echo "Run with --help for usage." >&2
    exit 2
fi

if [[ ${#MANIFESTS[@]} -eq 0 ]]; then
    echo "Error: At least one manifest name is required" >&2
    echo "Run with --help for usage." >&2
    exit 2
fi

# Resolve SSH host, user, and port from config
read -r SSH_HOST SSH_USER PORT < <(
    cd "$BASE_DIR/src" && python3 -c "
from config import load_host_config
from manifest_opr.server_mgmt import ServerManager
c = load_host_config('$HOST')
port = ServerManager.resolve_port(getattr(c, 'spec_server', '') or '')
print(c.ssh_host, c.ssh_user, port)
"
) || {
    echo "Error: Failed to resolve config for host '$HOST'" >&2
    exit 2
}

echo "==> Host: $HOST ($SSH_USER@$SSH_HOST, port $PORT)"
echo "==> Manifests: ${MANIFESTS[*]}"

# Check if server is already running
SERVER_RUNNING="false"
if ssh "$SSH_USER@$SSH_HOST" \
    "cd ~/iac/iac-driver" \
    "&& ./run.sh server status --json --port $PORT" 2>/dev/null \
    | python3 -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('running') and d.get('healthy') else 1)" 2>/dev/null; then
    SERVER_RUNNING="true"
fi

if [[ "$SERVER_RUNNING" == "true" ]]; then
    echo "==> Server already running on $SSH_HOST:$PORT (reusing)"
else
    echo "==> Starting server on $SSH_HOST:$PORT..."
    if ! ssh "$SSH_USER@$SSH_HOST" \
        "cd ~/iac/iac-driver" \
        "&& ./run.sh server start --port $PORT --repos --repo-token ''" 2>&1; then
        echo "Error: Server start failed" >&2
        exit 2
    fi
    SERVER_STARTED="true"
fi

# Launch tests concurrently
echo "==> Running ${#MANIFESTS[@]} manifest test(s) in parallel..."
TMPDIR_BASE=$(mktemp -d)

for manifest in "${MANIFESTS[@]}"; do
    log_file="$TMPDIR_BASE/$manifest.log"
    # shellcheck disable=SC2086
    "$RUN_SH" manifest test -M "$manifest" -H "$HOST" $VERBOSE \
        >"$log_file" 2>&1 &
    pid=$!
    PIDS+=("$pid")
    MANIFEST_NAMES+=("$manifest")
    printf "  %-16s started (pid %d)\n" "$manifest" "$pid"
done

# Wait and collect results
echo ""
echo "==> Waiting for results..."
failures=0

for i in "${!PIDS[@]}"; do
    pid="${PIDS[$i]}"
    manifest="${MANIFEST_NAMES[$i]}"
    start_time=$SECONDS
    wait "$pid" 2>/dev/null && rc=0 || rc=$?
    duration=$(( SECONDS - start_time ))
    if [[ $rc -eq 0 ]]; then
        printf "  %-16s PASSED  (%ds)\n" "$manifest" "$duration"
    else
        printf "  %-16s FAILED  (rc=%d, %ds)\n" "$manifest" "$rc" "$duration"
        ((failures++)) || true
        # Show last 10 lines of log for failed tests
        echo "    --- last 10 lines of $manifest log ---"
        tail -10 "$TMPDIR_BASE/$manifest.log" 2>/dev/null | sed 's/^/    /'
        echo "    ---"
    fi
done

# Summary
echo ""
passed=$(( ${#MANIFESTS[@]} - failures ))
echo "==> Done. $passed/${#MANIFESTS[@]} passed"
echo "==> Logs in: $TMPDIR_BASE/"
echo "==> Reports in: $BASE_DIR/reports/"

# Cleanup temp dir on success (keep on failure for debugging)
if [[ $failures -eq 0 ]]; then
    rm -rf "$TMPDIR_BASE"
fi

exit "$failures"
