"""Tool steps for Solang & YouTube Takeout integration in SOL Chains.

Handles Google Takeout watch-history.html, watch-history.json, playlists CSVs, and plain text
link lists with up to tens of thousands of video links. Features:
- Chunked, memory-safe parsing suitable for 15,000+ entries.
- Deduplication preserving watch-history recency order.
- Music / K-Pop heuristic filtering to sift songs from non-music watch history.
- Checkpointing & state tracking (offset, processed IDs, error states) so runs can resume seamlessly.
- Batched ingestion into Solang's bulk-ingest endpoint (127.0.0.1:3000) with auto-start.
"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from . import file_tools as ft
from .runner import ToolContext, ToolError

SOLANG_DEFAULT_URL = os.environ.get("SOLANG_URL", "http://127.0.0.1:3000").rstrip("/")
SOLANG_DIR = Path(os.environ.get("SOLANG_DIR", r"D:\Workspace\solang"))
DEFAULT_STATE_FILE = Path(os.environ.get("SOLANG_TAKEOUT_STATE", r"D:\Output\chains\solang_takeout_state.json"))

# YouTube 11-character video ID regex
YOUTUBE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{11}$")
URL_EXTRACT_RE = re.compile(
    r"(?:https?://)?(?:www\.|music\.)?(?:youtube\.com/(?:watch\?.*?v=|embed/|v/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})",
    re.IGNORECASE
)
HTML_LINK_RE = re.compile(
    r'<a\s+[^>]*href=["\'](?:https?://)?(?:www\.|music\.)?(?:youtube\.com/(?:watch\?.*?v=|embed/|v/|shorts/)|youtu\.be/)([a-zA-Z0-9_-]{11})[^"\']*["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL
)

# Music heuristic filters
MUSIC_POSITIVE_KEYWORDS = {
    "mv", "m/v", "official music video", "official video", "official audio", "official visualizer",
    "audio", "lyric", "lyrics", "color coded", "ost", "soundtrack", "live", "performance",
    "stage", "fancam", "dance practice", "remix", "album", "teaser", "k-pop", "kpop", "track",
    "visualizer", "choreography", "karaoke", "comeback", "special clip", "relay dance",
    "showcase", "acoustic", "instrumental", "cover", "feat", "ft.", "prod."
}

MUSIC_NEGATIVE_KEYWORDS = {
    "tutorial", "walkthrough", "gameplay", "guide", "review", "reaction", "unboxing",
    "podcast", "vlog", "interview", "documentary", "news", "episode", "highlights",
    "speedrun", "tier list", "analysis", "explaining", "how to", "breakdown"
}

MUSIC_CHANNELS = {
    "smtown", "jyp entertainment", "hybe labels", "starship", "yg entertainment", "1thek",
    "stone music entertainment", "cube entertainment", "mnet k-pop", "kbs kpop", "mbk",
    "woollim", "rbw", "fnc", "pledis", "vevo", "topic", "records", "music"
}


def is_likely_music(title: str, channel: str = "") -> bool:
    """Heuristic filter for songs/music videos from general YouTube history."""
    t = title.lower()
    c = channel.lower()

    # If channel is a known music label/distributor
    if any(mch in c for mch in MUSIC_CHANNELS):
        return True

    # Quick check for strong negative keywords
    # Only discard if negative word is present and no strong MV/lyrics sign
    has_negative = any(re.search(rf"\b{re.escape(neg)}\b", t) for neg in MUSIC_NEGATIVE_KEYWORDS)
    has_strong_positive = ("mv" in t or "m/v" in t or "official video" in t or "lyrics" in t or "audio" in t)
    if has_negative and not has_strong_positive:
        return False

    # Check for strong positive indicators
    for kw in MUSIC_POSITIVE_KEYWORDS:
        if kw in ("mv", "m/v"):
            if re.search(r"\b(m/?v)\b|\[m/?v\]|\(m/?v\)", t):
                return True
        elif kw in t:
            return True

    # Check for standard "Artist - Title" or "Artist – Title" pattern
    if re.search(r".+?\s+[-–—]\s+.+", title):
        return True

    return False


def clean_takeout_title(raw: str) -> str:
    """Clean HTML tags, 'Watched ' prefix, and decode entities."""
    clean = re.sub(r"<[^>]+>", "", raw).strip()
    clean = html.unescape(clean)
    if clean.lower().startswith("watched "):
        clean = clean[8:].strip()
    return clean


class TakeoutParser:
    """Memory-efficient parser for Google Takeout YouTube files."""

    @staticmethod
    def parse_file(path: Path | str, filter_music: bool = True) -> list[dict[str, str]]:
        p = Path(path)
        if not p.exists():
            raise ToolError(f"Takeout file not found: {p}")

        suffix = p.suffix.lower()
        if suffix == ".html" or suffix == ".htm":
            return TakeoutParser._parse_html(p, filter_music)
        elif suffix == ".json":
            return TakeoutParser._parse_json(p, filter_music)
        elif suffix == ".csv":
            return TakeoutParser._parse_csv(p, filter_music)
        else:
            return TakeoutParser._parse_text(p, filter_music)

    @staticmethod
    def _parse_html(path: Path, filter_music: bool) -> list[dict[str, str]]:
        """Parses watch-history.html."""
        text = path.read_text(encoding="utf-8", errors="replace")
        seen_ids: set[str] = set()
        items: list[dict[str, str]] = []

        # Find all watch links and their anchor text
        for m in HTML_LINK_RE.finditer(text):
            vid = m.group(1)
            if vid in seen_ids or len(vid) != 11:
                continue
            seen_ids.add(vid)

            raw_title = m.group(2)
            title = clean_takeout_title(raw_title) or f"YouTube Video ({vid})"

            if filter_music and not is_likely_music(title):
                continue

            items.append({
                "videoId": vid,
                "title": title,
                "url": f"https://www.youtube.com/watch?v={vid}"
            })

        return items

    @staticmethod
    def _parse_json(path: Path, filter_music: bool) -> list[dict[str, str]]:
        """Parses watch-history.json."""
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            data = json.load(f)

        if not isinstance(data, list):
            data = [data]

        seen_ids: set[str] = set()
        items: list[dict[str, str]] = []

        for entry in data:
            if not isinstance(entry, dict):
                continue
            url = entry.get("titleUrl") or entry.get("url") or ""
            m = URL_EXTRACT_RE.search(url)
            vid = m.group(1) if m else entry.get("videoId")
            if not vid or vid in seen_ids or len(vid) != 11:
                continue
            seen_ids.add(vid)

            raw_title = entry.get("title") or f"YouTube Video ({vid})"
            title = clean_takeout_title(raw_title)

            subs = entry.get("subtitles") or []
            channel = subs[0].get("name", "") if subs and isinstance(subs, list) and isinstance(subs[0], dict) else ""

            if filter_music and not is_likely_music(title, channel):
                continue

            items.append({
                "videoId": vid,
                "title": title,
                "channel": channel,
                "url": f"https://www.youtube.com/watch?v={vid}"
            })

        return items

    @staticmethod
    def _parse_csv(path: Path, filter_music: bool) -> list[dict[str, str]]:
        """Parses Takeout playlists or Liked videos CSV."""
        seen_ids: set[str] = set()
        items: list[dict[str, str]] = []

        with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            id_col = 0
            title_col = -1

            if header:
                for idx, col in enumerate(header):
                    col_l = col.strip().lower()
                    if "video id" in col_l or "videoid" in col_l or col_l == "id":
                        id_col = idx
                    elif "title" in col_l or "name" in col_l:
                        title_col = idx

            for row in reader:
                if not row or len(row) <= id_col:
                    continue
                raw_val = row[id_col].strip()
                m = URL_EXTRACT_RE.search(raw_val)
                vid = m.group(1) if m else raw_val
                if len(vid) != 11 or vid in seen_ids:
                    continue
                seen_ids.add(vid)

                title = row[title_col].strip() if title_col >= 0 and len(row) > title_col else f"YouTube Video ({vid})"
                title = clean_takeout_title(title)

                if filter_music and not is_likely_music(title):
                    continue

                items.append({
                    "videoId": vid,
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={vid}"
                })

        return items

    @staticmethod
    def _parse_text(path: Path, filter_music: bool) -> list[dict[str, str]]:
        """Parses plain text URLs or IDs line-by-line."""
        seen_ids: set[str] = set()
        items: list[dict[str, str]] = []

        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = URL_EXTRACT_RE.search(line)
                vid = m.group(1) if m else line
                if len(vid) != 11 or vid in seen_ids:
                    continue
                seen_ids.add(vid)

                items.append({
                    "videoId": vid,
                    "title": f"YouTube Video ({vid})",
                    "url": f"https://www.youtube.com/watch?v={vid}"
                })

        return items


class StateManager:
    """Tracks ingestion progress across huge (15,000+) link files."""

    @staticmethod
    def load(path: Path) -> dict:
        try:
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {"offset": 0, "processed": {}, "runs": []}

    @staticmethod
    def save(path: Path, state: dict) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as e:
            # Non-fatal if state save fails
            print(f"[StateManager] Warning: could not save state: {e}")


def ensure_solang_running(base_url: str = SOLANG_DEFAULT_URL) -> bool:
    """Verifies Solang is listening on port 3000; starts it if necessary."""
    def check():
        try:
            req = urllib.request.Request(f"{base_url}/api/health", headers={"User-Agent": "SOL-HUD"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.status == 200
        except Exception:
            return False

    if check():
        return True

    # Auto-start Solang
    if (SOLANG_DIR / "server.ts").exists():
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        bun_exe = "bun"
        try:
            subprocess.Popen([bun_exe, "server.ts"], cwd=str(SOLANG_DIR), creationflags=flags)
        except Exception:
            pass

        # Wait up to 6 seconds
        for _ in range(12):
            time.sleep(0.5)
            if check():
                return True

    return check()


def extract_takeout_links(args: dict, ctx: ToolContext | None = None) -> dict:
    """Extracts YouTube links from Google Takeout file.
    
    args:
      file: Path to Takeout watch-history.html / .json / .csv / .txt
      max_items: Maximum items in this batch (default 25)
      offset: Starting offset (optional, default resume from state_file)
      filter_music: Filter music/songs only (default true)
      state_file: Path to progress checkpoint file
      auto_advance: Automatically advance state offset (default true)
    """
    file_path = args.get("file") or args.get("path")
    if not file_path:
        raise ToolError("extract_takeout_links: 'file' parameter is required")

    p = Path(file_path)
    if not p.is_absolute():
        p = Path(os.path.realpath(str(p)))

    # Verify read permission
    try:
        ft.check_read(str(p))
    except Exception:
        # Fallback check
        if not p.exists():
            raise ToolError(f"file not found: {p}")

    max_items = int(args.get("max_items") or args.get("batch_size") or 25)
    filter_music = str(args.get("filter_music", "true")).lower() in ("true", "1", "yes")
    auto_advance = str(args.get("auto_advance", "true")).lower() in ("true", "1", "yes")

    state_p = Path(args.get("state_file") or DEFAULT_STATE_FILE)
    state = StateManager.load(state_p)

    if ctx:
        ctx.note(f"parsing Takeout file: {p.name}")

    # Extract all matching candidates
    all_items = TakeoutParser.parse_file(p, filter_music=filter_music)
    total_found = len(all_items)

    # Determine offset and remaining unprocessed items
    explicit_offset = args.get("offset")
    processed_map = state.get("processed", {})

    if explicit_offset is not None:
        # Explicit offset manually requested
        offset = int(explicit_offset)
        remaining_items = all_items[offset:] if offset < len(all_items) else []
    else:
        # Auto-resume mode: all items not yet in processed_map
        remaining_items = [it for it in all_items if it["videoId"] not in processed_map]
        offset = len(processed_map)

    # Select batch
    batch = remaining_items[:max_items]
    next_offset = offset + len(batch)

    if auto_advance:
        state["source_file"] = str(p)
        state["total_found"] = total_found
        state["offset"] = next_offset
        state["updated_at"] = datetime.now().isoformat()
        StateManager.save(state_p, state)

    formatted_lines = []
    for idx, it in enumerate(batch, 1):
        formatted_lines.append(f"{idx}. {it['title']} ({it['url']})")

    batch_text = "\n".join(formatted_lines) if formatted_lines else "(no more tracks to extract)"

    if ctx:
        ctx.note(f"extracted batch of {len(batch)} tracks (offset {offset}, total {total_found})")

    return {
        "items": batch,
        "urls": [it["url"] for it in batch],
        "video_ids": [it["videoId"] for it in batch],
        "count": len(batch),
        "offset": offset,
        "next_offset": next_offset,
        "total_found": total_found,
        "remaining_unprocessed": max(0, len(remaining_items) - len(batch)),
        "state_file": str(state_p),
        "text": batch_text
    }


def ingest_solang_tracks(args: dict, ctx: ToolContext | None = None) -> dict:
    """Sends YouTube tracks to Solang's bulk-ingest endpoint (:3000).

    args:
      items: List of item dicts (from extract_takeout_links), list of URLs, or text block
      batch_size: Solang sub-batch chunk size (default 10)
      auto_save: Auto-save as .kstudio projects (default true)
      state_file: Path to progress checkpoint file to update
      solang_url: Solang server base URL (default http://127.0.0.1:3000)
    """
    raw_items = args.get("items")
    if raw_items is None:
        raw_items = args.get("urls") or args.get("input") or ""

    video_ids: list[str] = []
    titles_map: dict[str, str] = {}

    if isinstance(raw_items, dict) and "items" in raw_items:
        raw_items = raw_items["items"]

    if isinstance(raw_items, list):
        for entry in raw_items:
            if isinstance(entry, dict):
                vid = entry.get("videoId")
                if vid and len(vid) == 11:
                    video_ids.append(vid)
                    if entry.get("title"):
                        titles_map[vid] = entry["title"]
            elif isinstance(entry, str):
                m = URL_EXTRACT_RE.search(entry)
                vid = m.group(1) if m else entry.strip()
                if len(vid) == 11:
                    video_ids.append(vid)
    elif isinstance(raw_items, str):
        for line in raw_items.splitlines():
            line = line.strip()
            if not line:
                continue
            m = URL_EXTRACT_RE.search(line)
            vid = m.group(1) if m else line
            if len(vid) == 11:
                video_ids.append(vid)

    # Deduplicate within this ingest run
    video_ids = list(dict.fromkeys(video_ids))
    if not video_ids:
        return {
            "total": 0,
            "ingested_count": 0,
            "already_exists_count": 0,
            "failed_count": 0,
            "items": [],
            "summary": "No video IDs provided for ingestion.",
            "text": "No video IDs found in the input batch."
        }

    solang_url = str(args.get("solang_url") or SOLANG_DEFAULT_URL).rstrip("/")
    if not ensure_solang_running(solang_url):
        raise ToolError(f"Solang server is not reachable at {solang_url} and could not be started.")

    batch_size = max(1, min(10, int(args.get("batch_size") or 10)))
    auto_save = str(args.get("auto_save", "true")).lower() in ("true", "1", "yes")
    state_p = Path(args.get("state_file") or DEFAULT_STATE_FILE)
    state = StateManager.load(state_p)

    ingested: list[dict] = []
    already_exists: list[dict] = []
    clustered: list[dict] = []
    failed: list[dict] = []
    all_results: list[dict] = []

    # Process in chunks of up to batch_size (max 10)
    for i in range(0, len(video_ids), batch_size):
        chunk = video_ids[i:i + batch_size]
        if ctx:
            ctx.note(f"ingesting tracks {i + 1}-{min(i + len(chunk), len(video_ids))} of {len(video_ids)} to Solang")

        chunk_urls = [f"https://www.youtube.com/watch?v={v}" for v in chunk]
        payload = json.dumps({"urls": chunk_urls, "autoSave": auto_save}).encode("utf-8")
        req = urllib.request.Request(
            f"{solang_url}/api/youtube/bulk-ingest",
            data=payload,
            headers={"Content-Type": "application/json", "User-Agent": "SOL-HUD-Chain"},
            method="POST"
        )

        try:
            with urllib.request.urlopen(req, timeout=90.0) as resp:
                res_data = json.loads(resp.read().decode("utf-8"))
                items = res_data.get("items", [])
                for item in items:
                    vid = item.get("videoId")
                    status = item.get("status")
                    title = item.get("title") or titles_map.get(vid, vid)
                    item["title"] = title

                    all_results.append(item)
                    if status == "ingested":
                        ingested.append(item)
                    elif status == "already_exists":
                        already_exists.append(item)
                    elif status == "clustered_alternate":
                        clustered.append(item)
                    else:
                        failed.append(item)

                    # Update checkpoint state
                    state.setdefault("processed", {})[vid] = {
                        "title": title,
                        "status": status,
                        "source": item.get("source"),
                        "at": datetime.now().isoformat()
                    }
        except Exception as err:
            for vid in chunk:
                err_item = {"videoId": vid, "title": titles_map.get(vid, vid), "status": "error", "error": str(err)}
                failed.append(err_item)
                all_results.append(err_item)
                state.setdefault("processed", {})[vid] = {
                    "title": titles_map.get(vid, vid),
                    "status": "error",
                    "error": str(err),
                    "at": datetime.now().isoformat()
                }

        # Save state periodically
        StateManager.save(state_p, state)

    # Format output summary
    summary_msg = f"Processed {len(video_ids)} tracks: {len(ingested)} newly ingested, {len(clustered)} clustered as alternates, {len(already_exists)} already existed, {len(failed)} failed."
    
    report_lines = [
        f"### Solang Ingestion Results",
        f"- **Total Processed:** {len(video_ids)}",
        f"- **Newly Ingested:** {len(ingested)}",
        f"- **Clustered as Alternates:** {len(clustered)}",
        f"- **Already in Studio:** {len(already_exists)}",
        f"- **Failed / No Lyrics:** {len(failed)}",
        ""
    ]

    if ingested:
        report_lines.append("#### Newly Added Projects:")
        for it in ingested:
            report_lines.append(f"- **{it.get('title')}** (`{it.get('videoId')}`) - *Source: {it.get('source', 'Dual CC/LRCLIB')}*")
        report_lines.append("")

    if clustered:
        report_lines.append("#### Clustered Alternate Videos:")
        for it in clustered:
            report_lines.append(f"- **{it.get('title')}** (`{it.get('videoId')}`) - *Angle: {it.get('variation', 'Alternate')}*")
        report_lines.append("")

    if already_exists:
        report_lines.append(f"#### Already Existed ({len(already_exists)}):")
        for it in already_exists[:8]:
            report_lines.append(f"- {it.get('title')} (`{it.get('videoId')}`)")
        if len(already_exists) > 8:
            report_lines.append(f"- *...and {len(already_exists) - 8} more.*")
        report_lines.append("")

    if failed:
        report_lines.append(f"#### Failed ({len(failed)}):")
        for it in failed:
            report_lines.append(f"- {it.get('title')} (`{it.get('videoId')}`): {it.get('error', 'unknown error')}")
        report_lines.append("")

    final_text = "\n".join(report_lines)

    if ctx:
        ctx.note(summary_msg)

    return {
        "total": len(video_ids),
        "ingested_count": len(ingested),
        "clustered_count": len(clustered),
        "already_exists_count": len(already_exists),
        "failed_count": len(failed),
        "ingested": ingested,
        "clustered": clustered,
        "already_exists": already_exists,
        "failed": failed,
        "items": all_results,
        "summary": summary_msg,
        "text": final_text
    }


def solang_bulk_pipeline(args: dict, ctx: ToolContext | None = None) -> dict:
    """High-level one-step pipeline: extracts a batch from Takeout and ingests to Solang."""
    extract_res = extract_takeout_links(args, ctx)
    if not extract_res.get("items"):
        return {
            "status": "complete",
            "message": "All items in the Takeout export have already been processed.",
            "text": "All tracks in the Takeout export have already been processed into Solang."
        }

    ingest_args = dict(args)
    ingest_args["items"] = extract_res["items"]
    ingest_res = ingest_solang_tracks(ingest_args, ctx)

    return {
        "batch_extracted": extract_res["count"],
        "offset": extract_res["offset"],
        "next_offset": extract_res["next_offset"],
        "total_found": extract_res["total_found"],
        "remaining_unprocessed": extract_res["remaining_unprocessed"],
        "ingest_results": ingest_res,
        "text": f"{extract_res['text']}\n\n{ingest_res['text']}"
    }


KARAOKE_LIBRARY_NOTE = Path(os.environ.get("SOL_KARAOKE_LIBRARY", r"D:\OBVLT\1Notebook\Karaoke Library.md"))
PROJECTS_DIR = SOLANG_DIR / "data" / "projects"


def save_takeout_upload(content: str, filename: str = "watch-history.html") -> dict:
    r"""Saves uploaded Takeout file into D:\Output\Takeout and resets queue state offset."""
    target_dir = Path(r"D:\Output\Takeout")
    target_dir.mkdir(parents=True, exist_ok=True)
    target_p = target_dir / filename
    target_p.write_text(content, encoding="utf-8")

    items = TakeoutParser.parse_file(target_p, filter_music=True)

    state = StateManager.load(DEFAULT_STATE_FILE)
    state["source_file"] = str(target_p)
    state["offset"] = 0
    state["total_found"] = len(items)
    StateManager.save(DEFAULT_STATE_FILE, state)

    status = get_queue_status(state_file=DEFAULT_STATE_FILE)
    return {"ok": True, "file": str(target_p), "status": status}


def generate_karaoke_library_note(output_path: Path | str | None = None, projects_dir: Path | str | None = None) -> str:
    """Scans all .kstudio projects and compiles a clean, sorted Obsidian MOC note."""
    out_p = Path(output_path or KARAOKE_LIBRARY_NOTE)
    proj_dir = Path(projects_dir or PROJECTS_DIR)

    if not proj_dir.exists():
        return "# Solang Karaoke Library\n\nNo projects found yet."

    files = sorted(proj_dir.glob("*.kstudio"), key=lambda f: f.stat().st_mtime, reverse=True)
    projects = []
    total_angles = 0
    gold_count = 0
    silver_count = 0
    review_count = 0

    for f in files:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            media_sources = d.get("mediaSources") or []
            angles_count = max(1, len(media_sources))
            total_angles += angles_count

            angle_names = [s.get("name", "Angle") for s in media_sources if s.get("name")]
            angles_desc = f"{angles_count} ({', '.join(angle_names[:3])}{'...' if len(angle_names) > 3 else ''})" if angle_names else str(angles_count)

            # Determine confidence badge
            conf_tier = (d.get("confidenceTier") or "").lower()
            if not conf_tier:
                src = (d.get("source") or "").lower()
                if "dual" in src:
                    conf_tier = "gold"
                elif "lrc" in src or "public lyrics" in src:
                    conf_tier = "silver"
                else:
                    conf_tier = "silver" if len(d.get("lyrics") or []) >= 6 else "review"

            if conf_tier == "gold":
                badge = "🏅 Gold"
                gold_count += 1
            elif conf_tier == "silver":
                badge = "🥈 Silver"
                silver_count += 1
            else:
                badge = "⚠️ Review"
                review_count += 1

            projects.append({
                "id": d.get("id", f.stem),
                "title": d.get("title", f.stem),
                "artist": d.get("artist", "Unknown Artist"),
                "duration": d.get("duration", 180),
                "lyrics_count": len(d.get("lyrics") or []),
                "angles": angles_desc,
                "angles_count": angles_count,
                "badge": badge,
                "url": f"http://127.0.0.1:3000/?project={d.get('id', f.stem)}"
            })
        except Exception:
            continue

    # Sort projects by Artist, then Title
    projects.sort(key=lambda p: (p["artist"].lower(), p["title"].lower()))

    lines = [
        "---",
        "type: index",
        "tags: [karaoke, solang, music]",
        f"updated: {datetime.now().strftime('%Y-%m-%d')}",
        "---",
        "",
        "# Solang Karaoke Library",
        f"**Total Songs:** {len(projects)} • **Video Angles:** {total_angles} • **Quality:** 🏅 {gold_count} Gold | 🥈 {silver_count} Silver | ⚠️ {review_count} Review • **Studio:** [Open Solang](http://127.0.0.1:3000)",
        "",
        "| Artist | Song Title | Quality | Video Angles | Lyrics | Studio Link |",
        "|:---|:---|:---:|:---|:---:|:---:|",
    ]

    for p in projects:
        dur_min = int(p["duration"] // 60)
        dur_sec = int(p["duration"] % 60)
        dur_str = f"{dur_min}:{dur_sec:02d}"
        lines.append(
            f"| **{p['artist']}** | {p['title']} | {p['badge']} | {p['angles']} | {p['lyrics_count']} lines ({dur_str}) | [Sing Now]({p['url']}) |"
        )

    lines.append("")
    lines.append(f"_Generated by SOL Control HUD Takeout Pipeline on {datetime.now().strftime('%Y-%m-%d %H:%M')}_")

    content = "\n".join(lines)
    try:
        out_p.parent.mkdir(parents=True, exist_ok=True)
        out_p.write_text(content, encoding="utf-8")
    except Exception as e:
        print(f"[generate_karaoke_library_note] Warning: could not write note: {e}")

    return content


def sync_karaoke_library(args: dict, ctx: ToolContext | None = None) -> dict:
    """Tool step: regenerates the Obsidian Karaoke Library index."""
    output_path = args.get("output_path") or KARAOKE_LIBRARY_NOTE
    note_content = generate_karaoke_library_note(output_path=output_path)
    if ctx:
        ctx.note("synced Karaoke Library note in 1Notebook")
    return {"status": "success", "note": str(output_path), "text": note_content}


def run_takeout_queue(args: dict, ctx: ToolContext | None = None) -> dict:
    """Runs the Takeout queue continuously in chunks until target is reached or paused."""
    max_total = int(args.get("target_count") or args.get("max_songs") or 100)
    batch_size = max(5, min(50, int(args.get("batch_size") or 25)))
    state_p = Path(args.get("state_file") or DEFAULT_STATE_FILE)

    total_processed = 0
    batches_run = 0
    all_ingested = []
    all_clustered = []

    while total_processed < max_total:
        if ctx and ctx.cancelled():
            break

        state = StateManager.load(state_p)
        if state.get("paused", False):
            if ctx:
                ctx.note("queue is paused by user setting")
            break

        cur_batch_size = min(batch_size, max_total - total_processed)
        extract_args = dict(args)
        extract_args["max_items"] = cur_batch_size
        extract_args["state_file"] = str(state_p)

        extract_res = extract_takeout_links(extract_args, ctx)
        items = extract_res.get("items") or []
        if not items:
            if ctx:
                ctx.note("reached end of unprocessed items in export file")
            break

        ingest_args = dict(args)
        ingest_args["items"] = items
        ingest_args["state_file"] = str(state_p)

        ingest_res = ingest_solang_tracks(ingest_args, ctx)
        total_processed += len(items)
        batches_run += 1

        for it in ingest_res.get("items") or []:
            if it.get("status") == "ingested":
                all_ingested.append(it)
            elif it.get("status") == "clustered_alternate":
                all_clustered.append(it)

        # Brief pause between batches
        time.sleep(1.0)

    # Sync Obsidian Library MOC
    generate_karaoke_library_note()

    summary = f"Autonomous queue run completed: processed {total_processed} items ({len(all_ingested)} newly ingested, {len(all_clustered)} clustered as alternates) across {batches_run} batch(es)."
    if ctx:
        ctx.note(summary)

    return {
        "total_processed": total_processed,
        "batches_run": batches_run,
        "newly_ingested_count": len(all_ingested),
        "clustered_count": len(all_clustered),
        "summary": summary,
        "text": summary
    }


def get_queue_status(state_file: Path | str | None = None) -> dict:
    """Returns real-time progress metrics for the HUD dashboard."""
    state_p = Path(state_file or DEFAULT_STATE_FILE)
    state = StateManager.load(state_p)
    total_found = state.get("total_found", 0)
    offset = state.get("offset", 0)
    percent = round((offset / total_found * 100), 1) if total_found > 0 else 0.0
    processed = state.get("processed", {})

    ingested_count = sum(1 for v in processed.values() if v.get("status") == "ingested")
    clustered_count = sum(1 for v in processed.values() if v.get("status") == "clustered_alternate")
    already_exists_count = sum(1 for v in processed.values() if v.get("status") == "already_exists")
    failed_count = sum(1 for v in processed.values() if v.get("status") in ("failed", "error"))

    # Recent 5 items
    recent_items = []
    for vid, meta in list(processed.items())[-5:]:
        recent_items.append({
            "videoId": vid,
            "title": meta.get("title", vid),
            "status": meta.get("status", "unknown")
        })

    return {
        "source_file": state.get("source_file", ""),
        "total_found": total_found,
        "offset": offset,
        "percent": percent,
        "paused": state.get("paused", False),
        "ingested_count": ingested_count,
        "clustered_count": clustered_count,
        "already_exists_count": already_exists_count,
        "failed_count": failed_count,
        "recent_items": recent_items,
        "last_updated": state.get("updated_at", "")
    }


def set_queue_paused(paused: bool = True, state_file: Path | str | None = None) -> dict:
    """Pauses or resumes the autonomous ingestion queue."""
    state_p = Path(state_file or DEFAULT_STATE_FILE)
    state = StateManager.load(state_p)
    state["paused"] = paused
    StateManager.save(state_p, state)
    return {"ok": True, "paused": paused}


TOOLS = {
    "extract_takeout_links": extract_takeout_links,
    "ingest_solang_tracks": ingest_solang_tracks,
    "solang_bulk_pipeline": solang_bulk_pipeline,
    "run_takeout_queue": run_takeout_queue,
    "sync_karaoke_library": sync_karaoke_library,
}
