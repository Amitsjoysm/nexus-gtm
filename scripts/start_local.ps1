# NEXUS GTM — local dev launcher (Windows).
#
# Starts the API server and the background worker (automation heartbeat + job consumer)
# in separate PowerShell windows, killing any stale instances first so ports and the
# SQLite file are never contended. Run from anywhere:
#
#   powershell -ExecutionPolicy Bypass -File scripts\start_local.ps1
#
# The worker is what drives daily ICP discovery, account refresh, cadences, and digests —
# without it only the synchronous API paths run.

$root = Split-Path -Parent $PSScriptRoot

# Stop stale instances (uvicorn server or worker) so we never run two schedulers
# against the same database.
Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='uvicorn.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -match 'nexus\.main:app|nexus\.workers\.worker' } |
  ForEach-Object {
    Write-Host "Stopping stale process $($_.ProcessId): $($_.CommandLine.Substring(0, [Math]::Min(80, $_.CommandLine.Length)))"
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  }
Start-Sleep -Seconds 2

Write-Host "Starting API server on http://127.0.0.1:8000 ..."
# `python -m uvicorn` (not bare `uvicorn`): freshly spawned windows may not have the
# Python Scripts dir on PATH, but `python` itself always resolves.
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "Set-Location '$root'; python -m uvicorn nexus.main:app --host 127.0.0.1 --port 8000"
)

Write-Host "Starting background worker (automation heartbeat + queue consumer) ..."
Start-Process powershell -ArgumentList @(
  "-NoExit", "-Command",
  "Set-Location '$root'; python -m nexus.workers.worker"
)

Start-Sleep -Seconds 5
try {
  $health = Invoke-RestMethod -Uri "http://127.0.0.1:8000/health" -TimeoutSec 10
  Write-Host "API healthy: $($health | ConvertTo-Json -Compress)" -ForegroundColor Green
  Write-Host "Open http://127.0.0.1:8000 to use the app." -ForegroundColor Green
} catch {
  Write-Host "API not responding yet - check the server window for startup errors." -ForegroundColor Yellow
}
