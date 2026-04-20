$ErrorActionPreference = "Stop"
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$root = Split-Path -Parent $scriptDir

$pidFile = Join-Path $root "app.pid"
$logFile = Join-Path $root "app.log"
$errFile = Join-Path $root "app.err.log"
$python  = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    Write-Error "venv not found at $python. Run: uv sync"
    exit 1
}

if (Test-Path $pidFile) {
    $existingPid = (Get-Content $pidFile).Trim()
    if ($existingPid -and (Get-Process -Id $existingPid -ErrorAction SilentlyContinue)) {
        Write-Host "mtgcompare already running (pid $existingPid)."
        exit 0
    }
    Remove-Item $pidFile -Force
}

$env:PYTHONIOENCODING = "utf-8"
$proc = Start-Process -FilePath $python -ArgumentList "-m", "mtgcompare.web" `
    -WorkingDirectory $root `
    -RedirectStandardOutput $logFile `
    -RedirectStandardError  $errFile `
    -PassThru -NoNewWindow

$proc.Id | Out-File -FilePath $pidFile -Encoding ascii

Start-Sleep -Milliseconds 500
if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
    Write-Error "Failed to start. Check $errFile"
    Remove-Item $pidFile -Force
    exit 1
}

Write-Host "Started mtgcompare (pid $($proc.Id))"
Write-Host "URL: http://127.0.0.1:5000"
Write-Host "Log: $logFile"
