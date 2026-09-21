#!/bin/bash
#
# Manual test script for the unified graceful shutdown feature
# (GlancesShutdownManager, /health endpoint, graceful exit of the
# standalone / server / webserver modes).
#
# Usage:
#   ./test.sh                # run all the tests
#   PYTHON=/path/to/python ./test.sh
#
# Note: the server/webserver integration tests need to bind local TCP
# ports; they are skipped automatically in restricted (sandboxed)
# environments where bind() is not permitted.

set -u

cd "$(dirname "$0")"

# Python interpreter used to run Glances
PYTHON="${PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x ".venv/bin/python" ]; then
        PYTHON=".venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

TEST_TMP=$(mktemp -d /tmp/glances_shutdown_test.XXXXXX)
trap 'rm -rf "$TEST_TMP"' EXIT

# Glances writes its log to \$XDG_CACHE_HOME/glances/glances.log
export XDG_CACHE_HOME="$TEST_TMP/xdg"
mkdir -p "$XDG_CACHE_HOME"
GLANCES_LOG="$XDG_CACHE_HOME/glances/glances.log"

PASS=0
FAIL=0
SKIP=0

ok()   { PASS=$((PASS + 1)); echo "  [PASS] $1"; }
ko()   { FAIL=$((FAIL + 1)); echo "  [FAIL] $1"; }
skip() { SKIP=$((SKIP + 1)); echo "  [SKIP] $1"; }

# Wait for a process to exit (timeout in seconds); return its exit code
wait_pid() {
    local pid=$1 timeout=$2 elapsed=0
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$elapsed" -ge "$timeout" ]; then
            return 124
        fi
        sleep 1
        elapsed=$((elapsed + 1))
    done
    wait "$pid"
    return $?
}

# Check if binding a local TCP socket is allowed in this environment
can_bind() {
    "$PYTHON" - << 'EOF'
import socket, sys
s = socket.socket()
try:
    s.bind(('127.0.0.1', 0))
except OSError:
    sys.exit(1)
sys.exit(0)
EOF
}

# Number of lines already present in the Glances log (used to only grep
# the lines written by the current test)
log_mark() {
    if [ -f "$GLANCES_LOG" ]; then
        wc -l < "$GLANCES_LOG"
    else
        echo 0
    fi
}

# Return the log lines written since the given mark
log_since() {
    tail -n +"$(( $1 + 1 ))" "$GLANCES_LOG" 2>/dev/null
}

echo "=================================================================="
echo " Glances graceful shutdown - manual test suite"
echo " Python: $PYTHON"
echo "=================================================================="

# ------------------------------------------------------------------
echo
echo "[1/4] Unit tests (tests/test_shutdown.py)"
# ------------------------------------------------------------------
if "$PYTHON" -m pytest tests/test_shutdown.py -q; then
    ok "unit tests"
else
    ko "unit tests"
fi

# ------------------------------------------------------------------
echo
echo "[2/4] Standalone mode: SIGINT triggers a graceful shutdown"
# ------------------------------------------------------------------
MARK=$(log_mark)
"$PYTHON" -m glances -q -t 1 --disable-check-update &
GLANCES_PID=$!
sleep 4
if ! kill -0 "$GLANCES_PID" 2>/dev/null; then
    ko "standalone: Glances exited before SIGINT (see $GLANCES_LOG)"
else
    kill -INT "$GLANCES_PID"
    wait_pid "$GLANCES_PID" 30
    RET=$?
    if [ "$RET" -eq 0 ]; then
        ok "standalone: exited with code 0 after SIGINT"
    else
        ko "standalone: exit code $RET after SIGINT (expected 0)"
    fi
    if log_since "$MARK" | grep -q "Graceful shutdown"; then
        ok "standalone: graceful shutdown phases found in the log"
    else
        ko "standalone: no graceful shutdown trace in $GLANCES_LOG"
    fi
fi

# ------------------------------------------------------------------
echo
echo "[3/4] Web server mode: /health endpoint + SIGTERM graceful shutdown"
# ------------------------------------------------------------------
if ! can_bind; then
    skip "webserver: binding TCP sockets is not allowed in this environment"
else
    WEB_PORT=61834
    MARK=$(log_mark)
    "$PYTHON" -m glances -w --disable-webui -B 127.0.0.1 -p $WEB_PORT -t 1 --disable-check-update \
        &
    GLANCES_PID=$!
    # Wait for the API to be up
    UP=0
    for _ in $(seq 1 20); do
        if curl -s -o /dev/null "http://127.0.0.1:$WEB_PORT/api/4/status"; then
            UP=1
            break
        fi
        sleep 1
    done
    if [ "$UP" -ne 1 ]; then
        ko "webserver: API did not start (see $GLANCES_LOG)"
        kill -9 "$GLANCES_PID" 2>/dev/null
    else
        HEALTH=$(curl -s "http://127.0.0.1:$WEB_PORT/api/4/health")
        echo "  /health -> $HEALTH"
        echo "$HEALTH" | grep -q '"status": *"ready"\|"status":"ready"' \
            && ok "webserver: /health reports ready" \
            || ko "webserver: /health does not report ready"
        echo "$HEALTH" | grep -q '"plugins"' \
            && ok "webserver: /health contains components health" \
            || ko "webserver: /health misses components health"
        kill -TERM "$GLANCES_PID"
        wait_pid "$GLANCES_PID" 30
        RET=$?
        if [ "$RET" -eq 0 ]; then
            ok "webserver: exited with code 0 after SIGTERM"
        else
            ko "webserver: exit code $RET after SIGTERM (expected 0)"
        fi
        if log_since "$MARK" | grep -q "Graceful shutdown"; then
            ok "webserver: graceful shutdown phases found in the log"
        else
            ko "webserver: no graceful shutdown trace in $GLANCES_LOG"
        fi
    fi
fi

# ------------------------------------------------------------------
echo
echo "[4/4] XML-RPC server mode: SIGINT graceful shutdown"
# ------------------------------------------------------------------
if ! can_bind; then
    skip "server: binding TCP sockets is not allowed in this environment"
else
    SRV_PORT=61835
    MARK=$(log_mark)
    "$PYTHON" -m glances -s -B 127.0.0.1 -p $SRV_PORT --disable-autodiscover --disable-check-update \
        &
    GLANCES_PID=$!
    sleep 4
    if ! kill -0 "$GLANCES_PID" 2>/dev/null; then
        ko "server: Glances exited before SIGINT (see $GLANCES_LOG)"
    else
        kill -INT "$GLANCES_PID"
        wait_pid "$GLANCES_PID" 30
        RET=$?
        if [ "$RET" -eq 0 ]; then
            ok "server: exited with code 0 after SIGINT"
        else
            ko "server: exit code $RET after SIGINT (expected 0)"
        fi
        if log_since "$MARK" | grep -q "Graceful shutdown"; then
            ok "server: graceful shutdown phases found in the log"
        else
            ko "server: no graceful shutdown trace in $GLANCES_LOG"
        fi
    fi
fi

# ------------------------------------------------------------------
echo
echo "=================================================================="
echo " Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "=================================================================="

[ "$FAIL" -eq 0 ]
