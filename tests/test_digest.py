"""E2: the machine's week as a note - when it's due, what it says, where it goes."""
import sqlite3
from datetime import datetime

from sol_control_hud import digest, metrics


def test_week_end_is_the_last_sunday_8pm():
    assert digest.week_end(datetime(2026, 10, 8, 18, 0)) == datetime(2026, 10, 4, 20, 0)     # Thursday
    assert digest.week_end(datetime(2026, 10, 11, 19, 59)) == datetime(2026, 10, 4, 20, 0)   # Sunday, not yet
    assert digest.week_end(datetime(2026, 10, 11, 20, 0)) == datetime(2026, 10, 11, 20, 0)   # Sunday 20:00 sharp
    assert digest.week_name(datetime(2026, 10, 4, 20, 0)) == "2026-W40"      # same naming as the Weekly digest chain


def test_due_once_a_week_and_not_on_the_very_first_start():
    state = {}
    assert digest.due(datetime(2026, 10, 8, 18, 0), state) is None and state == {"last": "2026-W40"}
    assert digest.due(datetime(2026, 10, 11, 19, 0), state) is None              # Sunday before 20:00
    end = digest.due(datetime(2026, 10, 11, 20, 1), state)
    assert end == datetime(2026, 10, 11, 20, 0)
    state["last"] = digest.week_name(end)
    assert digest.due(datetime(2026, 10, 12, 9, 0), state) is None                # written: not again
    assert digest.due(datetime(2026, 10, 20, 9, 0), {"last": "2026-W41"}) == datetime(2026, 10, 18, 20, 0)  # PC was off


def _db(tmp_path, end):
    db = tmp_path / "m.sqlite"
    m = metrics.Metrics(db, clock=lambda: end)
    day = 86400
    rows = [(end - 2 * day + i * 60, 30.0, 60.0, 8.0, 12.0, 20.0, 25.0, 5.0, 60, 70, 80, 95 if i == 3 else 85,
             1.0, 1.0, '{"E": 300.0}' if i else '{"E": 377.0}', 0.6, tps) for i, tps in enumerate([80.0, 80.0, 80.0, 30.0, 75.0])]
    rows.append((end - 9 * day, 10.0, 20.0, 5.0, 6.0, 20.0, 25.0, 5.0, 50, 60, 70, 99, 1.0, 1.0, '{"E": 400.0}', None, None))
    m.db.executemany("INSERT INTO minutes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    m.db.executemany("INSERT INTO events VALUES (?,?,?,?)", [
        (end - 3 * day, "away_start", "sol-away", ""), (end - 3 * day + 7200, "away_end", "sol-away", ""),
        (end - 3 * day + 100, "job_failed", "Transcribe lecture", "model gone"),
        (end - day, "chain", "Bug hunt", "failed"), (end - day, "chain", "Code review", "finished"),
        (end - day, "spill", "slow", "1.40 GB, 4 min"), (end - day, "spill", "fine", "1.10 GB, 1 min")])
    m.db.commit()
    m.db.close()
    return db


def test_the_note_says_what_the_week_was(tmp_path):
    end = datetime(2026, 10, 11, 20, 0)
    db = _db(tmp_path, end.timestamp())
    text = digest.build(db, end, [{"drive": "E", "free_gb": 300.0, "percent": 90.0}])
    assert text.startswith("# 2026-W41: the machine's week")
    assert "- Away: 2.0 h over 1 run" in text and "jobs 0 done, 1 failed" in text
    assert "  - failed: Transcribe lecture (model gone)" in text
    assert "- Chains: 2 runs, 1 failed (Bug hunt)." in text
    assert "Typical answer speed: 75 tok/s over 3 answers (slowest 30, fastest 80)" in text   # repeats counted once
    assert "VRAM spills: 2, of which 1 slowed answers." in text
    assert "Hottest GPU hotspot: 95 °C" in text                     # the 99 was the week before
    assert "- E: -77.0 GB free over the week; 300 GB free, about 27 days left at this rate." in text


def test_written_beside_the_chain_digest_atomically(tmp_path):
    end = datetime(2026, 10, 11, 20, 0)
    p = digest.write("# hi\n", end, tmp_path / "Digests")
    assert p.name == "2026-W41 machine.md" and p.read_text(encoding="utf-8") == "# hi\n"
    assert not list((tmp_path / "Digests").glob("*.tmp"))
    assert digest.build(tmp_path / "missing.sqlite", end).startswith("# 2026-W41")   # no history yet: still a note


def test_state_round_trip(tmp_path):
    f = tmp_path / "digest-state.json"
    assert digest.load_state(f) == {}
    digest.save_state({"last": "2026-W41"}, f)
    assert digest.load_state(f) == {"last": "2026-W41"}
