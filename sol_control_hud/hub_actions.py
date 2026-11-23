"""The dashboard's button actions (POST /api/action, /api/review): moved out of hub.py on 2026-11-22.

Each function takes the Hub as `self`, unchanged from when it was a method. Names from hub.py are read through
`_hub.<name>` at call time, so tests that patch hub.Path, hub.log, hub.SETTINGS_FILE ... still reach this code."""
from __future__ import annotations

from . import hub as _hub


def do_action(self, action: str, target: str = "", body: dict | None = None) -> dict:
    """The dashboard's buttons. Machine changes go through the same safe paths as the ticker and the Away panel."""
    import subprocess
    llm = _hub.Path(r"D:\AI\Cache\llm")
    if action == "restart":
        from .paths import ROOT
        _hub.log("restart requested from dashboard: starting detached restart")
        py = ROOT / ".venv" / "Scripts" / "python.exe"
        cmd = f'"{py}" -m sol_control_hud --restart'
        ps = (f"$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create -Arguments @{{CommandLine='{cmd}';"
              f" CurrentDirectory='{ROOT}'}}; exit $r.ReturnValue")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
                         creationflags=0x08000000)
        return {"ok": True, "why": "Restarting HUD… reconnecting shortly"}
    if action == "stop_ai":
        state = _hub.json.loads((llm / "state.json").read_text(encoding="utf-8")) if (llm / "state.json").exists() else {}
        if state.get("mode") != "away":
            return {"ok": False, "why": "Away isn't running"}
        (llm / "away-stop.flag").write_text(_hub.time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        return {"ok": True, "why": "stopping: the job goes back in the queue"}
    if action == "abort_inference":
        flag = llm / "abort_inference.flag"
        try:
            flag.write_text(_hub.time.strftime("%Y-%m-%dT%H:%M:%S"), encoding="utf-8")
        except OSError as e:
            return {"ok": False, "why": f"Could not write abort flag: {e}"}
        from .data.collectors.ai_ops import read_live_thought, write_live_thought
        cur = read_live_thought()
        cur["active"] = False
        cur["phase"] = "aborted"
        write_live_thought(cur)
        _hub.log("abort inference requested from dashboard")
        return {"ok": True, "why": "Inference abort signal sent"}
    if action in ("away", "away-sleep"):
        script = _hub.Path(r"D:\OBVLT\tools\sol-llm.ps1")
        subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), action],
                         creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True, "why": f"starting {action}"}
    if action == "wake-alchemy":
        script = _hub.Path(r"D:\Workspace\youtube-alchemy\serve-install.mjs")
        if script.exists():
            subprocess.Popen(["bun", str(script)], cwd=str(script.parent),
                             creationflags=0x08000000, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            _hub.log("wake-alchemy: started serve-install.mjs on :8788")
            return {"ok": True, "why": "started YouTube Alchemy service"}
        return {"ok": False, "why": "serve-install.mjs not found"}
    if action == "open" and target in self.OPEN_TARGETS:
        self.cmds.put(("open", target))       # the ticker's own targets (folders, the daily note, reports)
        return {"ok": True}
    if action == "theme" and target:
        self.cmds.put(("theme", target))
        return {"ok": True}
    if action in ("model", "models_unload_all", "ai_power"):
        from . import models_ctl
        try:
            router = self.collectors["llama_swap"].get() if hasattr(self, "collectors") else {}
            running = (router or {}).get("running") or []
            if action == "model":
                why = models_ctl.model_op(target, str((body or {}).get("op", "")), [m.get("model") for m in running])
            elif action == "models_unload_all":
                why = models_ctl.unload_all([m.get("model") for m in running if m.get("state") in ("loaded", "sleeping", "loading")])
            else:
                why = models_ctl.ai_power(target == "on")
            if hasattr(self, "collectors"):
                self.collectors["llama_swap"].value = None     # ask the router again on the next update
            _hub.log(f"models: {action} {target} {(body or {}).get('op', '')}".strip())
            return {"ok": True, "why": why}
        except models_ctl.ModelError as e:
            return {"ok": False, "why": str(e)}
    if action in ("pihole_pause", "pihole_resume"):
        from .data.collectors import network
        res = network.pihole_pause(300) if action == "pihole_pause" else network.pihole_resume()
        _hub.log(f"network: {action} -> {res.get('why')}")
        return res
    if action in ("tailscale_open", "tailscale_close", "tailscale_up"):
        from .data.collectors import network
        if action == "tailscale_open":
            res = network.tailscale_open()
        elif action == "tailscale_up":
            res = network.tailscale_up(hours=float(body.get("hours") or 12.0))
        else:
            res = network.tailscale_close()
        _hub.log(f"network: {action} -> {res.get('why')}")
        return res
    if action in ("security_toggle", "security_test", "security_ack", "deck_start", "deck_stop", "deck_restart"):
        from .data.collectors import security
        if action == "security_toggle":
            res = security.toggle_security_feature(target or "all")
        elif action == "security_test":
            res = security.trigger_test_alarm()
        elif action == "security_ack":
            res = security.acknowledge_alerts()
        elif action == "deck_start":
            res = security.start_deck()
        elif action == "deck_stop":
            res = security.stop_deck()
        elif action == "deck_restart":
            res = security.restart_deck()
        if hasattr(self, "collectors") and "security" in self.collectors:
            self.collectors["security"].at = 0
            self.collectors["security"].value = None
        _hub.log(f"security: {action} target={target} -> {res.get('why')}")
        return res
    if action in ("pad_start", "pad_stop", "pad_restart", "pad_rdp_popos", "pad_popos_wake", "pad_popos_sleep", "pad_popos_stop_servers"):
        from .data.collectors import sol_pad
        if action == "pad_start":
            res = sol_pad.start_pad()
        elif action == "pad_stop":
            res = sol_pad.stop_pad()
        elif action == "pad_rdp_popos":
            res = sol_pad.open_popos_rdp()
        elif action == "pad_popos_wake":
            res = sol_pad.wake_popos()
        elif action == "pad_popos_sleep":
            res = sol_pad.sleep_popos()
        elif action == "pad_popos_stop_servers":
            res = sol_pad.stop_popos_servers()
        else:
            res = sol_pad.restart_pad()
        if hasattr(self, "collectors") and "sol_pad" in self.collectors:
            self.collectors["sol_pad"].at = 0
            self.collectors["sol_pad"].value = None
        _hub.log(f"sol_pad: {action} -> {res.get('why')}")
        return res
    if action in ("ask", "ask_remove", "chain"):
        from . import control
        b = body or {}
        try:
            if action == "ask":
                files = b.get("files") or []
                if b.get("now"):
                    r = control.ask_now(str(b.get("title", "")), str(b.get("question", "")),
                                        [str(x) for x in files] if isinstance(files, list) else [],
                                        str(b.get("model") or "sol-smart"))
                    _hub.log(f"asked now: {r['job']}")
                    return {"ok": True, "why": f"answering now on {b.get('model') or 'sol-smart'} "
                                               "(it shows in Answers when done)", **r}
                r = control.queue_ask(str(b.get("title", "")), str(b.get("question", "")),
                                      [str(x) for x in files] if isinstance(files, list) else [], str(b.get("model") or "sol-away"))
                _hub.log(f"asked overnight: {r['job']}")
                return {"ok": True, "why": f"queued: {r['job'][4:]} (it runs in the next Away session)", **r}
            if action == "ask_remove":
                control.remove_ask(target)
                return {"ok": True, "why": "removed from the queue"}
            if str(b.get("op", "")) == "schedule":
                canon = control.set_schedule(target, str(b.get("schedule") or ""))
                _hub.log(f"chain {target}: schedule {canon or 'removed'} (dashboard)")
                return {"ok": True, "why": f"{target}: " + (f"runs {canon}" if canon else "no schedule"), "schedule": canon}
            status = control.chain_op(target, str(b.get("op", "")))
            _hub.log(f"chain {target}: status {status} (dashboard)")
            if b.get("op") == "swap_run":
                why = f"{target}: swapping model and running now"
            elif b.get("op") == "now":
                why = f"{target}: running now, skipping the wait"
            else:
                why = f"{target}: {status}"
            return {"ok": True, "why": why, "status": status}
        except control.ControlError as e:
            return {"ok": False, "why": str(e)}
    if action == "notify" and target in ("on", "off", "test"):
        if target == "test":
            return {"ok": self.notify_now("SOL Control HUD", "Notifications work. You'll see Away results, chain failures, "
                                          "crashes, disks filling up and VRAM spills here."), "why": "test notification sent"}
        if not isinstance(self.settings.get("notify"), dict):
            from . import notify
            self.settings["notify"] = _hub.json.loads(_hub.json.dumps(notify.DEFAULTS))
        self.settings["notify"]["enabled"] = target == "on"
        if self.notifier:
            self.notifier.settings = self.settings["notify"]
        _hub.save_settings(self.settings)
        return {"ok": True, "why": f"notifications {target}", "notify": target == "on"}
    if action == "login_start" and target in ("on", "off"):
        self._login = _hub.start_at_login(target == "on")
        _hub.log(f"start at login: {'on' if self._login else 'off'}")
        return {"ok": True, "why": f"start at login: {'on' if self._login else 'off'}", "start_at_login": self._login}
    if action in ("ai_inbox_open", "ai_inbox_tick"):
        from .data.collectors import ai_inbox
        b = body or {}
        if action == "ai_inbox_open":
            return ai_inbox.open_note(target)
        line = b.get("line")
        r = ai_inbox.tick_task(str(b.get("text") or ""), line if isinstance(line, int) else -1)
        if r.get("ok") and "ai_inbox" in self.collectors:
            self.collectors["ai_inbox"].at = 0.0           # next payload re-reads the file
        _hub.log(f"ai inbox tick: {r.get('why')}")
        return r
    if action == "open_note":
        from .data.collectors import notes
        b = body or {}
        vault = str(b.get("vault") or "OBVLT")
        file_path = str(b.get("file") or target)
        r = notes.open_note(vault, file_path)
        _hub.log(f"open note: {vault}/{file_path} -> {r.get('ok')}")
        return r
    if action == "scratch":
        from .views.scratch_dialog import append_scratch_note
        b = body or {}
        text = str(b.get("text", "")).strip()
        dest = str(b.get("destination", "daily"))
        r = append_scratch_note(text, destination=dest)
        _hub.log(f"scratch note ({dest}): {r.get('success')} -> {r.get('message')}")
        return {"ok": bool(r.get("success")), "why": r.get("message", "")}
    if action == "open_folder":
        from .data.collectors import notes
        b = body or {}
        vault = str(b.get("vault") or "OBVLT")
        file_path = str(b.get("file") or target)
        r = notes.open_folder(vault, file_path)
        _hub.log(f"open folder: {vault}/{file_path} -> {r.get('ok')}")
        return r
    if action == "digest_now":                # Settings: the machine's week so far, as the Sunday note would say it
        from datetime import datetime
        from . import digest
        snap = self.collector.get_snapshot()
        now = datetime.now()
        try:
            path = digest.write(digest.build(_hub.DATA_DIR / "metrics.sqlite", now, snap.disks), now)
        except OSError as e:
            return {"ok": False, "why": f"could not write it: {e}"}
        _hub.log(f"digest written now: {path}")
        return {"ok": True, "why": f"written: Digests/{path.name} (Sunday's replaces it)", "path": str(path)}
    if action == "ack_crashes":
        from .views.crash_dialog import ACK_FILE
        from .views.ticker import write_crash_ack
        write_crash_ack(ACK_FILE)
        _hub.log("crashes acknowledged via web dashboard")
        return {"ok": True, "why": "Crash events acknowledged and cleared"}
    if action == "setting":                   # the dashboard's Settings panel (settings_api.py checks every value)
        from . import settings_api
        try:
            r = settings_api.change(self, target, (body or {}).get("value"))
        except settings_api.SettingError as e:
            return {"ok": False, "why": str(e)}
        _hub.log(f"setting {target} = {(body or {}).get('value')!r}: {r.get('why')}")
        return r
    if action in ("project", "media", "solang"):
        return self._launch(action, target, str((body or {}).get("op", "")), body or {})
    if action == "project_bughunt":
        from .chains import project_chains
        r = project_chains.queue_bug_hunt(target)
        _hub.log(f"project bughunt {target}: {r.get('why')}")
        return r
    if action == "open_project_review":
        from .chains import project_chains
        rev = project_chains.find_latest_review(target)
        if not rev:
            return {"ok": False, "why": f"No review note found yet for {target}"}
        from .data.collectors import notes
        return notes.open_note("OBVLT", rev["path"])
    if action == "toggle_review_task":
        from .chains import project_chains
        rev = project_chains.find_latest_review(target)
        if not rev:
            return {"ok": False, "why": f"No review note found yet for {target}"}
        idx = int((body or {}).get("index", 0))
        resolved = bool((body or {}).get("resolved", True))
        r = project_chains.toggle_review_task(_hub.Path(rev["full_path"]), idx, resolved)
        _hub.log(f"toggle review task {target} [{idx}] -> {resolved}: {r.get('why')}")
        if hasattr(self, "collectors") and "projects" in self.collectors:
            self.collectors["projects"].value = None
        return r

    if action == "git_post_commit" and target:
        now = _hub.time.time()
        if not hasattr(self, "_last_git_post_commit"):
            self._last_git_post_commit = {}
        last = self._last_git_post_commit.get(target, 0)
        if now - last < 600:  # 10 min cooldown
            return {"ok": True, "why": f"Cooldown active for {target} ({int(600 - (now - last))}s remaining)"}
        self._last_git_post_commit[target] = now
        from .chains import project_chains
        r = project_chains.queue_bug_hunt(target)
        _hub.log(f"git post-commit: {r.get('why')} for {target}")
        return r

    if action == "toggle_project_hook" and target:
        from .chains import project_chains
        wdir = project_chains.WORKSPACE_DIR
        repo_path = wdir / target
        enable = bool((body or {}).get("enable", True))
        if enable:
            r = project_chains.install_project_hook(repo_path)
        else:
            r = project_chains.uninstall_project_hook(repo_path)
        _hub.log(f"toggle project hook {target} -> {enable}: {r.get('why')}")
        if hasattr(self, "collectors") and "projects" in self.collectors:
            self.collectors["projects"].value = None
        return r

    if action == "start_overnight":
        from .chains import overnight
        chains = (body or {}).get("chains", [])
        disp_mode = str((body or {}).get("display_mode", "dpms_off"))
        sleep_done = bool((body or {}).get("sleep_when_done", False))
        r = overnight.start_overnight(chains, display_mode=disp_mode, sleep_when_done=sleep_done)
        _hub.log(f"overnight start: {r.get('why')}")
        return r

    if action == "cancel_overnight":
        from .chains import overnight
        r = overnight.cancel_overnight()
        _hub.log(f"overnight cancel: {r.get('why')}")
        return r

    if action == "set_overnight_bedtime":
        from .chains import overnight
        b = body or {}
        r = overnight.set_bedtime_config(
            enabled=b.get("enabled", False),
            chains=b.get("chains"),
            display_mode=b.get("display_mode"),
            sleep_when_done=b.get("sleep_when_done"),
        )
        _hub.log(f"overnight bedtime config: {r.get('why')}")
        return r

    if action in ("monitor_off", "monitor_off_event"):
        from .chains import overnight
        r = overnight.trigger_bedtime_run()
        _hub.log(f"overnight monitor_off trigger: {r.get('why')}")
        return r

    if action in ("guard_ignore", "guard_resume"):
        # the guard turned the AI off for something that is not a game: say so once, instead of editing the
        # config by hand (09-25 Notepad and Edge, 09-27 claude x3, 10-07/08 steamwebhelper x5)
        from . import game_guard
        if action == "guard_resume":
            r = game_guard.resume()
        else:
            g = self.collectors["guard"].get() if hasattr(self, "collectors") else {}
            if not (g or {}).get("can_ignore") or (target and target != (g or {}).get("name")):
                return {"ok": False, "why": "that is not what turned the AI off"}
            r = game_guard.ignore((g or {}).get("name"))
        _hub.log(f"{action} {target}: {r.get('why')}")
        if hasattr(self, "collectors") and "guard" in self.collectors:
            self.collectors["guard"].value = None
        return r
    if action == "close_port":
        # ports.close_listener looks the port up itself and refuses the AI stack, Windows services and us
        from .data.collectors import ports
        b = body or {}
        r = ports.close_listener(target or b.get("port"), b.get("pid"))
        _hub.log(f"close port {target or b.get('port')}: {r.get('why')}")
        if hasattr(self, "collectors") and "ports" in self.collectors:
            self.collectors["ports"].value = None      # the card lists what's left on its next poll
        return r
    if action == "media_target":
        from .data.collectors import media
        new_target = str(target or (body or {}).get("target", "")).strip()
        if not new_target:
            return {"ok": False, "why": "missing target"}
        media.set_media_target(new_target)
        _hub.log(f"media target -> {new_target}")
        return {"ok": True, "target": new_target}
    if action == "media_transport":
        b = body or {}
        op = str(target or b.get("op", "")).lower()
        vk_map = {
            "play": 0xB3,
            "pause": 0xB3,
            "play_pause": 0xB3,
            "next": 0xB0,
            "prev": 0xB1,
            "stop": 0xB2,
        }
        vk = vk_map.get(op)
        if not vk:
            return {"ok": False, "why": f"unknown transport op {op!r}"}
        try:
            _hub.ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
            _hub.ctypes.windll.user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP = 2
            _hub.log(f"media transport -> {op} (VK 0x{vk:02X})")
            return {"ok": True, "op": op}
        except Exception as e:
            return {"ok": False, "why": str(e)}
    if action == "kill_process":
        import psutil
        b = body or {}
        try:
            pid = int(target or b.get("pid", 0))
        except (ValueError, TypeError):
            return {"ok": False, "why": "invalid PID"}
        if pid <= 4 or pid == _hub.os.getpid():
            return {"ok": False, "why": "cannot kill system or HUD process"}
        try:
            p = psutil.Process(pid)
            pname = p.name().lower()
            protected = {
                "system", "idle", "registry", "smss.exe", "csrss.exe", "wininit.exe",
                "services.exe", "lsass.exe", "svchost.exe", "winlogon.exe", "explorer.exe",
                "sol-watch.exe", "sol-net.exe", "llama-server.exe", "pythonw.exe",
            }
            if pname in protected:
                return {"ok": False, "why": f"refusing to terminate protected process {pname}"}
            p.terminate()
            _hub.log(f"terminated process PID {pid} ({pname}) via Process Radar")
            return {"ok": True, "pid": pid, "name": pname}
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            return {"ok": False, "why": str(e)}
    if action == "svc_control":
        b = body or {}
        svc = str(target or b.get("service", "")).lower()
        cmd = str(b.get("cmd", "restart")).lower()
        if svc in ("router", "llama_router", "llama"):
            from .data.collectors import engines
            _hub.log(f"service control -> {svc} ({cmd})")
            try:
                engines.llama_swap()  # queries / triggers health check
                return {"ok": True, "service": svc, "status": "active"}
            except Exception as e:
                return {"ok": False, "why": str(e)}
        elif svc in ("watcher", "sol_watch"):
            import subprocess
            subprocess.Popen(["powershell", "-NoProfile", "-Command", "Start-Process sol-watch.exe -WindowStyle Hidden"],
                             shell=False, creationflags=0x08000000 if _hub.os.name == "nt" else 0)
            _hub.log("service control -> started sol-watch.exe")
            return {"ok": True, "service": svc, "why": "started sol-watch.exe"}
        elif svc in ("chains", "chains_runner"):
            from . import control
            try:
                c_list = control.list_chains()
                return {"ok": True, "service": svc, "chains_count": len(c_list)}
            except Exception as e:
                return {"ok": False, "why": str(e)}
        return {"ok": False, "why": f"unknown service {svc!r}"}
    if action == "review":                    # the Exam review card (exam_review.py)
        return self.review_action(target, body or {})
    return {"ok": False, "why": f"unknown action {action!r}"}


def review_action(self, target: str, body: dict | None = None) -> dict:
    from . import exam_review
    w = exam_review.watcher()
    if target == "pick":                      # the box is drawn on the Tk thread
        self.cmds.put(("review_pick", None))
        return {"ok": True, "why": "drag a box around the question (Esc cancels)"}
    if target == "open_note":
        note = w.state().get("note")
        if not note or not _hub.Path(note).exists():
            return {"ok": False, "why": "no review note yet: it's made with the first missed question"}
        _hub.os.startfile(note)
        return {"ok": True, "why": "opening today's review note"}
    if target in ("retry_gemini", "bad", "verify"):
        r = w.retry_gemini()
        if r.get("ok") and r.get("item"):
            self.cmds.put(("review_update_hud", r["item"]))
        _hub.log(f"exam review: retry_gemini -> {r.get('why')}")
        return r
    if target == "set_key":
        b = body or {}
        key = str(b.get("key", "")).strip()
        if not key:
            return {"ok": False, "why": "API key cannot be empty"}
        s = exam_review.load_settings()
        s["gemini_api_key"] = key
        exam_review.save_settings(s)
        _hub.log("exam review: saved Gemini API key to exam-review.json")
        return {"ok": True, "why": "Gemini API key saved"}
    if target in ("add_snip", "snip", "snip_same"):
        r = w.add_snip()
        _hub.log(f"exam review: add_snip -> {r.get('why')}")
        if r.get("ok"):
            self.cmds.put(("review_snip_added", r.get("count", 2)))
            self.notify_now("Exam Snip Added", f"Captured scroll part ({r.get('count', 2)} parts stitched)")
        return r
    if target in ("pick_snip", "pick_part2"):
        self.cmds.put(("review_pick_snip", None))
        return {"ok": True, "why": "drag a box around the scrolled section (Esc cancels)"}
    if target in ("auto_scroll", "scroll_solve"):
        r = w.auto_scroll_and_solve()
        _hub.log(f"exam review: auto_scroll_and_solve -> {r.get('why')}")
        if r.get("ok"):
            self.cmds.put(("review_snip_added", r.get("parts", 2)))
            self.notify_now("Exam Auto-Scroll", "Captured, stitched, and solved scrolled question")
        return r
    if target in ("brave_search", "brave"):
        cur = w.state().get("current")
        if not cur:
            return {"ok": False, "why": "No current question to verify"}
        text = exam_review.format_verification_text(cur)
        query = exam_review.build_brave_ai_query(cur)
        import urllib.parse
        url = f"https://search.brave.com/search?q={urllib.parse.quote_plus(query)}"
        _hub.webbrowser.open_new_tab(url)
        _hub.log(f"exam review: brave_search -> opened {url[:60]}...")
        return {"ok": True, "why": "Opened Brave AI Search", "query": query, "text": text}
    if target in ("hud", "popup", "toggle_hud"):
        b = body or {}
        enabled = b.get("enabled") if "enabled" in b else not w.state().get("hud_enabled", False)
        w.set_hud_enabled(enabled)
        self.cmds.put(("review_set_hud", enabled))
        _hub.log(f"exam review: set hud_enabled={enabled}")
        return {"ok": True, "why": f"floating exam HUD {'enabled' if enabled else 'disabled'}", "hud_enabled": enabled}
    ops = {"start": w.start, "stop": w.stop, "look": w.look_now, "clear": w.clear}
    if target not in ops:
        return {"ok": False, "why": f"unknown review action {target!r}"}
    r = ops[target]()
    _hub.log(f"exam review: {target} -> {r.get('why')}")
    return r
