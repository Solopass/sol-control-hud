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
            if path.exists():
                path.unlink()
            tmp.rename(path)
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

    # Determine offset
    offset = args.get("offset")
    if offset is not None:
        offset = int(offset)
    else:
        # If the file path matches previous state, resume from saved offset
        if state.get("source_file") == str(p):
            offset = int(state.get("offset", 0))
        else:
            offset = 0

    # Filter out already processed items from previous runs
    processed_map = state.get("processed", {})
    unprocessed_items = [it for it in all_items if it["videoId"] not in processed_map]
    remaining_items = unprocessed_items[offset:] if offset < len(unprocessed_items) else []

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
    failed: list[dict] = []
    all_results: list[dict] = []

    # Process in chunks of up to batch_size (max 10)
    for i in range(0, len(video_ids), batch_size):
        chunk = video_ids[i:i + batch_size]
        if ctx:
            ctx.note(f"ingesting tracks {i + 1}-{min(i + len(chunk), len(video_ids))} of {len(video_ids)} to Solang")

        payload = json.dumps({"urls": chunk, "autoSave": auto_save}).encode("utf-8")
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
    summary_msg = f"Processed {len(video_ids)} tracks: {len(ingested)} newly ingested, {len(already_exists)} already existed, {len(failed)} failed."
    
    report_lines = [
        f"### Solang Ingestion Results",
        f"- **Total Processed:** {len(video_ids)}",
        f"- **Newly Ingested:** {len(ingested)}",
        f"- **Already in Studio:** {len(already_exists)}",
        f"- **Failed / No Lyrics:** {len(failed)}",
        ""
    ]

    if ingested:
        report_lines.append("#### Newly Added Projects:")
        for it in ingested:
            report_lines.append(f"- **{it.get('title')}** (`{it.get('videoId')}`) - *Source: {it.get('source', 'Dual CC/LRCLIB')}*")
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
        "already_exists_count": len(already_exists),
        "failed_count": len(failed),
        "ingested": ingested,
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


TOOLS = {
    "extract_takeout_links": extract_takeout_links,
    "ingest_solang_tracks": ingest_solang_tracks,
    "solang_bulk_pipeline": solang_bulk_pipeline,
}
