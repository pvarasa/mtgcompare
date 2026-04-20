#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$script_dir/.." && pwd)"

# See start.sh for why Windows has to go through PowerShell.
case "${OSTYPE:-$(uname -s)}" in
    msys*|cygwin*|MINGW*|MSYS*)
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script_dir/stop.ps1"
        ;;
esac

pid_file="$root/app.pid"

if [ ! -f "$pid_file" ]; then
    echo "Not running (no pid file)."
    exit 0
fi

app_pid="$(cat "$pid_file")"
if kill -0 "$app_pid" 2>/dev/null; then
    kill "$app_pid"
    echo "Stopped mtgcompare (pid $app_pid)."
else
    echo "Process $app_pid not running; cleaning up pid file."
fi
rm -f "$pid_file"
