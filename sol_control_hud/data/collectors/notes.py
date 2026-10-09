"""Obsidian vault notes collector: scans recently modified notes and handles opening/previewing."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any
from ...swallow import note as _swallowed

DEFAULT_VAULTS: dict[str, Path] = {
    "OBVLT": Path(r"D:\OBVLT"),
    "Polymatica Vault": Path(r"D:\Polymatica Vault"),
}


def discover_vaults() -> dict[str, Path]:
    """Discovers Obsidian vaults from %APPDATA%/obsidian/obsidian.json or defaults."""
    appdata = os.environ.get("APPDATA")
    vaults: dict[str, Path] = {}
    if appdata:
        cfg = Path(appdata) / "obsidian" / "obsidian.json"
        if cfg.exists():
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
                for item in (data.get("vaults") or {}).values():
                    raw_path = item.get("path")
                    if raw_path:
                        p = Path(raw_path)
                        if p.exists() and p.is_dir():
                            vaults[p.name] = p
            except Exception:
                _swallowed("notes.discover_vaults")
    # Always include known default vaults if they exist on disk
    for name, path in DEFAULT_VAULTS.items():
        if path.exists() and path.is_dir() and name not in vaults:
            vaults[name] = path
    return vaults


def get_vault_path(vault_name: str) -> Path | None:
    """Returns the Path for a named vault, or None if not found."""
    vaults = discover_vaults()
    if vault_name in vaults:
        return vaults[vault_name]
    # Case-insensitive match fallback
    for name, path in vaults.items():
        if name.lower() == vault_name.lower():
            return path
    return None


def format_relative_time(epoch: float, now: float | None = None) -> str:
    """Formats epoch timestamp into concise human-readable relative time."""
    now = now or time.time()
    diff = max(0.0, now - epoch)
    if diff < 45:
        return "just now"
    if diff < 3600:
        mins = int(diff // 60)
        return f"{mins}m ago"
    if diff < 86400:
        hours = int(diff // 3600)
        return f"{hours}h ago"
    try:
        if diff < 172800:
            return f"Yesterday {time.strftime('%H:%M', time.localtime(epoch))}"
        return time.strftime("%b %d", time.localtime(epoch))
    except (OSError, OverflowError, ValueError):
        days = int(diff // 86400)
        return f"{days}d ago"


_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
CACHE_TTL = 4.0  # seconds


def extract_metadata(file_path: Path) -> tuple[list[str], int]:
    """Extracts frontmatter tags and approximate word count from a markdown file."""
    tags: list[str] = []
    words: int = 0
    try:
        content = file_path.read_text(encoding="utf-8", errors="ignore")
        words = len(content.split())
        lines = content.splitlines()
        if lines and lines[0].strip() == "---":
            for line in lines[1:]:
                if line.strip() == "---":
                    break
                stripped = line.strip()
                if stripped.startswith("tags:") or stripped.startswith("tag:"):
                    val = stripped.split(":", 1)[1].strip()
                    if val.startswith("[") and val.endswith("]"):
                        tags = [t.strip().strip("'\"#") for t in val[1:-1].split(",") if t.strip()]
                    elif val:
                        tags = [t.strip().strip("'\"#") for t in val.split() if t.strip()]
                elif stripped.startswith("- ") and tags:
                    tags.append(stripped[2:].strip().strip("'\"#"))
    except Exception:
        _swallowed("notes.extract_metadata")
    return tags, words


def scan_vault_notes(vault_name: str = "OBVLT", force: bool = False) -> list[dict[str, Any]]:
    """Scans all non-hidden markdown files in a vault, sorted by st_mtime descending."""
    now = time.time()
    if not force and vault_name in _cache:
        cached_at, cached_notes = _cache[vault_name]
        if now - cached_at < CACHE_TTL:
            return cached_notes

    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return []

    notes: list[dict[str, Any]] = []
    try:
        for root, dirs, files in os.walk(vault_path):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d.lower() not in (".trash", "node_modules")]
            rel_dir = os.path.relpath(root, vault_path)
            folder_display = "" if rel_dir == "." else rel_dir.replace("\\", "/")

            for fname in files:
                if fname.startswith(".") or not fname.lower().endswith(".md"):
                    continue
                full_path = Path(root) / fname
                try:
                    st = full_path.stat()
                except OSError:
                    continue

                rel_file = (Path(folder_display) / fname).as_posix() if folder_display else fname
                notes.append({
                    "title": full_path.stem,
                    "file": rel_file,
                    "vault": vault_name,
                    "mtime": st.st_mtime,
                    "folder": folder_display,
                    "size_bytes": st.st_size,
                    "words": 0,
                    "tags": [],
                    "_path": full_path,
                })
    except Exception:
        _swallowed("notes.scan_vault_notes")

    notes.sort(key=lambda x: x["mtime"], reverse=True)

    # Populate tags and word counts for top 50 recently edited notes
    for n in notes[:50]:
        p = n.pop("_path", None)
        if p and p.exists():
            tags, words = extract_metadata(p)
            n["tags"] = tags
            n["words"] = words

    for n in notes[50:]:
        n.pop("_path", None)

    _cache[vault_name] = (now, notes)
    return notes


def list_notes(
    vault_name: str = "OBVLT",
    limit: int = 30,
    query: str = "",
) -> list[dict[str, Any]]:
    """Returns recently edited notes for a vault, with optional search filtering."""
    notes = scan_vault_notes(vault_name)
    if query:
        q = query.lower().strip()
        notes = [n for n in notes if q in n["title"].lower() or q in n["file"].lower()]

    now = time.time()
    out = []
    for n in notes[:limit]:
        item = dict(n)
        item["mtime_str"] = format_relative_time(n["mtime"], now)
        out.append(item)
    return out


def read_note_content(vault_name: str, rel_path: str) -> dict[str, Any]:
    """Safely reads a markdown note's content from a vault, guarding against directory traversal."""
    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return {"ok": False, "why": f"Vault '{vault_name}' not found"}

    # Guard against directory traversal
    resolved_vault = vault_path.resolve()
    target = (vault_path / rel_path).resolve()
    try:
        target.relative_to(resolved_vault)
    except ValueError:
        return {"ok": False, "why": "Security error: path outside vault boundary"}

    if not target.exists() or not target.is_file():
        return {"ok": False, "why": f"Note '{rel_path}' not found"}
    if target.suffix.lower() != ".md":
        return {"ok": False, "why": "Only markdown (.md) files can be read"}

    try:
        text = target.read_text(encoding="utf-8", errors="replace")
        return {
            "ok": True,
            "title": target.stem,
            "vault": vault_name,
            "file": rel_path.replace("\\", "/"),
            "text": text,
        }
    except Exception as e:
        return {"ok": False, "why": f"Failed to read file: {e}"}


def open_note(vault_name: str, rel_path: str) -> dict[str, Any]:
    """Opens a note directly in the Obsidian desktop application via obsidian:// URI."""
    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return {"ok": False, "why": f"Vault '{vault_name}' not found"}

    resolved_vault = vault_path.resolve()
    target = (vault_path / rel_path).resolve()
    try:
        target.relative_to(resolved_vault)
    except ValueError:
        return {"ok": False, "why": "Security error: path outside vault boundary"}

    clean_file = rel_path.replace("\\", "/")
    # Format obsidian://open?vault=...&file=...
    uri = f"obsidian://open?vault={urllib.parse.quote(vault_name)}&file={urllib.parse.quote(clean_file)}"
    try:
        from ...obsidian_open import open_uri     # opens it, then brings Obsidian to the front once it shows the note
        open_uri(uri, Path(clean_file).stem, vault_name)
        return {"ok": True, "why": f"Opened {clean_file} in Obsidian", "uri": uri}
    except Exception as e:
        # Fallback to opening file path directly
        if target.exists():
            try:
                os.startfile(str(target))
                return {"ok": True, "why": f"Opened {clean_file} with default handler", "path": str(target)}
            except Exception as e2:
                return {"ok": False, "why": f"Could not open note: {e2}"}
        return {"ok": False, "why": f"Could not launch Obsidian: {e}"}


def save_note_content(vault_name: str, rel_path: str, text: str) -> dict[str, Any]:
    """Safely saves edited markdown content to a note in the vault, invalidating cache."""
    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return {"ok": False, "why": f"Vault '{vault_name}' not found"}

    resolved_vault = vault_path.resolve()
    target = (vault_path / rel_path).resolve()
    try:
        target.relative_to(resolved_vault)
    except ValueError:
        return {"ok": False, "why": "Security error: path outside vault boundary"}

    if target.suffix.lower() != ".md":
        return {"ok": False, "why": "Only markdown (.md) files can be edited"}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file in same directory
        tmp_target = target.with_suffix(".tmp.md")
        tmp_target.write_text(text, encoding="utf-8")
        tmp_target.replace(target)
        _cache.pop(vault_name, None)
        return {"ok": True, "why": f"Saved {rel_path}", "size": len(text)}
    except Exception as e:
        return {"ok": False, "why": f"Failed to save note: {e}"}


def create_note(vault_name: str, rel_path: str, initial_text: str = "") -> dict[str, Any]:
    """Creates a new markdown note in the specified vault."""
    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return {"ok": False, "why": f"Vault '{vault_name}' not found"}

    clean_rel = rel_path.strip().replace("\\", "/")
    if not clean_rel.lower().endswith(".md"):
        clean_rel += ".md"

    resolved_vault = vault_path.resolve()
    target = (vault_path / clean_rel).resolve()
    try:
        target.relative_to(resolved_vault)
    except ValueError:
        return {"ok": False, "why": "Security error: path outside vault boundary"}

    if target.exists():
        return {"ok": False, "why": f"Note '{clean_rel}' already exists"}

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(initial_text, encoding="utf-8")
        _cache.pop(vault_name, None)
        return {"ok": True, "file": clean_rel, "why": f"Created {clean_rel}"}
    except Exception as e:
        return {"ok": False, "why": f"Failed to create note: {e}"}


def open_folder(vault_name: str, rel_path: str = "") -> dict[str, Any]:
    """Opens the directory containing the note (or the vault root) in Windows Explorer."""
    vault_path = get_vault_path(vault_name)
    if not vault_path or not vault_path.exists():
        return {"ok": False, "why": f"Vault '{vault_name}' not found"}

    resolved_vault = vault_path.resolve()
    target = (vault_path / rel_path).resolve() if rel_path else resolved_vault
    try:
        target.relative_to(resolved_vault)
    except ValueError:
        return {"ok": False, "why": "Security error: path outside vault boundary"}

    target_dir = target if target.is_dir() else target.parent
    try:
        os.startfile(str(target_dir))
        return {"ok": True, "why": f"Opened folder {target_dir.name}"}
    except Exception as e:
        return {"ok": False, "why": f"Failed to open folder: {e}"}

