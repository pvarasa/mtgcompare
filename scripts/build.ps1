$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Definition)
Set-Location $root

Write-Host "Installing build dependencies..."
uv pip install pyinstaller pystray pillow

Write-Host "Running tests..."
uv run pytest
if (-not $?) { Write-Error "Tests failed — aborting build."; exit 1 }

Write-Host "Building with PyInstaller..."
uv run pyinstaller mtgcompare.spec --clean --noconfirm

$dist = Join-Path $root "dist\mtgcompare"
if (-not (Test-Path $dist)) {
    Write-Error "Build failed — dist\mtgcompare not found."
    exit 1
}

$zip = Join-Path $root "dist\mtgcompare-windows.zip"
Write-Host "Zipping to $zip..."
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path $dist -DestinationPath $zip

Write-Host ""
Write-Host "Done: $zip"
Write-Host "Test by running: dist\mtgcompare\mtgcompare.exe"
