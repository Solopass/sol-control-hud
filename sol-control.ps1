# SOL Control HUD: start it (detached, no console window), or tell the running copy what to show.
#   .\sol-control.ps1                 # your saved views (the ticker by default)
#   .\sol-control.ps1 -Show dashboard # or ticker | both | tray
#   .\sol-control.ps1 -Restart        # ask the running HUD to exit cleanly, then start a fresh one (never kill it)
param([ValidateSet('', 'ticker', 'dashboard', 'both', 'tray')][string]$Show = '', [switch]$Restart)
$py = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $py)) { Write-Error "no virtual environment at $py (see README: Setup)"; exit 1 }
if ($Restart) {
  & (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -m sol_control_hud --restart
  exit $LASTEXITCODE
}
$cmd = "`"$py`" -m sol_control_hud" + $(if ($Show) { " --show $Show" } else { '' })
# Win32_Process.Create: fully detached, doesn't keep this script (or a shortcut's console) open
$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = $cmd; CurrentDirectory = $PSScriptRoot }
if ($r.ReturnValue -ne 0) { Write-Error "start failed ($($r.ReturnValue))"; exit 1 }