"""The "Deck services" card: are the projects the HUD autostarts (sol-deck, sol-cs2) up, since when, what did the
last autostart do, and has the iPad deck client been seen lately. Built only from what other collectors already
hold (the Localhost card's listener rows, the security collector), so it adds no scan and no request. It must not
read the projects collector: that one scans processes and git, and would then refresh all the time. Also the down-too-long warning."""
from __future__ import annotations

DOWN_AFTER_S = 120.0        # an autostart project down this long ...
HUD_UP_AFTER_S = 120.0      # ... while the HUD itself has been up this long (right after a start it is still launching them)


def autostart_names(reg: dict) -> list[str]:
    """The projects marked `autostart: true` in launchpad.yaml (never this app itself), in file order."""
    return [n for n, spec in (reg.get("projects") or {}).items()
            if (spec or {}).get("autostart") and not (spec or {}).get("self")]


def build_payload(reg: dict, listening: list[dict], results: dict | None, security: dict | None, now: float) -> dict:
    """listening: the ports collector's rows (port, pid, up_seconds). A listener on a project's port counts as that
    project running (no check of which process it is, so a foreign process on the port reads as up, never "busy").
    results: {name: {"ok", "why", "at"(epoch)}} from Hub._autostart. security: the security collector's answer."""
    results = results or {}
    by_port = {}
    for r in listening or []:
        by_port.setdefault(r.get("port"), r)
    services = []
    for name in autostart_names(reg):
        spec = (reg.get("projects") or {}).get(name) or {}
        port = spec.get("port")
        run = by_port.get(int(port)) if port else None
        res = results.get(name)
        services.append({
            "name": name,
            "title": spec.get("title") or name,
            "port": spec.get("port"),
            "up": bool(run),
            "pid": (run or {}).get("pid"),
            "up_seconds": (run or {}).get("up_seconds"),
            "busy_by": None,
            "can_start": not run and bool(spec.get("start")),
            "autostart": ({"ok": bool(res.get("ok")), "why": str(res.get("why") or ""), "ago_s": max(0, int(now - (res.get("at") or now)))}
                          if res else None),
        })
    sec = security or {}
    client = sec.get("client") or {}
    ipad = {"deck_up": bool(sec.get("up")), "active": bool(client.get("active")), "name": client.get("name"),
            "status_text": client.get("status_text") or ""}
    return {"available": True, "services": services, "ipad": ipad,
            "down": [s["name"] for s in services if not s["up"]]}


class DownWatch:
    """One warning per down period per project: raised once it has been down > DOWN_AFTER_S while the HUD has been up
    > HUD_UP_AFTER_S, cleared (so a later outage warns again) when it is back. Clock values are passed in."""

    def __init__(self) -> None:
        self.down_since: dict[str, float] = {}
        self.told: set[str] = set()

    def step(self, down: list[str], now: float, hud_up_s: float) -> list[tuple[str, str]]:
        """Returns [(name, key)] to warn about now; key is unique per down period."""
        out = []
        for name in list(self.down_since):
            if name not in down:
                del self.down_since[name]
                self.told.discard(name)
        for name in down:
            since = self.down_since.setdefault(name, now)
            if name not in self.told and now - since > DOWN_AFTER_S and hud_up_s > HUD_UP_AFTER_S:
                self.told.add(name)
                out.append((name, f"deck_down:{name}:{int(since)}"))
        return out
