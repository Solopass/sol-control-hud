"""Phase D: the minute history, 24 h / 7 d chart series, the week card, pruning."""
import json
import time

from sol_control_hud import metrics
from sol_control_hud.data.snapshot import Snapshot

T0 = 1_790_000_000.0   # a fixed "now" (2026-09-21-ish), minute-aligned below


def snap(t, **k):
    base = dict(gpu_load=50.0, vram_used_gb=8.0, ram_used_gb=30.0, cpu_percent=10.0, gpu_temp=60, gpu_hotspot=70,
                net_down_kb=100.0, net_up_kb=10.0, disks=[{"drive": "D", "free_gb": 1650.0}])
    base.update(k)
    return Snapshot(sampled_at=t, **base)


def test_one_row_a_minute_with_averages_and_peaks(tmp_path):
    m = metrics.Metrics(tmp_path / "m.sqlite", clock=lambda: T0)
    start = T0 // 60 * 60
    for i, load in enumerate([10.0, 30.0, 50.0]):                   # three samples in the same minute
        m.add(snap(start + i * 10, gpu_load=load, gpu_hotspot=70 + i * 10))
    m.add(snap(start + 20))                                         # the same sample again: ignored
    m.add(snap(start + 20, gpu_load=None))                          # ... still ignored (same sampled_at)
    m.add(snap(start + 65))                                         # next minute -> the first row is written
    row = m.db.execute("SELECT t, gpu, gpu_max, hotspot_max, disks FROM minutes").fetchall()
    assert row == [(int(start), 30.0, 50.0, 90, json.dumps({"D": 1650.0}))]
    m.close()
    assert m.db is not None


def test_chart_series_and_week(tmp_path):
    path = tmp_path / "m.sqlite"
    m = metrics.Metrics(path, clock=lambda: T0)
    start = T0 // 60 * 60 - 3 * 86400
    for i in range(0, 3 * 1440, 5):                                 # every 5th minute over 3 days
        t = start + i * 60
        m.add(snap(t, gpu_load=float(i % 100), vram_used_gb=4.0 + (i % 10), disks=[{"drive": "D", "free_gb": 1650.0 - i / 100}]))
    m.add(snap(T0 + 120, disks=[{"drive": "D", "free_gb": 1600.0}]))   # D: lost 50 GB over the days
    m.event("away_start", "sol-away-27b", "", T0 - 10 * 3600)
    m.event("away_end", "", "queue done", T0 - 7 * 3600)
    m.event("job_done", "evals", "", T0 - 9 * 3600)
    m.event("job_failed", "pick", "", T0 - 8 * 3600)
    m.event("chain", "Code review", "finished", T0 - 8 * 3600)
    m.event("crash", "crash", "1 since the last review", T0 - 3600)
    m.close()
    day = metrics.series(path, 24, now=T0 + 60)
    assert set(day) == {"t", *metrics.COLS} and 200 < len(day["t"]) <= 720      # 2-minute buckets
    assert all(T0 - 86400 - 120 <= t <= T0 + 120 for t in day["t"])
    week = metrics.series(path, 168, now=T0 + 60)
    assert len(week["t"]) <= 720 and week["t"] == sorted(week["t"])
    w = metrics.week(path, now=T0 + 60)
    assert w["away_hours"] == 3.0 and w["away_runs"] == 1 and w["jobs_done"] == 1 and w["jobs_failed"] == 1
    assert w["chains"] == {"finished": 1} and w["crashes"] == 1 and w["vram_max"] == 9.0
    assert w["disks"]["D"] < -40                                     # free space went down over the 3 days
    assert metrics.week(tmp_path / "none.sqlite")["away_runs"] == 0   # no file yet: an empty week, no error


def test_events_from_the_logs(tmp_path):
    away, chains = tmp_path / "away.jsonl", tmp_path / "chains.log"
    away.write_text(""); chains.write_text("")
    m = metrics.Metrics(tmp_path / "m.sqlite", away, chains)
    with open(away, "a") as f:
        f.write(json.dumps({"time": "2026-09-26T02:57:25", "event": "away-start", "model": "sol-away-27b"}) + "\n")
        f.write(json.dumps({"time": "2026-09-26T03:14:10", "event": "job-done", "job": "evals"}) + "\n")
    with open(chains, "a") as f:
        f.write("2026-09-26T05:00:00 Code review.md: run 16 succeeded\n")
    m.read_logs()
    kinds = [r[0] for r in m.db.execute("SELECT kind FROM events ORDER BY t")]
    assert kinds == ["away_start", "job_done", "chain"]


def test_old_rows_are_pruned(tmp_path):
    clock = {"t": T0}
    m = metrics.Metrics(tmp_path / "m.sqlite", clock=lambda: clock["t"])
    old = T0 - 100 * 86400
    m.add(snap(old)); m.add(snap(old + 61))
    m.event("chain", "x", "finished", old)
    clock["t"] = T0
    m.last_prune = 0
    m.add(snap(T0)); m.add(snap(T0 + 61))                           # a flush triggers the daily prune
    assert m.db.execute("SELECT COUNT(*) FROM minutes WHERE t < ?", (T0 - 90 * 86400,)).fetchone()[0] == 0
    assert m.db.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_first_start_backfills_the_week_from_the_logs(tmp_path):
    away, chains = tmp_path / "away.jsonl", tmp_path / "chains.log"
    away.write_text("\n".join(json.dumps(e) for e in [
        {"time": "2026-09-26T02:57:25", "event": "away-start"}, {"time": "2026-09-26T03:14:10", "event": "job-done", "job": "a"},
        {"time": "2026-09-26T12:24:07", "event": "away-end", "reason": "stopped by you"}]) + "\n")
    chains.write_text("2026-09-26T12:28:05 Code review.md: run 15 cancelled: cancelled by user\n")
    now = time.mktime(time.strptime("2026-09-26T20:00:00", "%Y-%m-%dT%H:%M:%S"))
    m = metrics.Metrics(tmp_path / "m.sqlite", away, chains, clock=lambda: now)
    m.close()
    w = metrics.week(tmp_path / "m.sqlite", now=now)
    assert w["away_runs"] == 1 and w["jobs_done"] == 1 and w["chains"] == {"cancelled": 1} and w["away_hours"] == 9.4   # 02:57 -> 12:24
    m2 = metrics.Metrics(tmp_path / "m.sqlite", away, chains, clock=lambda: now)   # second start: no double count
    m2.close()
    assert metrics.week(tmp_path / "m.sqlite", now=now)["jobs_done"] == 1
