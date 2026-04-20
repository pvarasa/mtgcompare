$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir
$pidFile = Join-Path $root "app.pid"

if (-not (Test-Path $pidFile)) {
    Write-Host "Not running (no pid file)."
    exit 0
}

$appPid = [int]((Get-Content $pidFile).Trim())

# The venv python.exe shim spawns a child (the real interpreter), so stop the whole tree.
function Stop-Tree($targetPid) {
    Get-CimInstance Win32_Process -Filter "ParentProcessId=$targetPid" |
        ForEach-Object { Stop-Tree $_.ProcessId }
    Stop-Process -Id $targetPid -Force -ErrorAction SilentlyContinue
}

if ($appPid -and (Get-Process -Id $appPid -ErrorAction SilentlyContinue)) {
    Stop-Tree $appPid
    Write-Host "Stopped mtgcompare (pid $appPid)."
} else {
    Write-Host "Process $appPid not running; cleaning up pid file."
}

Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
