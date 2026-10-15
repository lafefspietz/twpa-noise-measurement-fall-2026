#!/usr/bin/env bash
# Start the remote experiment server (Linux counterpart of run-server.bat).
#   ./run-server.sh                      real instruments, reachable from LAN + Tailscale
#   ./run-server.sh --sim                simulators
#   ./run-server.sh --host tailscale     only reachable over Tailscale
#   ./run-server.sh --host local         only from this machine
# Extra arguments are passed through.
# Ctrl-C stops the server and leaves instrument outputs as they are.

cd "$(dirname "$0")"

PY=python3
[[ -x .venv/bin/python ]] && PY=.venv/bin/python
[[ -x ../.venv/bin/python ]] && PY=../.venv/bin/python

exec "$PY" -m labserver.server "$@"
