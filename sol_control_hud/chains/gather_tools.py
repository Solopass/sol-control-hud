"""`gather:` steps for chain notes: collect material without a model (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 4).

  gather: files in <folder> matching <glob>[ changed in the last N days]  -> one text: "### name" + text per file
  gather: commits in <repo or folder of repos>[ for the last N days]      -> one text: commit subjects per repo
  gather: changes in <repo or folder of repos> since last review         -> list of changed files with their diff
     (first review of a repo: the last 7 days). Vendored/binary/lock/generated files are skipped (file_tools.skip_reason).
     A hidden last step `mark_reviewed` records each repo's reviewed commit, only when the whole chain succeeded.
All reads go through file_tools' read rules (roots, secrets, junctions).
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from . import file_tools as ft
from ..paths import DATA_DIR

NO_WINDOW = ft.NO_WINDOW   # no console window per git call

MARKERS = Path(os.environ.get("SOL_REVIEW_MARKERS", DATA_DIR / "review-markers.json"))
MAX_CHARS = 90000            # ~30k tokens: what fits a 32k model with room to answer
REVIEW_MAX_FILE_LINES = 800  # bigger diffs are listed as skipped (a 32k Away model must fit the diff + its answer)
REVIEW_MAX_DIFF_CHARS = 6000   # a longer diff is cut here (~2k tokens: sol-fast's 16k context must fit prompt + answer)
REVIEW_MAX_PER_REPO = 12       # files reviewed per repo per run; a repo with more is reviewed in part (the rest is noted)
REVIEW_MAX_TOTAL = 40          # files reviewed per run; a repo that would go over is left for the next run (not marked reviewed)


def _prune_file_text(text: str, filename: str) -> str:
    """Semantic pruning for large files exceeding 20k characters."""
    if len(text) <= 20000:
        return text
    lines = text.splitlines()
    pruned = []
    in_block_comment = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("/*") and not stripped.endswith("*/"):
            in_block_comment = True
            continue
        if in_block_comment:
            if "*/" in stripped:
                in_block_comment = False
            continue
        if filename.endswith((".py", ".js", ".ts")):
            if (
                stripped.startswith(("def ", "class ", "async def ", "export ", "function ", "import ", "from ", "@"))
                or stripped.startswith(('"""', "'''"))
                or not stripped
            ):
                pruned.append(line)
        else:
            pruned.append(line)
    result = "\n".join(pruned)
    return result if len(result) < len(text) else text[:20000] + "\n...(truncated for context length)..."


def gather_files(args: dict, ctx=None) -> str:
    items = ft.list_files({"folder": args["folder"], "glob": args.get("glob") or "*.md", "recursive": True,
                           "since_days": args.get("since_days"), "max_files": 300})
    items.sort(key=lambda i: i["rel"].lower())
    parts, used, cut = [], 0, 0
    for it in items:
        text = ft.read_item(it).strip()
        if len(text) > 20000:
            text = _prune_file_text(text, it.get("rel", ""))
        block = f"### {it['rel']}\n{text}\n"
        if used + len(block) > int(args.get("max_chars", MAX_CHARS)):
            cut += 1
            continue
        parts.append(block); used += len(block)
    if ctx is not None:
        ctx.note(f"gathered {len(parts)} file(s), {used // 1000} KB" + (f", {cut} left out (too much text)" if cut else ""))
    if not parts:
        return "(no matching files)"
    return "\n".join(parts) + (f"\n_({cut} more file(s) left out: too much text)_" if cut else "")


def _repos(path: str) -> list[Path]:
    """The repo itself and/or the repos one level inside it (D:\\Workspace is a repo that holds project repos)."""
    root = ft.check_read(path)
    inner = sorted(p for p in root.iterdir() if p.is_dir() and (p / ".git").exists())
    return ([root] if (root / ".git").exists() else []) + inner


def _git(repo: Path, *a: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=120, creationflags=NO_WINDOW)
    return r.stdout if r.returncode == 0 else ""


def gather_commits(args: dict, ctx=None) -> str:
    days = int(args.get("days") or 7)
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    out = []
    for repo in _repos(args["path"]):
        log = _git(repo, "log", f"--since={since}", "--no-merges", "--date=short", "--pretty=format:- %ad %s")
        lines = [ln for ln in log.splitlines() if ln.strip()]
        if lines:
            out.append(f"### {repo.name} ({len(lines)} commit{'s' if len(lines) != 1 else ''})\n" + "\n".join(lines[:80])
                       + (f"\n- … {len(lines) - 80} more" if len(lines) > 80 else ""))
    if ctx is not None:
        ctx.note(f"commits from {len(out)} repo(s)")
    return "\n\n".join(out) if out else f"(no commits in the last {days} days)"


def load_markers() -> dict:
    try:
        return json.loads(MARKERS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cap_diff(diff: str, limit: int) -> str:
    if len(diff) <= limit:
        return diff
    cut = diff[:limit].rsplit(chr(10), 1)[0]
    return f"{cut}{chr(10)}...(diff cut: {len(diff) - len(cut)} more characters not shown)"


def review_changes(args: dict, ctx=None) -> list[dict]:
    """Changed files to review, bounded: each diff is cut at `max_diff_chars`, a repo gives at most `max_per_repo` files,
    and a run at most `max_total`. A repo that doesn't fit in this run is skipped whole (no marker is written for it, so
    it is reviewed next time); a repo cut to `max_per_repo` counts as reviewed and the note says how many were left out."""
    markers = load_markers()
    max_diff = int(args.get("max_diff_chars") or REVIEW_MAX_DIFF_CHARS)
    max_repo = int(args.get("max_per_repo") or REVIEW_MAX_PER_REPO)
    max_total = int(args.get("max_total") or REVIEW_MAX_TOTAL)
    items, summary = [], []
    for repo in _repos(args["path"]):
        head = _git(repo, "rev-parse", "HEAD").strip()
        since = markers.get(str(repo), {}).get("commit")
        if since == head:
            continue  # nothing new since the last review
        if since and subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{since}^{{commit}}"],
                                    capture_output=True, creationflags=NO_WINDOW).returncode != 0:
            since = None  # history rewritten (rebase/force-push): fall back to the last 7 days
        try:
            ch = ft.git_changes({"repo": str(repo), "since": since, "max_file_lines": REVIEW_MAX_FILE_LINES})
        except ft.FileToolError as e:
            summary.append(f"{repo.name}: skipped ({e})")
            continue
        if not ch["files"]:
            continue
        skipped = [f for f in ch["files"] if "skipped" in f]
        # the files with the least change first: whole small diffs review better than cut big ones
        reviewable = sorted(ch["reviewable"], key=lambda f: len(f["diff"]))
        taken = reviewable[:max_repo]
        if items and len(items) + len(taken) > max_total:
            summary.append(f"{repo.name}: {len(reviewable)} file(s) left for the next run (today's limit is {max_total})")
            continue
        left_out = len(reviewable) - len(taken)
        summary.append(f"{repo.name}: {len(taken)} file(s) to review, {len(skipped)} skipped"
                       + (f", {left_out} more not reviewed (limit {max_repo} per repo)" if left_out else ""))
        for f in taken:
            items.append({"name": f"{repo.name}: {f['file']}", "repo": repo.name, "repo_path": str(repo), "head": head,
                          "file": f["file"], "status": f["status"], "diff": _cap_diff(f["diff"], max_diff)})
    if ctx is not None:
        ctx.note("; ".join(summary) or "no repo has changes since its last review")
    return items


def mark_reviewed(args: dict, ctx=None) -> dict:
    """Hidden last step of a review chain: remember each reviewed repo's commit (runs only if every step succeeded)."""
    changes = args.get("changes") or []
    markers = load_markers()
    done = {}
    for c in changes:
        if isinstance(c, dict) and c.get("repo_path") and c.get("head"):
            done[c["repo_path"]] = c["head"]
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    for repo, head in done.items():
        markers[repo] = {"commit": head, "reviewed": stamp}
    MARKERS.parent.mkdir(parents=True, exist_ok=True)
    tmp = MARKERS.with_suffix(".tmp")
    tmp.write_text(json.dumps(markers, indent=1), encoding="utf-8")
    os.replace(tmp, MARKERS)
    if ctx is not None:
        ctx.note(f"marked {len(done)} repo(s) as reviewed")
    return {"marked": sorted(Path(r).name for r in done)}


TOOLS = {"gather_files": gather_files, "gather_commits": gather_commits, "review_changes": review_changes,
         "mark_reviewed": mark_reviewed}
