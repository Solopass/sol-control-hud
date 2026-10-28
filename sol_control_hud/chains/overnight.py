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
        "description": "Deep review across all workspace projects",
        "chains": ["Deep review sol-control-hud", "Deep review speed-reading", "Deep review media-api", "Deep review kiiy2k"],
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
            st = json.loads(OVERNIGHT_STATE.read_text(encoding="utf-8"))
            st.setdefault("bedtime_autostart", False)
            st.setdefault("selected_chains", [])
            return st
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
        "bedtime_autostart": False,
        "selected_chains": [],
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

    prev_state = get_overnight_state()
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
        "bedtime_autostart": prev_state.get("bedtime_autostart", False),
        "selected_chains": prev_state.get("selected_chains", []),
    }
    # Trigger the first chain
    first_path = cdir / f"{first_chain}.md"
    try:
        chain_note.set_chain_status(first_path, "queued", skip_wait="true")
    except Exception as e:
        return {"ok": False, "why": f"Could not start first chain '{first_chain}': {e}"}

    save_overnight_state(state)

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
    current_status = "done"
    if current_path and current_path.is_file():
        # read only the note's properties: before 2026-10-28 this parsed the whole chain and read a `status` attribute
        # the Chain object doesn't have (or failed to parse it), so every chain counted as "done" and a batch
        # "finished" on its first tick, releasing the keep-awake while its chains were still queued
        current_status = str(chain_note.chain_front(current_path).get("status") or "done").lower()
        # If still queued, running, or waiting, it is not done yet
        if current_status in ("queued", "running", "waiting"):
            is_current_done = False

    # Also check live chains.json to ensure daemon is not actively processing or holding this chain
    if is_current_done and current:
        try:
            cj_path = LLM_DIR / "chains.json"
            if cj_path.is_file():
                cj = json.loads(cj_path.read_text(encoding="utf-8"))
                running_info = cj.get("running")
                if running_info and running_info.get("chain") == current:
                    is_current_done = False
                pending = [p.get("chain") for p in cj.get("pending", []) if isinstance(p, dict)]
                if current in pending:
                    is_current_done = False
        except Exception:
            pass

    if not is_current_done:
        return state

    # Mark current chain as completed
    if current:
        if current not in state["completed"]:
            state["completed"].append(current)
        state.setdefault("results", {})[current] = current_status

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
                results=state.get("results", {}),
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
    state["queue"] = []
    state["current"] = None
    state["status"] = "cancelled"
    save_overnight_state(state)
    display_ctl.release_pc_awake()
    return {"ok": True, "why": "Overnight session cancelled", "state": state}


_monitor_off_triggered: bool = False


def is_monitor_off_active() -> bool:
    """Checks if the MonitorOff process or single-instance mutex is currently active."""
    try:
        import win32api
        import win32event
        import pywintypes
        h = win32event.OpenMutex(win32event.SYNCHRONIZE, False, r"Local\MonitorOff_SingleInstance")
        if h:
            win32api.CloseHandle(h)
            return True
    except Exception:
        pass

    # Process name fallback
    try:
        import psutil
        for proc in psutil.process_iter(["name"]):
            if (proc.info.get("name") or "").lower().startswith("monitoroff"):
                return True
    except Exception:
        pass
    return False


def check_monitor_off_trigger(chains_dir: Path | None = None) -> dict | None:
    """Checks if MonitorOff is running, and if bedtime_autostart is enabled, triggers the overnight run once."""
    global _monitor_off_triggered
    is_active = is_monitor_off_active()
    if not is_active:
        _monitor_off_triggered = False
        return None

    if _monitor_off_triggered:
        return None

    state = get_overnight_state()
    if not state.get("bedtime_autostart"):
        return None

    if state.get("active"):
        _monitor_off_triggered = True
        return None

    return trigger_bedtime_run(chains_dir=chains_dir)


def trigger_bedtime_run(chains_dir: Path | None = None) -> dict:
    """Directly triggers bedtime run when MonitorOff is launched."""
    global _monitor_off_triggered
    _monitor_off_triggered = True
    state = get_overnight_state()
    if state.get("active"):
        return {"ok": False, "why": "Overnight run is already active."}

    if not state.get("bedtime_autostart"):
        return {"ok": False, "why": "Bedtime auto-start is toggled off."}

    chains_to_run = state.get("selected_chains") or PRESETS.get("full_tidy", {}).get("chains", ["Note Condenser"])
    res = start_overnight(
        chains=chains_to_run,
        display_mode="none",  # MonitorOff is already powering down monitors and holding wake-lock
        sleep_when_done=bool(state.get("sleep_when_done", False)),
        chains_dir=chains_dir,
    )
    res["triggered_by"] = "monitor_off"
    return res


def set_bedtime_config(
    enabled: bool,
    chains: list[str] | None = None,
    display_mode: str | None = None,
    sleep_when_done: bool | None = None,
) -> dict:
    """Updates bedtime auto-start preferences."""
    state = get_overnight_state()
    state["bedtime_autostart"] = bool(enabled)
    if chains is not None:
        state["selected_chains"] = list(chains)
    if display_mode is not None:
        state["display_mode"] = display_mode
    if sleep_when_done is not None:
        state["sleep_when_done"] = bool(sleep_when_done)
    save_overnight_state(state)
    return {
        "ok": True,
        "why": f"Bedtime auto-start {'armed' if enabled else 'disabled'}",
        "state": state,
    }


def generate_night_summary(completed: list[str], started_at: float, results: dict | None = None) -> Path:
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
    res_map = results or {}
    for c in completed:
        st = res_map.get(c, "completed")
        lines.append(f"- **{c}**: {st}")

    lines.append("")
    lines.append("## Results & Notes")
    lines.append("- Full chain outputs saved to `1Notebook/Chains/Results/`")
    lines.append("")

    report_file.write_text("\n".join(lines), encoding="utf-8")
    return report_file
