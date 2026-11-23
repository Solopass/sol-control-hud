"""app.js was split into static/app/*.js (2026-11-22): the pieces must exist and load in the original order."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "sol_control_hud" / "views" / "web" / "static"


def _scripts() -> list[str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return re.findall(r'<script src="/static/([^"]+)"></script>', html)


def test_every_script_tag_points_at_a_file():
    names = _scripts()
    assert names and all((STATIC / n).is_file() for n in names), [n for n in names if not (STATIC / n).is_file()]


def test_app_js_first_and_boot_runs_before_launchpad():
    names = _scripts()
    assert names[0] == "app.js"                                  # $, post, toast, esc ... for every other file
    assert names.index("app/boot.js") == names.index("launchpad.js") - 1
    pieces = [n for n in names if n.startswith("app/")]
    assert pieces == sorted(pieces, key=names.index) and len(pieces) == len(set(pieces))


def test_every_split_piece_is_loaded():
    loaded = set(_scripts())
    assert {f"app/{p.name}" for p in (STATIC / "app").glob("*.js")} <= loaded
