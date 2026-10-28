"""Project-specific prompt chain helpers: discover, auto-generate, and run Bug Hunt chains for projects in D:\\Workspace."""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import chain_note
from ..paths import DATA_DIR

CHAINS_DIR = Path(os.environ.get("SOL_CHAINS", r"D:\OBVLT\1Notebook\Chains"))
REVIEWS_DIR = Path(os.environ.get("SOL_REVIEWS", r"D:\OBVLT\1Notebook\Reviews"))
WORKSPACE_DIR = Path(os.environ.get("SOL_WORKSPACE", r"D:\Workspace"))


def parse_review_tasks(file_path: Path) -> dict:
    """Parses actionable findings / task checkboxes in a review note."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return {"total": 0, "open": 0, "resolved": 0, "items": []}

    # If the note has a dedicated '## Real issues' section, prioritize findings in it
    scope_text = text
    sec_match = re.search(r"(?m)^## Real issues\s*\n(.*?)(?=\n## |\Z)", text, re.S)
    if sec_match:
        cand = sec_match.group(1).strip()
        if any(marker in cand for marker in ("**[", "**File:", "- [", "* [", "1.")):
            scope_text = cand

    items = []
    lines = scope_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        # Checkbox line: - [ ] or - [x] or * [ ] or 1. [ ]
        cb_m = re.match(r"^[ \t]*(?:[-*]|\d+\.)[ \t]+\[([ xX])\][ \t]*(.*?)$", line)
        if cb_m:
            resolved = cb_m.group(1).lower() == "x"
            task_text = cb_m.group(2).strip()
            sev_match = re.search(r"\*\*\[(high|medium|low|critical)\]\*\*", task_text, re.IGNORECASE)
            severity = sev_match.group(1).lower() if sev_match else None
            ctx = []
            j = i + 1
            while j < len(lines) and re.match(r"^[ \t]+(?:[-*]|\*)[ \t]+", lines[j]):
                sub = re.sub(r"^[ \t]+(?:[-*]|\*)[ \t]+", "", lines[j]).strip()
                if sub.lower().startswith("**problem:**") or sub.lower().startswith("problem:"):
                    clean_p = re.sub(r"^\*\*Problem:\*\*[ \t]*", "", sub, flags=re.I)
                    ctx.append(clean_p)
                j += 1
            if ctx:
                task_text += " · " + " ".join(ctx)
            items.append({
                "text": task_text,
                "resolved": resolved,
                "severity": severity,
                "checkbox": True,
            })
            i += 1
            continue

        # Bullet severity: - **[high]** or * **[high]** or 1. **[high]**
        sev_m = re.match(r"^[ \t]*(?:[-*]|\d+\.)[ \t]+(\*\*\[?(high|medium|low|critical)\]?\*\*.*?)$", line, re.I)
        if sev_m:
            task_text = sev_m.group(1).strip()
            sev_match = re.search(r"\*\*\[(high|medium|low|critical)\]\*\*", task_text, re.IGNORECASE)
            severity = sev_match.group(1).lower() if sev_match else None
            items.append({
                "text": task_text,
                "resolved": False,
                "severity": severity,
            })
            i += 1
            continue

        # Numbered File item: 1.  **File: `...`**
        file_m = re.match(r"^[ \t]*\d+\.[ \t]+(\*\*File:.*?\*\*)$", line)
        if file_m:
            task_text = file_m.group(1).strip()
            ctx = []
            j = i + 1
            while j < len(lines) and re.match(r"^[ \t]+(?:[-*]|\*)[ \t]+", lines[j]):
                sub = re.sub(r"^[ \t]+(?:[-*]|\*)[ \t]+", "", lines[j]).strip()
                if sub.lower().startswith("**problem:**") or sub.lower().startswith("problem:"):
                    clean_p = re.sub(r"^\*\*Problem:\*\*[ \t]*", "", sub, flags=re.I)
                    ctx.append(clean_p)
                j += 1
            if ctx:
                task_text += " · " + " ".join(ctx)
            items.append({
                "text": task_text,
                "resolved": False,
                "severity": "high",
            })
            i += 1
            continue

        i += 1

    open_count = sum(1 for it in items if not it["resolved"])
    resolved_count = sum(1 for it in items if it["resolved"])
    return {
        "total": len(items),
        "open": open_count,
        "resolved": resolved_count,
        "items": items,
    }


SCORE_EXEMPT_KINDS = {"maintenance", "ingest"}  # judged by run health, not by findings you act on


def chain_scoreboard(chain_path: Path, meta: dict, reviews_dir: Path | None = None, window: int = 4,
                     threshold: float = 0.2) -> dict | None:
    """How often a chain's findings get acted on (chains v2 plan, phase 0): its last `window` review notes in
    1Notebook/Reviews, counting checkbox findings and the ones you ticked. None for chains that aren't scored:
    `kind: maintenance` / `kind: ingest`, or output that doesn't go to Reviews. `flag` = retirement candidate:
    a full window, at least one tickable finding, and under `threshold` of them ticked. Findings written as plain
    bullets can't be ticked; they're counted as `untracked` and never flag a chain."""
    if str(meta.get("kind") or "").lower() in SCORE_EXEMPT_KINDS:
        return None
    save_to = str(meta.get("save_to") or "").replace("\\", "/")
    if not save_to.startswith("1Notebook/Reviews/"):
        return None
    pattern = re.sub(r"\{\{[^}]*\}\}", "*", save_to.rsplit("/", 1)[-1])
    rdir = reviews_dir or REVIEWS_DIR
    notes = sorted(rdir.glob(pattern), key=lambda p: p.stat().st_mtime)[-window:] if rdir.is_dir() else []
    tickable = acted = untracked = 0
    for note in notes:
        for it in parse_review_tasks(note)["items"]:
            if it.get("checkbox"):
                tickable += 1
                acted += it["resolved"]
            else:
                untracked += 1
    rate = acted / tickable if tickable else None
    return {"chain": chain_path.stem, "runs": len(notes), "findings": tickable, "acted": acted,
            "untracked": untracked, "rate": rate,
            "flag": len(notes) >= window and rate is not None and rate < threshold}


def toggle_review_task(file_path: Path, item_index: int, resolved: bool) -> dict:
    """Toggles the task checkbox at item_index in file_path."""
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "why": f"Cannot read {file_path.name}: {e}"}

    lines = text.splitlines()
    checkbox_idx = 0
    found = False
    new_lines = []
    mark = "[x] " if resolved else "[ ] "

    sec_match = re.search(r"(?m)^## Real issues\s*\n(.*?)(?=\n## |\Z)", text, re.S)
    has_real_issues_scope = bool(sec_match and any(marker in sec_match.group(1) for marker in ("**[", "**File:", "- [", "* [", "1.")))
    in_real_issues = False

    cb_pattern = re.compile(r"^([ \t]*(?:[-*]|\d+\.)[ \t]+)(\[[ xX]\][ \t]*)(.*)$")
    bullet_pattern = re.compile(r"^([ \t]*(?:[-*]|\d+\.)[ \t]+)(\*\*\[?(?:high|medium|low|critical)\]?\*\*.*)$", re.IGNORECASE)
    file_pattern = re.compile(r"^([ \t]*\d+\.[ \t]+)(\*\*File:.*?\*\*)$")

    for line in lines:
        if line.startswith("## Real issues"):
            in_real_issues = True
        elif line.startswith("## ") and in_real_issues:
            in_real_issues = False

        if has_real_issues_scope and not in_real_issues:
            new_lines.append(line)
            continue

        m = cb_pattern.match(line)
        if m:
            if checkbox_idx == item_index:
                new_lines.append(f"{m.group(1)}{mark}{m.group(3)}")
                found = True
            else:
                new_lines.append(line)
            checkbox_idx += 1
            continue

        bf = bullet_pattern.match(line)
        if bf:
            if checkbox_idx == item_index:
                new_lines.append(f"{bf.group(1)}{mark}{bf.group(2).lstrip()}")
                found = True
            else:
                new_lines.append(line)
            checkbox_idx += 1
            continue

        fm = file_pattern.match(line)
        if fm:
            if checkbox_idx == item_index:
                new_lines.append(f"{fm.group(1)}{mark}{fm.group(2).lstrip()}")
                found = True
            else:
                new_lines.append(line)
            checkbox_idx += 1
            continue

        new_lines.append(line)

    if not found:
        return {"ok": False, "why": f"Task index {item_index} not found in {file_path.name}"}

    try:
        file_path.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""), encoding="utf-8")
        return {"ok": True, "why": f"Updated task in {file_path.name}", "tasks": parse_review_tasks(file_path)}
    except OSError as e:
        return {"ok": False, "why": f"Cannot write {file_path.name}: {e}"}


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

Reply with at most 8 markdown task checkboxes, highest risk first: - [ ] **[high|medium]** file: the problem, then the fix in one sentence.
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
    norm_tokens = norm.replace("-", " ").replace("_", " ")
    matches: list[Path] = []
    for p in rdir.glob("*.md"):
        stem = p.stem.lower()
        stem_tokens = stem.replace("-", " ").replace("_", " ")
        is_match = (
            (f" {norm_tokens} " in f" {stem_tokens} ")
            or (stem_tokens.startswith(f"{norm_tokens} "))
            or (stem_tokens.endswith(f" {norm_tokens}"))
            or (norm == "media-api" and "bug hunt" in stem and not any(other in stem for other in ("speed", "suno", "voice", "polymatica")))
        )
        if is_match:
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
        "tasks": parse_review_tasks(latest),
    }


HOOK_MARKER = "# SOL Control HUD git-post-commit-hook"

HOOK_SCRIPT = """#!/bin/sh
# SOL Control HUD git-post-commit-hook
# Non-blocking HUD trigger
COMMIT_MSG=$(git log -1 --pretty=%B 2>/dev/null)
case "$COMMIT_MSG" in
  *"[skip-ai]"*|*"[no-review]"*|*"[wip]"*) exit 0 ;;
esac

REPO_NAME=$(basename -s .git "$(git rev-parse --show-toplevel 2>/dev/null)")
if [ -n "$REPO_NAME" ]; then
  (curl -s -m 2 -X POST "http://127.0.0.1:7900/api/action" \\
    -H "Content-Type: application/json" \\
    -d "{\\"action\\":\\"git_post_commit\\",\\"target\\":\\"$REPO_NAME\\"}" >/dev/null 2>&1 &) &
fi
exit 0
"""


def is_project_hook_installed(repo_path: Path) -> bool:
    """Checks whether the SOL post-commit hook is installed in the repository."""
    hook_file = repo_path / ".git" / "hooks" / "post-commit"
    if not hook_file.is_file():
        return False
    try:
        content = hook_file.read_text(encoding="utf-8", errors="replace")
        return HOOK_MARKER in content
    except OSError:
        return False


def install_project_hook(repo_path: Path) -> dict:
    """Installs a non-blocking post-commit hook in repo_path to trigger local AI review."""
    git_dir = repo_path / ".git"
    if not git_dir.exists():
        return {"ok": False, "why": f"{repo_path.name} is not a git repository"}
    hooks_dir = git_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    hook_file = hooks_dir / "post-commit"

    if hook_file.is_file():
        try:
            content = hook_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if HOOK_MARKER in content:
            return {"ok": True, "installed": True, "why": f"Auto-hunt hook already active for {repo_path.name}"}
        new_content = content.rstrip() + "\n\n" + HOOK_SCRIPT.strip() + "\n"
    else:
        new_content = HOOK_SCRIPT

    try:
        hook_file.write_text(new_content, encoding="utf-8")
        try:
            hook_file.chmod(hook_file.stat().st_mode | 0o111)
        except Exception:
            pass
        return {"ok": True, "installed": True, "why": f"Auto-hunt hook installed for {repo_path.name}"}
    except OSError as e:
        return {"ok": False, "why": f"Could not write hook file: {e}"}


def uninstall_project_hook(repo_path: Path) -> dict:
    """Removes the SOL post-commit hook from repo_path."""
    hook_file = repo_path / ".git" / "hooks" / "post-commit"
    if not hook_file.is_file():
        return {"ok": True, "installed": False, "why": f"No hook installed for {repo_path.name}"}
    try:
        content = hook_file.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"ok": False, "why": f"Could not read hook file: {e}"}
    if HOOK_MARKER not in content:
        return {"ok": True, "installed": False, "why": f"Hook in {repo_path.name} was not managed by SOL"}

    # If the file only contains our hook script, remove it completely
    pure_lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if content.strip() == HOOK_SCRIPT.strip() or all(
        ln.startswith("#") or ln in ("exit 0", "#!/bin/sh") or "git_post_commit" in ln or "COMMIT_MSG" in ln or "REPO_NAME" in ln or "curl" in ln
        for ln in pure_lines
    ):
        try:
            hook_file.unlink()
        except OSError as e:
            return {"ok": False, "why": f"Could not remove hook file: {e}"}
    else:
        # Strip out our hook lines
        lines = []
        skip = False
        for line in content.splitlines():
            if HOOK_MARKER in line:
                skip = True
                continue
            if skip and line.strip() == "exit 0":
                skip = False
                continue
            if not skip:
                lines.append(line)
        try:
            hook_file.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
        except OSError as e:
            return {"ok": False, "why": f"Could not update hook file: {e}"}

    return {"ok": True, "installed": False, "why": f"Auto-hunt hook removed for {repo_path.name}"}
