"""Real-time local AI inference operations, reasoning telemetry and thought stream reader.

Telemetry is passively parsed from D:\\AI\\Cache\\llm\\router.log and live thought buffer.
RULE: Never call :11440/slots (counts as usage and keeps models loaded).
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

LLM_DIR = Path(os.environ.get("SOL_LLM_DIR", r"D:\AI\Cache\llm"))
ROUTER_LOG = LLM_DIR / "router.log"
THOUGHT_FILE = Path(os.environ.get("SOL_LLM_THOUGHT", str(LLM_DIR / "live_thought.json")))
TAIL_BYTES = 128 * 1024

_PORT = re.compile(r"^\[(\d+)\]")
_LAUNCH = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bslot\s+launch_slot_:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)")
_RELEASE = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bslot\s+release:\s+id\s+(\d+)\s*\|\s*task\s+(\d+).*?n_tokens\s*=\s*(\d+)")
_N_GEN = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bprint_timing:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*n_gen\s*=\s*(\d+),\s*tg\s*=\s*([\d.]+)\s*t/s(?:,\s*tg_3s\s*=\s*([\d.]+)\s*t/s)?")
_PROMPT_EVAL = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bprint_timing:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*prompt eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second")
_EVAL = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bprint_timing:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*eval time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens.*?([\d.]+)\s*tokens per second")
_TOTAL = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bprint_timing:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*total time\s*=\s*([\d.]+)\s*ms\s*/\s*(\d+)\s*tokens")
_DRAFT = re.compile(r"(?:\[(\d+)\]\s+)?.*?\bprint_timing:\s+id\s+(\d+)\s*\|\s*task\s+(\d+)\s*\|\s*draft acceptance\s*=\s*([\d.]+)\s*\(\s*(\d+)\s*accepted\s*/\s*(\d+)\s*generated\)(?:,\s*mean len\s*=\s*([\d.]+))?")
_PROXY = re.compile(r"proxying request to model\s+([a-zA-Z0-9_\-]+)\s+on port\s+(\d+)")


def _tail(path: Path, nbytes: int = TAIL_BYTES) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - nbytes))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def parse_router_events(text: str) -> dict:
    """Extract inference operations, speed, and status from llama.cpp router logs."""
    slots: dict[int, dict] = {}
    last_completed: dict | None = None
    last_port: int | None = None
    last_model_proxy: str | None = None

    for line in text.splitlines():
        if not line.strip():
            continue

        m_prox = _PROXY.search(line)
        if m_prox:
            last_model_proxy = m_prox.group(1)
            last_port = int(m_prox.group(2))

        m_port = _PORT.match(line)
        line_port = int(m_port.group(1)) if m_port else last_port

        m_launch = _LAUNCH.search(line)
        if m_launch:
            port = int(m_launch.group(1)) if m_launch.group(1) else line_port
            slot_id = int(m_launch.group(2))
            task_id = int(m_launch.group(3))
            slots[slot_id] = {
                "active": True,
                "slot": slot_id,
                "task": task_id,
                "port": port,
                "phase": "started",
                "tg_tps": None,
                "tg_3s_tps": None,
                "n_gen": 0,
                "prompt_ms": None,
                "prompt_tokens": None,
                "prompt_tps": None,
                "eval_ms": None,
                "eval_tokens": None,
                "eval_tps": None,
                "draft_rate": None,
                "draft_accepted": None,
                "draft_generated": None,
                "draft_mean_len": None,
            }
            continue

        m_ngen = _N_GEN.search(line)
        if m_ngen:
            slot_id = int(m_ngen.group(2))
            st = slots.setdefault(slot_id, {"active": True, "slot": slot_id, "task": int(m_ngen.group(3))})
            st["phase"] = "generating"
            st["n_gen"] = int(m_ngen.group(4))
            st["tg_tps"] = round(float(m_ngen.group(5)), 1)
            if m_ngen.group(6):
                st["tg_3s_tps"] = round(float(m_ngen.group(6)), 1)
            continue

        m_prompt = _PROMPT_EVAL.search(line)
        if m_prompt:
            slot_id = int(m_prompt.group(2))
            st = slots.setdefault(slot_id, {"active": True, "slot": slot_id, "task": int(m_prompt.group(3))})
            st["prompt_ms"] = round(float(m_prompt.group(4)), 1)
            st["prompt_tokens"] = int(m_prompt.group(5))
            st["prompt_tps"] = round(float(m_prompt.group(6)), 1)
            continue

        m_eval = _EVAL.search(line)
        if m_eval:
            slot_id = int(m_eval.group(2))
            st = slots.setdefault(slot_id, {"active": True, "slot": slot_id, "task": int(m_eval.group(3))})
            st["eval_ms"] = round(float(m_eval.group(4)), 1)
            st["eval_tokens"] = int(m_eval.group(5))
            st["eval_tps"] = round(float(m_eval.group(6)), 1)
            continue

        m_draft = _DRAFT.search(line)
        if m_draft:
            slot_id = int(m_draft.group(2))
            st = slots.setdefault(slot_id, {"active": True, "slot": slot_id, "task": int(m_draft.group(3))})
            st["draft_rate"] = round(float(m_draft.group(4)), 3)
            st["draft_accepted"] = int(m_draft.group(5))
            st["draft_generated"] = int(m_draft.group(6))
            if m_draft.group(7):
                st["draft_mean_len"] = round(float(m_draft.group(7)), 2)
            continue

        m_rel = _RELEASE.search(line)
        if m_rel:
            slot_id = int(m_rel.group(2))
            st = slots.pop(slot_id, None)
            if st:
                st["active"] = False
                st["phase"] = "completed"
                st["total_tokens"] = int(m_rel.group(4))
                last_completed = st
            else:
                last_completed = {
                    "active": False,
                    "slot": slot_id,
                    "task": int(m_rel.group(3)),
                    "total_tokens": int(m_rel.group(4)),
                    "phase": "completed",
                }

    active_slot = next((s for s in slots.values() if s.get("active")), None)
    return {
        "active": active_slot is not None,
        "current": active_slot,
        "last": last_completed,
        "proxied_model": last_model_proxy,
    }


def read_live_thought(path: Path = THOUGHT_FILE, max_age_s: float = 45.0) -> dict:
    """Read the live thinking stream buffer."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        updated_at = float(data.get("updated_at") or 0)
        # If marked active but stale beyond max_age_s, engine or client stopped
        if data.get("active") and (time.time() - updated_at > max_age_s):
            data["active"] = False
            data["phase"] = "idle"
        return data
    except (OSError, ValueError):
        return {
            "active": False,
            "phase": "idle",
            "model": None,
            "task": None,
            "thinking": "",
            "preview": "",
            "tokens": 0,
            "started_at": None,
            "updated_at": None,
            "thinking_s": None,
        }


def write_live_thought(data: dict, path: Path = THOUGHT_FILE) -> None:
    """Write thought stream chunk atomically."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def sample_ai_ops(procs: dict | None = None, router_log: Path = ROUTER_LOG, thought_file: Path = THOUGHT_FILE) -> dict:
    """Aggregate real-time inference telemetry and live thinking state for HUD dashboard."""
    log_text = _tail(router_log)
    events = parse_router_events(log_text)
    thought = read_live_thought(thought_file)

    by_port = {int(v["port"]): alias for alias, v in (procs or {}).items() if isinstance(v, dict) and v.get("port")}

    cur = events.get("current") or {}
    last = events.get("last") or {}

    active_port = cur.get("port")
    active_model = by_port.get(active_port) if active_port else events.get("proxied_model") or thought.get("model")

    is_busy = bool(events.get("active") or thought.get("active"))
    phase = thought.get("phase") if thought.get("active") else (cur.get("phase") or ("busy" if is_busy else "idle"))

    return {
        "available": True,
        "active": is_busy,
        "phase": phase,
        "model": active_model or thought.get("model"),
        "task": thought.get("task") or (f"task #{cur.get('task')}" if cur.get("task") else None),
        "speed": {
            "tg_tps": cur.get("tg_tps") or last.get("eval_tps"),
            "tg_3s_tps": cur.get("tg_3s_tps"),
            "prompt_tps": cur.get("prompt_tps") or last.get("prompt_tps"),
            "prompt_ms": cur.get("prompt_ms") or last.get("prompt_ms"),
            "prompt_tokens": cur.get("prompt_tokens") or last.get("prompt_tokens"),
            "eval_tps": last.get("eval_tps"),
            "eval_ms": last.get("eval_ms"),
            "eval_tokens": last.get("eval_tokens") or cur.get("n_gen"),
            "draft_rate": cur.get("draft_rate") or last.get("draft_rate"),
            "draft_accepted": cur.get("draft_accepted") or last.get("draft_accepted"),
            "draft_generated": cur.get("draft_generated") or last.get("draft_generated"),
            "draft_mean_len": cur.get("draft_mean_len") or last.get("draft_mean_len"),
        },
        "thinking": {
            "active": bool(thought.get("active")),
            "phase": thought.get("phase", "idle"),
            "text": thought.get("thinking", ""),
            "preview": thought.get("preview", ""),
            "tokens": thought.get("tokens", 0),
            "started_at": thought.get("started_at"),
            "updated_at": thought.get("updated_at"),
            "thinking_s": thought.get("thinking_s"),
        },
    }
