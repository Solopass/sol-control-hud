"""The chain runner: starts chain notes by themselves (OBVLT plans/LOCAL_AI_AUTOMATION_PLAN.md phase 3).

    python -m sol_control_hud.chains daemon          # started and kept alive by D:\\OBVLT\\tools\\sol-llm-watch.ps1

Every 10 s it reads the properties of 1Notebook\\Chains\\*.md and:
- `status: queued` -> runs the chain (resumes its last stopped run if you fixed it; see chain_note.plan_run);
- `status: cancel` -> stops that chain after its current step;
- `schedule:` due (`daily 07:00`, `weekly Sun 20:00`, `weekly Mon,Thu 09:30`, `on away`) -> a new run; if the PC was
  asleep at that time it runs once at the next chance, never twice. A chain created after today's time waits for the next one;
- `watch: <folder>\\<glob>` -> one new run per new file (after it stopped changing for 60 s; at most `watch_limit`
  per hour, default 6). `{{new file}}` / `{{new file text}}` hold it;
- a chain left `running` (PC slept or crashed mid-run) or `waiting` (model wasn't available) -> resumed when possible.
Lanes (chain_note.lane): fast = sol-fast only, runs any time but each step waits its turn (never while you're using
another model, never ahead of a request of yours); idle = sol-smart / sol-long, starts when you've been idle 10 min or in
Away; away = Away models, only in Away mode. Nothing starts while the game guard has the AI off or an Away queue job
runs. One chain at a time. Writes D:\\AI\\Cache\\llm\\chains.json (read by the watcher and the briefing).
"""
from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path

import yaml

from . import chain_note as cn
from . import file_tools
from .gpulock import gpu_lock
from .router import DESK_FALLBACK, ModelUnavailable, RouterClient, engine_mode
from .runner import Runner
from .store import Store, owner_alive
from ..paths import DATA_DIR

LLM_DIR = Path(os.environ.get("SOL_LLM_DIR", r"D:\AI\Cache\llm"))
QUEUE_RUNNING = Path(os.environ.get("SOL_QUEUE_RUNNING", r"D:\AI\Queue\running"))
STATE_FILE = DATA_DIR / "chains-state.json"
IDLE_MINUTES = 10
TICK = 10.0
RETRY_WAITING = 60.0
STABLE_SECONDS = 60
NEW_FILE_MAX_CHARS = 60000
DAYS = {d: i for i, d in enumerate(["mon", "tue", "wed", "thu", "fri", "sat", "sun"])}


# ---------------------------------------------------------------- small helpers
class _LII(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def idle_seconds() -> float:
    """Seconds since your last keyboard/mouse input (this session)."""
    try:
        lii = _LII(ctypes.sizeof(_LII), 0)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0
    except (AttributeError, OSError):
        return 0.0


def engine_since() -> float:
    try:
        s = json.loads((LLM_DIR / "state.json").read_text(encoding="utf-8-sig")).get("since")
        return datetime.fromisoformat(s).timestamp() if s else 0.0
    except (OSError, ValueError):
        return 0.0


def queue_job_running() -> bool:
    return QUEUE_RUNNING.is_dir() and any(QUEUE_RUNNING.glob("*.json"))


def queue_jobs_waiting() -> bool:
    """Away queue jobs go first: an Away chain doesn't start between two jobs (it'd hold the GPU the next job needs),
    nor in the seconds after Stop puts the running job back in the queue (09-26: it started then, in Desk)."""
    q = QUEUE_RUNNING.parent
    return q.is_dir() and any(q.glob("*.json"))


def front(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8-sig") as f:
            head = f.read(8000)
        m = cn._FRONT.match(head)
        meta = yaml.safe_load(m.group(1)) if m else {}
        return meta if isinstance(meta, dict) else {}
    except (OSError, yaml.YAMLError):
        return {}


# ---------------------------------------------------------------- schedules
class ScheduleError(ValueError):
    pass


def parse_schedule(text: str) -> dict:
    t = " ".join(str(text).lower().split())
    if t == "on away":
        return {"kind": "away"}
    m = re.fullmatch(r"daily (\d{1,2}):(\d{2})", t)
    if m:
        return {"kind": "at", "days": list(range(7)), "h": int(m.group(1)), "m": int(m.group(2))}
    m = re.fullmatch(r"weekly ([a-z, ]+?) (\d{1,2}):(\d{2})", t)
    if m:
        days = []
        for d in re.split(r"[ ,]+", m.group(1).strip()):
            if d[:3] not in DAYS:
                raise ScheduleError(f"unknown day '{d}'")
            days.append(DAYS[d[:3]])
        return {"kind": "at", "days": sorted(set(days)), "h": int(m.group(2)), "m": int(m.group(3))}
    raise ScheduleError(f"I don't understand `schedule: {text}`. Use `daily 07:00`, `weekly Sun 20:00`, "
                        f"`weekly Mon,Thu 09:30` or `on away`")


def last_occurrence(sched: dict, now: datetime) -> datetime | None:
    """The most recent scheduled time at or before now (within the last 8 days)."""
    for back in range(0, 8):
        day = now - timedelta(days=back)
        if day.weekday() in sched["days"]:
            at = day.replace(hour=sched["h"], minute=sched["m"], second=0, microsecond=0)
            if at <= now:
                return at
    return None


def next_occurrence(sched: dict, now: datetime) -> datetime | None:
    """The next scheduled time after now (for the ticker: "next: Weekly digest Sun 20:00")."""
    for ahead in range(0, 8):
        day = now + timedelta(days=ahead)
        if day.weekday() in sched["days"]:
            at = day.replace(hour=sched["h"], minute=sched["m"], second=0, microsecond=0)
            if at > now:
                return at
    return None


# ---------------------------------------------------------------- the daemon
class Daemon:
    def __init__(self, store: Store, chains_dir: Path | None = None, state_file: Path = STATE_FILE,
                 status_file: Path | None = None, client_factory=RouterClient, log=None, idle=idle_seconds,
                 mode=engine_mode, clock=datetime.now):
        self.store = store
        self.dir = Path(chains_dir or cn.CHAINS_DIR)
        self.state_file = state_file
        self.status_file = status_file or (LLM_DIR / "chains.json")
        self.client_factory = client_factory
        self.idle, self.mode, self.clock = idle, mode, clock
        self._log = log or self._file_log
        self.state = self._load_state()
        self.current: dict | None = None      # {"path", "run", "lane", "thread", "runner"}
        self.retry_at: dict[str, float] = {}
        self.waiting_note: dict[int, str] = {}

    # ---- persistence
    def _load_state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"chains": {}, "pending": []}

    def _save_state(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _file_log(self, msg: str) -> None:
        LLM_DIR.mkdir(parents=True, exist_ok=True)
        with open(LLM_DIR / "chains.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%dT%H:%M:%S} {msg}\n")

    # ---- lanes
    def lane_open(self, lane: str) -> tuple[bool, str]:
        mode = self.mode()
        if mode == "off":
            return False, "the game guard has the AI off"
        if queue_job_running():
            return False, "an Away queue job is running"
        if lane == "fast" and mode in ("desk", "away"):
            return True, ""
        if lane == "idle" and (mode == "away" or (mode == "desk" and self.idle() >= IDLE_MINUTES * 60)):
            return True, ""
        if lane == "away" and mode == "away":
            if queue_jobs_waiting():
                return False, "Away queue jobs go first"
            return True, ""
        return False, {"idle": f"waits until you've been idle {IDLE_MINUTES} min (or Away mode)",
                       "away": "waits for Away mode"}.get(lane, f"engine is in {mode} mode")

    def gate(self, client: RouterClient):
        """Before each model call of a background chain: wait politely (plan pre-flight change 1)."""
        def wait_turn(run_id: int, model: str, stopping) -> None:
            told = None
            while not stopping():
                why = self._turn_blocker(client, model)
                if why is None:
                    return
                if why == "OFF":
                    raise ModelUnavailable("the game guard has the AI off; the chain continues after the game")
                if why == "AWAY_ENDED":
                    raise ModelUnavailable("Away ended; the chain continues in the next Away session")
                if why != told:
                    self.store.event(run_id, "waiting_for_turn", message=why); told = why
                time.sleep(5)
        return wait_turn

    def _turn_blocker(self, client: RouterClient, model: str) -> str | None:
        mode = self.mode()
        if mode == "off":
            return "OFF"
        if queue_job_running():
            return "an Away queue job is using the GPU"
        if mode == "away":
            return None
        if model not in cn.DESK_MODELS:
            return "AWAY_ENDED"   # an Away model outside Away: pause (it resumes next Away), never load it at your desk
        main = "sol-fast" if model in cn.FAST_MODELS else DESK_FALLBACK.get(model, model)
        try:
            loaded = client.loaded()  # GET /models is answered by the router itself: it doesn't wake or keep any model
        except Exception:  # noqa: BLE001 - the router is restarting; the call itself will say so
            return None
        idle = self.idle() >= IDLE_MINUTES * 60
        if loaded not in (None, main):
            # Another model is loaded. Never poll it (/slots counts as use and would stop its idle unload: seen 09-25,
            # a chain waited on sol-smart while keeping it loaded). Swap only when you're idle.
            if idle:
                return None
            return f"you're using {loaded}; waiting until it unloads or you've been idle {IDLE_MINUTES} min"
        try:
            if loaded and client.busy(loaded):  # our own model: is it answering you right now?
                return f"{loaded} is answering someone else"
        except Exception:  # noqa: BLE001
            return None
        if loaded == main or main == "sol-fast" or idle:  # only loading a big model while you're at the PC has to wait
            return None
        return f"you're using {loaded}; waiting until it's free or you've been idle {IDLE_MINUTES} min" if loaded \
            else f"{model} would be loaded while you're at the PC; waiting until you've been idle {IDLE_MINUTES} min"

    # ---- scanning
    def chain_files(self) -> list[Path]:
        if not self.dir.is_dir():
            return []
        return sorted(p for p in self.dir.glob("*.md") if p.is_file())

    def _pending_paths(self) -> set[str]:
        return {p["path"] for p in self.state["pending"]}

    def _enqueue(self, path: Path, reason: str, new: bool, extra: dict | None = None, mark: bool = True) -> None:
        key = str(path)
        if any(p["path"] == key and not p.get("extra") and not extra for p in self.state["pending"]):
            return
        self.state["pending"].append({"path": key, "reason": reason, "new": new, "extra": extra or {},
                                      "added": time.time()})
        if mark:
            try:
                cn.set_chain_status(path, "queued")
            except OSError:
                pass
        self._log(f"queued {path.name}: {reason}")

    def scan(self) -> None:
        now = self.clock()
        self._scheduled: list[dict] = []   # rebuilt every scan: active (not paused) schedules, for chains.json
        cur = self.current["path"] if self.current else None
        live = {str(p) for p in self.chain_files()}
        self.state["pending"] = [p for p in self.state["pending"] if p["path"] in live]
        for path in self.chain_files():
            key = str(path)
            meta = front(path)
            if meta.get("type") == "chain-request":  # phase 5 forge: draft a chain from a description
                if str(meta.get("status") or "").lower() == "queued" and cur != key and key not in self._pending_paths():
                    self.state["pending"].append({"path": key, "reason": "chain request", "kind": "forge",
                                                  "new": True, "extra": {}, "added": time.time()})
                    self._log(f"queued {path.name}: chain request")
                continue
            if meta.get("type") not in (None, "chain"):
                continue
            info = self.state["chains"].setdefault(key, {"first_seen": now.timestamp()})
            status = str(meta.get("status") or "draft").lower()
            if status == "cancel":
                if cur == key:
                    self.store.request_cancel(self.current["run"]); self._log(f"cancel requested: {path.name}")
                else:
                    self.state["pending"] = [p for p in self.state["pending"] if p["path"] != key]
                    cn.set_chain_status(path, "cancelled"); self._log(f"cancelled {path.name} (wasn't running)")
                continue
            if cur == key:
                continue
            if status not in ("queued", "running", "waiting"):  # set back to draft / done by you: drop it from the queue
                self.state["pending"] = [p for p in self.state["pending"] if p["path"] != key]
            elif self._running_elsewhere(key):  # e.g. you started it by hand with `pipelines chain`
                continue
            elif status == "queued" and key not in self._pending_paths():
                self._enqueue(path, "status: queued", new=False, mark=False)
            elif status == "running":  # left running by a crash / sleep / restart: resume it
                self._enqueue(path, "resume after an interruption", new=False, mark=False)
            elif status == "waiting" and time.time() >= self.retry_at.get(key, 0) and key not in self._pending_paths():
                self._enqueue(path, "retry (was waiting)", new=False, mark=False)
            if status == cn.PAUSED:  # paused: no schedule, no watch (a queued run still works)
                continue
            self._check_schedule(path, meta, info, now)
            self._check_watch(path, meta, info, now)

    def _not_served(self, chain) -> list[str]:
        try:
            served = self.client_factory().models()
        except Exception:  # noqa: BLE001 - fake clients / router restarting: let the run itself decide
            return []
        models = {s.settings.get("model", chain.meta.get("model") or cn.DEFAULT_MODEL) for s in chain.steps}
        return sorted(m for m in models if m not in served)

    def _running_elsewhere(self, key: str) -> bool:
        last = self.store.latest_run(key)
        return bool(last and last["status"] in ("running", "queued") and owner_alive(last.get("owner_pid"), last.get("owner_started")))

    def _check_schedule(self, path: Path, meta: dict, info: dict, now: datetime) -> None:
        if not meta.get("schedule"):
            return
        try:
            sched = parse_schedule(meta["schedule"])
        except ScheduleError as e:
            if info.get("schedule_error") != str(e):
                info["schedule_error"] = str(e); self._log(f"{path.name}: {e}")
            return
        info.pop("schedule_error", None)
        nxt = next_occurrence(sched, now) if sched["kind"] == "at" else None
        self._scheduled.append({"chain": path.stem, "next": nxt.strftime("%Y-%m-%dT%H:%M") if nxt else "on away"})
        if sched["kind"] == "away":
            since = engine_since()
            if self.mode() == "away" and since > max(info.get("last_fired", 0), info["first_seen"]):
                info["last_fired"] = now.timestamp()
                self._enqueue(path, "schedule: on away", new=True)
            return
        at = last_occurrence(sched, now)
        if at and at.timestamp() > max(info.get("last_fired", 0), info["first_seen"]):
            info["last_fired"] = now.timestamp()
            late = (now - at).total_seconds() > 600
            self._enqueue(path, f"schedule ({meta['schedule']}){' - catching up' if late else ''}", new=True)

    def _check_watch(self, path: Path, meta: dict, info: dict, now: datetime) -> None:
        spec = meta.get("watch")
        if not spec:
            return
        folder, glob = os.path.split(str(spec))
        try:
            folder_p = file_tools.check_read(folder)
        except file_tools.FileToolError as e:
            if info.get("watch_error") != str(e):
                info["watch_error"] = str(e); self._log(f"{path.name}: watch: {e}")
            return
        files = {str(p): p.stat().st_mtime for p in folder_p.glob(glob or "*") if p.is_file()}
        if "seen" not in info or info.get("watch_spec") != spec:
            info["seen"], info["watch_spec"] = files, spec  # what's already there doesn't count as new
            return
        limit = int(meta.get("watch_limit", 6))
        info["fired"] = [t for t in info.get("fired", []) if now.timestamp() - t < 3600]
        for f, mtime in sorted(files.items(), key=lambda kv: kv[1]):
            if f in info["seen"] or now.timestamp() - mtime < STABLE_SECONDS:
                continue
            if len(info["fired"]) >= limit:
                if not info.get("limit_logged"):
                    self._log(f"{path.name}: watch limit {limit}/hour reached; the rest wait"); info["limit_logged"] = True
                return
            info.pop("limit_logged", None)
            try:
                text = file_tools.read_file(f)["text"]
                if len(text) > NEW_FILE_MAX_CHARS:  # ~20k tokens: fits sol-fast with room to answer
                    text = text[:NEW_FILE_MAX_CHARS] + f"\n\n(cut here: the file has {len(text) // 1000} KB of text)"
            except (file_tools.FileToolError, OSError, ValueError) as e:
                text = f"(couldn't read the file: {e})"
            info["seen"][f] = mtime
            info["fired"].append(now.timestamp())
            self._enqueue(path, f"new file {Path(f).name}", new=True,
                          extra={"_new_file": f, "_new_file_name": Path(f).stem, "_new_file_text": text})

    # ---- running
    def pick(self) -> dict | None:
        for p in self.state["pending"]:
            path = Path(p["path"])
            if p.get("kind") == "forge":
                model = str(front(path).get("model") or "sol-fast")
                lane = "fast" if model in cn.FAST_MODELS else "idle" if model in cn.DESK_MODELS else "away"
                ok, why = self.lane_open(lane)
                p["lane"], p["blocked"] = lane, (None if ok else why)
                if ok:
                    return p
                continue
            try:
                chain = cn.load_chain(path)
                cn.to_workflow(chain)
            except cn.ChainError as e:
                self.state["pending"].remove(p)
                cn.write_error_result(path, str(e)); self._log(f"{path.name}: can't run: {e}")
                return None
            lane = cn.lane(chain)
            ok, why = self.lane_open(lane)
            if ok and lane == "away":  # every model must be served by this Away session, or it would just wait (and keep the PC up)
                missing = self._not_served(chain)
                if missing:
                    ok, why = False, f"needs {', '.join(missing)}, which this Away session doesn't serve"
            p["lane"], p["blocked"] = lane, (None if ok else why)
            if ok:
                return p
        return None

    def _forge_work(self, path: Path, client, stop) -> None:
        from . import forge
        meta, description = forge.request_text(path)
        title = str(meta.get("title") or re.sub(r"(?i)\s*request$", "", path.stem) or path.stem)
        model = str(meta.get("model") or "sol-fast")
        cn.set_chain_status(path, "running")
        try:
            if not description:
                raise forge.ForgeError("the request has no description: write what the chain should do under the properties")

            def polite_chat(m, messages, **kw):
                while (why := self._turn_blocker(client, m)) is not None:
                    if why == "OFF" or stop():
                        raise ModelUnavailable("the AI is off or the request was cancelled")
                    time.sleep(5)
                return client.chat(m, messages, **kw)
            note, tries = forge.forge(description, title, polite_chat, model=model, notify=self._log)
            target = forge.save_draft(note, title)
            cn.set_chain_status(path, "done", made=f"\"[[{target.stem}]]\"")
            self._log(f"{path.name}: drafted {target.name} ({tries} tr{'y' if tries == 1 else 'ies'})")
        except ModelUnavailable as e:
            cn.set_chain_status(path, "waiting"); self.retry_at[str(path)] = time.time() + RETRY_WAITING
            self._log(f"{path.name}: waiting: {e}")
        except Exception as e:  # noqa: BLE001
            cn.set_chain_status(path, "needs-you", problem=json.dumps(str(e)[:300]))
            self._log(f"{path.name}: couldn't draft: {e}")

    def start(self, job: dict) -> None:
        path = Path(job["path"])
        self.state["pending"].remove(job)
        if job.get("kind") == "forge":
            client = self.client_factory()
            stop = threading.Event()
            t = threading.Thread(target=self._forge_work, args=(path, client, stop.is_set), daemon=True, name=f"forge {path.stem}")
            self.current = {"path": str(path), "lane": job.get("lane"), "thread": t, "run": None, "holder": {},
                            "reason": "chain request"}
            self._log(f"start {path.name} (chain request)")
            t.start()
            return
        client = self.client_factory()
        holder: dict = {}

        def make(on_step):
            holder["runner"] = Runner(client, self.store, tools=CHAIN_TOOLS(), gpu_lock=gpu_lock, on_step=on_step,
                                      gate=self.gate(client))
            return holder["runner"]

        def work():
            try:
                rid = cn.run_chain(path, self.store, make, new=job.get("new", False), extra=job.get("extra") or None)
                run = self.store.run(rid)
                self._log(f"{path.name}: run {rid} {run['status']}{': ' + run['error'] if run.get('error') else ''}")
                if run["status"] == "waiting":
                    self.retry_at[str(path)] = time.time() + RETRY_WAITING
            except cn.ChainError as e:
                cn.write_error_result(path, str(e)); self._log(f"{path.name}: can't run: {e}")
            except Exception as e:  # noqa: BLE001 - keep the daemon alive; the run itself is marked failed
                self._log(f"{path.name}: error {type(e).__name__}: {e}\n{traceback.format_exc()}")

        t = threading.Thread(target=work, daemon=True, name=f"chain {path.stem}")
        self.current = {"path": str(path), "lane": job.get("lane"), "thread": t, "run": None, "holder": holder,
                        "reason": job.get("reason")}
        self._log(f"start {path.name} ({job.get('reason')}, lane {job.get('lane')})")
        t.start()

    def tick(self) -> None:
        self.scan()
        if self.current:
            r = self.current["holder"].get("runner")
            if r and r.current_run:
                self.current["run"] = r.current_run
            if not self.current["thread"].is_alive():
                self.current = None
        if not self.current:
            job = self.pick()
            if job:
                self.start(job)
        self._save_state()
        self._write_status()

    def _progress(self) -> dict:
        """Where the running chain is (for the Away screen and the ticker): step n of N, its name, loop item i of n,
        and overall done/total in hundredths of a step. Empty if it can't be read (a chain request, no run yet)."""
        cur = self.current or {}
        run_id = cur.get("run")
        if not run_id:
            return {}
        try:
            return chain_progress(cn.load_chain(Path(cur["path"])), self.store.saved_outputs(run_id))
        except Exception:  # noqa: BLE001 - progress is a display nicety; never break the status file
            return {}

    def _write_status(self) -> None:
        runnable = [p for p in self.state["pending"] if not p.get("blocked")]
        status = {"updated": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                  "running": ({"chain": Path(self.current["path"]).stem, "run": self.current.get("run"),
                               "lane": self.current.get("lane"), "reason": self.current.get("reason"),
                               **self._progress()} if self.current else None),
                  "pending": [{"chain": Path(p["path"]).stem, "lane": p.get("lane"), "reason": p.get("reason"),
                               "blocked": p.get("blocked")} for p in self.state["pending"]],
                  "runnable_now": len(runnable),
                  "scheduled": sorted(getattr(self, "_scheduled", []), key=lambda s: (s["next"] == "on away", s["next"]))}
        try:
            self.status_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.status_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, indent=1), encoding="utf-8")
            os.replace(tmp, self.status_file)
        except OSError:
            pass

    def run_forever(self) -> None:
        self._log(f"chain runner started (pid {os.getpid()})")
        code_at_start = code_version()
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                self._log(f"tick error {type(e).__name__}: {e}")
            if not self.current and code_version() != code_at_start:
                # sol-hud was updated: exit between chains; the watcher starts the new code within 10 s
                self._log("code changed -> restarting to load it")
                return
            time.sleep(TICK)


def code_version() -> float:
    """Newest modification time of the pipelines code (09-25: an old runner read `gather:` as a prompt)."""
    here = Path(__file__).resolve().parent
    return max((p.stat().st_mtime for p in here.glob("*.py")), default=0.0)


def chain_progress(chain, saved: dict) -> dict:
    """Pure part of the progress: step n of N (1-based, the first unfinished step), its name, loop item i of n."""
    from .store import WHOLE
    steps = chain.steps
    done_steps = sum(1 for s in steps if WHOLE in saved.get(s.id, {}))
    out = {"steps": len(steps), "step": min(done_steps + 1, len(steps)), "done": done_steps * 100, "total": len(steps) * 100}
    if done_steps >= len(steps):
        out.update(step=len(steps), step_name=steps[-1].name, done=out["total"])
        return out
    cur = steps[done_steps]
    out["step_name"] = cur.name
    if cur.loop:
        items = (saved.get(f"{cur.id}__files") or saved.get(f"{cur.id}__items") or {}).get(WHOLE)
        if isinstance(items, list) and items:
            finished = len([k for k in saved.get(cur.id, {}) if k != WHOLE])
            out.update(item=min(finished + 1, len(items)), items=len(items))
            out["done"] += int(100 * finished / len(items))
    return out


def CHAIN_TOOLS() -> dict:
    return {**file_tools.TOOLS, **cn.TOOLS}


def single_instance(path: Path = LLM_DIR / "chains.lock"):
    """Hold an OS lock for the daemon's lifetime; None if another daemon already holds it."""
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        return fh
    except OSError:
        fh.close()
        return None
