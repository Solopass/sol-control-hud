# SOL Control HUD

One app for the SOL workstation (a Windows 11 PC that runs local AI models on an RX 9070 XT): a **taskbar ticker** and a
**web dashboard** as two views of one data core, plus the machinery they report on: **prompt chains** (notes that run
as local-LLM pipelines) and **Away mode** (the PC works unattended behind a black screen with progress).

Continues [sol-hud](https://github.com/Solopass/sol-hud) (tag `final-2026-09`), which built the parts separately.

## Status
**Live since 2026-09-26** (`D:\OBVLT\plans\SOL_UNIFIED_APP_PLAN.md` phases 0-4): one process (`python -m sol_control_hud`) with one
collector feeding the taskbar ticker and the dashboard, plus the tray icon; the OBVLT tools run the chain runner, the
Away screen and the doctor from here. Completed follow-up plans: `SOL_CONTROL_HUD_NEXT_PLAN.md` and `SOL_HUD_IMPROVEMENTS_PLAN.md`.

## Layout
| Folder / Module | What |
|---|---|
| `sol_control_hud/hub.py`, `control.py`, `models_ctl.py`, `settings_api.py` | the unified hub process: coordinates collectors, SSE live stream (`/api/stream`), model load/unload & AI mode controls (`desk` / `away` / `off`), chain & overnight actions, and settings |
| `sol_control_hud/hub_web.py`, `hub_actions.py`, `hub_launch.py` | split out of `hub.py` (2026-11-22): the FastAPI app (routes, middleware, cached collectors), the dashboard's button actions (`/api/action`, `/api/review`), and the Projects / Media cards' Start / Stop / Open. Each takes the Hub as `self` and reads hub.py names as `_hub.<name>`, so tests that patch `hub.*` still reach them |
| `sol_control_hud/layouts.py`, `views/web/static/layouts.js`, `layouts-core.js` | customizable dashboard layout presets (`Everyday`, `Gaming`, `AI work`, `Dev`, `Overnight`, `Minimal` + custom presets), quick switcher bar, card sizing (`XS`–`XL`), drag-and-drop card ordering, and auto-switching for games / Away |
| `sol_control_hud/data/` | the data core: collectors (CPU per-core P/E loads, GPU performance counters, AMD sensors, VRAM eviction & CPU-split guard, RAM, disks, backups, crash counts, WSL, git, Obsidian notes, localhost ports) and `snapshot.py` / `snapshot_model.py` / `snapshot_text.py` |
| `sol_control_hud/data/collectors/network.py`, `security.py` | **Network · Pi-hole** (direct DNS & FTL probes across `.249`/`.248`, blocking/upstream checks, 5-min snooze via `sol-deck`, LAN ARP device inventory) and **Cyber Security & Canary** (`sol-deck` `:5000` status, iPad mini link, TCP honeypot canaries, gateway ARP guard, LAN radar) |
| `sol_control_hud/data/collectors/media.py`, `media_apis.py` | now-playing media session reader & hardware media-key router (`sol-media.exe` with automatic `media_session.ps1` fallback) plus socket-safe status/stuck-job monitoring for `media-api` (`:8080`), `speedman` (`:8081`), and Vocal Savior (`:8765`) |
| `sol_control_hud/data/collectors/ai_ops.py`, `ai_usage.py`, `speed.py` | **Live AI Thinking** stream collector (inner monologue, prompt eval & token generation tok/s, speculative decoding draft acceptance, sparkline history) and **AI Usage** tracker (daily/weekly Claude Code & Antigravity consumption) |
| `sol_control_hud/data/collectors/displays.py`, `gpu_prefs.py` | warns when apps render on a graphics chip with **no monitor attached** (cross-adapter copy penalty) and manages per-app Windows GPU preference registry entries (`DirectXUserGlobalSettings`) |
| `sol_control_hud/views/ticker.py` (+ `ticker_*.py`) | the taskbar ticker (Tk): rotating slides (`HW`, `AI`, `RUN`, `SVC`, `DISK`, `SYS`, `NOTE`, `GIT`, `NET`, `MEDIA`, `APPS`), alerts first, media-key target menu, docks in the taskbar |
| `sol_control_hud/views/web/` | the web dashboard (FastAPI on `127.0.0.1:7900` + single-page UI with sticky header, Quick Switcher bar, masonry card grid, command palette `Ctrl+K`, note reader/editor, and live Doctor modal) |
| `views/web/static/app.js` + `static/app/*.js` | the dashboard script, split per card on 2026-11-22: `app.js` holds the shared helpers (`$`, `post`, `toast`, `esc` ...), then one file per card / feature (`render.js`, `chains.js`, `notes.js`, `playground.js` ... `boot.js` last), loaded by plain `<script>` tags in `index.html` in that order (no build step; `tests/test_static_order.py` checks it) |
| `sol_control_hud/away/` | the Away screen (black screens, progress ring, Enter to come back) and its corner panel; `display_ctl.py` provides physical monitor DPMS standby and `SetThreadExecutionState` keep-awake guards |
| `sol_control_hud/chains/` | prompt chains: runner with code-fence preserving context compaction (`compact_text`), model budget pre-flight, chain notes, daemon (schedules, file watches, lanes), tool suites (`file_tools.py`, `fix_tools.py`, `gather_tools.py`, `index_tools.py`, `media_tools.py`, `solang_tools.py`). What chains may touch is `file_tools.Policy`: read `D:\OBVLT`, `D:\Polymatica Vault`, `D:\Workspace`, `D:\Output`, `D:\AI\Vault` and `D:\AI\Cache\claude-log` (Claude Code session logs, for the Daily work summary; added 2026-11-23); write only `D:\OBVLT\1Notebook`, `D:\Output\chains`, `D:\AI\Vault\Chains`; never delete |
| `sol_control_hud/chains/overnight.py`, `project_chains.py` | **Overnight AI Run** orchestration (multi-chain batch presets, monitor DPMS standby, morning reports) and **Projects Bug Hunt** chains (review freshness against git commits, findings checklist, 1-click `.git/hooks/post-commit` installer) |
| `sol_control_hud/launchpad.yaml`, `launcher.py` | **Projects**, **Media APIs**, and **Solang Karaoke Studio** cards: Start / Stop / Open by name, sidecar controls, Takeout music history ingestion queue, and transcript-note workflow |
| `data/collectors/projects.py`, `data/gitcache.py` | the Projects card's data: one row per `D:\Workspace` folder (README, git, launch config: own `.claude/launch.json`, else the workspace one), git cached per repo until it changes and saved to `data/gitcache.json` across restarts, one shared process scan for watchers. Open uses the address the server listens on (`localhost` for IPv6-only Vite) |
| `sol_control_hud/exam_review.py`, `gemini_solver.py`, `quiz_capture.py`, `views/exam_hud.py`, `views/box_picker.py` | **Exam assist & review**: region picker (`box_picker.py`), auto-scroll multi-part stitcher, local `sol-vision` solver with **Gemini multimodal fallback** (`gemini_solver.py` with 503 exponential backoff & model fallback), **🦁 Brave AI verification search**, floating always-on-top **Exam HUD** (`exam_hud.py`), and Obsidian study note logging |
| `sol_control_hud/health.py`, `metrics.py`, `digest.py`, `notify.py`, `game_guard.py`, `heal.py`, `doctor.py` | **HUD health** (uptime, unexpected exits, collector staleness, `swallowed.log` tracking), SQLite minute/event metrics (`metrics.sqlite`), weekly machine digest notes, Windows toast notifications, game guard ignore/resume, VRAM spill auto-heal, and `sol-doctor` health checks |
| `sol_control_hud/paths.py` | where the app keeps its runtime files (`data/`, override with `SOL_CONTROL_DATA`) |

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
