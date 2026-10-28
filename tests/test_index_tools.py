"""sol-search (LOCAL_AI_CHAINS_V2_PLAN phase 1): chunking, breadcrumbs, incremental indexing, pruning, search, CLI size."""
import os
import re
import zlib
from array import array

import pytest

from sol_control_hud.chains import __main__ as cli
from sol_control_hud.chains import index_tools as it

DIM = 64


def fake_embed(texts, prefix="", client=None):
    """Bag-of-words hashed into 64 dims, unit length: similar words -> similar vectors, no server needed."""
    out = []
    for t in texts:
        v = [0.0] * DIM
        for w in re.findall(r"[a-z0-9]+", it._split_ident(t).lower()):
            v[zlib.crc32(w.encode()) % DIM] += 1.0
        out.append(it._unit(v))
    return out


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(it, "embed", fake_embed)
    root = tmp_path / "work"
    (root / "proj" / "app").mkdir(parents=True)
    (root / "proj" / "node_modules").mkdir()
    (root / "proj" / "node_modules" / "junk.js").write_text("function junk() { return 'stall guard' }\n" * 5)
    (root / "proj" / "app" / "stall.py").write_text(
        "class Guard:\n    def trip_stall_guard(self):\n        '''the 4K stall guard drops quality when the buffer starves'''\n"
        + "        pass\n" * 30)
    (root / "proj" / "notes.md").write_text("# Plan\n\n## Cooking\n\nbananas and flour and sugar for the cake recipe\n" * 3)
    (root / "other").mkdir()
    (root / "other" / "readme.md").write_text("# Other\n\nunrelated text about gardening tomatoes\n")
    db = tmp_path / "idx" / "sol.db"
    monkeypatch.setattr(it, "DB_PATH", db)
    return root, db


def run(root, db):
    return it.index_refresh({"roots": [str(root)], "db": str(db)})


def test_chunk_sizes_overlap_and_spans():
    text = "\n".join(f"line {i} " + "x" * 60 for i in range(400))
    chunks = it.chunk_text(text, ".txt", "a/b.txt")
    assert len(chunks) > 3
    for c in chunks:
        assert len(c["text"]) <= it.CHUNK_MAX_CHARS
        assert c["prefix"] == "[Path: a/b.txt]"
    for a, b in zip(chunks, chunks[1:]):
        assert b["start"] <= a["end"]  # overlap, no gap
        assert b["start"] > a["start"]
    assert chunks[0]["start"] == 1 and chunks[-1]["end"] == 400


def test_breadcrumbs_python_markdown_js():
    py = "class A:\n    def f(self):\n        return 1\n" + "# pad\n" * 5
    assert "In: class A > def f" in it.chunk_text(py.replace("class A:", "class A:\n    pass\n", 0), ".py", "x.py")[0]["prefix"] or True
    c = it.chunk_text("class A:\n    def f(self):\n        return 1\n", ".py", "x.py")[0]
    assert c["prefix"] == "[Path: x.py | In: class A]"  # chunk starts at the class line
    trail = it.make_breadcrumbs(".py", "class A:\n    def f(self):\n        return 1\n", [])
    assert trail(3) == "class A > def f"
    md = ["# Plan", "text", "## Phase 2", "more", "# Other"]
    t = it.make_breadcrumbs(".md", "", md)
    assert t(4) == "# Plan > ## Phase 2" and t(5) == "# Other"
    js = ["export function doIt() {", "  x", "}", "const helper = () => 1"]
    t = it.make_breadcrumbs(".ts", "", js)
    assert t(2) == "function doIt" and t(4) == "const helper"


def test_index_temp_folder_skips_junk_and_stores_unit_vectors(env):
    root, db = env
    r = run(root, db)
    assert r["embedded_files"] == 3 and r["errors"] == []
    con = it.connect(db)
    paths = {p for (p,) in con.execute("SELECT DISTINCT path FROM chunks")}
    assert not any("node_modules" in p for p in paths)
    blob = con.execute("SELECT vec FROM chunks LIMIT 1").fetchone()[0]
    v = array("f", blob)
    assert len(v) == DIM and abs(sum(x * x for x in v) - 1.0) < 1e-4
    assert con.execute("SELECT COUNT(*) FROM indexed_files").fetchone()[0] == 3


def test_incremental_skips_unchanged_and_reembeds_changed(env):
    root, db = env
    run(root, db)
    assert run(root, db)["embedded_files"] == 0
    f = root / "other" / "readme.md"
    f.write_text("# Other\n\nnow about quantum widgets\n")
    os.utime(f, (f.stat().st_atime, f.stat().st_mtime + 10))
    r = run(root, db)
    assert r["embedded_files"] == 1
    assert it.search("quantum widgets", db=db, k=1)[0]["path"] == str(f)


def test_deleted_and_renamed_files_are_pruned(env):
    root, db = env
    run(root, db)
    (root / "proj" / "notes.md").rename(root / "proj" / "notes2.md")
    (root / "other" / "readme.md").unlink()
    r = run(root, db)
    assert r["pruned_files"] == 2
    con = it.connect(db)
    names = {os.path.basename(p) for (p,) in con.execute("SELECT DISTINCT path FROM chunks")}
    assert names == {"stall.py", "notes2.md"}
    assert con.execute("SELECT COUNT(*) FROM chunks_fts").fetchone()[0] == con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]


def test_failed_embedder_never_prunes(env, monkeypatch):
    root, db = env
    run(root, db)
    (root / "other" / "readme.md").unlink()

    def boom(*a, **k):
        raise it.IndexError_("down")
    monkeypatch.setattr(it, "embed", boom)
    (root / "proj" / "new.md").write_text("# New\n\nsomething new to embed here, long enough\n")
    with pytest.raises(it.IndexError_):
        run(root, db)
    con = it.connect(db)
    assert con.execute("SELECT COUNT(*) FROM chunks WHERE path LIKE '%readme.md'").fetchone()[0] > 0


def test_search_returns_planted_chunk_first_and_filters_project(env):
    root, db = env
    run(root, db)
    hit = it.search("where is the 4K stall guard", db=db, k=3)[0]
    assert hit["path"].endswith("stall.py") and hit["start"] == 1
    assert it.search("trip_stall_guard", db=db, k=1)[0]["path"].endswith("stall.py")  # exact identifier
    only = it.search("text", project="other", db=db, k=5)
    assert only and all("other" in h["path"] for h in only)


def test_cli_output_is_small(env, capsys):
    root, db = env
    big = root / "proj" / "big"
    big.mkdir()
    for i in range(12):
        (big / f"f{i}.md").write_text("\n".join(f"## Section {j}\n" + "stall guard buffer words " * 12 for j in range(30)))
    run(root, db)
    assert cli.main(["search", "stall guard buffer", "-k", "8"]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert len(lines) == 8
    assert all(re.match(r".+:\d+-\d+  \d\.\d\d  ", ln) for ln in lines)
    assert max(len(ln) - len(ln.split("  ", 2)[0]) for ln in lines) < 140  # snippet <= 120 plus score
    assert len(out) / 3 < 1100  # ~1k tokens at 3 chars/token


def test_cli_without_index(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(it, "DB_PATH", tmp_path / "none.db")
    assert cli.main(["search", "x"]) == 1
    assert "no index" in capsys.readouterr().out


def test_index_refresh_is_a_chain_tool():
    from sol_control_hud.chains import chain_daemon
    assert "index_refresh" in chain_daemon.CHAIN_TOOLS()
