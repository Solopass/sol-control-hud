"""Ticker: what needs you first, trend colors, chain waits + schedule, the last Away session (2026-09-26)."""
import json
import os
import time
from datetime import datetime

from sol_control_hud.data import snapshot as td
from sol_control_hud.chains.chain_daemon import next_occurrence, parse_schedule
from sol_control_hud.data.snapshot import AMBER, CYAN, GREEN, MUTED, RED, TEXT, Snapshot, format_multiline_rows, format_slides

OK = dict(ai_state="sleeping", ai_model="sol-fast", services={"Router": True, "Embed": True, "HUD": True, "Chains": True},
          sampled_at=1.0)


def slide(s, tag):
    return next((x for x in format_slides(s) if x["tag"] == tag), None)


def test_disk_space_trend_colors():
    hist = [(0.0, {"C": 500.0, "D": 1650.0, "E": 900.0}), (1800.0, {"C": 500.2, "D": 1642.0, "E": 905.0}),
            (3000.0, {"C": 500.1, "D": 1640.5, "E": 906.4})]
    trends = td.disk_trends_from(hist, now=3000.0)
    assert trends == {"C": 0.1, "D": -9.5, "E": 6.4}
    assert td.disk_trends_from(hist[:1], now=100.0) == {}                 # under 10 min of history: no trend yet
    disks = [{"drive": "C", "free_gb": 500.1, "percent": 50}, {"drive": "D", "free_gb": 1640.5, "percent": 18},
             {"drive": "E", "free_gb": 906.4, "percent": 55}]
    s = Snapshot(disks=disks, disk_trends=trends, **OK)
    d = slide(s, "DISK")
    colors = {t.split(":")[0]: c for t, c in d["segments"] if ":" in t}
    assert colors == {"C": TEXT, "D": RED, "E": GREEN}                    # red = filling up, green = freed
    assert "D: 1640G ▼9.5" in d["text"] and "E: 906G ▲6.4" in d["text"]
    assert d["level"] == "info"
    assert slide(Snapshot(disks=disks, **OK), "DISK")["level"] == "quiet"   # nothing changing: not in the rotation
    assert td.disk_color({"free_gb": 12, "percent": 97}, None) == RED       # nearly full is red regardless


def test_attention_lists_what_needs_you_worst_first():
    assert td.attention(Snapshot(**OK)) == []
    s = Snapshot(**{**OK, "gpu_resets": 1, "vram_evicted": True, "vram_spill_impact": "slow", "backup_stale": True,
                    "chain_last_finished": "Weekly digest (failed)"})
    got = td.attention(s)
    assert got[0] == (RED, "1 new crash event: Away blocked until reviewed", "SYS")
    assert (RED, "model spilled out of VRAM: answers slow", "HW") in got
    assert got[-2:] == [(AMBER, "chain failed: Weekly digest", "RUN"), (AMBER, "WSL backup is stale", "SYS")]
    alert = slide(s, "ALERT")
    assert alert["level"] == "alert" and alert["color"] == RED and alert["target"] == "SYS"


def test_rotation_puts_alerts_first_and_skips_quiet_slides():
    slides = [{"tag": "HW", "level": "info"}, {"tag": "AI", "level": "info"}, {"tag": "NET", "level": "quiet"},
              {"tag": "RUN", "level": "info"}, {"tag": "ALERT", "level": "alert"}]
    assert td.rotation(slides) == [4, 0, 4, 1, 4, 3]
    calm = [dict(sl) for sl in slides[:4]]
    assert td.rotation(calm) == [0, 1, 3]                                  # quiet NET left out
    few = [{"tag": "HW", "level": "info"}, {"tag": "NET", "level": "quiet"}]
    assert td.rotation(few) == [0, 1]                                      # but never fewer than 3 if it can help


def test_parts_are_colored_by_meaning():
    s = Snapshot(**{**OK, "ram_percent": 94.0, "ram_used_gb": 90.0, "gpu_temp": 70, "vram_used_gb": 6.0,
                    "vram_total_gb": 16.0, "backup_age_h": 3.0, "note_exists": True, "note_words": 300})
    hw = dict((t.split(" ")[0], c) for t, c in slide(s, "HW")["segments"] if t.strip() and c != MUTED)
    assert hw["RAM"] == RED and hw["GPU"] == TEXT
    ai = slide(s, "AI")["segments"]
    assert ai[0] == ("sol-fast (sleeping)", CYAN) and ai[-1] == (":11440", MUTED)
    sys_ = dict(slide(s, "SYS")["segments"])
    assert sys_["WSL Backup: 3.0h ago"] == GREEN
    assert sys_["Stability: no new crashes, Away allowed"] == GREEN
    assert dict(slide(s, "NOTE")["segments"])["Today: 300 words"] == GREEN
    hw_row = next(r for r in format_multiline_rows(s) if r["title"] == "HARDWARE")
    assert ("RAM", "90.0/0G", RED) in hw_row["items"]


def test_chain_waits_and_next_schedule(tmp_path):
    f = tmp_path / "chains.json"
    f.write_text(json.dumps({"pending": [{"chain": "Code review", "lane": "away", "blocked": "waits for Away mode"}],
                             "scheduled": [{"chain": "Weekly digest", "next": "2026-09-27T20:00"},
                                           {"chain": "Code review", "next": "on away"}]}))
    pending, nxt = td.read_chains_extra(f, now=datetime(2026, 9, 26, 14, 0))
    assert pending == [{"chain": "Code review", "blocked": "waits for Away mode"}]
    assert nxt == "Weekly digest tomorrow 20:00"
    s = Snapshot(chain_pending=pending, chain_pending_count=1, chain_next=nxt,
                 chain_last_finished="Code review (cancelled)", **OK)
    run = slide(s, "RUN")
    assert run["text"] == ("Chains: Code review waits for Away mode  ·  next: Weekly digest tomorrow 20:00"
                           "  ·  Last: Code review (cancelled)")                  # the news first
    colors = dict(run["segments"])
    assert colors["Last: Code review (cancelled)"] == AMBER and colors["Chains: Code review waits for Away mode"] == AMBER
    sched = parse_schedule("weekly Sun 20:00")
    assert next_occurrence(sched, datetime(2026, 9, 26, 14, 0)) == datetime(2026, 9, 27, 20, 0)
    assert next_occurrence(sched, datetime(2026, 9, 27, 20, 0)) == datetime(2026, 10, 4, 20, 0)   # strictly after


def test_last_away_session_summary(tmp_path):
    away = tmp_path / "away.jsonl"
    rows = [{"time": "2026-09-26T02:57:25", "event": "away-start"},
            {"time": "2026-09-26T03:14:10", "event": "job-done", "job": "hard-120b"},
            {"time": "2026-09-26T03:20:00", "event": "job-failed", "job": "evals"},
            {"time": "2026-09-26T03:40:00", "event": "job-done", "job": "evals"},        # retried and finished
            {"time": "2026-09-26T04:11:39", "event": "job-failed", "job": "pick"},
            {"time": "2026-09-26T12:24:07", "event": "away-end", "reason": "stopped by you"}]
    away.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    chains = tmp_path / "chains.log"
    chains.write_text("2026-09-26T05:00:00 Code review.md: run 16 succeeded\n2026-09-26T13:00:00 X.md: run 1 failed\n")
    now = datetime(2026, 9, 26, 13, 0).timestamp()
    n = td.night_summary(away, chains, now=now)
    assert n == {"start": "02:57", "end": "12:24", "end_iso": "2026-09-26T12:24:07", "done": 2, "failed": ["pick"],
                 "chains": ["Code review (finished)"], "reason": "stopped by you"}
    sl = slide(Snapshot(night=n, **OK), "NIGHT")
    assert sl["text"] == ("Last Away 02:57–12:24: 2 jobs ✓  ·  1 ✗ (pick)  ·  Code review (finished)"
                          "  ·  ended: stopped by you")
    assert td.night_summary(away, chains, now=now + 19 * 3600) is None     # old news after 18 h


def test_segment_label_cuts_long_text_with_an_ellipsis():
    import tkinter as tk
    from tkinter import font as tkfont
    from sol_control_hud.views.ticker import SegmentLabel
    root = tk.Tk(); root.withdraw()
    try:
        f = tkfont.Font(family="Segoe UI", size=9)
        lbl = SegmentLabel(root, f, bg="#000000", fg="#ffffff")
        lbl.set_segments([("GPU 24%", "#e2e8f0"), ("  ·  ", "#94a3b8"), ("VRAM 6.5/16G " * 20, "#f87171")], max_px=200)
        texts = [lbl.itemcget(i, "text") for i in lbl.find_all()]
        fills = [lbl.itemcget(i, "fill") for i in lbl.find_all()]
        assert texts[0] == "GPU 24%" and texts[-1].endswith("…") and fills[-1] == "#f87171"
        assert sum(f.measure(t) for t in texts) <= 200 + f.measure("…")
        lbl.configure(text="plain", fg="#123456")
        assert lbl.cget("text") == "plain" and lbl.cget("fg") == "#123456"
    finally:
        root.destroy()


def test_router_port_is_not_probed_twice(monkeypatch):
    asked = []
    monkeypatch.setattr(td, "check_port", lambda port, *a, **k: asked.append(port) or True)
    monkeypatch.setattr(td, "chain_runner_alive", lambda *a, **k: True)
    monkeypatch.setattr(td, "wsl_running", lambda: False)
    out = td.probe_services(router_up=False)
    assert out["Router"] is False and 11440 not in asked and set(asked) == {11443, 7900}


def test_short_trends_and_their_colors(tmp_path, monkeypatch):
    assert td.rise([(0, 5.0), (300, 6.5), (590, 7.2)], now=600, window=600) == 2.2      # latest minus the lowest
    assert td.rise([(0, 5.0)], now=10, window=600) is None                              # one sample: no trend yet
    assert td.rise([(0, 9.0), (500, 5.0), (590, 6.0)], now=600, window=50) is None      # only one inside 50 s
    hw = lambda s: dict((t.split(" ")[0], c) for t, c in slide(s, "HW")["segments"] if t.strip() and c != MUTED)  # noqa: E731
    base = {**OK, "vram_used_gb": 9.0, "vram_total_gb": 16.0, "gpu_temp": 62, "ai_mode": "desk"}
    assert hw(Snapshot(**base))["VRAM"] == TEXT
    assert hw(Snapshot(**{**base, "vram_rise_gb": 1.4}))["VRAM"] == AMBER                # growing at the desk
    assert hw(Snapshot(**{**base, "vram_rise_gb": 1.4, "ai_mode": "away"}))["VRAM"] == TEXT   # Away loads models: normal
    assert hw(Snapshot(**{**base, "temp_rise_c": 12}))["GPU"] == AMBER


def test_old_uncommitted_work_turns_red(tmp_path):
    repo = tmp_path / "repo"; repo.mkdir()
    old, new = repo / "old.py", repo / "new.py"
    old.write_text("x"); new.write_text("y")
    now = time.time()
    os.utime(old, (now - 5 * 86400, now - 5 * 86400))
    assert td.dirty_age_days(repo, " M old.py\n?? new.py\n D gone.py\n", now=now) == 5.0
    git = lambda days: slide(Snapshot(git_dirty_count=1, git_dirty_repos=["media-api"], git_total_repos=13,  # noqa: E731
                                      git_dirty_days={"media-api": days}, **OK), "GIT")
    assert git(5.0)["text"].endswith("oldest 5 days") and dict(git(5.0)["segments"])[git(5.0)["text"].split("  ·  ")[0]] == RED
    assert dict(git(1.0)["segments"])["1 repos dirty (media-api)"] == AMBER


def test_daily_note_reminder_after_eight(monkeypatch):
    note = lambda: dict(slide(Snapshot(note_exists=True, note_words=40, **OK), "NOTE")["segments"])["Today: 40 words"]  # noqa: E731
    monkeypatch.setattr(td.time, "localtime", lambda *a: time.struct_time((2026, 9, 26, 21, 0, 0, 5, 269, 0)))
    assert note() == AMBER
    monkeypatch.setattr(td.time, "localtime", lambda *a: time.struct_time((2026, 9, 26, 14, 0, 0, 5, 269, 0)))
    assert note() == TEXT


# ---- 2026-10-07: answer speed on the AI slide, and a warning when the card is nearly full
def test_ai_slide_shows_answer_speed_colored_by_health():
    from sol_control_hud.data.snapshot import GREEN, RED, Snapshot, colorize, format_slides
    s = Snapshot(ai_model="sol-fast", ai_state="loaded", ai_tps=71.4)
    ai = next(x for x in format_slides(s) if x["tag"] == "AI")
    assert ai["text"].endswith("71 tok/s")
    assert dict(colorize(ai, s))["71 tok/s"] == GREEN
    s = Snapshot(ai_model="sol-fast", ai_state="loaded", ai_tps=26.0, ai_tps_slow=True)
    ai = next(x for x in format_slides(s) if x["tag"] == "AI")
    assert dict(colorize(ai, s))["26 tok/s"] == RED
    s = Snapshot(ai_model="sol-fast", ai_state="loaded")                    # no recent answer: the port, as before
    assert next(x for x in format_slides(s) if x["tag"] == "AI")["text"].endswith(":11440")


def test_card_nearly_full_warning_names_the_biggest_app():
    from sol_control_hud.data.snapshot import AMBER, Snapshot, attention
    slow = Snapshot(ai_mode="desk", ai_state="loaded", ai_tps=26.0, ai_tps_slow=True, vram_card_full=True,
                    vram_used_gb=12.3, vram_hog="brave 1.4 GB")
    assert (AMBER, "AI slowed (26 tok/s): card 12.3 GB · brave 1.4 GB", "AI") in attention(slow)
    full = Snapshot(ai_mode="desk", ai_state="sleeping", vram_card_full=True, vram_used_gb=11.8)
    assert (AMBER, "card nearly full (11.8 GB): the AI may slow", "AI") in attention(full)
    for quiet in (Snapshot(ai_mode="desk", ai_state="loaded", vram_used_gb=9.5),           # room on the card
                  Snapshot(ai_mode="off", ai_state="unloaded", vram_card_full=True, vram_used_gb=12.0)):   # a game
        assert not [a for a in attention(quiet) if "card" in a[1]]
