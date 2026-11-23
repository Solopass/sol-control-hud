"""The dashboard web app: FastAPI routes, middleware and the cached collectors behind them. Moved out of hub.py on 2026-11-22.

build_app takes the Hub as `self`, unchanged from when it was a method; hub.py names are read as `_hub.<name>`
(route annotations too: they are strings here, and FastAPI resolves them from this module's globals)."""
from __future__ import annotations

from . import hub as _hub


def build_app(self):
    """The dashboard's web app on this hub's data: the page + /static, /api/stream (pushed), /api/snapshot,
    /api/history, /api/meta, /api/views and /api/action (POST: our page only), /api/status (the old cards)."""
    import asyncio

    from fastapi import Body
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.staticfiles import StaticFiles

    from .views.web import feed
    from .views.web.app import STATIC, Cached, build_collectors, create_app
    self.collectors = build_collectors(self.collector._sampler, self.guard)
    from . import models_ctl
    from .data.collectors import cpu, ports
    self.collectors["ports"] = Cached(ports.listeners, 3)      # also keeps the open/closed history up to date
    self.collectors.update({"cpu": Cached(cpu.sample, 1.5),    # per-core load + clock for the CPU · GPU card
                            "model_procs": Cached(models_ctl.model_processes, 5),
                            "model_desc": Cached(models_ctl.descriptions, 60)})
    self.collectors.update({"away": Cached(feed.away_info, 1.5), "chains": Cached(lambda: feed._read_json(feed.LLM_DIR / "chains.json"), 1.5),
                            "activity": Cached(feed.activity, 5)})
    from .data.collectors import media_apis, projects

    def probe() -> dict:          # one wsl.exe for every media service, never while WSL is stopped
        reg = projects.load_registry()
        return media_apis.wsl_probe(reg["media"], media_apis.wsl_port_list(reg), reg.get("wsl_distro", "Ubuntu-24.04"),
                                    (self.collectors["wsl"].get() or {}).get("state"))
    self.collectors.update({"media_probe": Cached(probe, 10), "projects": Cached(self.projects_payload, 4),
                            "media": Cached(self.media_payload, 4)})
    from .data.collectors import speed           # real answer speed (router.log) and the watcher's spill auto-heal
    self.collectors.update({"speed": Cached(lambda: speed.answer_speed(self.collectors["model_procs"].get()), 3),
                            "heal": Cached(speed.heal_status, 10)})
    from .data.collectors import displays, gpu   # apps rendering on a GPU with no monitor (10-08)
    gpu_names: dict[int, str] = {}
    self.collectors["displays"] = Cached(
        lambda: displays.report(self.collector._sampler.last_util, lambda p: gpu.process_name(p, gpu_names)), 10)
    from . import game_guard                       # why the AI is off, and the buttons that answer it
    self.collectors["guard"] = Cached(game_guard.state, 3)
    from .data.collectors import ai_ops
    self.collectors["ai_ops"] = Cached(lambda: ai_ops.sample_ai_ops(self.collectors["model_procs"].get()), 1.5)
    from .data.collectors import ai_usage
    self.collectors["ai_usage"] = Cached(ai_usage.get_ai_usage, 10)
    from .data.collectors import security
    self.collectors["security"] = Cached(security.get_security_status, 2)
    self.collectors["deck_services"] = Cached(self.deck_services_payload, 5)
    from .data.collectors import ai_inbox          # "From your AI": reads a few note files, nothing else
    self.collectors["ai_inbox"] = Cached(ai_inbox.collect, 30)
    from .data.collectors import network          # Pi-hole + LAN devices: its own thread probes, this only reads
    network.start_network_monitor()
    self.collectors["network"] = Cached(network.last_status, 2)
    from .data.collectors import sol_pad
    self.collectors["sol_pad"] = Cached(sol_pad.get_sol_pad_status, 5)
    self.collectors["top_procs"] = Cached(self._top_processes_payload, 4)   # one process scan per 4 s, however many tabs
    app = create_app(collectors=self.collectors)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/api/ai-ops")
    def ai_ops_state() -> dict:
        return self.collectors["ai_ops"].get() if "ai_ops" in self.collectors else {}

    ALLOWED_ORIGINS = {
        self.url,
        f"http://localhost:{self.port}",
        "https://www.youtube.com",
        "https://m.youtube.com",
    }

    @app.middleware("http")
    async def guard_and_seen(request: _hub.Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "OPTIONS":
            if origin in ALLOWED_ORIGINS:
                from fastapi import Response
                return Response(
                    status_code=204,
                    headers={
                        "Access-Control-Allow-Origin": origin,
                        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                        "Access-Control-Allow-Headers": "Content-Type, x-sol-control",
                        "Access-Control-Max-Age": "86400",
                    },
                )
        if request.method == "POST":
            # only this page and allowed workstation origins may change things: a custom header forces a CORS preflight
            if request.headers.get("x-sol-control") != "1" or (origin and origin not in ALLOWED_ORIGINS):
                return JSONResponse({"error": "not allowed"}, status_code=403)
        if request.url.path.startswith("/api/"):
            self.last_web = _hub.time.monotonic()      # someone has the dashboard open: keep the 2 s pace
        response = await call_next(request)
        if origin and origin in ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
        if request.url.path.startswith("/static/"):
            # revalidate every time (cheap: 304 if unchanged): after an update the browser must not keep an old
            # app.js (09-25 lesson with the old HUD's page)
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/api/stream")
    async def stream(request: _hub.Request):
        async def events():
            state = None
            while not await request.is_disconnected():
                self.last_web = _hub.time.monotonic()  # an open stream = a dashboard on screen (hidden tabs close it)
                payload = await self.shared_payload.get()   # one build per tick for all tabs
                msg, state = _hub.stream_message(state, payload)   # after the first message: only what changed
                yield f"data: {_hub.json.dumps(msg, default=str, separators=(',', ':'))}\n\n"
                await asyncio.sleep(_hub.ACTIVE_PACE)
        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/history")
    def history(range: str = "1h") -> dict:   # noqa: A002 - the query parameter's name
        if range in ("24h", "7d"):
            from .metrics import series
            return series(_hub.DATA_DIR / "metrics.sqlite", 24 if range == "24h" else 168)
        return self.history.export()

    @app.get("/api/week")
    def week_summary() -> dict:
        from .metrics import week
        return week(_hub.DATA_DIR / "metrics.sqlite")

    @app.get("/api/meta")
    def meta() -> dict:
        from .views.ticker import THEMES
        theme = getattr(self.ticker, "theme_name", "cyber-cyan")
        return {"themes": THEMES, "theme": theme, "port": self.port, "pid": _hub.os.getpid()}

    @app.get("/api/layouts")
    def layouts_read() -> dict:
        """The layout presets, the cards and their sizes, the ticker's slides (layouts.py)."""
        return self.layout_state()

    @app.post("/api/layouts")
    def layouts_change(body: dict = Body(default={})) -> dict:
        return self.layout_action(body)

    @app.post("/api/action")
    def action(body: dict = Body(default={})) -> dict:
        return self.do_action(str(body.get("action", "")), str(body.get("target", "")), body)

    @app.get("/api/control")
    def control_state() -> dict:
        from . import control
        return {"models": control.away_models(), "desk_models": control.desk_models(), "asks": control.list_asks(), "answers": control.list_answers(),
                "chains": control.list_chains()}

    @app.get("/api/answer")
    def answer(file: str = "") -> dict:
        from . import control
        try:
            return {"ok": True, "text": control.read_answer(file)}
        except control.ControlError as e:
            return {"ok": False, "why": str(e)}

    @app.get("/api/chain-result")
    def chain_result(name: str = "") -> dict:
        from . import control
        try:
            return {"ok": True, "text": control.chain_result(name)}
        except control.ControlError as e:
            return {"ok": False, "why": str(e)}

    @app.get("/api/chain-details")
    def chain_details(name: str = "") -> dict:
        from . import control
        try:
            return control.chain_details(name)
        except control.ControlError as e:
            return {"ok": False, "why": str(e)}

    @app.get("/api/overnight")
    def overnight_state() -> dict:
        from .chains import overnight
        return {
            "ok": True,
            "state": overnight.get_overnight_state(),
            "presets": overnight.PRESETS,
            "chains": overnight.list_available_chains(),
        }

    @app.get("/api/vaults")
    def vaults_list() -> dict:
        from .data.collectors import notes
        discovered = notes.discover_vaults()
        names = list(discovered.keys())
        if "OBVLT" in names:
            names.remove("OBVLT")
            names.insert(0, "OBVLT")
        return {"ok": True, "vaults": names, "default": "OBVLT" if "OBVLT" in names else (names[0] if names else "")}

    @app.get("/api/notes")
    def notes_list(vault: str = "OBVLT", limit: int = 30, q: str = "") -> dict:
        from .data.collectors import notes
        items = notes.list_notes(vault_name=vault, limit=min(max(1, limit), 100), query=q)
        stale_count = 0
        if vault == "OBVLT":
            try:
                from .chains import file_tools
                stale = file_tools.scan_stale_notes({"older_than_days": 30, "limit": 100})
                stale_count = len(stale)
            except Exception:
                pass
        return {"ok": True, "vault": vault, "count": len(items), "stale_count": stale_count, "notes": items}

    @app.get("/api/note-content")
    def note_content(vault: str = "OBVLT", file: str = "") -> dict:
        from .data.collectors import notes
        return notes.read_note_content(vault_name=vault, rel_path=file)

    @app.post("/api/note-save")
    def note_save(body: dict = Body(default={})) -> dict:
        from .data.collectors import notes
        return notes.save_note_content(
            vault_name=str(body.get("vault") or "OBVLT"),
            rel_path=str(body.get("file") or ""),
            text=str(body.get("text", "")),
        )

    @app.post("/api/note-create")
    def note_create(body: dict = Body(default={})) -> dict:
        from .data.collectors import notes
        return notes.create_note(
            vault_name=str(body.get("vault") or "OBVLT"),
            rel_path=str(body.get("file") or ""),
            initial_text=str(body.get("text", "")),
        )

    @app.get("/api/git-status")
    def git_repo_status(repo: str = "") -> dict:
        import subprocess
        ws = _hub.Path(r"D:\Workspace")
        target_repo = (ws / repo).resolve()
        try:
            target_repo.relative_to(ws.resolve())
        except ValueError:
            return {"ok": False, "why": "Invalid repo path"}
        if not target_repo.exists() or not (target_repo / ".git").exists():
            return {"ok": False, "why": f"Repo '{repo}' not found"}
        try:
            proc = subprocess.run(
                ["git", "status", "--short", "--branch"],
                cwd=str(target_repo),
                capture_output=True,
                text=True,
                timeout=5.0,
            )
            return {"ok": True, "repo": repo, "status": proc.stdout.strip()}
        except Exception as e:
            return {"ok": False, "why": str(e)}

    @app.get("/api/crashes")
    def list_crashes() -> dict:
        from .views.crash_dialog import parse_unacknowledged_crashes
        events = parse_unacknowledged_crashes()
        return {"ok": True, "count": len(events), "events": events}

    @app.get("/api/ports")
    def localhost_ports() -> dict:
        """What listens on this PC and what closed recently (the Localhost card polls this), with the graphics chip
        each one draws on when it uses one (the GPU sampler's pid_chips)."""
        data = dict(self.collectors["ports"].get() or {})
        chips = (self.collectors["gpu"].get() or {}).get("pid_chips") or {}
        data["listening"] = [{**r, "chip": chips.get(r.get("pid"))} for r in data.get("listening") or []]
        return {"ok": True, **data}

    @app.get("/api/health")
    def hud_health() -> dict:
        """The HUD health card (health.py)."""
        return {"ok": True, **self.health_report()}

    @app.get("/api/doctor")
    def run_doctor() -> dict:
        """Instant non-blocking sol-doctor check using the hub's live collectors."""
        from . import doctor
        c = self.collectors
        ls = c["llama_swap"].get() or {}
        ol = c["ollama"].get() if "ollama" in c else {}
        status = {
            "llama_swap": ls,
            "ollama": ol,
            "backend": {} if ls.get("up") else {"known": False},
            "gpu": c["gpu"].get() or {},
            "vram": c["vram"].get() or {},
            "displays": c["displays"].get() or {},
            "speed": c["speed"].get() or {},
            "memory": c["memory"].get() if "memory" in c else {},
            "disks": c["disks"].get() if "disks" in c else [],
            "backups": c["backups"].get() if "backups" in c else {},
            "wsl": c["wsl"].get() or {},
            "stability": c["stability"].get() or {},
        }
        if not status["memory"] or not status["disks"]:
            from .data.collectors import system
            status["memory"] = status["memory"] or system.memory()
            status["disks"] = status["disks"] or system.disks()
            status["backups"] = status["backups"] or system.backups()
        results = doctor.checks(status)
        worst = 2 if any(r[1] == doctor.FAIL for r in results) else 1 if any(r[1] == doctor.WARN for r in results) else 0
        return {
            "ok": True,
            "exit": worst,
            "checks": [{"name": n.strip(), "indent": n.startswith("  "), "level": l, "detail": d} for n, l, d in results],
        }

    @app.get("/api/guard")
    def guard_state() -> dict:
        """Why the local AI is off, and whether a button can answer it (the AI card polls this)."""
        return {"ok": True, **(self.collectors["guard"].get() or {})}

    @app.get("/api/displays")
    def display_adapters() -> dict:
        """Which adapters drive a monitor, and what is rendering on one that doesn't (the GPU card polls this)."""
        return {"ok": True, **(self.collectors["displays"].get() or {})}

    @app.get("/api/ai-usage")
    def ai_usage_list() -> dict:
        """Daily and weekly Claude + Antigravity consumption from AI Usage Tracker."""
        return {"ok": True, **(self.collectors["ai_usage"].get() or {})}

    @app.get("/api/settings")
    def settings_read() -> dict:
        from . import settings_api
        return {"ok": True, **settings_api.read(self)}

    @app.get("/api/projects")
    def projects_list() -> dict:
        """The Projects card: every folder in D:\\Workspace, its state and whether it runs."""
        return {"ok": True, **(self.collectors["projects"].get() or {})}

    @app.get("/api/media")
    def media_list() -> dict:
        """The Media APIs card. Built without sending a single request to a sleeping service."""
        return {"ok": True, **(self.collectors["media"].get() or {})}

    @app.get("/api/media-live")
    def media_live(name: str = "") -> dict:
        """Live data from a media service that's already running (the tile's "Live" button)."""
        from . import launcher
        from .data.collectors import projects
        self.collectors["media_probe"].value = self.collectors["media"].value = None   # its state right now
        tiles = (self.collectors["media"].get() or {}).get("tiles") or []
        try:
            return launcher.live_media(projects.load_registry(), name, tiles)
        except launcher.LaunchError as e:
            return {"ok": False, "why": str(e)}

    @app.get("/api/top-processes")
    def top_processes() -> dict:
        return self.collectors["top_procs"].get()

    @app.get("/api/media-session")
    def get_media_session() -> dict:
        from .data.collectors import media
        info = None
        if hasattr(self, "collectors") and "media" in self.collectors:
            info = getattr(self.collectors["media"], "_media", None)
        if info is None:
            info = media.MediaInfo()
        target = media.get_media_target()
        menu = media.media_keys_menu(info, target)
        return {
            "ok": True,
            "status": info.status,
            "title": info.title,
            "artist": info.artist,
            "app": info.app,
            "clean_app": info.clean_app,
            "playing": info.playing,
            "keys": info.keys,
            "target": target,
            "last_key": info.last_key,
            "sessions": list(info.sessions),
            "menu": [{"label": m[0], "target": m[1]} for m in menu],
        }

    @app.get("/api/search")
    def search_index(q: str = "", project: str = "", k: int = 8, mode: str = "") -> dict:
        from .chains import index_tools
        query = q.strip()
        if not query:
            return {"ok": True, "query": "", "hits": []}
        try:
            force_kw = mode == "keyword"
            hits = index_tools.search(query, project=project.strip() or None, k=k, force_keyword=force_kw)
            return {"ok": True, "query": query, "hits": hits, "count": len(hits)}
        except Exception as e:
            return {"ok": False, "why": str(e), "hits": []}

    @app.get("/api/fix-queue")
    def get_fix_queue() -> dict:
        from .chains import fix_tools
        return fix_tools.queue_status()

    @app.post("/api/fix-queue")
    def modify_fix_queue(body: dict = Body(default={})) -> dict:
        from .chains import fix_tools
        action = str(body.get("action") or "add")
        repo = str(body.get("repo") or "").strip()
        task = str(body.get("task") or "").strip()
        if action == "add":
            try:
                ok = fix_tools.add_queue_task(repo, task)
                return {"ok": ok, "why": "added task" if ok else "failed to add task"}
            except fix_tools.Rejected as e:
                return {"ok": False, "why": str(e)}
        if action == "reset":
            try:
                ok = fix_tools.reset_queue_task(repo, task)
                return {"ok": ok, "why": "reset task" if ok else "failed to reset task"}
            except fix_tools.Rejected as e:
                return {"ok": False, "why": str(e)}
        return {"ok": False, "why": f"unknown action: {action}"}

    @app.post("/api/playground")
    def playground_run(body: dict = Body(default={})) -> dict:
        import urllib.request
        b = body or {}
        model = str(b.get("model", "sol-fast")).strip()
        prompt = str(b.get("prompt", "")).strip()
        system = str(b.get("system", "")).strip()
        use_context = bool(b.get("use_context", False))
        if not prompt:
            return {"ok": False, "why": "Prompt cannot be empty"}

        context_str = ""
        citations = []
        context_note = ""
        context_skipped = False
        if use_context and _hub._is_cloud_model(model):
            # Private local notes must never leave the PC: no search, no context for cloud models.
            context_skipped = True
            context_note = "Local context isn't sent to cloud models."
        elif use_context:
            import sqlite3
            from .chains import index_tools
            try:
                hits = index_tools.search(prompt, k=3)
                if hits:
                    context_parts = []
                    for h in hits:
                        citations.append({"path": h["path"], "lines": f"{h['start']}-{h['end']}", "snippet": h["snippet"]})
                        context_parts.append(f"File: {h['path']} (lines {h['start']}-{h['end']})\nSnippet: {h['snippet']}")
                    context_str = "\n\nRelevant Local Context:\n" + "\n---\n".join(context_parts)
            except (index_tools.IndexError_, OSError, sqlite3.Error):
                _hub._swallowed("hub.playground_run")
                context_note = "Local index wasn't available; answered without context."

        augmented_prompt = prompt + (f"\n\n[Context from local codebase/notes]:\n{context_str}" if context_str else "")

        t0 = _hub.time.monotonic()
        if _hub._is_cloud_model(model):
            from . import gemini_solver
            key = gemini_solver.get_api_key()
            if not key:
                return {"ok": False, "why": "no Gemini API key set"}
            try:
                text, used = gemini_solver.chat_text_model(augmented_prompt, system=system, model=model, api_key=key)
                dt = _hub.time.monotonic() - t0
                return {
                    "ok": True,
                    "model": used,                     # a fallback when the asked-for one was overloaded
                    "text": text,
                    "elapsed_s": round(dt, 2),
                    "tok_s": round(len(text) / 4.0 / max(dt, 0.001), 1),
                    "thought": "",
                    "citations": citations,
                    "context_skipped": context_skipped,
                    "context_note": context_note,
                }
            except gemini_solver.GeminiError as e:
                return {"ok": False, "why": f"Gemini error: {gemini_solver._scrub(str(e), key)}"}
        else:
            # Local llama router
            messages = []
            if system:
                messages.append({"role": "system", "content": system})
            messages.append({"role": "user", "content": augmented_prompt})

            payload = {
                "model": model,
                "messages": messages,
                "temperature": float(b.get("temperature", 0.7)),
                "max_tokens": int(b.get("max_tokens", 2048)),
            }
            req = urllib.request.Request(
                "http://127.0.0.1:11440/v1/chat/completions",
                data=_hub.json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=60.0) as resp:
                    data = _hub.json.loads(resp.read().decode("utf-8"))
                dt = _hub.time.monotonic() - t0
                choice = (data.get("choices") or [{}])[0]
                msg = choice.get("message") or {}
                usage = data.get("usage") or {}
                out_tokens = usage.get("completion_tokens") or (len(msg.get("content", "")) // 4)
                return {
                    "ok": True,
                    "model": model,
                    "text": msg.get("content", ""),
                    "thought": msg.get("reasoning_content", ""),
                    "elapsed_s": round(dt, 2),
                    "tok_s": round(out_tokens / max(dt, 0.001), 1),
                    "usage": usage,
                    "citations": citations,
                    "context_skipped": context_skipped,
                    "context_note": context_note,
                }
            except Exception as e:
                return {"ok": False, "why": f"Router error: {e}"}

    @app.get("/api/snapshot")
    def snapshot() -> dict:
        return _hub.snapshot_payload(self.collector, self.views())

    @app.get("/api/views")
    def get_views() -> dict:
        return self.views()

    @app.post("/api/views")
    def set_views(body: dict = Body(default={})) -> dict:
        if "ticker" in body:
            (self.show_ticker if body["ticker"] else self.hide_ticker)()
        if body.get("dashboard"):
            self.open_dashboard()
        if "dashboard_at_start" in body:
            self.cmds.put(("dashboard_at_start", bool(body["dashboard_at_start"])))
        if body.get("exit"):
            self.exit("dashboard")
        return {**self.views(), "queued": True}

    return app
