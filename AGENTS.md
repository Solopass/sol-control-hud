# Working on SOL Control HUD (for Claude, Gemini and any other coding agent)

This app runs live on this PC (the ticker, the dashboard on http://127.0.0.1:7900, the tray). These rules keep it
that way while you work on it.

## Restarting the HUD: never kill it
```
.\sol-control.ps1 -Restart
```
(or `.venv\Scripts\python.exe -m sol_control_hud --restart`). It asks the running HUD to exit cleanly, waits until it
has, starts a fresh detached copy and waits until it answers. It prints the result; exit code 0 = running again.

Do **not** `Stop-Process` / `taskkill` the `pythonw.exe -m sol_control_hud` process. A kill looks exactly like a
crash: the HUD health card counts it ("ended without Exit"), three in a day turn it red (TROUBLE), and a kill can
land in the middle of a save. On 2026-10-10 three restarts by killing, one after each commit, did exactly that.

## Before you commit
The pre-commit hook (`.githooks/pre-commit`) runs, in order: `scripts/check_names.py` (no new undefined names in
staged Python), `node --check` on staged `.js` files (via WSL), then the whole test suite. Don't skip it
(`--no-verify`); fix what it reports. Tests never touch the live `data/` folder (`tests/conftest.py`).

## Things that look harmless but aren't
- **Never send HTTP to media-api (:8080) or speedman (:8081) just to look.** They are systemd socket-activated in
  WSL: any request starts them. Read their state from systemd / their files (see `data/collectors/media_apis.py`).
- **Never call the AI router's `/slots`**: it counts as use and keeps models loaded. `GET :11440/models` is fine.
- Errors you deliberately carry on after: `swallow.note("<module>.<function>")`, not `except Exception: pass`.
- The dashboard's static files are served with `Cache-Control: no-cache`, so a reload picks up your change.

## Where things are
README.md (what each file does), `D:\OBVLT\plans\` (plans and their logs), `data/hub.log` and `data/ticker.log` (the
HUD's own logs; `data/swallowed.log` for errors it carried on after).
