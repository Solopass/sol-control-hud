"""swallow.note: errors the code carries on after are written down, once an hour per place, and never raise."""
from sol_control_hud import swallow


def _raise_and_note(where, exc):
    try:
        raise exc
    except Exception:
        swallow.note(where)


def test_written_once_per_place_and_kind(tmp_path, monkeypatch):
    monkeypatch.setattr(swallow, "LOG_FILE", tmp_path / "swallowed.log")
    monkeypatch.setattr(swallow, "_last", {})
    for _ in range(50):                                   # a handler on a 2 s loop
        _raise_and_note("ticker.TickerApp._render", ValueError("bad width"))
    _raise_and_note("ticker.TickerApp._render", KeyError("x"))       # another kind: its own line
    _raise_and_note("snapshot.read_state", ValueError("y"))           # another place: its own line
    lines = (tmp_path / "swallowed.log").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert "ticker.TickerApp._render (_raise_and_note line" in lines[0] and "ValueError: bad width" in lines[0]


def test_again_after_the_quiet_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(swallow, "LOG_FILE", tmp_path / "s.log")
    monkeypatch.setattr(swallow, "_last", {})
    clock = {"t": 1000.0}
    monkeypatch.setattr(swallow.time, "monotonic", lambda: clock["t"])
    _raise_and_note("p", OSError("a"))
    clock["t"] += swallow.REPEAT_S - 1
    _raise_and_note("p", OSError("a"))
    clock["t"] += 2
    _raise_and_note("p", OSError("a"))
    assert len((tmp_path / "s.log").read_text(encoding="utf-8").splitlines()) == 2


def test_never_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(swallow, "LOG_FILE", tmp_path / "missing-dir" / "x" / "s.log")
    monkeypatch.setattr(swallow, "_last", {})
    monkeypatch.setattr(swallow, "open", lambda *a, **k: (_ for _ in ()).throw(PermissionError("locked")), raising=False)
    _raise_and_note("p", RuntimeError("boom"))           # can't write: still no exception
    swallow.note("outside an except")                    # nothing being handled: nothing happens


def test_the_file_starts_over_when_big(tmp_path, monkeypatch):
    log = tmp_path / "s.log"
    log.write_text("x" * (swallow.MAX_BYTES + 10))
    monkeypatch.setattr(swallow, "LOG_FILE", log)
    monkeypatch.setattr(swallow, "_last", {})
    _raise_and_note("p", ValueError("v"))
    assert (tmp_path / "s.log.old").exists() and log.stat().st_size < 500
