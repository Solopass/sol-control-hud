"""The "From your AI" card: parsing, ticking a task, and that the page never names a path."""
import os
import time

from sol_control_hud.data.collectors import ai_inbox as ai

DIGEST = """---
made_by: sol-chain
date: '2026-11-15'
---
## What happened
- stuff

## Decisions
None noted.

## Open loops
- Figure out the iPad.  
- Try Gemini. [src]

## Next week
- Start on HEALPix. [Add a HEALPix index.]
- Begin the audit. [Finish SAMP.]
"""
BRIEFING = """---
date: 2026-11-16
---
# Briefing

## Focus
It is a quiet day.
Do [[plans/X|the plan]].

## Needs attention
- **WARN** VRAM headroom low

## Open work
- [[plans/Y]]
"""
TASKS = "# Tasks\n\n- [ ] one\n- [x] done\n- [ ] two\n- [ ] one\n"
BUGS = "---\nstatus: done\n---\n## First pass\n* x\n\n## Real issues\n**[high]** `a.py`: bad\n- **[low]** `b.py`: meh\n\n## Next\nz\n"


def _setup(tmp_path):
    dg = tmp_path / "Digests"
    dg.mkdir()
    (dg / "2026-W45.md").write_text(DIGEST.replace("iPad", "OLD"), encoding="utf-8")
    (dg / "2026-W46.md").write_text(DIGEST, encoding="utf-8")
    (dg / "2026-W47 machine.md").write_text("## Next week\n- machine\n", encoding="utf-8")
    br = tmp_path / "Briefings"
    br.mkdir()
    (br / "2026-11-15_Briefing.md").write_text("## Focus\nold", encoding="utf-8")
    (br / "2026-11-16_Briefing-2.md").write_text(BRIEFING, encoding="utf-8")
    old = time.time() - 3600
    os.utime(br / "2026-11-15_Briefing.md", (old, old))          # newest by mtime wins
    tk = tmp_path / "Tasks.md"
    tk.write_text(TASKS, encoding="utf-8")
    bh = tmp_path / "Bug hunt (result).md"
    bh.write_text(BUGS, encoding="utf-8")
    chains = tmp_path / "Chains"
    chains.mkdir()
    (chains / "Broken.md").write_text("---\nstatus: needs-you\nreason: no files found\n---\nx", encoding="utf-8")
    (chains / "Fine.md").write_text("---\nstatus: done\n---\nx", encoding="utf-8")
    return dict(digests=dg, tasks=tk, briefings=br, bughunt=bh, chains_dir=chains)


def test_digest_sections_skip_machine_file_and_strip_brackets(tmp_path):
    d = ai.collect(**_setup(tmp_path))["digest"]
    assert d["name"] == "2026-W46" and d["date"] == "2026-11-15"
    assert d["next"] == ["Start on HEALPix.", "Begin the audit."]
    assert d["loops"] == ["Figure out the iPad.", "Try Gemini."]


def test_briefing_focus_and_attention(tmp_path):
    b = ai.collect(**_setup(tmp_path))["briefing"]
    assert b["name"] == "2026-11-16_Briefing-2"
    assert b["focus"] == "It is a quiet day. Do the plan."
    assert b["attention"] == ["WARN VRAM headroom low"]


def test_tasks_open_only_capped(tmp_path):
    kw = _setup(tmp_path)
    t = ai.collect(**kw)["tasks"]
    assert [x["text"] for x in t["open"]] == ["one", "two", "one"] and t["more"] == 0
    kw["tasks"].write_text("\n".join(f"- [ ] t{i}" for i in range(11)), encoding="utf-8")
    t = ai.collect(**kw)["tasks"]
    assert len(t["open"]) == 8 and t["more"] == 3


def test_bug_hunt_real_issues_and_freshness(tmp_path):
    kw = _setup(tmp_path)
    assert ai.collect(**kw)["bughunt"]["items"] == ["[high] a.py: bad", "[low] b.py: meh"]
    old = time.time() - 15 * 86400
    os.utime(kw["bughunt"], (old, old))
    assert "bughunt" not in ai.collect(**kw)


def test_needs_you_lists_chains(tmp_path):
    n = ai.collect(**_setup(tmp_path))["needs"]["chains"]
    assert n == [{"name": "Broken", "status": "needs you", "reason": "no files found"}]


def test_missing_files_are_just_absent(tmp_path):
    d = ai.collect(tmp_path / "a", tmp_path / "t.md", tmp_path / "b", tmp_path / "bh.md", tmp_path / "c")
    assert d == {"available": True}


def test_tick_only_the_exact_open_line(tmp_path):
    kw = _setup(tmp_path)
    p = kw["tasks"]
    lines = TASKS.split("\n")
    idx = [i for i, ln in enumerate(lines) if ln == "- [ ] one"]
    assert ai.tick_task("one", idx[1], p)["ok"]
    out = p.read_text(encoding="utf-8").split("\n")
    assert out[idx[0]] == "- [ ] one" and out[idx[1]] == "- [x] one"
    assert ai.tick_task("one", idx[1], p)["ok"] is False        # already ticked
    assert ai.tick_task("done", 3, p)["ok"] is False            # a ticked line is not an open task
    assert ai.tick_task("wrong text", idx[0], p)["ok"] is False
    assert p.read_text(encoding="utf-8").count("[x]") == 2


def test_tick_keeps_crlf(tmp_path):
    p = tmp_path / "Tasks.md"
    p.write_bytes(b"# T\r\n- [ ] a\r\n- [ ] b\r\n")
    assert ai.tick_task("b", 2, p)["ok"]
    assert p.read_bytes() == b"# T\r\n- [ ] a\r\n- [x] b\r\n"


def test_tick_refuses_when_file_changed_meanwhile(tmp_path, monkeypatch):
    p = tmp_path / "Tasks.md"
    p.write_text("- [ ] a\n", encoding="utf-8")
    real_replace = os.replace
    orig_write = type(p).write_bytes

    def write_then_touch(self, data):          # Obsidian saves while we hold our copy
        orig_write(self, data)
        st = os.stat(p)
        os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    monkeypatch.setattr(type(p), "write_bytes", write_then_touch)
    monkeypatch.setattr(os, "replace", lambda *a: real_replace(*a))
    r = ai.tick_task("a", 0, p)
    assert r["ok"] is False
    assert p.read_text(encoding="utf-8") == "- [ ] a\n"
    assert not (tmp_path / "Tasks.md.tmp").exists()


def test_page_never_chooses_a_path(tmp_path):
    kw = _setup(tmp_path)
    opened = []
    for bad in (r"C:\Windows\win.ini", "../x", str(kw["tasks"]), "chain:../../etc", "chain:Nope", ""):
        assert ai.open_note(bad, opener=opened.append, **kw)["ok"] is False
    assert opened == []
    for good, name in (("digest", "2026-W46"), ("briefing", "2026-11-16_Briefing-2"), ("tasks", "Tasks"),
                       ("bughunt", "Bug hunt (result)"), ("chain:Broken", "Broken")):
        assert ai.open_note(good, opener=opened.append, **kw)["ok"] is True
        assert opened[-1].stem == name
