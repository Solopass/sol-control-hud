"""Project-specific prompt chain helpers: discover, auto-generate, and run Bug Hunt chains for projects in D:\\Workspace."""
from __future__ import annotations

import os
from pathlib import Path

from . import chain_note
from ..paths import DATA_DIR

CHAINS_DIR = Path(os.environ.get("SOL_CHAINS", r"D:\OBVLT\1Notebook\Chains"))
REVIEWS_DIR = Path(os.environ.get("SOL_REVIEWS", r"D:\OBVLT\1Notebook\Reviews"))
WORKSPACE_DIR = Path(os.environ.get("SOL_WORKSPACE", r"D:\Workspace"))


def find_chain_for_project(name: str, chains_dir: Path | None = None) -> Path | None:
    """Finds an existing Bug hunt or Deep review chain for the given project name."""
    cdir = chains_dir or CHAINS_DIR
    if not cdir.is_dir():
        return None

    norm = name.strip().lower()
    # 1. Exact bug hunt note match: "Bug hunt <name>.md"
    exact_hunt = cdir / f"Bug hunt {name}.md"
    if exact_hunt.is_file():
        return exact_hunt

    # 2. Case-insensitive or normalized check
    for p in cdir.glob("Bug hunt *.md"):
        target_name = p.stem[len("Bug hunt "):].strip().lower()
        if target_name == norm:
            return p

    # 3. Special case: media-api has the baseline "Bug hunt.md"
    if norm == "media-api":
        baseline = cdir / "Bug hunt.md"
        if baseline.is_file():
            return baseline

    # 4. Deep review note: "Deep review <name>.md"
    for p in cdir.glob("Deep review *.md"):
        target_name = p.stem[len("Deep review "):].strip().lower()
        if target_name == norm:
            return p

    return None


def detect_globs_for_project(project_folder: Path) -> str:
    """Inspects a project folder and returns the most appropriate glob pattern for bug hunt."""
    if not project_folder.is_dir():
        return "*.py,*.js,*.ts,*.go"

    exts: set[str] = set()
    try:
        # Shallow/sample scan for prominent language extensions
        for p in project_folder.rglob("*"):
            if not p.is_file():
                continue
            if any(part.startswith(".") or part in ("node_modules", "dist", "build", "vendor", ".venv", "venv", "__pycache__") for part in p.parts):
                continue
            s = p.suffix.lower()
            if s in (".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs", ".cs", ".cpp", ".c", ".html"):
                exts.add(f"*{s}")
            if len(exts) >= 4:
                break
    except OSError:
        pass

    if exts:
        # Sort so deterministic
        return ",".join(sorted(exts))
    return "*.py,*.js,*.ts,*.go"


def ensure_bug_hunt_chain(name: str, chains_dir: Path | None = None, workspace: Path | None = None) -> Path:
    """Returns an existing chain note for the project, or generates a tailored one."""
    existing = find_chain_for_project(name, chains_dir=chains_dir)
    if existing:
        return existing

    cdir = chains_dir or CHAINS_DIR
    wdir = workspace or WORKSPACE_DIR
    proj_folder = wdir / name

    globs = detect_globs_for_project(proj_folder)
    note_path = cdir / f"Bug hunt {name}.md"

    content = f"""---
type: chain
status: queued
model: sol-fast
skip_wait: true
system: You are a careful senior code reviewer reading whole files. Report only real problems - bugs, crashes, data loss, security risks, race conditions, wrong edge cases, resource leaks. Never style, naming or "consider adding" advice. Be specific (file, function, why it breaks, the fix). You only give advice; you never change code. Only report a defect you can tie to a specific line in front of you. If judging it would need code you cannot see, or an assumption about how the caller behaves, leave it out - a wrong finding costs more than a missed one.
save_to: 1Notebook/Reviews/{{{{today}}}} bug hunt {name}.md
save_steps: Real issues, First pass
---
Bug hunt for {name}. Read-only: it writes a note in Reviews, never touches code.

## Files
gather: files in {proj_folder} matching {globs} changed in the last 180 days

## First pass
reasoning: off
max tokens: 3500
Read these source files from the {name} project and look for real defects.

{{{{Files}}}}

Reply with at most 12 bullets: **[high|medium|low]** file: what breaks, the function or line, and the one-sentence fix.
If you find nothing important, reply exactly: No issues.

## Real issues
model: sol-fast
reasoning: off
max tokens: 2500
check: at most 450 words
Candidate defects found in {name}:

{{{{First pass}}}}

Many first-pass findings are not real problems. Keep only what can actually bite: crashes, wrong results, data loss, hangs, races, security. Drop style, naming, "consider adding", and anything clearly intentional.
Reply with at most 8 bullets, highest risk first: **[high|medium]** file: the problem, then the fix in one sentence.
If nothing real is left, reply exactly: No real issues.
"""
    cdir.mkdir(parents=True, exist_ok=True)
    note_path.write_text(content, encoding="utf-8")
    return note_path


def queue_bug_hunt(name: str, chains_dir: Path | None = None, workspace: Path | None = None) -> dict:
    """Queues a bug hunt chain for the project and requests immediate start (skip_wait: true)."""
    note = ensure_bug_hunt_chain(name, chains_dir=chains_dir, workspace=workspace)
    try:
        chain_note.set_chain_status(note, "queued", skip_wait="true")
        return {
            "ok": True,
            "why": f"Queued {note.stem} (running now)",
            "chain": note.stem,
            "note": str(note),
        }
    except Exception as e:
        return {"ok": False, "why": f"Could not queue chain: {e}"}


def find_latest_review(name: str, reviews_dir: Path | None = None) -> dict | None:
    """Finds the most recent review markdown file for the given project in 1Notebook/Reviews."""
    rdir = reviews_dir or REVIEWS_DIR
    if not rdir.is_dir():
        return None

    norm = name.strip().lower()
    matches: list[Path] = []
    for p in rdir.glob("*.md"):
        stem = p.stem.lower()
        if norm in stem or (norm == "media-api" and "bug hunt" in stem and not any(other in stem for other in ("speed", "suno", "voice", "polymatica"))):
            matches.append(p)

    if not matches:
        return None

    # Sort newest first by modification time or filename date
    matches.sort(key=lambda p: (p.stat().st_mtime, p.name), reverse=True)
    latest = matches[0]
    return {
        "name": latest.name,
        "path": f"1Notebook/Reviews/{latest.name}",
        "full_path": str(latest),
        "mtime": latest.stat().st_mtime,
    }
