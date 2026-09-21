#!/bin/bash

# Manual test suite for the unified graceful shutdown manager
# (GlancesShutdownManager in glances/__init__.py).
#
# Covered behaviour:
#   1. standalone mode: SIGTERM triggers the phased shutdown and the last
#      export batch is flushed to disk (no data loss)
#   2. standalone mode: SIGINT sets the graceful exit flag (no traceback)
#   3. server (XML-RPC) mode: SIGINT triggers the phased shutdown
#   4. webserver mode: /api/4/health reports "ready" then the phased
#      shutdown runs on SIGTERM
#   5. webserver mode: in-flight connections are drained up to the
#      configured shutdown_drain_timeout before exit
#
# Run from the repository root:
#   ./tests/test_graceful_shutdown.sh
#
# Prerequisites: glances dependencies installed (psutil, defusedxml,
# fastapi, uvicorn for the web tests), curl for the HTTP checks.

set -u

# --- Configuration -----------------------------------------------------------

if [ -x .venv/bin/python ]; then
    GLANCES=".venv/bin/python -m glances"
else
    GLANCES="python3 -m glances"
fi

TEST_DIR=$(mktemp -d /tmp/glances_shutdown_test.XXXXXX)
export XDG_CACHE_HOME="$TEST_DIR/cache"
LOG_FILE="$XDG_CACHE_HOME/glances/glances.log"
XMLRPC_PORT=62101
WEB_PORT=62102

PASS=0
FAIL=0
SKIP=0

# --- Helpers -----------------------------------------------------------------

cleanup() {
    # Kill any leftover glances process started by this script
    jobs -p 2>/dev/null | xargs -I{} kill -9 {} 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT

ok() {
    PASS=$((PASS + 1))
    echo "  [OK] $1"
}

ko() {
    FAIL=$((FAIL + 1))
    echo "  [FAIL] $1"
}

skip() {
    SKIP=$((SKIP + 1))
    echo "  [SKIP] $1"
}

reset_log() {
    rm -rf "$XDG_CACHE_HOME"
}

check_phases_in_log() {
    # check_phases_in_log <test_label>
    local label="$1"
    local all_ok=1
    for phase in "phase 1/4" "phase 2/4" "phase 3/4" "phase 4/4"; do
        if ! grep -q "Graceful shutdown: $phase" "$LOG_FILE" 2>/dev/null; then
            echo "  [....] missing log line: Graceful shutdown: $phase"
            all_ok=0
        fi
    done
    if [ "$all_ok" -eq 1 ]; then
        ok "$label: all 4 shutdown phases logged"
    else
        ko "$label: missing shutdown phases in $LOG_FILE"
    fi
}

wait_process_exit() {
    # wait_process_exit <pid> <timeout_sec>; returns the process exit code
    local pid="$1"
    local timeout="$2"
    local elapsed=0
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

echo "======================================================================"
echo "Glances graceful shutdown - manual test suite"
echo "Test dir: $TEST_DIR"
echo "Command:  $GLANCES"
echo "======================================================================"

# --- Test 1: standalone mode flushes exports on SIGTERM ---------------------

echo ""
echo "Test 1: standalone mode + JSON export, SIGTERM -> graceful shutdown"
reset_log
JSON_EXPORT="$TEST_DIR/glances-export.json"
rm -f "$JSON_EXPORT"

$GLANCES --quiet --export json --export-json-file "$JSON_EXPORT" -t 1 >/dev/null 2>&1 &
PID=$!
sleep 5
kill -TERM "$PID"
wait_process_exit "$PID" 30
RC=$?

if [ "$RC" -eq 0 ]; then
    ok "standalone exited with code 0 after SIGTERM"
else
    ko "standalone exit code: $RC (expected 0)"
fi
check_phases_in_log "standalone"
if [ -s "$JSON_EXPORT" ]; then
    if command -v jq >/dev/null 2>&1; then
        if jq -e . "$JSON_EXPORT" >/dev/null 2>&1; then
            ok "JSON export file flushed and valid ($JSON_EXPORT)"
        else
            ko "JSON export file is not valid JSON (last batch lost?)"
        fi
    else
        ok "JSON export file flushed ($JSON_EXPORT) - jq not available, validity not checked"
    fi
else
    ko "JSON export file missing or empty (export buffers not flushed)"
fi

# --- Test 2: standalone mode graceful exit flag on SIGINT -------------------

echo ""
echo "Test 2: standalone mode, SIGINT -> graceful exit flag (no traceback)"
reset_log
STDOUT_ERR="$TEST_DIR/standalone-stderr.log"

$GLANCES --quiet -t 1 >/dev/null 2>"$STDOUT_ERR" &
PID=$!
sleep 4
kill -INT "$PID"
wait_process_exit "$PID" 30
RC=$?

if [ "$RC" -eq 0 ]; then
    ok "standalone exited with code 0 after SIGINT"
else
    ko "standalone exit code: $RC (expected 0)"
fi
if grep -q "Traceback" "$STDOUT_ERR" 2>/dev/null; then
    ko "traceback found in stderr (brutal exit instead of graceful flag)"
else
    ok "no traceback on SIGINT"
fi

# --- Test 3: XML-RPC server mode --------------------------------------------

echo ""
echo "Test 3: server (XML-RPC) mode, SIGINT -> graceful shutdown"
reset_log

$GLANCES -s -B 127.0.0.1 -p "$XMLRPC_PORT" >/dev/null 2>&1 &
PID=$!
sleep 4
kill -INT "$PID"
wait_process_exit "$PID" 30
RC=$?

if [ "$RC" -eq 0 ]; then
    ok "XML-RPC server exited with code 0 after SIGINT"
else
    ko "XML-RPC server exit code: $RC (expected 0)"
fi
check_phases_in_log "xmlrpc-server"
if grep -q "XML-RPC server shutdown requested" "$LOG_FILE" 2>/dev/null; then
    ok "XML-RPC server stopped accepting new requests (shutdown method)"
else
    ko "XML-RPC server shutdown() not called"
fi

# --- Test 4: webserver mode + /health endpoint ------------------------------

echo ""
echo "Test 4: webserver mode, /api/4/health then SIGTERM -> graceful shutdown"
if ! $GLANCES --help >/dev/null 2>&1; then
    skip "glances command not working"
elif ! python3 -c "import fastapi, uvicorn" >/dev/null 2>&1 && ! .venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    skip "fastapi/uvicorn not installed"
else
    reset_log

    $GLANCES -w -B 127.0.0.1 -p "$WEB_PORT" >/dev/null 2>&1 &
    PID=$!

    # Wait for the REST API to be up
    UP=0
    for _ in $(seq 1 20); do
        if curl -s -o /dev/null "http://127.0.0.1:$WEB_PORT/api/4/status" 2>/dev/null; then
            UP=1
            break
        fi
        sleep 1
    done

    if [ "$UP" -ne 1 ]; then
        ko "webserver did not start on port $WEB_PORT"
        kill -9 "$PID" 2>/dev/null
    else
        HEALTH=$(curl -s "http://127.0.0.1:$WEB_PORT/api/4/health")
        echo "  /api/4/health -> $HEALTH"
        if echo "$HEALTH" | grep -qE '"status": *"ready"'; then
            ok "/health reports status ready"
        else
            ko "/health does not report ready: $HEALTH"
        fi
        for component in plugins exports processlist; do
            if echo "$HEALTH" | grep -q "\"$component\""; then
                ok "/health includes $component component info"
            else
                ko "/health missing $component component info"
            fi
        done

        kill -TERM "$PID"
        wait_process_exit "$PID" 30
        RC=$?
        if [ "$RC" -eq 0 ]; then
            ok "webserver exited with code 0 after SIGTERM"
        else
            ko "webserver exit code: $RC (expected 0)"
        fi
        check_phases_in_log "webserver"
        if grep -q "Web server shutdown requested" "$LOG_FILE" 2>/dev/null; then
            ok "webserver stopped accepting new connections"
        else
            ko "webserver stop() not called"
        fi
    fi
fi

# --- Test 5: webserver drains in-flight connections -------------------------

echo ""
echo "Test 5: webserver mode, in-flight connection drained up to shutdown_drain_timeout"
if ! python3 -c "import fastapi, uvicorn" >/dev/null 2>&1 && ! .venv/bin/python -c "import fastapi, uvicorn" >/dev/null 2>&1; then
    skip "fastapi/uvicorn not installed"
else
    reset_log
    DRAIN_CONF="$TEST_DIR/glances-drain.conf"
    printf '[outputs]\nshutdown_drain_timeout = 3\n' > "$DRAIN_CONF"

    $GLANCES -w -B 127.0.0.1 -p "$WEB_PORT" -C "$DRAIN_CONF" >/dev/null 2>&1 &
    PID=$!

    UP=0
    for _ in $(seq 1 20); do
        if curl -s -o /dev/null "http://127.0.0.1:$WEB_PORT/api/4/status" 2>/dev/null; then
            UP=1
            break
        fi
        sleep 1
    done

    if [ "$UP" -ne 1 ]; then
        ko "webserver did not start on port $WEB_PORT"
        kill -9 "$PID" 2>/dev/null
    else
        # Hold a raw TCP connection open (simulates a lingering client)
        exec 3<>"/dev/tcp/127.0.0.1/$WEB_PORT"

        START=$(date +%s)
        kill -TERM "$PID"
        wait_process_exit "$PID" 30
        RC=$?
        END=$(date +%s)
        ELAPSED=$((END - START))
        exec 3>&- 3<&-

        echo "  shutdown took ${ELAPSED}s with a lingering connection (drain timeout: 3s)"
        if [ "$ELAPSED" -ge 3 ]; then
            ok "shutdown waited for the drain timeout before exiting"
        else
            ko "shutdown took only ${ELAPSED}s, in-flight connection not drained"
        fi
        if [ "$RC" -eq 0 ]; then
            ok "webserver exited with code 0 after drain"
        else
            ko "webserver exit code after drain: $RC (expected 0)"
        fi
        if grep -q "still active" "$LOG_FILE" 2>/dev/null; then
            ok "drain timeout warning logged (connection forced after timeout)"
        else
            echo "  [....] note: no 'still active' warning (connection may have closed earlier)"
        fi
    fi
fi

# --- Summary -----------------------------------------------------------------

echo ""
echo "======================================================================"
echo "Results: $PASS passed, $FAIL failed, $SKIP skipped"
echo "Logs and artifacts: $TEST_DIR"
echo "======================================================================"

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
