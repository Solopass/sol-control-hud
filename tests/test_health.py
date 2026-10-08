"""E1: the HUD health card - how the last run ended, real errors from its logs, samplers that went quiet."""
from datetime import datetime

from sol_control_hud import health

NOW = datetime(2026, 10, 8, 18, 0, 0).timestamp()


def test_how_the_last_run_ended():
    boot = datetime(2026, 10, 8, 9, 0, 0).timestamp()
    assert health.previous_end(None, boot) is None                                        # clean Exit: no marker
    assert health.previous_end({"pid": 1, "started": "2026-10-07T22:00:00"}, boot) == health.ENDED_WITH_PC
    assert health.previous_end({"pid": 1, "started": "2026-10-08T12:00:00"}, boot) == health.ENDED_UNEXPECTEDLY
    assert health.previous_end({"pid": 1}, boot) == health.ENDED_UNEXPECTEDLY            # no start time: assume the worst


def test_restarts_count_only_unexpected_ends_in_the_window():
    log = "\n".join([
        f"2026-09-20T10:00:00 {health.ENDED_UNEXPECTEDLY} (pid 1)",          # older than a week
        f"2026-10-05T10:00:00 {health.ENDED_UNEXPECTEDLY} (pid 2)",
        f"2026-10-08T07:00:00 {health.ENDED_WITH_PC} (pid 3)",               # a reboot is not a HUD problem
        f"2026-10-08T12:00:00 {health.ENDED_UNEXPECTEDLY} (pid 4)",
        "2026-10-08T12:00:01 start (pid 5, dashboard http://127.0.0.1:7900, ticker on)",
    ])
    r = health.restarts(log, now=NOW)
    assert (r["week"], r["day"]) == (2, 1)
    assert r["last"] == datetime(2026, 10, 8, 12, 0, 0).timestamp()


HUB_LOG = """2026-10-08T10:00:00 start (pid 5, dashboard http://127.0.0.1:7900, ticker on)
2026-10-08T10:05:00 notice (error): While you were busy: 2 things
Exception in callback _ProactorBasePipeTransport._call_connection_lost()
handle: <Handle _ProactorBasePipeTransport._call_connection_lost()>
Traceback (most recent call last):
  File "C:\\Program Files\\Python314\\Lib\\asyncio\\proactor_events.py", line 166, in _call_connection_lost
    self._sock.shutdown(socket.SHUT_RDWR)
ConnectionResetError: [WinError 10054] An existing connection was forcibly closed by the remote host
2026-10-08T11:00:00 tick error: KeyError: 'vram'
2026-10-08T11:30:00 heal: not evicted
Traceback (most recent call last):
  File "D:\\Workspace\\sol-control-hud\\sol_control_hud\\hub.py", line 900, in _serve
    raise ValueError("boom")
ValueError: boom
INFO:     some uvicorn chatter
2026-10-08T12:00:00 crash: Traceback (most recent call last):
  File "x.py", line 1, in <module>
ZeroDivisionError: division by zero
2026-10-08T12:00:05 exit: tray Exit
"""


def test_real_errors_newest_first_without_the_noise():
    got = health.errors([("hub", HUB_LOG), ("ticker", "2026-10-08T09:00:00 error: Tk window lost\n")])
    assert [(e["source"], e["text"]) for e in got] == [
        ("hub", "ZeroDivisionError: division by zero"),        # a crash: line with its own traceback
        ("hub", "ValueError: boom"),                           # a stderr traceback, timed by the line before it
        ("hub", "tick error: KeyError: 'vram'"),
        ("ticker", "error: Tk window lost"),
    ]
    assert got[1]["at"] == datetime(2026, 10, 8, 11, 30, 0).timestamp()
    assert "hub.py" in got[1]["detail"] and "uvicorn" not in got[1]["detail"]
    assert not any("ConnectionResetError" in e["detail"] for e in got)     # the closed-stream noise is gone
    assert len(health.errors([("hub", HUB_LOG)], n=2)) == 2
    chained = """2026-10-08T13:00:00 x
Traceback (most recent call last):
  File "a"
KeyError: 1

During handling of the above exception, another exception occurred:

Traceback (most recent call last):
  File "b"
RuntimeError: second
"""
    (one,) = health.errors([("hub", chained)])
    assert one["text"] == "RuntimeError: second" and "KeyError: 1" in one["detail"]   # one entry, the last exception


def test_samplers_that_stopped_or_went_quiet():
    got = health.stale([
        ("GPU counters", NOW - 3, 2.0, True),        # fine
        ("Snapshot", NOW - 60, 2.0, True),            # 60 s on a 2 s pace: stale
        ("VRAM guard", NOW - 1, 4.0, False),          # its thread died
        ("Slow one", NOW - 150, 60.0, True),          # 150 s on a 60 s pace: still fine (stale past 185 s)
    ], now=NOW)
    assert got == [{"name": "Snapshot", "age_s": 60, "why": "stale"}, {"name": "VRAM guard", "age_s": 1, "why": "stopped"}]


def test_level():
    assert health.level({"restarts": {"week": 0, "day": 0}, "stale": [], "broken": []}) == "ok"
    assert health.level({"restarts": {"week": 1, "day": 0}, "stale": [], "broken": []}) == "warn"
    assert health.level({"restarts": {"week": 0, "day": 0}, "stale": [], "broken": ["disks"]}) == "warn"
    assert health.level({"restarts": {"week": 3, "day": 3}, "stale": [], "broken": []}) == "bad"
    assert health.level({"restarts": {}, "stale": [{"name": "x", "age_s": 1, "why": "stopped"}], "broken": []}) == "bad"


def test_handled_counts_the_last_day_from_the_swallowed_log():
    log = "\n".join([
        "2026-10-06T10:00:00 ticker.TickerApp._render (_render line 3): ValueError: old",          # 2 days ago
        "2026-10-08T09:00:00 ticker.TickerApp._render (_render line 3): ValueError: bad width",
        "2026-10-08T11:00:00 ticker.TickerApp._render (_render line 3): ValueError: bad width",
        "2026-10-08T12:00:00 snapshot.read_state (read_state line 9): OSError: locked",
    ])
    h = health.handled(log, now=NOW)
    assert (h["day"], h["places"]) == (3, 2)
    assert h["recent"][0] == {"at": datetime(2026, 10, 8, 12).timestamp(), "where": "snapshot.read_state (read_state line 9)",
                              "text": "OSError: locked"}
    assert health.level({"restarts": {"week": 0, "day": 0}, "stale": [], "broken": [], "handled": h}) == "ok"   # info only
