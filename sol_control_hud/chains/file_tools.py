"""Safe file tools for chains (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 1.5).

Tools (tool steps; arguments come only from the workflow, never from model output):
  list_files  {folder, glob="*.md", recursive=true, max_files=500, chunk_tokens=null, since_days=null}
              -> [{name, path, rel, size, modified, part, parts, start, end}]  (no text: it's read per item, lazily)
  read_text   {path} -> {name, path, text, no_text}          (md/txt/code; PDF via pypdf; scans -> no_text)
  git_changes {repo, since=null (7 days), max_file_lines=1500}
              -> {repo, base, head, files: [{file, status, added, deleted, diff | skipped}]}
  save_note   {path | folder+name, text, frontmatter={}} -> {path}   (name = any title; made into one safe file name)

Rules (checked on the real path: `..`, symlinks and junctions are resolved first):
- reads only under READ_ROOTS; writes only under WRITE_ROOTS; nothing is ever deleted;
- never reads .git/.venv/node_modules/.obsidian or secret-looking files (.env, keys, tokens, credentials);
- save_note only replaces a file this tool wrote itself (frontmatter `made_by: sol-chain`), never your own notes.
"""
from __future__ import annotations

import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import yaml

# pythonw has no console: without this every git call flashes a console window (09-26, the Code review gather)
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CHARS_PER_TOKEN = 3.0  # conservative, same as llm.py
MARKER = "sol-chain"


class FileToolError(Exception):
    pass


@dataclass
class Policy:
    read_roots: list[str] = field(default_factory=lambda: [r"D:\OBVLT", r"D:\Polymatica Vault", r"D:\Workspace", r"D:\Output",
                                                           r"D:\AI\Vault"])
    write_roots: list[str] = field(default_factory=lambda: [r"D:\OBVLT\1Notebook", r"D:\Output\chains", r"D:\AI\Vault\Chains"])
    repo_roots: list[str] = field(default_factory=lambda: [r"D:\Workspace"])


POLICY = Policy()
DENY_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".obsidian", ".pytest_cache", ".trash"}
DENY_FILES = ["*.env", ".env*", "*.pem", "*.key", "*.pfx", "*.p12", "id_rsa*", "id_ed25519*", "*secret*", "*credential*",
              "*token*", "*.sqlite", "*.sqlite-*", "*.db", "*.kdbx"]
TEXT_EXT = {".md", ".txt", ".py", ".ps1", ".psm1", ".js", ".jsx", ".ts", ".tsx", ".json", ".yaml", ".yml", ".toml", ".ini",
            ".cfg", ".html", ".css", ".scss", ".sh", ".bat", ".cmd", ".rs", ".go", ".java", ".c", ".h", ".cpp", ".cs", ".sql",
            ".csv", ".xml", ".base", ".canvas", ".srt", ".vtt", ".log"}
# code review skip rules (plan pre-flight change 3): vendored / generated / lock files aren't reviewed
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock", "poetry.lock", "uv.lock",
              "Cargo.lock", "composer.lock", "Gemfile.lock"}
VENDOR_PARTS = {"vendor", "vendors", "node_modules", "dist", "build", "third_party", "external", ".next", "coverage"}


def _real(path: str | Path) -> Path:
    return Path(os.path.realpath(os.path.expandvars(str(path))))


def _under(p: Path, roots: list[str]) -> bool:
    pn = os.path.normcase(str(p))
    for r in roots:
        rn = os.path.normcase(str(_real(r)))
        if pn == rn or pn.startswith(rn.rstrip("\\/") + os.sep):
            return True
    return False


def _denied(p: Path) -> str | None:
    if any(part.lower() in DENY_DIRS for part in p.parts):
        return "a tool/system folder"
    name = p.name.lower()
    if any(fnmatch.fnmatch(name, pat) for pat in DENY_FILES):
        return "a secret-looking or database file"
    return None


def check_read(path: str | Path, policy: Policy | None = None) -> Path:
    policy = policy or POLICY  # looked up at call time, so it can be changed (tests, config)
    p = _real(path)
    if not _under(p, policy.read_roots):
        raise FileToolError(f"not allowed to read {path} (outside {', '.join(policy.read_roots)})")
    why = _denied(p)
    if why:
        raise FileToolError(f"not allowed to read {path}: {why}")
    return p


def check_write(path: str | Path, policy: Policy | None = None) -> Path:
    policy = policy or POLICY
    p = _real(path)
    if not _under(p, policy.write_roots):
        raise FileToolError(f"not allowed to write {path} (chains write only under {', '.join(policy.write_roots)})")
    if _denied(p):
        raise FileToolError(f"not allowed to write {path}")
    return p


def safe_name(name: str, limit: int = 120) -> str:
    """A file name from any text (e.g. a title): no path separators, reserved characters or trailing dots."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(name))
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    return (name[:limit].rstrip() or "untitled")


# ---- reading
def _read_pdf(p: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    return "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()


def read_file(path: str | Path, policy: Policy | None = None) -> dict:
    p = check_read(path, policy)
    if not p.is_file():
        raise FileToolError(f"no such file: {path}")
    if p.suffix.lower() == ".pdf":
        text = _read_pdf(p)
    elif p.suffix.lower() in TEXT_EXT or not p.suffix:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    else:
        raise FileToolError(f"can't read {p.suffix} files as text: {path}")
    return {"name": p.name, "path": str(p), "text": text, "no_text": not text.strip()}


def read_item(item: dict, policy: Policy | None = None) -> str:
    """Text of one list_files item (only its part, if the file was split)."""
    f = read_file(item["path"], policy)
    if f["no_text"]:
        return "(this file has no text layer, e.g. a scanned PDF; nothing to read)"
    start, end = item.get("start"), item.get("end")
    return f["text"][start:end] if start is not None else f["text"]


def _split_points(text: str, max_chars: int) -> list[tuple[int, int]]:
    """Cut into parts of at most max_chars, at a paragraph or line break where possible."""
    parts, start = [], 0
    while len(text) - start > max_chars:
        cut = text.rfind("\n\n", start + max_chars // 2, start + max_chars)
        if cut == -1:
            cut = text.rfind("\n", start + max_chars // 2, start + max_chars)
        if cut == -1:
            cut = start + max_chars
        parts.append((start, cut)); start = cut
    parts.append((start, len(text)))
    return parts


def list_files(args: dict, ctx=None, policy: Policy | None = None) -> list[dict]:
    folder = check_read(args["folder"], policy)
    if not folder.is_dir():
        raise FileToolError(f"no such folder: {args['folder']}")
    globs = [g.strip() for g in str(args.get("glob") or "*.md").split(",") if g.strip()]
    recursive = bool(args.get("recursive", True))
    max_files = int(args.get("max_files", 500))
    chunk_tokens = args.get("chunk_tokens")
    since_days = args.get("since_days")
    cutoff = (datetime.now() - timedelta(days=float(since_days))).timestamp() if since_days not in (None, "") else None
    found: list[Path] = []
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d.lower() not in DENY_DIRS)
        for f in sorted(files):
            p = Path(root) / f
            if any(fnmatch.fnmatch(f.lower(), g.lower()) for g in globs) and not _denied(p) \
                    and (cutoff is None or p.stat().st_mtime >= cutoff):
                found.append(p)
        if not recursive:
            break
    if len(found) > max_files:
        raise FileToolError(f"{len(found)} files match in {folder} (limit {max_files}); narrow the glob or raise max_files")
    items = []
    for p in found:
        st = p.stat()
        base = {"name": p.name, "path": str(p), "rel": str(p.relative_to(folder)), "size": st.st_size,
                "modified": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"), "part": 1, "parts": 1}
        if chunk_tokens and p.suffix.lower() in TEXT_EXT | {".pdf"}:
            try:
                text = read_file(p, policy)["text"]
            except (FileToolError, OSError, ValueError):
                text = ""
            spans = _split_points(text, int(int(chunk_tokens) * CHARS_PER_TOKEN)) if text else [(0, 0)]
            if len(spans) > 1:
                for n, (a, b) in enumerate(spans, 1):
                    items.append({**base, "name": f"{p.name} (part {n}/{len(spans)})", "part": n, "parts": len(spans),
                                  "start": a, "end": b})
                continue
        items.append(base)
    if ctx is not None:
        ctx.note(f"{len(found)} file(s), {len(items)} item(s)")
    return items


def read_text(args: dict, ctx=None, policy: Policy | None = None) -> dict:
    return read_file(args["path"], policy)


# ---- writing
def _made_by_us(p: Path) -> bool:
    try:
        head = p.read_text(encoding="utf-8-sig", errors="replace")[:2000]
    except OSError:
        return False
    m = re.match(r"^---\r?\n(.*?)\r?\n---", head, re.S)
    if not m:
        return False
    try:
        return (yaml.safe_load(m.group(1)) or {}).get("made_by") == MARKER
    except yaml.YAMLError:
        return False


def save_note(args: dict, ctx=None, policy: Policy | None = None) -> dict:
    if args.get("name") is not None:  # a title (maybe from model output): always one file name inside `folder`
        target = Path(str(args["folder"])) / (safe_name(args["name"]) + ".md")
    else:
        target = Path(str(args["path"]))
        target = target.with_name(safe_name(target.stem) + (target.suffix or ".md"))
    p = check_write(target, policy)
    if p.exists() and not _made_by_us(p):
        raise FileToolError(f"{p} already exists and wasn't written by a chain; not overwriting your note")
    front = {"made_by": MARKER, **(args.get("frontmatter") or {})}
    body = str(args.get("text") or "")
    text = "---\n" + yaml.safe_dump(front, sort_keys=False, allow_unicode=True).strip() + "\n---\n" + body.rstrip() + "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)  # atomic: Obsidian never sees a half-written note
    if ctx is not None:
        ctx.note(f"wrote {p}")
    return {"path": str(p)}


# ---- git (code review)
def _git(repo: Path, *a: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, creationflags=NO_WINDOW)
    if r.returncode != 0:
        raise FileToolError(f"git {' '.join(a[:2])} failed in {repo}: {r.stderr.strip()[:200]}")
    return r.stdout


def skip_reason(path: str, added: int | None, deleted: int | None, max_lines: int) -> str | None:
    name = path.rsplit("/", 1)[-1]
    parts = {x.lower() for x in path.split("/")[:-1]}
    if added is None:
        return "binary"
    if name in LOCK_FILES:
        return "lock file"
    if ".min." in name.lower() or name.lower().endswith((".map", ".wasm")):
        return "minified/generated"
    if parts & VENDOR_PARTS or (("public" in parts or "static" in parts) and name.lower().endswith((".js", ".css", ".wasm"))):
        return "vendored/build output"
    if _denied(Path(path)):
        return "secret-looking file"
    if (added or 0) + (deleted or 0) > max_lines:
        return f"too big ({added + deleted} changed lines; likely generated)"
    return None


def git_changes(args: dict, ctx=None, policy: Policy | None = None) -> dict:
    policy = policy or POLICY
    repo = check_read(args["repo"], policy)
    if not _under(repo, policy.repo_roots) or not (repo / ".git").exists():
        raise FileToolError(f"{args['repo']} isn't a git repo under {', '.join(policy.repo_roots)}")
    max_lines = int(args.get("max_file_lines", 1500))
    head = _git(repo, "rev-parse", "HEAD").strip()
    since = args.get("since")
    if since and re.fullmatch(r"[0-9a-f]{7,40}", str(since)):
        base = str(since)
    else:
        when = str(since) if since else (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
        base = _git(repo, "rev-list", "-1", f"--before={when}", "HEAD").strip()
        if not base:  # the repo is younger than that: compare against the empty tree
            base = _git(repo, "hash-object", "-t", "tree", os.devnull).strip()
    files = []
    if base != head:
        status = {}
        for line in _git(repo, "diff", "--name-status", "--no-renames", base, head).splitlines():
            st, _, f = line.partition("\t")
            status[f] = st[:1]
        for line in _git(repo, "diff", "--numstat", "--no-renames", base, head).splitlines():
            a, d, f = line.split("\t", 2)
            added, deleted = (None, None) if a == "-" else (int(a), int(d))
            entry = {"file": f, "status": status.get(f, "M"), "added": added, "deleted": deleted}
            why = skip_reason(f, added, deleted, max_lines)
            if why:
                entry["skipped"] = why
            elif entry["status"] == "D":
                entry["diff"] = "(file deleted)"
            else:
                entry["diff"] = _git(repo, "diff", "-U3", "--no-renames", base, head, "--", f)
            files.append(entry)
    if ctx is not None:
        ctx.note(f"{repo.name}: {len(files)} changed file(s), {sum(1 for f in files if 'skipped' in f)} skipped")
    return {"repo": str(repo), "name": repo.name, "base": base, "head": head, "files": files,
            "reviewable": [f for f in files if "diff" in f]}


TOOLS = {"list_files": list_files, "read_text": read_text, "save_note": save_note, "git_changes": git_changes}
