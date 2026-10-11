"""Overnight AI run orchestration and multi-chain batch sequencing.
Coordinates running a list of chains, controlling display standby, and
generating morning completion reports.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path

from ..away import display_ctl
from . import chain_note

LLM_DIR = Path(os.environ.get("SOL_LLM_DIR", r"D:\AI\Cache\llm"))
OVERNIGHT_STATE = LLM_DIR / "overnight.json"
CHAINS_DIR = Path(os.environ.get("SOL_CHAINS", r"D:\OBVLT\1Notebook\Chains"))
REPORTS_DIR = Path(os.environ.get("SOL_REPORTS", r"D:\OBVLT\reports"))

PRESETS: dict[str, dict] = {
    "full_tidy": {
        "label": "Full Night Tidy",
        "description": "Note Condenser + Monthly digest",
        "chains": ["Note Condenser", "Monthly digest"],
    },
    "code_review": {
        "label": "Code Reviews",
        "description": "Bug hunt across all workspace projects",
        "chains": ["Bug hunt sol-control-hud", "Bug hunt speed-reading", "Bug hunt media-api", "Bug hunt kiiy2k"],
    },
    "vault_only": {
        "label": "Vault Only",
        "description": "Condense notes into digests",
        "chains": ["Note Condenser"],
    },
}


def get_overnight_state() -> dict:
    """Reads current overnight execution state."""
    try:
        if OVERNIGHT_STATE.is_file():
            return json.loads(OVERNIGHT_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {
        "active": False,
        "queue": [],
        "completed": [],
        "current": None,
        "status": "idle",
        "display_mode": "dpms_off",
        "sleep_when_done": False,
        "started_at": None,
    }


def save_overnight_state(state: dict) -> None:
    """Atomically saves overnight state."""
    try:
        OVERNIGHT_STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = OVERNIGHT_STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, OVERNIGHT_STATE)
    except Exception as e:
        print(f"[overnight] Warning: could not save state: {e}")


def list_available_chains(chains_dir: Path | None = None) -> list[dict]:
    """Lists available chains in 1Notebook/Chains for checklist selection."""
    cdir = chains_dir or CHAINS_DIR
    if not cdir.is_dir():
        return []
    chains = []
    for p in cdir.glob("*.md"):
        if p.name.startswith((".", "$")) or p.stem.endswith(" request") or p.stem.lower() in ("readme", "template", "chain"):
            continue
        try:
            c = chain_note.load_chain(p)
            chains.append({
                "name": c.name,
                "path": str(p),
                "model": c.model or "sol-fast",
                "steps": len(c.steps),
                "schedule": c.schedule,
            })
        except Exception:
            chains.append({
                "name": p.stem,
                "path": str(p),
                "model": "sol-fast",
                "steps": 0,
                "schedule": None,
            })
    chains.sort(key=lambda x: x["name"].lower())
    return chains


def start_overnight(
    chains: list[str],
    display_mode: str = "dpms_off",
    sleep_when_done: bool = False,
    chains_dir: Path | None = None,
) -> dict:
    """Starts an overnight batch session with the given list of chains."""
    cdir = chains_dir or CHAINS_DIR
    valid_chains = []
    for name in chains:
        target = cdir / f"{name}.md"
        if target.is_file():
            valid_chains.append(name)
        elif (cdir / name).is_file():
            valid_chains.append(Path(name).stem)

    if not valid_chains:
        return {"ok": False, "why": "No valid chains were found to run."}

    first_chain = valid_chains[0]
    remaining_queue = valid_chains[1:]

    state = {
        "active": True,
        "started_at": time.time(),
        "display_mode": display_mode,
        "sleep_when_done": sleep_when_done,
        "queue": remaining_queue,
        "completed": [],
        "current": first_chain,
        "status": "running",
        "results": {},
    }
    save_overnight_state(state)

    # Trigger the first chain
    first_path = cdir / f"{first_chain}.md"
    try:
        chain_note.set_chain_status(first_path, "queued", skip_wait="true")
    except Exception as e:
        return {"ok": False, "why": f"Could not start first chain '{first_chain}': {e}"}

    # Handle display mode
    if display_mode == "dpms_off":
        display_ctl.turn_monitors_off()
    elif display_mode == "away_screen":
        display_ctl.keep_pc_awake(allow_display_sleep=True)
        display_ctl.launch_away_screen()
    else:
        display_ctl.keep_pc_awake(allow_display_sleep=True)

    return {
        "ok": True,
        "why": f"Overnight session started: running '{first_chain}' ({len(valid_chains)} total)",
        "state": state,
    }


def check_and_advance(chains_dir: Path | None = None) -> dict:
    """Checks progress of active overnight run, advancing to next chain upon completion."""
    state = get_overnight_state()
    if not state.get("active"):
        return state

    current = state.get("current")
    cdir = chains_dir or CHAINS_DIR
    current_path = cdir / f"{current}.md" if current else None

    # Check if current chain has completed
    is_current_done = True
    if current_path and current_path.is_file():
        try:
            c = chain_note.load_chain(current_path)
            # If still queued or running, it is not done yet
            if c.status in ("queued", "running"):
                is_current_done = False
        except Exception:
            is_current_done = True

    if not is_current_done:
        return state

    # Mark current chain as completed
    if current and current not in state["completed"]:
        state["completed"].append(current)

    queue = state.get("queue") or []
    if queue:
        # Advance to next chain in the batch
        next_chain = queue.pop(0)
        state["queue"] = queue
        state["current"] = next_chain
        save_overnight_state(state)

        next_path = cdir / f"{next_chain}.md"
        try:
            chain_note.set_chain_status(next_path, "queued", skip_wait="true")
        except Exception as e:
            print(f"[overnight] Could not queue next chain '{next_chain}': {e}")
    else:
        # Batch is finished!
        state["active"] = False
        state["current"] = None
        state["status"] = "completed"
        save_overnight_state(state)

        # Release keep-awake guard
        display_ctl.release_pc_awake()

        # Generate summary report
        try:
            report_file = generate_night_summary(
                completed=state.get("completed", []),
                started_at=state.get("started_at", time.time()),
            )
            state["report"] = str(report_file)
        except Exception as e:
            print(f"[overnight] Could not generate summary report: {e}")

        # Put system to sleep if user requested it
        if state.get("sleep_when_done"):
            try:
                subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
            except Exception:
                pass

    return state


def cancel_overnight() -> dict:
    """Cancels the active overnight session."""
    state = get_overnight_state()
    state["active"] = False
    state["status"] = "cancelled"
    save_overnight_state(state)
    display_ctl.release_pc_awake()
    return {"ok": True, "why": "Overnight session cancelled", "state": state}


def generate_night_summary(completed: list[str], started_at: float) -> Path:
    """Generates a summary markdown report of the overnight run in reports/."""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    report_file = REPORTS_DIR / f"night-{today}.md"

    elapsed_m = max(1, int((time.time() - started_at) / 60))
    lines = [
        f"# Night Run Summary · {today}",
        "",
        f"**Started:** {datetime.fromtimestamp(started_at).strftime('%H:%M:%S')}  ",
        f"**Duration:** ~{elapsed_m} minutes  ",
        f"**Completed Chains:** {len(completed)}  ",
        "",
        "## Chains Executed",
    ]
    for c in completed:
        lines.append(f"- **{c}**: Completed successfully")

    lines.append("")
    lines.append("## Results & Notes")
    lines.append(f"- Full chain outputs saved to `1Notebook/Chains/Results/`")
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")
    return report_file
