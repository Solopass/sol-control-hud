"""The now-playing reader: the Go sol-media.exe when it's there and works, else media_session.ps1 (2026-11-07)."""
from pathlib import Path

from sol_control_hud.data.collectors import media
from sol_control_hud.data.collectors.media import MediaCollector, parse_line


def _files(tmp_path: Path, exe=True, script=True):
    e, s = tmp_path / "sol-media.exe", tmp_path / "media_session.ps1"
    if exe:
        e.write_bytes(b"")
    if script:
        s.write_text("")
    return MediaCollector(exe_path=e, script_path=s), e, s


def test_prefers_exe_with_the_same_flags(tmp_path):
    c, exe, _ = _files(tmp_path)
    cmd = c.command(3.5)
    assert cmd[0] == str(exe)
    assert cmd[1:3] == ["-Every", "3.5"] and cmd[3] == "-ParentPid"


def test_uses_ps1_when_no_exe(tmp_path):
    c, _, script = _files(tmp_path, exe=False)
    cmd = c.command()
    assert cmd[0] == "powershell.exe" and cmd[-1] == str(script)


def test_nothing_to_run(tmp_path):
    c, _, _ = _files(tmp_path, exe=False, script=False)
    assert c.command() is None


def test_falls_back_after_quick_exe_failures(tmp_path, monkeypatch):
    noted = []
    monkeypatch.setattr(media, "_swallowed", noted.append)
    c, _, script = _files(tmp_path)
    for _ in range(media.EXE_MAX_QUICK_FAILS):
        assert c.using_exe()
        c.note_exit(was_exe=True, ran_s=0.2, lines=0)
    assert not c.using_exe()
    assert c.command()[-1] == str(script)
    assert noted == ["media.MediaCollector.exe_fallback"]


def test_a_good_run_resets_the_failure_count(tmp_path):
    c, _, _ = _files(tmp_path)
    for _ in range(media.EXE_MAX_QUICK_FAILS - 1):
        c.note_exit(was_exe=True, ran_s=0.2, lines=0)
    c.note_exit(was_exe=True, ran_s=600, lines=170)
    c.note_exit(was_exe=True, ran_s=0.2, lines=0)
    assert c.using_exe()


def test_a_long_run_without_output_is_not_a_failed_start(tmp_path):
    c, _, _ = _files(tmp_path)
    for _ in range(media.EXE_MAX_QUICK_FAILS + 1):
        c.note_exit(was_exe=True, ran_s=media.EXE_QUICK_FAIL_S + 1, lines=0)
    assert c.using_exe()


def test_ps1_exits_never_count(tmp_path):
    c, _, _ = _files(tmp_path)
    for _ in range(10):
        c.note_exit(was_exe=False, ran_s=0.1, lines=0)
    assert c.using_exe()


def test_go_output_parses_like_ps1_output():
    line = '{"app":"Spotify.exe","title":"Song ✓","artist":"A","status":"Playing"}'
    info = parse_line(line)
    assert info.playing and info.title == "Song ✓" and info.clean_app == "Spotify"
    assert parse_line('{"status":"none"}').status == "none"
    assert parse_line('{"app":"vlc.exe","title":null,"artist":null,"status":"Paused"}').title is None


# ---- the media-key router (2026-11-07) ----

YT = "Brave._crx_" + media.YOUTUBE_APP_ID


def test_long_running_exe_routes_keys_ps1_does_not(tmp_path):
    c, _, _ = _files(tmp_path)
    cmd = c.command(3.5)
    assert "-Keys" in cmd and cmd[cmd.index("-Target") + 1] == str(media.TARGET_FILE)
    assert "-Keys" not in c.command()                       # one-shot polls never grab the keys
    no_exe = tmp_path / "no-exe"
    no_exe.mkdir()
    c2, _, _ = _files(no_exe, exe=False)
    assert "-Keys" not in c2.command(3.5)                   # the .ps1 can't route keys


def test_router_fields_parse():
    info = parse_line('{"status":"Paused","app":"Spotify.exe","keys":"on","target":"youtube",'
                      '"last_key":"08:40:03 play/pause -> Spotify.exe",'
                      '"sessions":[{"app":"Spotify.exe","title":"A","status":"Paused"},{"bad":1}]}')
    assert info.keys == "on" and info.target == "youtube" and info.last_key.endswith("Spotify.exe")
    assert info.sessions == ({"app": "Spotify.exe", "title": "A", "status": "Paused"},)
    assert parse_line('{"status":"none"}').keys == ""        # the .ps1: no router


def test_target_file_round_trip(tmp_path):
    p = tmp_path / "t.json"
    assert media.get_media_target(p) == "youtube"           # default
    media.set_media_target("app:Spotify.exe", p)
    assert media.get_media_target(p) == "app:Spotify.exe"
    p.write_text("{broken")
    assert media.get_media_target(p) == "youtube"


def test_session_label():
    assert media.session_label(YT) == "YouTube app (Brave)"
    assert media.session_label("Spotify.exe") == "Spotify"


def _labels(rows):
    return [r[0] for r in rows]


def test_menu_default_and_sessions():
    info = media.MediaInfo(keys="on", sessions=(
        {"app": YT, "title": "Some video", "status": "Paused"},
        {"app": "Spotify.exe", "title": "Song", "status": "Playing"},
        {"app": "Spotify.exe", "title": "Song", "status": "Playing"},
    ))
    rows = media.media_keys_menu(info, "youtube")
    assert rows[0] == ("✓ YouTube app (Brave)  ·  default", "youtube")
    assert rows[1][1] == "auto" and not rows[1][0].startswith("✓")
    picks = [r for r in rows if r[1] and r[1].startswith("app:")]
    assert picks == [("   Spotify · Song  (Playing)", "app:Spotify.exe")]   # YouTube not repeated, duplicates merged
    assert not any("⚠" in (r[0] or "") for r in rows)


def test_menu_marks_a_picked_app_even_when_it_is_gone():
    rows = media.media_keys_menu(media.MediaInfo(keys="on"), "app:Discord")
    assert ("✓ Discord  (not running: keys go to Automatic)", "app:Discord") in rows
    assert not rows[0][0].startswith("✓")


def test_menu_says_when_keys_are_taken_or_router_off():
    assert "⚠ Another program holds the media keys" in _labels(media.media_keys_menu(media.MediaInfo(keys="taken"), "youtube"))
    assert "Router off: keys work the Windows way" in _labels(media.media_keys_menu(media.MediaInfo(), "youtube"))
    last = media.media_keys_menu(media.MediaInfo(keys="on", last_key="08:40 play/pause -> x"), "auto")
    assert ("Last: 08:40 play/pause -> x", None) in last


def test_winerror_4551_switches_to_ps1_immediately(tmp_path, monkeypatch):
    noted = []
    monkeypatch.setattr(media, "_swallowed", noted.append)
    c, _, script = _files(tmp_path)

    def blocked(*a, **k):
        err = OSError("Application Control policy has blocked this file")
        err.winerror = 4551
        raise err

    monkeypatch.setattr(media.subprocess, "run", blocked)
    c.poll_once()
    assert not c.using_exe()
    assert c.command()[-1] == str(script)
    assert noted == []
