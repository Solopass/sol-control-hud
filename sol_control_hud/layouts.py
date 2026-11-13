"""Dashboard layout presets: which cards show, in what order, at what size (plans/SOL_HUD_LAYOUTS_PLAN.md).

Stored in data/layouts.json (the HUD, not the browser: the same presets in any browser, kept across a cleared cache,
readable by Claude). The page reads it through GET /api/layouts and changes it through POST /api/layouts.

Sizes: every card has 2-5 of XS / S / M / L / XL. A size is a grid span (columns, rows) plus which parts of the card
show (app.css, `.card[data-size=..]`). The Everyday preset is exactly the dashboard as it was before presets.

Auto-switch: a preset can say `auto_when: "game"` or `"away"`. When that starts, the hub switches to it and remembers
what you had; when it ends, it switches back - unless you picked a preset by hand in between.

A preset can also choose the ticker's slides (`ticker_slides`); null means "the slides you set in Settings", which
are remembered the first time a preset with its own slides takes over and given back when a null preset returns.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

from .paths import DATA_DIR

FILE = DATA_DIR / "layouts.json"
VERSION = 1
SIZES = ("XS", "S", "M", "L", "XL")
AUTO_WHEN = ("game", "away")
NAME = re.compile(r"^[^\x00-\x1f<>\"]{1,40}$")
MAX_PRESETS = 20

# card id -> (title, {size: [columns, rows]}, today's size). Order = the dashboard's order before presets.
WIDGETS: dict[str, tuple[str, dict[str, list[int]], str]] = {
    "c-gpu": ("CPU · GPU", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [1, 2], "XL": [2, 2]}, "L"),
    "c-vram": ("VRAM", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-ai": ("Local AI", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-ai-thinking": ("Live AI Thinking", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-review": ("Exam assist & review", {"XS": [1, 1], "L": [2, 1]}, "L"),
    "c-chains": ("Chains", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-projects": ("Projects", {"XS": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-media": ("Media APIs", {"XS": [1, 1], "S": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-solang": ("Solang", {"XS": [1, 1], "M": [1, 1], "L": [2, 1]}, "L"),
    "c-overnight": ("Overnight AI Run", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-ask": ("Ask it overnight", {"XS": [1, 1], "S": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-awayq": ("Away & queue", {"XS": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-system": ("Memory · Network", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-disks": ("Disks", {"XS": [1, 1], "M": [1, 1]}, "M"),
    "c-ports": ("Localhost", {"XS": [1, 1], "S": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-security": ("Cyber Security & Canary", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-stab": ("Stability & backups", {"XS": [1, 1], "M": [1, 1]}, "M"),
    "c-svc": ("Services", {"XS": [1, 1], "M": [1, 1]}, "M"),
    "c-net": ("Network · Pi-hole", {"XS": [1, 1], "M": [1, 1], "L": [2, 1]}, "L"),
    "c-health": ("HUD health", {"XS": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-work": ("Workspace", {"XS": [1, 1], "M": [1, 1]}, "M"),
    "c-notes": ("Obsidian Notes", {"XS": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-week": ("This week", {"XS": [1, 1], "S": [1, 1], "M": [1, 1]}, "M"),
    "c-ai-usage": ("AI Usage", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "L"),
    "c-playground": ("Quick AI Console", {"XS": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
    "c-procs": ("Process Radar", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-media-ctl": ("Media & Audio Deck", {"XS": [1, 1], "S": [1, 1], "M": [1, 1], "L": [2, 1]}, "M"),
    "c-activity": ("Activity", {"XS": [1, 1], "M": [1, 1], "L": [2, 1], "XL": [3, 1]}, "L"),
}
TICKER_SLIDES = ("HW", "AI", "RUN", "SVC", "DISK", "SYS", "NOTE", "GIT", "NET", "MEDIA", "APPS")


def _preset(name: str, icon: str, cards: list[tuple[str, str]] | None, auto_when=None, ticker=None) -> dict:
    """cards: [(id, size)] shown in that order, the rest hidden; None = every card at today's size."""
    if cards is None:
        widgets = [{"id": w, "size": WIDGETS[w][2], "hidden": False} for w in WIDGETS]
    else:
        shown = [{"id": w, "size": s, "hidden": False} for w, s in cards]
        widgets = shown + [{"id": w, "size": WIDGETS[w][2], "hidden": True} for w in WIDGETS if w not in dict(cards)]
    return {"name": name, "icon": icon, "auto_when": auto_when, "ticker_slides": ticker, "widgets": widgets}


def default_presets() -> list[dict]:
    return [
        _preset("Everyday", "◧", None),
        _preset("Gaming", "🎮", [
            ("c-gpu", "S"),
            ("c-vram", "S"),
            ("c-media-ctl", "S"),
            ("c-procs", "M"),
            ("c-health", "XS"),
            ("c-svc", "XS"),
        ], auto_when="game", ticker=["HW", "MEDIA", "SYS"]),
        _preset("AI work", "🧠", [
            ("c-playground", "XL"),
            ("c-ai-thinking", "XL"),
            ("c-ai", "L"),
            ("c-vram", "L"),
            ("c-chains", "XL"),
            ("c-ask", "L"),
            ("c-awayq", "M"),
            ("c-ai-usage", "L"),
            ("c-activity", "L"),
        ], ticker=["HW", "AI", "RUN", "NOTE"]),
        _preset("Dev", "‹/›", [
            ("c-projects", "XL"),
            ("c-ports", "L"),
            ("c-procs", "L"),
            ("c-work", "M"),
            ("c-notes", "L"),
            ("c-media", "L"),
            ("c-gpu", "M"),
            ("c-net", "M"),
        ], ticker=["HW", "GIT", "APPS", "NOTE", "NET"]),
        _preset("Overnight", "🌙", [
            ("c-overnight", "XL"),
            ("c-awayq", "L"),
            ("c-chains", "L"),
            ("c-stab", "M"),
            ("c-disks", "M"),
            ("c-security", "L"),
            ("c-health", "M"),
            ("c-week", "M"),
        ], auto_when="away", ticker=["AI", "RUN", "DISK", "SYS"]),
        _preset("Minimal", "·", [
            ("c-gpu", "XS"),
            ("c-vram", "XS"),
            ("c-ai", "XS"),
            ("c-disks", "XS"),
        ], ticker=["HW", "AI"]),
    ]


def default_data() -> dict:
    return {"version": VERSION, "active": "Everyday", "auto": True, "ticker_base": None, "rev": 0,
            "presets": default_presets()}


# ---- validation: everything from the page goes through here
class Bad(ValueError):
    pass


def clean_preset(p: dict) -> dict:
    """A preset as the page sent it -> a valid one, or Bad. Unknown cards are dropped, missing ones added hidden, a
    size a card doesn't have falls back to its default."""
    if not isinstance(p, dict):
        raise Bad("a preset must be an object")
    name = str(p.get("name") or "").strip()
    if not NAME.match(name):
        raise Bad("a preset name is 1-40 characters, no < > or quotes")
    auto = p.get("auto_when")
    if auto not in (None, *AUTO_WHEN):
        raise Bad("auto_when is game, away or nothing")
    ticker = p.get("ticker_slides")
    if ticker is not None:
        if not isinstance(ticker, list) or not ticker:
            raise Bad("ticker_slides is a non-empty list, or null for your Settings")
        ticker = [t for t in dict.fromkeys(str(x) for x in ticker) if t in TICKER_SLIDES]
        if not ticker:
            raise Bad("ticker_slides has no known slide")
    seen, widgets = set(), []
    for w in p.get("widgets") or []:
        if not isinstance(w, dict):
            continue
        wid = str(w.get("id") or "")
        if wid not in WIDGETS or wid in seen:
            continue
        seen.add(wid)
        size = str(w.get("size") or "")
        widgets.append({"id": wid, "size": size if size in WIDGETS[wid][1] else WIDGETS[wid][2],
                        "hidden": bool(w.get("hidden"))})
    widgets += [{"id": w, "size": WIDGETS[w][2], "hidden": True} for w in WIDGETS if w not in seen]
    icon = str(p.get("icon") or "")[:4]
    return {"name": name, "icon": icon, "auto_when": auto, "ticker_slides": ticker, "widgets": widgets}


def merge(data: dict) -> dict:
    """A stored file -> the current shape: every preset cleaned (cards added later appear hidden; in Everyday they
    are shown at the end), at least one preset, a valid active one."""
    out = default_data()
    presets = []
    for p in (data.get("presets") or [])[:MAX_PRESETS]:
        try:
            cp = clean_preset(p)
        except Bad:
            continue
        if cp["name"] not in [x["name"] for x in presets]:
            if cp["name"] == "Everyday":
                known = {w.get("id") for w in p.get("widgets") or [] if isinstance(w, dict)}
                for w in cp["widgets"]:
                    if w["id"] not in known:
                        w["hidden"] = False              # a brand-new card: show it in the everyday view
                w_order = {wid: i for i, wid in enumerate(WIDGETS)}
                cp["widgets"].sort(key=lambda x: w_order.get(x["id"], 999))
            elif cp["name"] == "AI work":
                known_shown = {w.get("id") for w in p.get("widgets") or [] if isinstance(w, dict) and not w.get("hidden")}
                if "c-ai-thinking" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-ai-thinking":
                            w["hidden"] = False
                            w["size"] = "XL"
                if "c-playground" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-playground":
                            w["hidden"] = False
                            w["size"] = "XL"
                if "c-ai-usage" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-ai-usage":
                            w["hidden"] = False
                            w["size"] = "L"
            elif cp["name"] == "Gaming":
                known_shown = {w.get("id") for w in p.get("widgets") or [] if isinstance(w, dict) and not w.get("hidden")}
                if "c-media-ctl" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-media-ctl":
                            w["hidden"] = False
                            w["size"] = "S"
                if "c-procs" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-procs":
                            w["hidden"] = False
                            w["size"] = "M"
            elif cp["name"] == "Dev":
                known_shown = {w.get("id") for w in p.get("widgets") or [] if isinstance(w, dict) and not w.get("hidden")}
                if "c-procs" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-procs":
                            w["hidden"] = False
                            w["size"] = "L"
            elif cp["name"] == "Overnight":
                known_shown = {w.get("id") for w in p.get("widgets") or [] if isinstance(w, dict) and not w.get("hidden")}
                if "c-overnight" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-overnight":
                            w["hidden"] = False
                            w["size"] = "XL"
                if "c-disks" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-disks":
                            w["hidden"] = False
                            w["size"] = "M"
                if "c-security" not in known_shown:
                    for w in cp["widgets"]:
                        if w["id"] == "c-security":
                            w["hidden"] = False
                            w["size"] = "L"
            presets.append(cp)
    if presets:
        out["presets"] = presets
    names = [p["name"] for p in out["presets"]]
    out["active"] = data.get("active") if data.get("active") in names else names[0]
    out["auto"] = bool(data.get("auto", True))
    tb = data.get("ticker_base")
    out["ticker_base"] = [t for t in tb if t in TICKER_SLIDES] if isinstance(tb, list) else None
    out["rev"] = int(data.get("rev") or 0)
    return out


def load(path: Path | None = None) -> dict:
    path = path or FILE
    if not path.exists():
        return default_data()
    try:
        return merge(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        try:                                             # never lose it silently: keep the bad file beside it
            path.replace(path.with_suffix(".json.bad"))
        except OSError:
            pass
        from .hub import log
        log(f"layouts.json unreadable ({type(e).__name__}: {e}): kept as layouts.json.bad, using the defaults")
        return default_data()


def save(data: dict, path: Path | None = None) -> dict:
    path = path or FILE
    data = merge(data)
    data["rev"] = data.get("rev", 0) + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return data


def preset(data: dict, name: str) -> dict | None:
    return next((p for p in data["presets"] if p["name"] == name), None)


# ---- the page's actions (POST /api/layouts)
def apply_action(data: dict, body: dict) -> tuple[dict, str]:
    """(new data, what happened) or Bad. Pure: the caller saves."""
    data = copy.deepcopy(data)
    act = str(body.get("action") or "")
    names = [p["name"] for p in data["presets"]]
    if act == "activate":
        name = str(body.get("name") or "")
        if name not in names:
            raise Bad(f"no preset called {name!r}")
        data["active"] = name
        return data, f"layout: {name}"
    if act == "save":                                    # create or replace (by name; `old` renames)
        p = clean_preset(body.get("preset") or {})
        old = str(body.get("old") or p["name"])
        if p.get("auto_when"):
            for other in data["presets"]:
                if other["name"] != old and other.get("auto_when") == p["auto_when"]:
                    other["auto_when"] = None
        if old in names:
            if p["name"] != old and p["name"] in names:
                raise Bad(f"there is already a preset called {p['name']!r}")
            data["presets"][names.index(old)] = p
            if data["active"] == old:
                data["active"] = p["name"]
            return data, f"saved {p['name']}"
        if p["name"] in names:
            raise Bad(f"there is already a preset called {p['name']!r}")
        if len(names) >= MAX_PRESETS:
            raise Bad(f"at most {MAX_PRESETS} presets")
        data["presets"].append(p)
        return data, f"new preset {p['name']}"
    if act == "delete":
        name = str(body.get("name") or "")
        if name not in names:
            raise Bad(f"no preset called {name!r}")
        if len(names) == 1:
            raise Bad("the last preset can't be deleted")
        data["presets"] = [p for p in data["presets"] if p["name"] != name]
        if data["active"] == name:
            data["active"] = data["presets"][0]["name"]
        return data, f"deleted {name}"
    if act == "move":                                    # reorder the presets themselves (menu order, Alt+N)
        name = str(body.get("name") or "")
        try:
            to = int(body.get("to", -1))
        except (TypeError, ValueError):
            raise Bad("which preset, and where?")
        if name not in names or not 0 <= to < len(names):
            raise Bad("which preset, and where?")
        p = data["presets"].pop(names.index(name))
        data["presets"].insert(to, p)
        return data, f"moved {name}"
    if act == "reset":                                   # a starter preset back to how it shipped
        name = str(body.get("name") or "")
        starter = next((p for p in default_presets() if p["name"] == name), None)
        if not starter or name not in names:
            raise Bad(f"{name!r} isn't a starter preset")
        data["presets"][names.index(name)] = starter
        return data, f"reset {name}"
    if act == "reset_defaults":                          # reset all presets to curated factory presets
        data["presets"] = default_presets()
        if data["active"] not in [p["name"] for p in data["presets"]]:
            data["active"] = data["presets"][0]["name"]
        return data, "reset all presets to curated defaults"
    if act == "auto":
        data["auto"] = bool(body.get("on"))
        return data, f"auto-switch {'on' if data['auto'] else 'off'}"
    if act == "import":
        raw_list = body.get("presets")
        if isinstance(raw_list, dict):
            raw_list = [raw_list]
        if not isinstance(raw_list, list) or not raw_list:
            raise Bad("no presets to import")
        imported = 0
        for raw in raw_list:
            try:
                cp = clean_preset(raw)
            except Bad:
                continue
            base_name = cp["name"]
            cand_name = base_name
            num = 2
            existing = [x["name"] for x in data["presets"]]
            while cand_name in existing:
                cand_name = f"{base_name} {num}"
                num += 1
            if len(data["presets"]) >= MAX_PRESETS:
                break
            cp["name"] = cand_name
            if cp.get("auto_when"):
                for other in data["presets"]:
                    if other.get("auto_when") == cp["auto_when"]:
                        other["auto_when"] = None
            data["presets"].append(cp)
            imported += 1
        if not imported:
            raise Bad("no valid presets to import")
        return data, f"imported {imported} preset(s)"
    raise Bad(f"unknown action {act!r}")


# ---- auto-switch (the hub calls this every tick with what's going on)
class Auto:
    """Remembers what you had before a game / Away switched the layout, and whether you changed it by hand since."""

    def __init__(self):
        self.cond: str | None = None
        self.before: str | None = None
        self.switched_to: str | None = None

    def step(self, data: dict, cond: str | None) -> str | None:
        """The preset to activate now, or None to leave it. cond: "game", "away" or None."""
        if cond == self.cond:
            return None
        prev, self.cond = self.cond, cond
        target = None
        if prev and self.switched_to:                    # the condition ended (or changed): give back what you had
            if data["active"] == self.switched_to and self.before and preset(data, self.before):
                target = self.before
            self.before = self.switched_to = None
        if cond and data.get("auto", True):
            p = next((p for p in data["presets"] if p.get("auto_when") == cond), None)
            if p and p["name"] != data["active"]:
                self.before = target or data["active"]
                self.switched_to = p["name"]
                target = p["name"]
        return target


def ticker_changes(data: dict, new_name: str, current: dict[str, bool]) -> tuple[dict[str, bool] | None, list | None]:
    """(slides to set, new ticker_base) when switching to `new_name`. None slides = leave the ticker alone."""
    p = preset(data, new_name) or {}
    want = p.get("ticker_slides")
    base = data.get("ticker_base")
    if want:
        if base is None:                                 # your own Settings choice, kept for later
            base = [t for t, on in current.items() if on]
        return {t: (t in want) for t in TICKER_SLIDES}, base
    if base:                                             # back to "your Settings": give them back
        return {t: (t in base) for t in TICKER_SLIDES}, None
    return None, base


def registry() -> list[dict]:
    """What the page needs to know about the cards."""
    return [{"id": w, "title": t, "sizes": {s: span for s, span in sizes.items()}, "default": d}
            for w, (t, sizes, d) in WIDGETS.items()]


def condition(snap) -> str | None:
    """game / away / None from the snapshot (the watcher's game guard and Away mode)."""
    if snap is None:
        return None
    if snap.ai_mode == "off" and str(snap.ai_reason or "").startswith("game"):
        return "game"
    if snap.ai_mode == "away":
        return "away"
    return None

