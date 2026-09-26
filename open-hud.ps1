# SOL HUD launcher (desktop shortcut "SOL HUD"): start the HUD in the background if it isn't running, then open it.
# The HUD keeps running after the browser tab closes (it's light: a few collectors every few seconds).
# Stop it: Stop-Process on the pythonw.exe whose command line has "sol_control_hud.views.web.app", or restart the PC.
# Log: data\hud-launch.log (starts), data\hud.log (the HUD's own messages when it runs without a console).
param([switch]$NoBrowser)  # -NoBrowser: start/check only (tests)
$url = 'http://127.0.0.1:7900'
$log = Join-Path $PSScriptRoot 'data\hud-launch.log'
function Up { try { (Invoke-WebRequest $url -TimeoutSec 1 -UseBasicParsing).StatusCode -eq 200 } catch { $false } }
function Note($m) { Add-Content -Path $log -Value "$(Get-Date -Format s) $m" -Encoding utf8 }
function Start-Hud {
  # Win32_Process.Create: fully detached, no console window, doesn't hold this script open
  $py = Join-Path $PSScriptRoot '.venv\Scripts\pythonw.exe'
  $r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{ CommandLine = "`"$py`" -m sol_control_hud.views.web.app"; CurrentDirectory = $PSScriptRoot }
  Note "start: rv=$($r.ReturnValue) pid=$($r.ProcessId)"
  for ($i = 0; $i -lt 30; $i++) { if (Up) { return $true }; Start-Sleep -Milliseconds 500 }
  return $false
}
if (-not (Up)) {
  # if the first start doesn't come up (never seen in tests: 3/3 up in 2 s), try once more before giving up
  if (-not (Start-Hud)) { Note 'not up after 15 s, retrying'; Start-Sleep 3; if (-not (Start-Hud)) { Note 'still not up; see data\hud.log' } }
}
# a new address each time skips any page the browser cached before the HUD sent no-cache headers (09-25)
if ($NoBrowser) { "up: $(Up)" } else { Start-Process "$url/?t=$([DateTimeOffset]::Now.ToUnixTimeSeconds())" }
