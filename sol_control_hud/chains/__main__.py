"""Run pipelines from the command line and watch their events live (until HUD M2 shows them).

  python -m sol_control_hud.chains note <file-or-url> [--title T] [--engine parakeet|whisper] [--language en]
  python -m sol_control_hud.chains run <workflow.yaml> [--input name=value ...]   # any workflow file
  python -m sol_control_hud.chains resume <run_id>   # continue a stopped run from its last finished step
  python -m sol_control_hud.chains cancel <run_id>   # stop a run after its current step (from any window)
  python -m sol_control_hud.chains chain <note.md> [--new]  # run/resume a chain note (1Notebook/Chains) now
  python -m sol_control_hud.chains daemon          # the chain runner (triggers, lanes); kept alive by sol-llm-watch.ps1
  python -m sol_control_hud.chains forge "<what the chain should do>" --title "<name>" [--model sol-smart]   # draft a chain
  python -m sol_control_hud.chains check <workflow.yaml | chain.md>   # validate without running
  python -m sol_control_hud.chains runs            # recent runs
  python -m sol_control_hud.chains show <run_id>   # one run's events and result

Model calls go to the llama.cpp router (Local AI v2, 127.0.0.1:11440) and take turns on the GPU with other
processes (gpulock.py). Exit codes: 0 succeeded, 1 stopped (needs you / failed / cancelled), 3 waiting for a model.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import yaml

from . import chain_note, file_tools, media_tools
from .gpulock import gpu_lock
from .llm import load_registry
from .router import RouterClient
from .runner import Runner
from .store import RESUMABLE, Store
from .workflow import WorkflowError, load, parse
from ..paths import ROOT as PKG_ROOT

ROOT = PKG_ROOT
WORKFLOWS = ROOT / "workflows"
RUNS_DB = Path(os.environ.get("SOL_RUNS_DB", ROOT / "data" / "runs.sqlite"))
TOOLS = {**media_tools.TOOLS, **file_tools.TOOLS, **chain_note.TOOLS}


def open_store() -> Store:
    RUNS_DB.parent.mkdir(parents=True, exist_ok=True)
    store = Store(RUNS_DB)
    interrupted = store.recover_after_restart()  # only runs whose process is gone
    if interrupted:
        print(f"(marked {interrupted} run(s) whose process ended as interrupted; `resume <id>` continues them)")
    return store


def make_runner(store: Store, on_step=None) -> Runner:
    return Runner(RouterClient(), store, tools=TOOLS, gpu_lock=gpu_lock, on_step=on_step)


def format_event(e: dict) -> str:
    at = datetime.fromtimestamp(e["at"]).strftime("%H:%M:%S")
    data = dict(e["data"])
    message = data.pop("message", None)
    for noisy in ("steps", "after"):
        data.pop(noisy, None)
    detail = " ".join(f"{k}={v}" for k, v in data.items() if v is not None)
    parts = [at, f"{(e['step'] or '-'):<10}", f"{e['kind']:<15}", message or "", detail]
    return "  ".join(p for p in parts if p).rstrip()


def print_events(store: Store, run_id: int, after: int = 0) -> int:
    for e in store.events(run_id, after):
        print(format_event(e), flush=True)
        after = e["id"]
    return after


def watch(store: Store, runner: Runner, start) -> int | None:
    """Run `start()` in a thread, print its events live; Ctrl+C cancels after the current step."""
    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(run_id=start()), daemon=True)
    thread.start()
    after = 0
    try:
        while thread.is_alive():
            if runner.current_run:
                after = print_events(store, runner.current_run, after)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("cancelling after the current step...", flush=True)
        runner.cancel()
        while thread.is_alive():
            if runner.current_run:
                after = print_events(store, runner.current_run, after)
            time.sleep(0.5)
    run_id = result.get("run_id")
    if run_id is not None:
        print_events(store, run_id, after)
    return run_id


def cmd_note(args) -> int:
    store = open_store()
    wf = load(WORKFLOWS / "transcript-note.yaml")
    runner = make_runner(store)
    inputs = {"source": args.source, "title": args.title or "", "engine": args.engine or "", "language": args.language or ""}
    run_id = watch(store, runner, lambda: runner.run(wf, inputs, source=str(WORKFLOWS / "transcript-note.yaml")))
    if run_id is None:
        print("run did not start")
        return 2
    return report(store.run(run_id))


def parse_inputs(pairs: list[str] | None) -> dict:
    out = {}
    for p in pairs or []:
        k, sep, v = p.partition("=")
        if not sep:
            raise SystemExit(f"--input needs name=value, got '{p}'")
        out[k.strip()] = v
    return out


def cmd_run(args) -> int:
    path = Path(args.workflow).resolve()
    try:
        wf = load(path)
    except (WorkflowError, yaml.YAMLError, OSError) as e:
        print(f"can't run {path}: {e}")
        return 2
    store = open_store()
    runner = make_runner(store)
    run_id = watch(store, runner, lambda: runner.run(wf, parse_inputs(args.input), source=str(path)))
    return report(store.run(run_id)) if run_id is not None else 2


def cmd_resume(args) -> int:
    store = open_store()
    run = store.run(args.run_id)
    if not run:
        print(f"no run {args.run_id}")
        return 2
    if run["status"] not in RESUMABLE:
        print(f"run {args.run_id} is {run['status']}; only {', '.join(RESUMABLE)} runs can be resumed")
        return 2
    if not run.get("source"):
        print(f"run {args.run_id} doesn't record its workflow file (started before resume existed)")
        return 2
    if run["source"].lower().endswith(".md"):  # a chain note: re-read it (it may have been fixed) and continue
        return cmd_chain(argparse.Namespace(note=run["source"], new=False))
    wf = load(run["source"])
    runner = make_runner(store)
    run_id = watch(store, runner, lambda: runner.resume(args.run_id, wf))
    return report(store.run(run_id)) if run_id is not None else 2


def cmd_cancel(args) -> int:
    store = open_store()
    run = store.run(args.run_id)
    if not run:
        print(f"no run {args.run_id}")
        return 2
    store.request_cancel(args.run_id)
    print(f"run {args.run_id} ({run['status']}): it stops after its current step; finished steps are kept")
    return 0


def cmd_chain(args) -> int:
    store = open_store()
    holder: dict = {}

    def make(on_step):
        holder["runner"] = make_runner(store, on_step)
        return holder["runner"]

    result: dict = {}
    thread = threading.Thread(target=lambda: result.update(run_id=chain_note.run_chain(args.note, store, make, new=args.new)),
                              daemon=True)
    thread.start()
    after = 0
    try:
        while thread.is_alive():
            r = holder.get("runner")
            if r and r.current_run:
                after = print_events(store, r.current_run, after)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("cancelling after the current step...", flush=True)
        if holder.get("runner"):
            holder["runner"].cancel()
        thread.join()
    except chain_note.ChainError as e:
        print(f"can't run the chain: {e}")
        return 2
    thread.join()
    if "run_id" not in result:
        try:  # show why it didn't start (a ChainError raised inside the thread)
            chain_note.to_workflow(chain_note.load_chain(args.note))
        except chain_note.ChainError as e:
            print(f"can't run the chain: {e}")
        return 2
    print_events(store, result["run_id"], after)
    code = report(store.run(result["run_id"]))
    chain = chain_note.load_chain(args.note)
    print(f"result: {chain_note.results_dir(chain) / (chain_note.result_name(chain) + '.md')}")
    return code


def cmd_daemon(args) -> int:
    from . import chain_daemon
    lock = chain_daemon.single_instance()
    if lock is None:
        print("the chain runner is already running")
        return 0
    store = open_store()
    d = chain_daemon.Daemon(store)
    if args.once:
        d.tick()
        print(json.dumps(json.loads(d.status_file.read_text(encoding="utf-8")), indent=1))
        return 0
    d.run_forever()
    return 0


def cmd_forge(args) -> int:
    from . import forge
    client = RouterClient()
    try:
        note, tries = forge.forge(args.description, args.title, client.chat, model=args.model, notify=print)
    except forge.ForgeError as e:
        print(f"couldn't draft it: {e}")
        return 1
    if args.print:
        print(note)
        return 0
    target = forge.save_draft(note, args.title)
    print(f"drafted {target} ({tries} tr{'y' if tries == 1 else 'ies'}); read it, then set status: queued to run it")
    return 0


def check_file(path: Path) -> list[str]:
    """Plain-English problems in a workflow file or chain note (empty list = fine)."""
    if path.suffix.lower() == ".md":
        return chain_note.check_chain(path, TOOLS, load_registry())
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        return [f"can't open the file: {e}"]
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        where = f" (line {mark.line + 1}, column {mark.column + 1})" if mark else ""
        return [f"the YAML isn't valid{where}: {getattr(e, 'problem', e)}"]
    try:
        wf = parse(doc)
    except WorkflowError as e:
        return [str(e)]
    problems = []
    registry = load_registry()
    for s in wf.steps:
        if s.tool and s.tool not in TOOLS:
            problems.append(f"step '{s.id}' uses tool '{s.tool}', which doesn't exist (tools: {', '.join(sorted(TOOLS))})")
        for m in (s.model, s.escalate_to, s.long_model):
            if m and registry and m not in registry:
                problems.append(f"step '{s.id}' uses model '{m}', which isn't in models.json ({', '.join(sorted(registry))})")
    return problems


def cmd_check(args) -> int:
    path = Path(args.workflow)
    problems = check_file(path)
    if problems:
        print(f"{path.name}: {len(problems)} problem(s)")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"{path.name}: OK")
    return 0


def report(run: dict) -> int:
    took = f"{run['finished_at'] - run['started_at']:.0f}s" if run.get("finished_at") and run.get("started_at") else "?"
    print(f"\nrun {run['id']} {run['workflow']}: {run['status'].upper()} ({took})")
    if run.get("error"):
        print(f"reason: {run['error']}")
    outputs = json.loads(run["outputs"]) if run.get("outputs") else {}
    if isinstance(outputs.get("note"), dict) and "path" in outputs["note"]:
        print(f"note: {outputs['note']['path']}")
    if run["status"] in RESUMABLE:
        print(f"continue later: python -m sol_control_hud.chains resume {run['id']}")
    return 0 if run["status"] == "succeeded" else 3 if run["status"] == "waiting" else 1


def cmd_runs(args) -> int:
    store = open_store()
    rows = store._db.execute("SELECT id, workflow, status, created_at, error FROM runs ORDER BY id DESC LIMIT ?", (args.limit,)).fetchall()
    for r in rows:
        when = datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d %H:%M")
        print(f"{r['id']:>4}  {when}  {r['workflow']:<18} {r['status']:<11} {(r['error'] or '')[:80]}")
    return 0


def cmd_show(args) -> int:
    store = open_store()
    run = store.run(args.run_id)
    if not run:
        print(f"no run {args.run_id}")
        return 2
    print(f"inputs: {run['inputs']}")
    print_events(store, args.run_id)
    return report(run)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m sol_control_hud.chains")
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("note", help="recording/URL -> transcript -> summary -> Obsidian note")
    n.add_argument("source", help="audio/video file (D:\\...) or URL")
    n.add_argument("--title", help="note title (default: video title or file name)")
    n.add_argument("--engine", choices=["parakeet", "whisper"], help="speech-to-text engine (default: media-api's, parakeet)")
    n.add_argument("--language", help="language code, e.g. en; languages parakeet lacks use whisper")
    n.set_defaults(fn=cmd_note)
    ru = sub.add_parser("run", help="run a workflow file")
    ru.add_argument("workflow")
    ru.add_argument("--input", action="append", help="name=value (repeat for more)")
    ru.set_defaults(fn=cmd_run)
    rs = sub.add_parser("resume", help="continue a stopped run from its last finished step")
    rs.add_argument("run_id", type=int)
    rs.set_defaults(fn=cmd_resume)
    c = sub.add_parser("cancel", help="stop a run after its current step")
    c.add_argument("run_id", type=int)
    c.set_defaults(fn=cmd_cancel)
    ch = sub.add_parser("chain", help="run or resume a chain note now")
    ch.add_argument("note")
    ch.add_argument("--new", action="store_true", help="start over instead of resuming the last stopped run")
    ch.set_defaults(fn=cmd_chain)
    dm = sub.add_parser("daemon", help="the chain runner: starts chain notes by themselves")
    dm.add_argument("--once", action="store_true", help="one scan, print the status, exit (testing)")
    dm.set_defaults(fn=cmd_daemon)
    fg = sub.add_parser("forge", help="draft a chain note from a description (saved as a draft, never run)")
    fg.add_argument("description")
    fg.add_argument("--title", required=True)
    fg.add_argument("--model", default="sol-fast")
    fg.add_argument("--print", action="store_true", help="print the draft instead of saving it")
    fg.set_defaults(fn=cmd_forge)
    ck = sub.add_parser("check", help="validate a workflow file or chain note without running it")
    ck.add_argument("workflow")
    ck.set_defaults(fn=cmd_check)
    r = sub.add_parser("runs", help="list recent runs")
    r.add_argument("--limit", type=int, default=20)
    r.set_defaults(fn=cmd_runs)
    s = sub.add_parser("show", help="events and result of one run")
    s.add_argument("run_id", type=int)
    s.set_defaults(fn=cmd_show)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
