"""Ticker: what needs you first, trend colors, chain waits + schedule, the last Away session (2026-09-26)."""
import json
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
    s = Snapshot(**{**OK, "gpu_resets": 1, "vram_evicted": True, "backup_stale": True,
                    "chain_last_finished": "Weekly digest (failed)"})
    got = td.attention(s)
    assert got[0] == (RED, "1 new crash event: Away blocked until reviewed", "SYS")
    assert (RED, "model spilled out of VRAM (slow)", "HW") in got
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
