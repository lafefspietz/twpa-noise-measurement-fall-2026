@echo off
rem Start the remote experiment server.
rem   run-server.bat                      real instruments, reachable from LAN + Tailscale
rem   run-server.bat --sim                simulators
rem   run-server.bat --host tailscale     only reachable over Tailscale
rem   run-server.bat --host local         only from this machine
rem Extra arguments are passed through.
rem Ctrl-C stops the server and leaves instrument outputs as they are.

cd /d "%~dp0"

set PY=python
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if exist "..\.venv\Scripts\python.exe" set PY=..\.venv\Scripts\python.exe

%PY% -m labserver.server %*
