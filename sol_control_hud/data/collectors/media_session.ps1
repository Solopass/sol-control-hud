# Now playing (Windows media sessions) as one JSON line: {"app","title","artist","status"} or {"status":"none"}.
#   -Every N  : keep running and print a line every N seconds (sol_control_hud.data.collectors.media reads them). One process
#               for the ticker's whole life instead of a new PowerShell every 3.5 s (~1000 an hour, 09-26).
#   -ParentPid: exit when that process is gone (the ticker), so it never outlives it.
param([double]$Every = 0, [int]$ParentPid = 0)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
function Await($task, $type) {
    $asTask = $asTaskGeneric.MakeGenericMethod($type)
    $netTask = $asTask.Invoke($null, @($task))
    $netTask.Wait(1500) | Out-Null
    $netTask.Result
}
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media, ContentType = WindowsRuntime] | Out-Null
$mgr = $null
function NowPlaying {
    try {
        if (-not $script:mgr) {
            $op = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
            $script:mgr = Await $op ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
        }
        $session = if ($script:mgr) { $script:mgr.GetCurrentSession() } else { $null }
        if ($session) {
            $infoOp = $session.TryGetMediaPropertiesAsync()
            $info = Await $infoOp ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])
            $playback = $session.GetPlaybackInfo()
            [pscustomobject]@{
                app = $session.SourceAppUserModelId
                title = if ($info) { $info.Title } else { $null }
                artist = if ($info) { $info.Artist } else { $null }
                status = if ($playback) { $playback.PlaybackStatus.ToString() } else { 'none' }
            } | ConvertTo-Json -Compress
        } else {
            '{"status":"none"}'
        }
    } catch {
        $script:mgr = $null   # ask for a fresh session manager next time
        '{"status":"none"}'
    }
}
if ($Every -le 0) { NowPlaying; exit 0 }
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
while ($true) {
    if ($ParentPid -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { exit 0 }
    try { [Console]::Out.WriteLine((NowPlaying)); [Console]::Out.Flush() } catch { exit 0 }   # the reader is gone
    Start-Sleep -Milliseconds ([int]($Every * 1000))
}
