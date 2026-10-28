"""sol-search: a local semantic + keyword index of D:\\OBVLT, D:\\Workspace\\* and the Polymatica Vault, so Claude can find
the right file:lines first instead of grepping or exploring broadly (OBVLT plans/LOCAL_AI_CHAINS_V2_PLAN.md, phase 1).

Tool (chain tool step; run nightly by the chain `Index refresh`):
  index_refresh {roots=null, db=null} -> {files, embedded_files, chunks, pruned_files, errors, seconds}

Search: `python -m sol_control_hud.chains search "query" [--in project] [-k 8]`.

How it works:
- Files are cut into chunks of ~350-500 tokens (1050-1500 chars at 3 chars/token, as in file_tools) with a ~50-token
  overlap, each remembering its line span. A breadcrumb (`[Path: ... | In: class X > def y]`) is added in front of the
  text for embedding and keyword scoring only; the stored text is the plain file text.
- Vectors come from the CPU embedder (sol-embed, nomic, 768 dims) and are stored as unit-length float32 blobs in SQLite
  (D:\\AI\\Cache\\index\\sol.db), so cosine similarity is one dot product.
- Incremental by mtime. Every run stamps `last_seen` on each file it walks past; chunks of files not seen in a complete
  run are deleted (deleted/renamed/moved files drop out the same night).
- Search = cosine (numpy matrix, argpartition; pure-Python fallback if numpy can't be imported) + the SQLite FTS5 BM25
  score of the same chunks, so exact function and file names still match.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import math
import os
import re
import sqlite3
import time
from array import array
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

from . import file_tools

try:  # Application Control has blocked native DLLs on this PC before (sklearn); numpy is checked at runtime
    import numpy as np
except Exception:  # pragma: no cover - depends on the machine
    np = None

EMBED_URL = os.environ.get("SOL_EMBED_URL", "http://127.0.0.1:11443/v1/embeddings")
EMBED_MODEL = "sol-embed"
DB_PATH = Path(os.environ.get("SOL_INDEX_DB", r"D:\AI\Cache\index\sol.db"))
ROOTS = [r"D:\OBVLT", r"D:\Workspace", r"D:\Polymatica Vault"]
# never indexed: the user's private writing, and the frozen predecessor (a duplicate of sol-control-hud in every result)
EXCLUDE_DIRS = {os.path.normcase(r"D:\OBVLT\1Notebook"), os.path.normcase(r"D:\Workspace\sol-hud")}

CHARS_PER_TOKEN = 3.0
CHUNK_MAX_CHARS = int(500 * CHARS_PER_TOKEN)
OVERLAP_CHARS = int(50 * CHARS_PER_TOKEN)
MIN_CHUNK_CHARS = 20
MAX_FILE_BYTES = 400_000
EMBED_MAX_CHARS = 4500  # the embedder's context is 2048 tokens; stay far under it even for dense text

INDEX_EXT = {".md", ".txt", ".py", ".ps1", ".psm1", ".js", ".jsx", ".mjs", ".ts", ".tsx", ".sh", ".bat", ".cmd", ".rs", ".go",
             ".java", ".c", ".h", ".cpp", ".cs", ".sql", ".html", ".css", ".scss", ".yaml", ".yml", ".toml", ".ini", ".cfg",
             ".base"}
SKIP_DIRS = set(file_tools.DENY_DIRS) | {"dist", "build", "target", "out", ".next", "coverage", "vendor", "third_party",
                                         "site-packages", ".cache", ".parcel-cache", ".turbo", ".gradle", "bin", "obj"}


class IndexError_(Exception):
    pass


# ------------------------------------------------------------------ chunking
def _py_scopes(text: str) -> list[tuple[int, int, str]]:
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "def"
            out.append((node.lineno, getattr(node, "end_lineno", node.lineno), f"{kind} {node.name}"))
    return out


_JS_SYMBOL = re.compile(r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:(function\*?|class)\s+([A-Za-z_$][\w$]*)|"
                        r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=)")


def _js_symbols(lines: list[str]) -> list[tuple[int, str]]:
    out = []
    for i, ln in enumerate(lines, 1):
        m = _JS_SYMBOL.match(ln)
        if m:
            out.append((i, f"{(m.group(1) or 'const').rstrip('*')} {m.group(2) or m.group(3)}"))
    return out


def _md_headings(lines: list[str]) -> list[tuple[int, int, str]]:
    out, fence = [], False
    for i, ln in enumerate(lines, 1):
        if ln.lstrip().startswith("```"):
            fence = not fence
        m = None if fence else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", ln)
        if m:
            out.append((i, len(m.group(1)), f"{m.group(1)} {m.group(2)}"))
    return out


def make_breadcrumbs(ext: str, text: str, lines: list[str]):
    """-> function(line_no) giving the 'In:' trail for a chunk starting at that line ('' when there is none)."""
    if ext == ".py":
        scopes = _py_scopes(text)

        def trail(n):
            hit = sorted((s for s in scopes if s[0] <= n <= s[1]), key=lambda s: s[0])
            return " > ".join(s[2] for s in hit)
        return trail
    if ext in (".js", ".jsx", ".mjs", ".ts", ".tsx"):
        syms = _js_symbols(lines)

        def trail(n):
            last = ""
            for ln, name in syms:
                if ln > n:
                    break
                last = name
            return last
        return trail
    if ext in (".md", ".base"):
        heads = _md_headings(lines)

        def trail(n):
            stack: list[tuple[int, str]] = []
            for ln, level, title in heads:
                if ln > n:
                    break
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, title))
            return " > ".join(t for _, t in stack)
        return trail
    return lambda n: ""


def chunk_text(text: str, ext: str = "", rel: str = "") -> list[dict]:
    """-> [{start, end, text, prefix}] (1-based inclusive line spans). prefix is the breadcrumb, kept apart from text."""
    lines = text.splitlines()
    trail = make_breadcrumbs(ext.lower(), text, lines)
    # split very long lines so no piece exceeds the chunk size (they keep their line number)
    pieces: list[tuple[int, str]] = []
    for i, ln in enumerate(lines, 1):
        ln = ln.rstrip()
        if len(ln) <= CHUNK_MAX_CHARS:
            pieces.append((i, ln))
        else:
            pieces.extend((i, ln[k:k + CHUNK_MAX_CHARS]) for k in range(0, len(ln), CHUNK_MAX_CHARS))
    chunks, cur, size = [], [], 0

    def close():
        body = "\n".join(p for _, p in cur)
        if len(body.strip()) >= MIN_CHUNK_CHARS:
            n0 = next((n for n, p in cur if p.strip()), cur[0][0])
            where = trail(n0)
            prefix = f"[Path: {rel}" + (f" | In: {where}" if where else "") + "]"
            chunks.append({"start": cur[0][0], "end": cur[-1][0], "text": body, "prefix": prefix})

    for piece in pieces:
        add = len(piece[1]) + 1
        if cur and size + add > CHUNK_MAX_CHARS:
            close()
            keep, kept = [], 0
            for p in reversed(cur):  # ~50 tokens of overlap
                if kept + len(p[1]) + 1 > OVERLAP_CHARS:
                    break
                keep.insert(0, p)
                kept += len(p[1]) + 1
            cur, size = keep, kept
        cur.append(piece)
        size += add
    if cur and (not chunks or any(p[0] > chunks[-1]["end"] for p in cur)):  # skip a tail that is only overlap
        close()
    return chunks


# ------------------------------------------------------------------ embeddings
def _unit(vec: list[float]) -> bytes:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return array("f", (x / n for x in vec)).tobytes()


def _embed_one(client: httpx.Client, text: str) -> bytes:
    """One input per request: the embedder's physical batch is 512 tokens, so a dense chunk (code is ~2.5 chars/token) can
    overflow it and get a 500. Retry with the tail cut off; a vector of the first part is still a fine address."""
    err = None
    for _ in range(4):
        try:
            r = client.post(EMBED_URL, json={"model": EMBED_MODEL, "input": text})
            if r.status_code >= 500 and len(text) > 200:
                text, err = text[:len(text) // 2], r.text[:200]
                continue
            r.raise_for_status()
            return _unit(r.json()["data"][0]["embedding"])
        except (httpx.HTTPError, ValueError, KeyError, IndexError) as e:
            raise IndexError_(f"embedder at {EMBED_URL} failed: {e}") from e
    raise IndexError_(f"embedder at {EMBED_URL} rejected a chunk even when cut short: {err}")


def embed(texts: list[str], prefix: str = "search_document: ", client: httpx.Client | None = None) -> list[bytes]:
    """Unit-length float32 blobs, one per text (4 requests at a time: the embedder has 4 slots).
    nomic expects a task prefix on every input."""
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        jobs = [prefix + t[:EMBED_MAX_CHARS] for t in texts]
        if len(jobs) < 2:
            return [_embed_one(client, j) for j in jobs]
        with ThreadPoolExecutor(max_workers=4) as pool:
            return list(pool.map(lambda j: _embed_one(client, j), jobs))
    finally:
        if own:
            client.close()


# ------------------------------------------------------------------ database
SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, path TEXT NOT NULL, start_line INTEGER, end_line INTEGER,
  hash TEXT, prefix TEXT, text TEXT, vec BLOB);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS indexed_files (path TEXT PRIMARY KEY, mtime REAL, last_seen INTEGER);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body, tokenize='unicode61');
"""


def connect(db: Path | str | None = None) -> sqlite3.Connection:
    db = Path(db or DB_PATH)
    db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db), timeout=60)
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(SCHEMA)
    return con


def _split_ident(s: str) -> str:
    """snake_case and camelCase pieces as extra words, so 'stall guard' finds stallGuard / stall_guard."""
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", s.replace("_", " "))


def _fts_body(prefix: str, text: str) -> str:
    return f"{prefix}\n{_split_ident(prefix)}\n{text}\n{_split_ident(text)}"


# ------------------------------------------------------------------ walking
def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(".venv") or name.startswith("$") or name.startswith("~")


def _skip_file(name: str) -> bool:
    low = name.lower()
    if Path(low).suffix not in INDEX_EXT or low.startswith("$") or ".min." in low or name in file_tools.LOCK_FILES:
        return True
    return any(fnmatch.fnmatch(low, pat) for pat in file_tools.DENY_FILES)


def walk_files(roots: list[str]):
    for root in roots:
        for dirpath, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs if not _skip_dir(d) and os.path.normcase(os.path.join(dirpath, d)) not in EXCLUDE_DIRS]
            for f in files:
                if _skip_file(f):
                    continue
                p = os.path.join(dirpath, f)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                if 0 < st.st_size <= MAX_FILE_BYTES:
                    yield p, st.st_mtime


def _rel(path: str) -> str:
    return path[3:].replace("\\", "/") if path[1:3] == ":\\" else path.replace("\\", "/")


def _read(path: str) -> str | None:
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:4096]:
        return None
    return raw.decode("utf-8-sig", errors="replace")


# ------------------------------------------------------------------ indexing
def index_refresh(args: dict | None = None, ctx=None) -> dict:
    args = args or {}
    roots = [str(r) for r in (args.get("roots") or ROOTS)]
    t0 = time.time()
    con = connect(args.get("db"))
    run_id = (con.execute("SELECT COALESCE(MAX(last_seen), 0) FROM indexed_files").fetchone()[0] or 0) + 1
    seen = embedded_files = chunk_count = 0
    errors: list[str] = []
    client = httpx.Client(timeout=120)
    complete = all(os.path.isdir(r) for r in roots)
    try:
        for path, mtime in walk_files(roots):
            seen += 1
            row = con.execute("SELECT mtime FROM indexed_files WHERE path=?", (path,)).fetchone()
            if row and abs(row[0] - mtime) < 1e-6:
                con.execute("UPDATE indexed_files SET last_seen=? WHERE path=?", (run_id, path))
                continue
            text = _read(path)
            if text is None:
                continue
            chunks = chunk_text(text, Path(path).suffix, _rel(path))
            old = {r[0]: r[1] for r in con.execute("SELECT hash, vec FROM chunks WHERE path=?", (path,))}
            for c in chunks:
                c["hash"] = hashlib.sha1((c["prefix"] + "\n" + c["text"]).encode("utf-8", "replace")).hexdigest()
            todo = [c for c in chunks if c["hash"] not in old]
            try:
                vecs = dict(zip((c["hash"] for c in todo), embed([c["prefix"] + "\n" + c["text"] for c in todo], client=client)))
            except IndexError_ as e:
                errors.append(str(e))
                complete = False  # a partial walk must never prune
                break
            vecs.update(old)
            with con:
                ids = [r[0] for r in con.execute("SELECT id FROM chunks WHERE path=?", (path,))]
                con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in ids])
                con.execute("DELETE FROM chunks WHERE path=?", (path,))
                for c in chunks:
                    cur = con.execute("INSERT INTO chunks(path,start_line,end_line,hash,prefix,text,vec) VALUES(?,?,?,?,?,?,?)",
                                      (path, c["start"], c["end"], c["hash"], c["prefix"], c["text"], vecs[c["hash"]]))
                    con.execute("INSERT INTO chunks_fts(rowid, body) VALUES(?,?)", (cur.lastrowid, _fts_body(c["prefix"], c["text"])))
                con.execute("INSERT OR REPLACE INTO indexed_files(path,mtime,last_seen) VALUES(?,?,?)", (path, mtime, run_id))
            embedded_files += 1
            chunk_count += len(chunks)
            if ctx is not None and embedded_files % 50 == 0:
                ctx.note(f"indexed {embedded_files} changed file(s), {chunk_count} chunks so far")
        con.commit()
        pruned = 0
        if complete:
            with con:
                gone = [r[0] for r in con.execute("SELECT path FROM indexed_files WHERE last_seen != ?", (run_id,))]
                dead = [r[0] for r in con.execute("SELECT id FROM chunks WHERE path NOT IN "
                                                  "(SELECT path FROM indexed_files WHERE last_seen = :run_id)", {"run_id": run_id})]
                con.executemany("DELETE FROM chunks_fts WHERE rowid=?", [(i,) for i in dead])
                con.execute("DELETE FROM chunks WHERE path NOT IN (SELECT path FROM indexed_files WHERE last_seen = :run_id)",
                            {"run_id": run_id})
                con.execute("DELETE FROM indexed_files WHERE last_seen != ?", (run_id,))
                pruned = len(gone)
    finally:
        client.close()
        con.close()
    result = {"files": seen, "embedded_files": embedded_files, "chunks": chunk_count, "pruned_files": pruned,
              "errors": errors, "seconds": round(time.time() - t0, 1)}
    if ctx is not None:
        ctx.note(f"index refresh: {seen} files seen, {embedded_files} re-embedded ({chunk_count} chunks), {pruned} pruned")
    if errors:
        raise IndexError_(errors[0])
    return result


TOOLS = {"index_refresh": index_refresh}


# ------------------------------------------------------------------ search
_STOP = set("a an the of in on at to for and or is are was were be do does did how where what which who why when with from by "
            "it its this that i me my we our you your can could should would about into as if then than not no".split())
_cache: dict = {}


def _load(db: Path) -> dict:
    key = (str(db), db.stat().st_mtime_ns, (db.with_name(db.name + "-wal").stat().st_mtime_ns
                                            if db.with_name(db.name + "-wal").exists() else 0))
    if _cache.get("key") == key:
        return _cache
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        ids, vecs = [], []
        for cid, blob in con.execute("SELECT id, vec FROM chunks ORDER BY id"):
            ids.append(cid)
            vecs.append(blob)
        paths = {cid: p for cid, p in con.execute("SELECT id, path FROM chunks")}
    finally:
        con.close()
    dim = len(vecs[0]) // 4 if vecs else 0
    good = [i for i, b in enumerate(vecs) if len(b) == dim * 4]
    ids = [ids[i] for i in good]
    if np is not None and ids:
        matrix = np.frombuffer(b"".join(vecs[i] for i in good), dtype=np.float32).reshape(len(ids), dim)
    else:
        matrix = [array("f", vecs[i]) for i in good]
    _cache.clear()
    _cache.update(key=key, ids=ids, matrix=matrix, paths=paths, dim=dim)
    return _cache


def _terms(query: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]+", _split_ident(query))
    seen, out = set(), []
    for w in words:
        w = w.lower()
        if w not in _STOP and len(w) > 1 and w not in seen:
            seen.add(w)
            out.append(w)
    return out


def _keyword_scores(con: sqlite3.Connection, terms: list[str], limit: int = 200) -> dict[int, float]:
    if not terms:
        return {}
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        rows = con.execute("SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY 2 LIMIT ?",
                           (match, limit)).fetchall()
    except sqlite3.Error:
        return {}
    best = max((-s for _, s in rows), default=0)
    return {rid: (-s / best if best > 0 else 0.0) for rid, s in rows}


def _in_project(path: str, project: str) -> bool:
    return project.lower() in {p.lower() for p in Path(path).parts[1:]}


def _snippet(text: str, terms: list[str]) -> str:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    pick = next((ln for ln in lines if any(t in ln.lower() for t in terms)), lines[0] if lines else "")
    return pick if len(pick) <= 120 else pick[:117] + "..."


def search(query: str, project: str | None = None, k: int = 8, db: Path | str | None = None,
           semantic_weight: float = 1.0, keyword_weight: float = 0.3, per_file: int = 2) -> list[dict]:
    """-> [{path, start, end, score, snippet}] best first."""
    db = Path(db or DB_PATH)
    if not db.exists():
        raise IndexError_(f"no index at {db}; build it with: python -m sol_control_hud.chains index")
    data = _load(db)
    if not data["ids"]:
        return []
    q = embed([query], prefix="search_query: ")[0]
    ids, paths = data["ids"], data["paths"]
    if np is not None:
        scores = (data["matrix"] @ np.frombuffer(q, dtype=np.float32)).astype(float)
        if project:  # filter before picking the pool, or a small project's hits could fall outside it
            scores = np.where(np.fromiter((_in_project(paths[c], project) for c in ids), bool, len(ids)), scores, -9.0)
    else:
        qv = array("f", q)
        scores = [sum(a * b for a, b in zip(row, qv)) for row in data["matrix"]]
        if project:
            scores = [s if _in_project(paths[c], project) else -9.0 for s, c in zip(scores, ids)]
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=60)
    try:
        terms = _terms(query)
        kw = _keyword_scores(con, terms)
        pos = {cid: i for i, cid in enumerate(ids)}
        blended = {}
        n = len(ids)
        pool = max(k * 20, 100)
        if np is not None and n > pool:
            top = np.argpartition(-scores, pool - 1)[:pool]
        else:
            top = range(n)
        for i in top:
            blended[ids[i]] = semantic_weight * float(scores[i])
        for cid, s in kw.items():
            if cid in pos:
                blended.setdefault(cid, semantic_weight * float(scores[pos[cid]]))
        for cid in blended:
            blended[cid] += keyword_weight * kw.get(cid, 0.0)
        hits, per = [], Counter()
        for cid, sc in sorted(blended.items(), key=lambda kv: -kv[1]):
            p = paths[cid]
            if project and not _in_project(p, project):
                continue
            if per[p] >= per_file:
                continue
            per[p] += 1
            row = con.execute("SELECT start_line, end_line, text FROM chunks WHERE id=?", (cid,)).fetchone()
            if row:
                hits.append({"path": p, "start": row[0], "end": row[1], "score": sc, "snippet": _snippet(row[2], terms)})
            if len(hits) >= k:
                break
        return hits
    finally:
        con.close()


def format_hits(hits: list[dict]) -> str:
    return "\n".join(f"{h['path']}:{h['start']}-{h['end']}  {h['score']:.2f}  {h['snippet']}" for h in hits)
