# SOL Ticker HUD - Standalone Launcher
# Launches the taskbar ticker widget completely detached (no console window).

$py = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
if (-not (Test-Path $py)) {
    Write-Error "Virtual environment not found at $py"
    exit 1
}

# Win32 GUI detached launch (pythonw creates no console window)
Start-Process -FilePath $py -ArgumentList "-m sol_control_hud.views.ticker" -WorkingDirectory $PSScriptRoot

