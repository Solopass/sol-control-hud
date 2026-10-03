"""Obsidian vault notes collector: scans recently modified notes and handles opening/previewing."""
from __future__ import annotations

import json
import os
import time
import urllib.parse
from pathlib import Path
from typing import Any

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
                pass
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
            # Skip hidden and internal directories in-place to avoid descending into them
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
                })
    except Exception:
        pass

    notes.sort(key=lambda x: x["mtime"], reverse=True)
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
        os.startfile(uri)
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
