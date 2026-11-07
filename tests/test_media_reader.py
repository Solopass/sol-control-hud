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
