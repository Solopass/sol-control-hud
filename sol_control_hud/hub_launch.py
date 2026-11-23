"""The Projects and Media APIs cards' Start / Stop / Open (and the Solang queue buttons): moved out of hub.py on 2026-11-22.

Takes the Hub as `self`, unchanged from when it was a method; hub.py names are read as `_hub.<name>`."""
from __future__ import annotations

from . import hub as _hub


def _launch(self, kind: str, name: str, op: str, body: dict | None = None) -> dict:
    from . import launcher
    from .data.collectors import projects
    reg = projects.load_registry()
    b = body or {}
    # changes that need no current state: your tags / hides, and the transcript box
    if kind == "project" and op in ("hide", "show", "tag"):
        r = projects.set_user(name, op, str(b.get("tag") or ""), workspace=reg["workspace"])
        if "projects" in getattr(self, "collectors", {}):
            self.collectors["projects"].value = None
        _hub.log(f"project {op} {name} {b.get('tag') or ''}: {r.get('why')}".strip())
        return r
    if kind == "media" and op == "note":
        r = launcher.start_transcript_note(str(b.get("source") or ""), str(b.get("title") or ""))
        _hub.log(f"transcript note {str(b.get('source') or '')[:120]}: {r.get('why')}")
        if "media" in getattr(self, "collectors", {}):
            self.collectors["media"].value = None
        return r
    if kind == "media" and op == "open_note":
        return launcher.open_note_run(b.get("run"))
    if kind == "solang" and op == "bulk_add":
        r = launcher.bulk_add_solang(str(b.get("links") or b.get("source") or ""))
        _hub.log(f"solang bulk_add: {r.get('why')}")
        return r
    if kind == "solang" and op == "queue_status":
        from .chains import solang_tools
        return {"ok": True, "status": solang_tools.get_queue_status()}
    if kind == "solang" and op == "queue_pause":
        from .chains import solang_tools
        return solang_tools.set_queue_paused(True)
    if kind == "solang" and op == "queue_resume":
        from .chains import solang_tools
        return solang_tools.set_queue_paused(False)
    if kind == "solang" and op == "sync_library":
        from .chains import solang_tools
        solang_tools.generate_karaoke_library_note()
        return {"ok": True, "why": "Synced Karaoke Library in 1Notebook"}
    if kind == "solang" and op == "run_queue_batch":
        from .chains import solang_tools
        file_path = str(b.get("file") or r"D:\Output\Takeout\prepared_music_history.json")
        count = int(b.get("count") or 25)
        r = solang_tools.run_takeout_queue({"file": file_path, "target_count": count})
        return {"ok": True, "why": r.get("summary", "Ran queue batch"), "result": r}
    if kind == "solang" and op == "prepare_takeout":
        from .chains import solang_tools
        source_p = str(b.get("source") or b.get("file") or r"C:\Users\cohen\Downloads\Takeout\YouTube and YouTube Music\history\watch-history.html")
        r = solang_tools.prepare_takeout_file(source_p)
        return {"ok": True, "why": r["summary"], "result": r, "status": r["status"]}
    if kind == "solang" and op == "reset_queue":
        from .chains import solang_tools
        r = solang_tools.reset_takeout_queue()
        return {"ok": True, "why": r["why"], "status": solang_tools.get_queue_status()}
    if kind == "solang" and op == "upload_takeout":
        from .chains import solang_tools
        content = str(b.get("content") or "")
        filename = str(b.get("filename") or "watch-history.html")
        r = solang_tools.save_takeout_upload(content, filename)
        return {"ok": True, "why": f"Loaded Takeout file: {filename}", "status": r.get("status")}
    if kind == "project" and op in ("folder", "editor", "file"):
        try:                                      # opening needs no card state: no rebuild, so the window comes at once
            return launcher.open_project(reg, name, op)
        except launcher.LaunchError as e:
            return {"ok": False, "why": str(e)}
    spec = (reg.get("projects") or {}).get(name) or {}
    in_wsl = kind != "project" or spec.get("runs_in") == "wsl" or bool(spec.get("media"))
    self._refresh_launchpad(wsl=in_wsl)           # act on the current state, not a cached one
    try:
        rows = self.projects_payload()["projects"]
        tiles = self.media_payload()["tiles"]
        if kind == "project":
            if op == "start":
                r = launcher.start_project(reg, name, rows, tiles)
            elif op == "stop":
                r = launcher.stop_project(reg, name, rows, tiles)
            elif op in ("folder", "editor", "file"):
                r = launcher.open_project(reg, name, op)
            elif op == "sidecar_start":
                r = launcher.start_sidecar(reg, name, str(b.get("sidecar") or ""), rows)
            elif op == "sidecar_stop":
                r = launcher.stop_sidecar(reg, name, str(b.get("sidecar") or ""), rows)
            else:
                r = {"ok": False, "why": f"unknown project action {op!r}"}
        elif op == "start":
            r = launcher.start_media(reg, name, tiles, rows)
        elif op == "stop":
            r = launcher.stop_media(reg, name, tiles, rows)
        elif op == "folder":
            r = launcher.open_media_folder(reg, name)
        else:
            r = {"ok": False, "why": f"unknown media action {op!r}"}
    except launcher.LaunchError as e:
        r = {"ok": False, "why": str(e)}
    if op in ("start", "stop", "sidecar_start", "sidecar_stop"):
        _hub.log(f"{kind} {op} {name}{' ' + str(b.get('sidecar')) if op.startswith('sidecar') else ''}: {r.get('why')}")
        projects.forget_git(name)
        self._refresh_launchpad(wsl=in_wsl)
    return r
