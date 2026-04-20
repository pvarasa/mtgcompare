#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$script_dir/.." && pwd)"

# On Windows (Git Bash / MSYS / Cygwin), bash's $! gives a shell-local PID
# that doesn't match the real Windows PID — `kill` can't stop the real
# process. Delegate to the PowerShell script, which captures the real PID.
case "${OSTYPE:-$(uname -s)}" in
    msys*|cygwin*|MINGW*|MSYS*)
        exec powershell.exe -NoProfile -ExecutionPolicy Bypass -File "$script_dir/start.ps1"
        ;;
esac

pid_file="$root/app.pid"
log_file="$root/app.log"
python_bin="$root/.venv/bin/python"

if [ ! -x "$python_bin" ]; then
    echo "venv not found at $python_bin. Run: uv sync" >&2
    exit 1
fi

if [ -f "$pid_file" ]; then
    existing_pid="$(cat "$pid_file")"
    if kill -0 "$existing_pid" 2>/dev/null; then
        echo "mtgcompare already running (pid $existing_pid)."
        exit 0
    fi
    rm -f "$pid_file"
fi

export PYTHONIOENCODING=utf-8
nohup "$python_bin" -m mtgcompare.web >"$log_file" 2>&1 </dev/null &
app_pid=$!
echo "$app_pid" > "$pid_file"

sleep 0.5
if ! kill -0 "$app_pid" 2>/dev/null; then
    echo "Failed to start. Check $log_file" >&2
    rm -f "$pid_file"
    exit 1
fi

echo "Started mtgcompare (pid $app_pid)"
echo "URL: http://127.0.0.1:5000"
echo "Log: $log_file"
