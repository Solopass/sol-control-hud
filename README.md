# SOL Control HUD

One app for the SOL workstation (a Windows 11 PC that runs local AI models on an RX 9070 XT): a **taskbar ticker** and a
**web dashboard** as two views of one data core, plus the machinery they report on: **prompt chains** (notes that run
as local-LLM pipelines) and **Away mode** (the PC works unattended behind a black screen with progress).

Continues [sol-hud](https://github.com/Solopass/sol-hud) (tag `final-2026-09`), which built the parts separately.

## Status
**Live since 2026-09-26** (`D:\OBVLT\plans\SOL_UNIFIED_APP_PLAN.md` phases 0-4): one process (`python -m sol_control_hud`) with one
collector feeding the taskbar ticker and the dashboard, plus the tray icon; the OBVLT tools run the chain runner, the
Away screen and the doctor from here. Next: `D:\OBVLT\plans\SOL_CONTROL_HUD_NEXT_PLAN.md`.

## Layout
| Folder | What |
|---|---|
| `sol_control_hud/data/` | the data core: collectors (GPU performance counters, AMD sensors, VRAM eviction guard, RAM, disks, backups, crash counts, media) and `snapshot.py` (what it means: alerts first, colors by meaning, trends, Away progress) |
| `sol_control_hud/views/ticker.py` | the taskbar ticker (Tk): rotating slides, alerts first, docks in the taskbar |
| `sol_control_hud/views/web/` | the web dashboard (FastAPI on 127.0.0.1:7900 + one page) |
| `sol_control_hud/away/` | the Away screen (black screens, progress, Enter to come back) and its corner panel |
| `sol_control_hud/chains/` | prompt chains: the runner, chain notes, the chain daemon (schedules, file watches, lanes that take turns on one GPU), drafting chains from a description |
| `sol_control_hud/launchpad.yaml`, `launcher.py` | the Projects and Media APIs cards: what can't be read from the project folders, and Start / Stop / Open by name (`data/collectors/projects.py`, `media_apis.py` collect; nothing they do wakes a socket-activated service). Your own tags and hides from the card's ⋯ menu live in `data/launchpad-user.json`; the Media card's box runs the transcript-note workflow |
| `sol_control_hud/doctor.py` | health checks used by the morning briefing |
| `sol_control_hud/data/collectors/displays.py` | warns when apps render on a graphics chip with **no monitor attached** (every frame is then copied to the one that has): the GPU card's amber line and sol-doctor's "Rendering on the wrong GPU". Reads both the Windows Graphics setting and what is actually rendering, since a running app keeps its old adapter until restarted. What to do about it: `D:\OBVLT\reports\gpu-routing-2026-10-08.md` |
| `sol_control_hud/exam_review.py`, `quiz_capture.py`, `views/box_picker.py` | the Exam assist & review card: draw a box around the question area on an active practice exam or results page; every 3 s (never while busy) it checks the box for a new question, reads it with sol-vision and solves it in real time (or explains missed questions on graded reviews); saves questions, answers, and explanations to `1Notebook\School\<date> practice exam.md` (or review note), with live answers shown on the ticker (REVIEW) |
| `sol_control_hud/paths.py` | where the app keeps its files (`data/`, override with `SOL_CONTROL_DATA`) |

## Run (from this folder)
```powershell
.\sol-ticker.ps1                                          # the ticker
.\open-hud.ps1                                            # the web dashboard (http://127.0.0.1:7900)
.\sol-control.ps1 -Restart                                # restart it cleanly (agents: never kill it, see AGENTS.md)
.\.venv\Scripts\python.exe -m sol_control_hud.chains      # chains: note, run, chain, daemon, forge, check, runs, show
.\.venv\Scripts\pythonw.exe -m sol_control_hud.away       # the Away screen (started by tools\sol-llm.ps1)
.\.venv\Scripts\python.exe -m sol_control_hud.doctor --json
.\.venv\Scripts\python.exe -m pytest -q tests
```
Setup: `uv venv --python "C:\Program Files\Python314\python.exe" .venv` then
`uv pip install --python .venv\Scripts\python.exe -r requirements.txt`.

## Staying up
- **Start at login** (on/off: tray menu, the ticker's right-click menu, or the dashboard footer): a normal Windows Startup entry, `SOL Control HUD.lnk`.
- **Back after a crash:** while it runs the app keeps `data\app-running.json`; if that process dies without Exit, `D:\OBVLT\tools\sol-llm-watch.ps1` starts it again within ~10 s (at most 3 times in 10 min, then it stops and the morning briefing says so). Exit removes the file, so what you close stays closed.
- Tests use a temporary data folder (`tests\conftest.py`): `data\` here is the live machine's state.

## Working on it
- **Every commit runs the checks** (`.githooks/pre-commit`: no new undefined names, staged `.js` must parse, then the tests; enable in a fresh clone with `git config core.hooksPath .githooks`). Skip once only on purpose: `git commit --no-verify`.
- **Agents** (Claude, Gemini): read `AGENTS.md`. Restart with `.\sol-control.ps1 -Restart`; never kill the process.
- **Logs:** `data\hub.log` (becomes `hub.log.old` past 1 MB, at the next start), `data\ticker.log`, `data\swallowed.log`.
- Icon: `scripts\make_icon.py` draws `sol_control_hud\assets\sol.ico` and the dashboard's `sol.svg`.

## Rules the code keeps
- Servers bind 127.0.0.1 only. Nothing polls a model directly (`/slots` counts as use and keeps it loaded).
- No new process per poll; git with `--no-optional-locks`; Win32 handles passed as `wintypes.HWND`.
- One color legend everywhere: red = problem or getting worse, amber = worth a look, green = good or better,
  cyan = working now, grey = idle.

## Project history
solar-station (WSL HUD, retired 2026-09-13) → sol-hud (2026-09-13 → 09-26: dashboard, VRAM guard, chains, Away mode,
ticker) → **sol-control-hud** (one app, one data core, two views).

License: PolyForm Noncommercial 1.0.0 (see LICENSE.md, COMMERCIAL.md).
