"""The game guard, from the dashboard: why the local AI is off, and the two things you want to do about it.

Why: `tools\\sol-llm-watch.ps1` turns the AI off when it thinks a game started. It is right about games and wrong
about a surprising number of other things - Notepad (09-25), Edge at 54 % (09-25), the Epic launcher (09-25),
claude at 31-38 % (09-27, three times), steamwebhelper at 43-64 % and Antigravity at 31-32 % (10-07/08, seven
times in two days). Every one of those was fixed by hand-editing `configs\\local-ai.json`, while the dashboard sat
there displaying the reason and offering nothing. This turns that into a button.

Only the "<name> uses N% of the GPU" reason can be answered by name, so only that one offers to ignore. A real game
caught by `processNames` or by its install path is not something to wave away from here, and a fullscreen app has
no name to add. The watcher re-reads its config every cycle (10 s), so a change here needs no restart of anything.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import psutil

CONFIG = Path(r"D:\OBVLT\configs\local-ai.json")
STATE = Path(r"D:\AI\Cache\llm\state.json")
SOL_LLM = Path(r"D:\OBVLT\tools\sol-llm.ps1")
NO_WINDOW = 0x08000000

# the watcher's own wording, as it lands in state.json's reason
BUSY = re.compile(r"^(?P<name>[^\s]+) uses (?P<percent>\d+(?:\.\d+)?)% of the GPU$")
BY_NAME = re.compile(r"^game process (?P<name>.+)$")
BY_PATH = re.compile(r"^game (?P<name>.+)$")
SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
# the one line in the config this module may rewrite, left exactly as it is apart from the list.
# `\r?` before the anchor: the real file is hand-edited on Windows and has CRLF endings, and without this the
# match silently fails and every button press answers "could not find ignore3dProcesses".
IGNORE_LINE = re.compile(r'^(?P<pre>[ \t]*"ignore3dProcesses"[ \t]*:[ \t]*\[)(?P<items>.*?)(?P<post>\][ \t]*,?[ \t]*\r?)$',
                         re.MULTILINE)


def _read(path: Path) -> tuple[str, str]:
    """(text, encoding) - a config written with a BOM keeps it."""
    raw = path.read_bytes()
    return (raw.decode("utf-8-sig"), "utf-8-sig") if raw.startswith(b"\xef\xbb\xbf") else (raw.decode("utf-8"), "utf-8")


def tripped_by(reason: str | None) -> dict:
    """What the watcher says turned the AI off, and whether it can be answered by name."""
    text = (reason or "").strip()
    if text.lower().startswith("game:"):
        text = text.split(":", 1)[1].strip()
    if not text:
        return {"kind": None, "name": None, "detail": "", "can_ignore": False}
    m = BUSY.match(text)
    if m:
        return {"kind": "busy", "name": m["name"], "percent": float(m["percent"]), "detail": text,
                "can_ignore": bool(SAFE_NAME.match(m["name"]))}
    m = BY_NAME.match(text)
    if m:                                   # an anti-cheat service: a real game, by a name we put there on purpose
        return {"kind": "process", "name": m["name"].strip(), "detail": text, "can_ignore": False}
    if text in ("fullscreen D3D app", "presentation mode"):
        return {"kind": "fullscreen", "name": None, "detail": text, "can_ignore": False}
    m = BY_PATH.match(text)
    if m:                                   # matched by install path: Steam/Epic/Riot, so very probably a game
        return {"kind": "path", "name": m["name"].strip(), "detail": text, "can_ignore": False}
    return {"kind": "other", "name": None, "detail": text, "can_ignore": False}


def ignored(config: Path = CONFIG) -> list[str]:
    try:
        text, _ = _read(config)
        return list(json.loads(text)["games"]["ignore3dProcesses"])
    except (OSError, ValueError, KeyError, TypeError):
        return []


def state(config: Path = CONFIG, state_file: Path = STATE) -> dict:
    """What the AI card shows: the mode, what tripped the guard, and whether a button can answer it."""
    try:
        st = json.loads(_read(state_file)[0])
    except (OSError, ValueError):
        return {"available": False}
    mode, reason = st.get("mode"), st.get("reason")
    # the reason only describes a trip while the AI is off: in any other mode it says how it got back
    # ("game ended", "woke from sleep"), which would otherwise parse as a game called "ended"
    trip = tripped_by(reason) if mode == "off" else {"kind": None, "name": None, "detail": "", "can_ignore": False}
    off_for_a_game = mode == "off" and trip["kind"] is not None
    return {"available": True, "mode": mode, "reason": reason, "since": st.get("since"),
            "off_for_a_game": off_for_a_game, "ignored": ignored(config), **trip,
            "can_ignore": bool(off_for_a_game and trip["can_ignore"] and trip["name"] not in ignored(config))}


def running_names() -> set[str]:
    out = set()
    for p in psutil.process_iter(["name"]):
        name = (p.info.get("name") or "").removesuffix(".exe")
        if name:
            out.add(name.lower())
    return out


def ignore(name: str, config: Path = CONFIG, running: set[str] | None = None) -> dict:
    """Add one process name to games.ignore3dProcesses, rewriting only that line.

    The name has to look like a process name *and* be running: the page sends a name, and a name from anywhere else
    has no business being written into the machine's config."""
    name = (name or "").strip().removesuffix(".exe")
    if not SAFE_NAME.match(name):
        return {"ok": False, "why": "that is not a process name"}
    if name.lower() not in (running_names() if running is None else running):
        return {"ok": False, "why": f"no process called {name} is running"}
    try:
        text, encoding = _read(config)
    except OSError as e:
        return {"ok": False, "why": f"could not read the config: {type(e).__name__}"}
    m = IGNORE_LINE.search(text)
    if not m:
        return {"ok": False, "why": "could not find ignore3dProcesses in the config"}
    try:
        items = json.loads("[" + m["items"] + "]")
    except ValueError:
        return {"ok": False, "why": "could not read the current ignore list"}
    if any(str(x).lower() == name.lower() for x in items):
        return {"ok": True, "why": f"{name} was already ignored", "ignored": items}
    items.append(name)
    line = m["pre"] + ", ".join(json.dumps(x) for x in items) + m["post"]
    new = text[:m.start()] + line + text[m.end():]
    try:
        json.loads(new)                       # never leave the machine's config unparseable
    except ValueError:
        return {"ok": False, "why": "the edit would have broken the config, so nothing was written"}
    tmp = config.with_suffix(".json.tmp")
    try:
        # newline="": the text already carries the file's own endings, and letting Python translate them again
        # turns every CRLF into CR CRLF - the file still parses, so nothing complains until it is unreadable
        tmp.write_text(new, encoding=encoding, newline="")
        tmp.replace(config)
    except OSError as e:
        return {"ok": False, "why": f"could not write the config: {type(e).__name__}"}
    return {"ok": True, "ignored": items,
            "why": f"{name} is not a game any more — the watcher picks that up within 10 s"}


def resume() -> dict:
    """Back to desk now, instead of waiting out resumeAfterMinutes. Only sensible once the cause is gone."""
    if not SOL_LLM.is_file():
        return {"ok": False, "why": f"{SOL_LLM} is missing"}
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(SOL_LLM), "desk"],
                         creationflags=NO_WINDOW, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return {"ok": False, "why": f"could not start sol-llm.ps1: {type(e).__name__}"}
    return {"ok": True, "why": "going back to desk"}
