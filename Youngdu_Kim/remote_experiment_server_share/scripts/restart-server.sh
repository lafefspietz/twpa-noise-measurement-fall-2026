#!/usr/bin/env bash
# Restart the server in the background (Ubuntu). The git post-receive hook calls this after a push.
#   scripts/restart-server.sh                       restart with the arguments it is running with now;
#                                                   do nothing if it is not running
#   scripts/restart-server.sh --sim --host local    start (or restart) with these arguments
# The server stops on SIGTERM, which leaves instrument outputs as they are.
# Output goes to logs/stdout.log.
set -u
cd "$(dirname "$0")/.."
ROOT=$PWD

PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python

# The server started from this folder (run-server.sh cds here before exec'ing python).
find_pid() {
    local p
    for p in $(pgrep -f -- '-m labserver\.server'); do
        [[ "$(readlink "/proc/$p/cwd" 2>/dev/null)" == "$ROOT" ]] && { echo "$p"; return; }
    done
}

pid=$(find_pid)
if [[ $# -gt 0 ]]; then
    args=("$@")
elif [[ -n "$pid" ]]; then
    mapfile -d '' -t cmd < "/proc/$pid/cmdline"
    args=()
    for ((i = 0; i < ${#cmd[@]}; i++)); do
        [[ "${cmd[i]}" == labserver.server ]] && { args=("${cmd[@]:i+1}"); break; }
    done
else
    echo "server is not running; not starting it (start it with: scripts/restart-server.sh <args>)"
    exit 0
fi

if [[ -n "$pid" ]]; then
    echo "stopping server (pid $pid)"
    kill -TERM "$pid"
    for ((i = 0; i < 100; i++)); do
        kill -0 "$pid" 2>/dev/null || break
        # uvicorn treats a second SIGTERM as "exit now" (e.g. a websocket that won't close)
        ((i == 50)) && kill -TERM "$pid" 2>/dev/null
        sleep 0.2
    done
    if kill -0 "$pid" 2>/dev/null; then
        echo "server (pid $pid) did not stop; not starting a second one"
        exit 1
    fi
fi

# Port to check: --port if given, else server.port in the config file.
port=""
conf=config.yaml
for ((i = 0; i < ${#args[@]}; i++)); do
    [[ "${args[i]}" == --port ]] && port=${args[i+1]:-}
    [[ "${args[i]}" == --port=* ]] && port=${args[i]#--port=}
    [[ "${args[i]}" == --config ]] && conf=${args[i+1]:-}
    [[ "${args[i]}" == --config=* ]] && conf=${args[i]#--config=}
done
[[ -z "$port" ]] && port=$("$PY" -c "import sys, yaml; print(yaml.safe_load(open(sys.argv[1]))['server']['port'])" "$conf" 2>/dev/null)
port=${port:-8000}

echo "starting: ./run-server.sh ${args[*]}"
mkdir -p logs
echo "=== $(date '+%F %T') restart-server.sh ${args[*]}" >> logs/stdout.log
setsid nohup ./run-server.sh "${args[@]}" >> logs/stdout.log 2>&1 < /dev/null &

for ((i = 0; i < 60; i++)); do
    sleep 0.5
    if ! kill -0 $! 2>/dev/null; then
        echo "server exited during startup; last lines of logs/stdout.log:"
        tail -n 20 logs/stdout.log
        exit 1
    fi
    if "$PY" -c "import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=2)" \
            "http://127.0.0.1:$port/api/status" 2>/dev/null; then
        echo "server is up (pid $!, port $port)"
        exit 0
    fi
done
echo "server is running (pid $!) but /api/status did not answer within 30 s; see logs/stdout.log"
exit 1
