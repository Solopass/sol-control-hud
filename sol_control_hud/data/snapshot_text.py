"""A Snapshot in words and colors: the ticker's slides and rows, the attention list, the color rules.
Split out of snapshot.py on 2026-10-08 (moved verbatim); snapshot.py re-exports every name."""
from __future__ import annotations

import re
import time
from .snapshot_model import (
    AMBER, CYAN, DISK_TREND_GB, GIT_OLD_DAYS, GREEN, HOTSPOT_ALERT_C, MUTED, RED, SEP, SERVICE_ORDER, SPILL_WORD,
    Snapshot, TEMP_RISE_C, TEXT, VRAM_RISE_GB, joined, plain,
)


def disk_color(d: dict, trend: float | None) -> str:
    pct, free = d.get("percent", 0) or 0, d.get("free_gb", 0) or 0
    if pct >= 92 or free < 20:
        return RED
    if trend is not None and trend <= -DISK_TREND_GB:
        return RED                         # filling up: more space used in the last hour
    if trend is not None and trend >= DISK_TREND_GB:
        return GREEN                       # space freed in the last hour
    return AMBER if pct >= 85 else TEXT


def chain_result_color(text: str | None) -> str:
    t = (text or "").lower()
    if "(failed)" in t:
        return RED
    if "(cancelled)" in t or "(waiting)" in t:
        return AMBER
    return GREEN if any(k in t for k in ("(finished)", "(drafted)", "(completed)")) else MUTED


def attention(s: "Snapshot") -> list[tuple[str, str, str]]:
    """What needs you, worst first: (color, text, slide tag to open)."""
    out: list[tuple[str, str, str]] = []
    crashes = s.unexpected_reboots + s.gpu_resets
    if crashes:
        out.append((RED, f"{crashes} new crash event{'s' if crashes != 1 else ''}: Away blocked until reviewed", "SYS"))
    if s.hud_alert:
        out.append((RED, s.hud_alert, "SYS"))
    if s.ai_state == "offline" and s.ai_mode != "off":
        out.append((RED, "local AI router is down", "AI"))
    if s.vram_spill_impact == "slow":
        out.append((RED, f"model spilled out of VRAM: answers slow ({s.ai_tps:.0f} tok/s)" if s.ai_tps
                    else "model spilled out of VRAM: answers slow", "HW"))
    elif s.vram_spill_impact == "unknown":
        out.append((AMBER, f"{s.vram_spilled_gb:.1f} GB of the model in system RAM: may be slow", "HW"))
    if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
        out.append((RED, f"GPU hotspot {s.gpu_hotspot}°C", "HW"))
    for d in s.disks:
        drive, free, pct = d.get("drive"), d.get("free_gb", 0) or 0, d.get("percent", 0) or 0
        if pct >= 92 or free < 20:
            out.append((RED, f"{drive}: only {free:.0f} GB free", "DISK"))
        elif (s.disk_trends.get(drive) or 0) <= -5:
            out.append((AMBER, f"{drive}: {-s.disk_trends[drive]:.0f} GB used up in the last hour", "DISK"))
    if s.ram_percent >= 92:
        out.append((RED, f"RAM {s.ram_percent:.0f}% used", "HW"))
    if "(failed)" in (s.chain_last_finished or ""):
        out.append((AMBER, f"chain failed: {s.chain_last_finished.removesuffix(' (failed)')}", "RUN"))
    if s.backup_stale:
        out.append((AMBER, "WSL backup is stale", "SYS"))
    if s.ai_tps_slow and s.vram_card_full:
        out.append((AMBER, f"AI slowed ({s.ai_tps:.0f} tok/s): card {s.vram_used_gb:.1f} GB" + (f" · {s.vram_hog}" if s.vram_hog else ""), "AI"))
    elif s.vram_card_full and s.ai_mode == "desk" and s.ai_state in ("loaded", "sleeping"):
        out.append((AMBER, f"card nearly full ({s.vram_used_gb:.1f} GB): the AI may slow" + (f" · {s.vram_hog}" if s.vram_hog else ""), "AI"))
    if s.vram_tight and not s.vram_evicted and s.ai_mode != "away":
        out.append((AMBER, "VRAM tight: the model may spill", "HW"))
    return out


def format_rate(kb_s: float) -> str:
    """Formats KB/s into human-readable rate string (KB/s or MB/s)."""
    if kb_s >= 1024.0:
        return f"{kb_s / 1024.0:.1f} MB/s"
    return f"{kb_s:.0f} KB/s"


def format_slides(s: Snapshot) -> list[dict]:
    """Creates rotating slides for single-line ticker view.
    Each item has: category, text, color, badge_color.
    """
    slides = []

    # Slide 0: Hardware / Resources
    gpu_txt = f"{round(s.gpu_load)}%" if s.gpu_load is not None else "--"
    vram_used = f"{s.vram_used_gb:.1f}" if s.vram_used_gb is not None else "--"
    vram_tot = f"{s.vram_total_gb:.0f}" if s.vram_total_gb is not None else "16"
    ram_used = f"{s.ram_used_gb:.1f}G" if s.ram_used_gb else "--"

    hw_color = "#38bdf8"  # Cyan default
    reset_badge = ""
    vram_str = f"VRAM {vram_used}/{vram_tot}G"
    if s.unexpected_reboots > 0 or s.gpu_resets > 0:
        reset_badge = f"  ·  [CRASH x{s.unexpected_reboots + s.gpu_resets}!]"
        hw_color = "#f87171"
    if s.vram_spill_impact == "slow":
        hw_color = "#f87171"  # Red alert: spilled and an answer since was slow
        vram_str = f"VRAM {vram_used}/{vram_tot}G (EVICTED!)"
    elif s.vram_evicted:
        hw_color = "#f87171" if reset_badge else "#fbbf24"
        vram_str = f"VRAM {vram_used}/{vram_tot}G ({SPILL_WORD[s.vram_spill_impact]})"
    elif s.vram_tight:
        hw_color = "#f87171" if reset_badge else "#fbbf24"  # Amber warning
        vram_str = f"VRAM {vram_used}/{vram_tot}G (TIGHT)"
    elif s.vram_top_process:
        vram_str = f"VRAM {vram_used}/{vram_tot}G ({s.vram_top_process})"

    # Telemetry additions: GPU temperature & fan RPM
    gpu_telemetry = []
    if s.gpu_temp is not None:
        if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
            gpu_telemetry.append(f"{s.gpu_temp}°C (🔥{s.gpu_hotspot}°C!)")
            hw_color = "#f87171"  # Alert pulse on high hotspot
        else:
            gpu_telemetry.append(f"{s.gpu_temp}°C")
    if s.gpu_fan_rpm and s.gpu_fan_rpm > 0:
        gpu_telemetry.append(f"{s.gpu_fan_rpm}rpm")

    gpu_display = f"GPU {gpu_txt} ({' · '.join(gpu_telemetry)})" if gpu_telemetry else f"GPU {gpu_txt}"

    hw_detail = f"CPU {s.cpu_percent:.0f}% · {s.gpu_name or 'GPU'}"
    if s.gpu_temp is not None:
        hotspot_str = f", Hotspot {s.gpu_hotspot}°C" if s.gpu_hotspot else ""
        mem_str = f", Mem {s.gpu_mem_temp}°C" if s.gpu_mem_temp else ""
        fan_str = f" · Fan {s.gpu_fan_rpm or 0} RPM"
        hw_detail += f" · Temp: Edge {s.gpu_temp}°C{hotspot_str}{mem_str}{fan_str}"
    if s.vram_processes:
        top_str = " · ".join([f"{p['name']} {p['dedicated_gb']}G" for p in s.vram_processes[:3]])
        hw_detail += f" · Top VRAM: {top_str}"
    elif s.vram_top_process:
        hw_detail += f" · Top VRAM: {s.vram_top_process}"

    slides.append({
        "tag": "HW",
        "text": f"{gpu_display}  ·  {vram_str}  ·  RAM {ram_used}{reset_badge}",
        "color": hw_color,
        "detail": hw_detail
    })

    # Slide 0b: Away progress (first while Away works, so it's what you see when you glance at it)
    if s.ai_mode == "away" and s.away_line:
        pct = f"{int(s.away_fraction * 100)} %  ·  " if s.away_fraction is not None and s.away_phase == "working" else ""
        eta = f"  ·  {s.away_eta.split(' · ')[0]}" if s.away_eta else ""
        who = "you're here, AI keeps working" if s.away_present else "Away screen on"
        slides.insert(0, {
            "tag": "AWAY",
            "text": f"{pct}{s.away_line}{eta}",
            "color": "#4ade80" if s.away_phase == "done" else "#38bdf8",
            "detail": f"{s.away_line}\n{s.away_eta or 'no time estimate yet'} · {who} · right-click → Stop AI work",
        })

    # Slide 1: Local AI
    model_str = s.ai_model or "No model loaded"
    state_str = "generating ⚡" if s.ai_generating else s.ai_state
    if s.ai_mode == "away" and s.ai_until:
        try:
            until_part = s.ai_until.split("T")[-1][:5]
            mode_str = f"Away until {until_part}"
        except Exception:
            mode_str = "Away mode"
    elif s.ai_mode == "off" and s.ai_reason:
        clean_reason = s.ai_reason.removeprefix("game: ")
        mode_str = f"Off: {clean_reason}"
    else:
        mode_str = f"{s.ai_mode.capitalize()} mode"

    ai_color = "#94a3b8"  # Slate/muted
    if s.ai_generating:
        ai_color = "#4ade80"  # Bright green
    elif s.ai_state == "loaded":
        ai_color = "#4ade80"  # Green
    elif s.ai_state == "sleeping":
        ai_color = "#38bdf8"  # Cyan
    elif s.ai_state == "offline":
        ai_color = "#f87171"  # Red

    lock_flag = " [LOCK]" if s.gpu_locked else ""
    ctx_info = f" (ctx: {s.ai_ctx_size // 1024}k)" if s.ai_ctx_size else ""
    bolt = " ⚡" if s.ai_generating else ""
    model_part = f"{model_str}{bolt} ({state_str})" if s.ai_model or s.ai_state == "offline" else model_str
    slides.append({
        "tag": "AI",
        "text": f"{model_part}  ·  {mode_str}{lock_flag}  ·  " + (f"{s.ai_tps:.0f} tok/s" if s.ai_tps else ":11440"),
        "color": ai_color,
        "detail": f"Active inference on {model_str}{ctx_info}" if s.ai_generating else (f"Reason: {s.ai_reason}" if s.ai_reason else f"llama.cpp router{ctx_info}")
    })

    # Slide 2: Chains
    if s.chain_running:
        step_part = f" ({s.chain_step})" if s.chain_step else ""
        chain_txt = f"Run: {s.chain_running}{step_part}"
        chain_color = "#38bdf8"
    else:
        # say *why* a queued chain waits ("Code review waits for Away mode"), and when the next scheduled one runs
        blocked = next((p for p in s.chain_pending if p.get("blocked")), None)
        if blocked:
            q_part = f"{blocked['chain']} {blocked['blocked']}"
            if s.chain_pending_count > 1:
                q_part += f" (+{s.chain_pending_count - 1})"
        else:
            q_part = f"{s.chain_pending_count} queued" if s.chain_pending_count else "0 queued"
        last = f"  ·  Last: {s.chain_last_finished}" if s.chain_last_finished else ""
        nxt = f"  ·  next: {s.chain_next}" if s.chain_next else ""
        # something waiting is the news: it goes first (the single line is cut off after ~290 px)
        chain_txt = f"Chains: {q_part}{nxt}{last}" if blocked else f"Chains: idle{last}  ·  {q_part}{nxt}"
        chain_color = "#94a3b8"

    slides.append({
        "tag": "RUN",
        "text": chain_txt,
        "color": chain_color,
        "detail": f"Last finished: {s.chain_last_finished}" if s.chain_last_finished else "sol-hud pipelines daemon"
    })

    # Slide 3: Services
    svc_items = []
    for name in SERVICE_ORDER:
        up = s.services.get(name, False)
        dot = "●" if up else "○"
        svc_items.append(f"{name} {dot}")
    svc_txt = "  |  ".join(svc_items)

    svc_detail = "Router :11440, Embed :11443, HUD :7900, chain runner, WSL (its services start on demand)"
    if s.self_cpu > 0 or s.self_ram_mb > 0:
        svc_detail += f"\nHUD Overhead: {s.self_cpu:.1f}% CPU · {s.self_ram_mb:.0f} MB RAM ({s.self_latency_ms:.1f}ms loop)"

    slides.append({
        "tag": "SVC",
        "text": svc_txt,
        "color": "#38bdf8",
        "detail": svc_detail,
    })

    # Slide 4: Disks (if available)
    if s.disks:
        segs, lines = [], []
        for d in s.disks:
            if "free_gb" not in d:
                continue
            drive, trend = d.get("drive"), s.disk_trends.get(d.get("drive"))
            arrow = f" ▼{-trend:.1f}" if trend is not None and trend <= -DISK_TREND_GB else \
                f" ▲{trend:.1f}" if trend is not None and trend >= DISK_TREND_GB else ""
            segs.append((f"{drive}: {d['free_gb']:.0f}G{arrow}", disk_color(d, trend)))
            lines.append(f"{drive}: {d['free_gb']:.0f} GB free ({d.get('percent', 0):.0f}% used)"
                         + (f", {trend:+.1f} GB in the last hour" if trend is not None else ""))
        if segs:
            segs = [("Free ", MUTED)] + joined(segs)
            slides.append({
                "tag": "DISK",
                "text": plain(segs),
                "segments": segs,
                "color": RED if any(c == RED for _, c in segs) else AMBER if any(c == AMBER for _, c in segs) else CYAN,
                "detail": "\n".join(lines) + "\nRed = space used up in the last hour (▼ GB), green = space freed (▲ GB)",
            })

    # Slide 5: System & Backup
    if s.backup_age_h is not None or s.unexpected_reboots is not None:
        if s.backup_age_h is not None:
            age_str = f"{s.backup_age_h:.1f}h ago" if s.backup_age_h < 48 else f"{s.backup_age_h / 24:.1f}d ago"
        else:
            age_str = "no backup"
        crashes = s.unexpected_reboots + s.gpu_resets
        parts = [f"{s.unexpected_reboots} reboot{'s' if s.unexpected_reboots != 1 else ''}"] if s.unexpected_reboots else []
        if s.gpu_resets:
            parts.append(f"{s.gpu_resets} GPU reset{'s' if s.gpu_resets != 1 else ''}")
        reboot_str = f"{' + '.join(parts)} since last review: Away blocked" if crashes \
            else "no new crashes, Away allowed"
        sys_color = "#f87171" if s.backup_stale or crashes > 0 else "#38bdf8"
        slides.append({
            "tag": "SYS",
            "text": f"WSL Backup: {age_str}  ·  Stability: {reboot_str}",
            "color": sys_color,
            "detail": f"Latest backup: {s.backup_latest or 'none'} · counts unexpected reboots (Event 41) and GPU resets "
                      f"after the last review (reports\\stability-ack.json); new ones block Away until reviewed"
        })

    # Slide 6: Daily Note (Polymatica)
    if s.note_exists:
        note_color = "#4ade80" if s.note_words >= 250 else "#38bdf8"
        time_part = f"  ·  Edited {s.note_time}" if s.note_time else ""
        slides.append({
            "tag": "NOTE",
            "text": f"Today: {s.note_words} words{time_part}  ·  Polymatica",
            "color": note_color,
            "detail": f"Daily note ({s.note_words} words, edited {s.note_time or '--'}) · Click to open",
        })
    else:
        y_part = f"  ·  Yesterday: {s.note_words}w" if s.note_words > 0 else ""
        y_detail = f" (yesterday: {s.note_words} words)" if s.note_words > 0 else ""
        slides.append({
            "tag": "NOTE",
            "text": f"No entry today{y_part}  ·  Polymatica Vault",
            "color": "#94a3b8",
            "detail": f"Today's daily note not started yet{y_detail} · Click to create and open",
        })

    # Slide 7: Workspace Git
    if s.git_dirty_count > 0:
        if len(s.git_dirty_repos) <= 3:
            repos_summary = ", ".join(s.git_dirty_repos)
        else:
            repos_summary = f"{', '.join(s.git_dirty_repos[:2])} +{s.git_dirty_count - 2} more"
        oldest = max(s.git_dirty_days.values(), default=0)
        age_part = f"  ·  oldest {oldest:.0f} day{'s' if round(oldest) != 1 else ''}" if oldest >= 1 else ""
        slides.append({
            "tag": "GIT",
            "text": f"{s.git_dirty_count} repos dirty ({repos_summary}){age_part}",
            "color": "#fbbf24",
            "detail": f"Uncommitted: {', '.join(s.git_dirty_repos)} · {s.git_total_repos} total repos",
        })
    elif s.git_total_repos > 0:
        slides.append({
            "tag": "GIT",
            "text": f"All {s.git_total_repos} repos clean  ·  Workspace",
            "color": "#4ade80",
            "detail": f"All {s.git_total_repos} git repositories in D:\\Workspace clean",
        })

    # Slide 7b: what runs from D:\Workspace (projects started from the dashboard, media services that are up)
    if s.apps_running or s.media_running:
        parts = [f"▶ {a}" for a in s.apps_running[:3]] + ([f"+{len(s.apps_running) - 3} more"] if len(s.apps_running) > 3 else [])
        parts += [f"{m} up" for m in s.media_running]
        slides.append({
            "tag": "APPS",
            "text": "  ·  ".join(parts),
            "color": "#38bdf8",
            "detail": "Running: " + ", ".join(s.apps_running + [f"{m} (media)" for m in s.media_running])
                      + " · Click for the dashboard's Projects card",
        })

    # Slide 8: Network Throughput
    net_col = "#4ade80" if (s.net_down_kb > 500 or s.net_up_kb > 500) else "#38bdf8"
    slides.append({
        "tag": "NET",
        "text": f"↓ {format_rate(s.net_down_kb)}  ·  ↑ {format_rate(s.net_up_kb)}  ·  LAN/WAN",
        "color": net_col,
        "detail": f"Throughput: {format_rate(s.net_down_kb)} down, {format_rate(s.net_up_kb)} up",
    })

    # Slide 9: Media Now-Playing (if playing or paused track exists)
    if s.media_title and s.media_status in ("Playing", "Paused"):
        app_tag = f" · {s.media_app}" if s.media_app else ""
        artist_part = f" — {s.media_artist}" if s.media_artist else ""
        if s.media_status == "Playing":
            media_txt = f"▶ {s.media_title}{artist_part}{app_tag}"
            media_col = "#4ade80"
        else:
            media_txt = f"⏸ {s.media_title}{artist_part} (paused)"
            media_col = "#94a3b8"
        slides.append({
            "tag": "MEDIA",
            "text": media_txt,
            "color": media_col,
            "detail": f"Now Playing: {s.media_title}{artist_part} ({s.media_app or 'Media'}) · Click to focus player",
        })

    # The last Away session (a morning summary): shown until you click it or 18 h pass
    if s.night:
        n = s.night
        segs = [(f"Last Away {n['start']}–{n['end']}: ", MUTED), (f"{n['done']} job{'s' if n['done'] != 1 else ''} ✓", GREEN)]
        if n["failed"]:
            segs += [SEP, (f"{len(n['failed'])} ✗ ({', '.join(n['failed'][:2])})", RED)]
        for c in n["chains"]:
            segs += [SEP, (c, chain_result_color(c))]
        segs += [SEP, (f"ended: {n['reason']}", MUTED)]
        slides.append({"tag": "NIGHT", "text": plain(segs), "segments": segs, "color": RED if n["failed"] else GREEN,
                       "detail": f"Away {n['start']}–{n['end']} · jobs done: {n['done']} · failed: {', '.join(n['failed']) or 'none'}"
                                 f" · chains: {', '.join(n['chains']) or 'none'} · ended: {n['reason']}\n"
                                 "Click to open the newest review / report (this summary then goes away)",
                       "level": "info"})

    if s.review:
        slides.append(review_slide(s.review))

    # What needs you, in one slide (the ticker shows it first and holds it longer)
    alerts = attention(s)
    if alerts:
        segs = joined([(text, color) for color, text, _ in alerts])
        slides.append({"tag": "ALERT", "text": plain(segs), "segments": segs,
                       "color": RED if any(c == RED for c, _, _ in alerts) else AMBER,
                       "detail": "\n".join(f"• {t}" for _, t, _ in alerts), "target": alerts[0][2], "level": "alert"})

    for sl in slides:
        sl.setdefault("segments", colorize(sl, s))
        sl.setdefault("level", slide_level(sl, s))
    return slides


def review_slide(r: dict) -> dict:
    """The exam helper on the ticker: the last solved question while it watches, with full explanation in the tooltip.
    Click opens the dashboard's card."""
    cur, busy = r.get("current") or {}, r.get("status") in ("reading", "explaining", "answering", "waiting")
    is_gemini = bool(cur.get("gemini_retried") or cur.get("model") == "gemini-3.8-flash")
    prefix = "Exam ✨ " if is_gemini else "Exam "
    segs: list[tuple[str, str]] = [(prefix, CYAN if is_gemini else MUTED)]
    if cur:
        q_type = cur.get("question_type", "multiple_choice")
        if q_type == "matching":
            pairs_count = len(cur.get("matching_pairs") or [])
            ans_str = f"Match: {pairs_count} pairs" if pairs_count else "Match"
        elif q_type == "fill_in_the_blank":
            blanks = cur.get("blank_answers") or []
            ans_str = f"Input: {blanks[0][:20]}" if blanks else (cur.get("answer", "")[:20] or "Input")
        elif q_type == "ordering":
            steps_count = len(cur.get("ordered_sequence") or [])
            ans_str = f"Order: {steps_count} steps" if steps_count else "Order"
        else:
            ans = "/".join(cur.get("answer_labels") or cur.get("correct_labels") or [])
            if not ans and cur.get("answer"):
                ans = str(cur["answer"])[:15]
            ans_str = f"Ans: {ans or '?'}"
        segs += [(ans_str, CYAN if is_gemini else GREEN), SEP, (cur.get("topic") or "solved", CYAN)]
    else:
        segs += [(r.get("text") or "watching", MUTED)]
    count_str = (f"{r.get('answered', 0)} solved" if r.get("answered")
                 else (f"{r.get('correct', 0)} ✓ {r.get('wrong', 0)} ✗" if (r.get("correct") or r.get("wrong")) else ""))
    if count_str:
        segs += [SEP, (count_str, TEXT)]
    if busy:
        segs += [SEP, (r.get("text") or r.get("status", ""), CYAN)]
    elif not r.get("running"):
        segs += [SEP, ("stopped", MUTED)]
    if cur:
        model_line = f"[{cur.get('model', 'sol-vision')}]\n" if cur.get("model") else ""
        why_wrong = f"\nPrevious attempt issue: {cur['why_previous_wrong']}\n" if cur.get("why_previous_wrong") else ""
        exhibit = f"\n[Exhibit]:\n{cur['exhibit_text']}\n" if cur.get("exhibit_text") else ""
        detail = (f"{model_line}{cur.get('question', '')}{exhibit}\nAnswer: {cur.get('answer', '')}\n"
                  f"{cur.get('explanation', '')}{why_wrong}")
    else:
        detail = r.get("text") or ""
    return {"tag": "REVIEW", "text": plain(segs), "segments": segs, "color": CYAN if (busy or is_gemini) else MUTED,
            "detail": detail + "\nClick to open the Exam card", "level": "alert" if r.get("running") else "info"}


def _part_color(tag: str, part: str, s: Snapshot) -> str:
    """The color of one ' · '-separated part of a slide (see the color meanings at the top of this file)."""
    p = part.strip()
    if tag == "HW":
        if p.startswith("[CRASH"):
            return RED
        if p.startswith("GPU"):
            if s.gpu_hotspot is not None and s.gpu_hotspot >= HOTSPOT_ALERT_C:
                return RED
            return AMBER if (s.gpu_temp or 0) >= 85 or (s.temp_rise_c or 0) >= TEMP_RISE_C else TEXT
        if p.startswith("VRAM"):
            growing = (s.vram_rise_gb or 0) >= VRAM_RISE_GB and s.ai_mode == "desk"
            return (RED if s.vram_spill_impact == "slow" else AMBER if s.vram_evicted or s.vram_tight or growing
                    else TEXT)
        if p.startswith("RAM"):
            return RED if s.ram_percent >= 92 else AMBER if s.ram_percent >= 85 else TEXT
    elif tag == "AI":
        if p == ":11440":
            return MUTED
        if p.endswith("tok/s"):
            return RED if s.ai_tps_slow else GREEN
        if p.startswith(("Away", "Off", "Desk")):
            return CYAN if p.startswith("Away") else AMBER if p.startswith("Off") else TEXT
        if s.ai_generating or s.ai_state == "loaded":
            return GREEN
        return {"sleeping": CYAN, "offline": RED}.get(s.ai_state, MUTED)
    elif tag == "RUN":
        if p.startswith("Run:"):
            return CYAN
        if p.startswith("Last:"):
            return chain_result_color(p)
        if p.startswith("next:"):
            return TEXT
        if p == "0 queued" or p.startswith("Chains: idle"):
            return MUTED
        return AMBER                                   # something is queued or waiting
    elif tag == "SVC":
        return GREEN if "●" in p else (MUTED if p.startswith("WSL") else RED)
    elif tag == "SYS":
        if p.startswith("WSL Backup"):
            if s.backup_stale or s.backup_age_h is None:
                return RED
            return GREEN if s.backup_age_h < 26 else AMBER
        if p.startswith("Stability"):
            return RED if (s.unexpected_reboots + s.gpu_resets) else GREEN
    elif tag == "NOTE":
        late = time.localtime().tm_hour >= 20     # after 20:00 an unfinished daily note is worth a look
        if p.startswith("Today:"):
            return GREEN if s.note_words >= 250 else AMBER if late else TEXT
        if p.startswith("No entry today"):
            return AMBER if late else MUTED
        return MUTED
    elif tag == "GIT":
        if s.git_dirty_count:
            return RED if max(s.git_dirty_days.values(), default=0) > GIT_OLD_DAYS else AMBER
        return GREEN if p.startswith("All") else MUTED
    elif tag == "NET":
        if p == "LAN/WAN":
            return MUTED
        return CYAN if max(s.net_down_kb, s.net_up_kb) >= 1024 else TEXT
    elif tag == "MEDIA":
        return GREEN if s.media_status == "Playing" else MUTED
    elif tag == "APPS":
        return MUTED if p.startswith("+") else CYAN
    elif tag == "AWAY":
        if p.endswith("%"):
            return GREEN
        return TEXT if "left" in p else CYAN
    return TEXT


def colorize(slide: dict, s: Snapshot) -> list[tuple[str, str]]:
    """Split a slide's text on its separators and color each part."""
    text, tag = slide.get("text", ""), slide.get("tag", "")
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"(  ·  |  \|  )", text):
        if chunk in ("  ·  ", "  |  "):
            out.append((chunk, MUTED))
        elif chunk:
            out.append((chunk, _part_color(tag, chunk, s)))
    return out


def slide_level(slide: dict, s: Snapshot) -> str:
    """alert (shown first, held longer) | info (in the rotation) | quiet (only when you click through: all fine)."""
    tag = slide.get("tag")
    if tag == "DISK":
        return "info" if any(c != TEXT for t, c in slide["segments"] if t not in ("Free ",) and c != MUTED) else "quiet"
    if tag == "SVC":
        return "quiet" if all(s.services.get(n, False) for n in ("Router", "Embed", "HUD", "Chains")) else "info"
    if tag == "SYS":
        old_backup = s.backup_age_h is None or s.backup_age_h >= 72
        return "info" if (s.unexpected_reboots + s.gpu_resets) or s.backup_stale or old_backup else "quiet"
    if tag == "GIT":
        return "info" if s.git_dirty_count else "quiet"
    if tag == "NET":
        return "info" if max(s.net_down_kb, s.net_up_kb) >= 1024 else "quiet"
    return "info"


def rotation(slides: list[dict]) -> list[int]:
    """The order the ticker cycles through by itself: the ALERT slide first and between every other slide, quiet
    slides left out (unless that would leave fewer than 3). Clicking still steps through every slide.
    With two alert-level slides (ALERT and the exam REVIEW) they take turns in those between-slots."""
    alert = [i for i, sl in enumerate(slides) if sl.get("level") == "alert"]
    rest = [i for i, sl in enumerate(slides) if sl.get("level") == "info"]
    if len(rest) < 3:
        rest = [i for i, sl in enumerate(slides) if sl.get("level") != "alert"]
    if not alert:
        return rest or list(range(len(slides)))
    order: list[int] = []
    for k, i in enumerate(rest):
        order += [alert[k % len(alert)], i]
    return order or alert


def format_multiline_rows(s: Snapshot) -> list[dict]:
    """Creates 5 rows of data for the multi-line tile view."""
    # Row 1: Hardware
    gpu_txt = f"{round(s.gpu_load)}%" if s.gpu_load is not None else "--"
    vram_used = f"{s.vram_used_gb:.1f}" if s.vram_used_gb is not None else "--"
    vram_tot = f"{s.vram_total_gb:.0f}" if s.vram_total_gb is not None else "16"
    slow_spill = s.vram_spill_impact == "slow"
    vram_status = "EVICTED" if slow_spill else "SPILL" if s.vram_evicted else ("TIGHT" if s.vram_tight else "OK")
    vram_status_col = "#f87171" if slow_spill else ("#fbbf24" if s.vram_evicted or s.vram_tight else "#4ade80")
    if slow_spill:
        vram_display = f"{vram_used}/{vram_tot}G (EVICTED!)"
    elif s.vram_evicted:
        vram_display = f"{vram_used}/{vram_tot}G ({SPILL_WORD[s.vram_spill_impact]})"
    elif s.vram_tight:
        vram_display = f"{vram_used}/{vram_tot}G (TIGHT)"
    elif s.vram_top_process:
        vram_display = f"{vram_used}/{vram_tot}G ({s.vram_top_process})"
    else:
        vram_display = f"{vram_used}/{vram_tot}G"

    if s.gpu_temp is not None:
        gpu_display = f"{gpu_txt} ({s.gpu_temp}°C)" if not s.gpu_hotspot else f"{gpu_txt} ({s.gpu_temp}°C/{s.gpu_hotspot}°C)"
    else:
        gpu_display = gpu_txt

    hw_items = [
        ("GPU", gpu_display, RED if (s.gpu_hotspot or 0) >= HOTSPOT_ALERT_C else AMBER if (s.gpu_temp or 0) >= 85 else TEXT),
        ("VRAM", vram_display, vram_status_col),
        ("RAM", f"{s.ram_used_gb:.1f}/{s.ram_total_gb:.0f}G", RED if s.ram_percent >= 92 else AMBER if s.ram_percent >= 85 else TEXT),
    ]
    if s.gpu_fan_rpm is not None:
        hw_items.append(("Fan", f"{s.gpu_fan_rpm} RPM", "#94a3b8"))
    hw_items.append(("CPU", f"{s.cpu_percent:.0f}%", "#94a3b8"))

    row_hw = {
        "title": "HARDWARE",
        "items": hw_items,
    }

    # Row 2: Local AI
    model_str = f"{s.ai_model} ⚡" if s.ai_generating else (s.ai_model or "None")
    if s.ai_generating:
        state_col = "#4ade80"
        state_disp = "generating ⚡"
    else:
        state_col = "#4ade80" if s.ai_state == "loaded" else ("#38bdf8" if s.ai_state == "sleeping" else "#94a3b8")
        state_disp = s.ai_state
    mode_display = s.ai_mode
    if s.ai_mode == "away" and s.ai_until:
        try:
            mode_display = f"away until {s.ai_until.split('T')[-1][:5]}"
        except Exception:
            pass
    elif s.ai_mode == "off" and s.ai_reason:
        mode_display = f"off: {s.ai_reason.removeprefix('game: ')}"

    row_ai = {
        "title": "LOCAL AI",
        "items": [
            ("Model", model_str, "#4ade80" if s.ai_generating else "#e2e8f0"),
            ("State", state_disp, state_col),
            ("Mode", mode_display, "#38bdf8"),
            ("Port", "11440", "#94a3b8"),
        ]
    }
    if s.ai_mode == "away" and s.away_line:
        pct = f"{int(s.away_fraction * 100)}% " if s.away_fraction is not None and s.away_phase == "working" else ""
        row_ai["items"][-1] = ("Job", f"{pct}{s.away_line}", "#38bdf8")
    if not s.ai_model and s.ai_state != "offline":
        row_ai["items"][0] = ("Model", "none loaded", "#94a3b8")

    # Row 3: Chains
    if s.chain_running:
        status_txt = f"{s.chain_running}" + (f" [{s.chain_step}]" if s.chain_step else "")
        status_col = "#38bdf8"
    elif s.chain_last_finished:
        status_txt = f"Idle (Last: {s.chain_last_finished})"
        status_col = "#94a3b8"
    else:
        status_txt = "Idle"
        status_col = "#94a3b8"

    row_chains = {
        "title": "CHAINS",
        "items": [
            ("Current", status_txt, status_col),
            ("Pending", f"{s.chain_pending_count}", "#e2e8f0"),
            ("GPU Lock", "Held" if s.gpu_locked else "Free", "#fbbf24" if s.gpu_locked else "#94a3b8"),
        ]
    }

    # Row 4: Services
    svc_items = []
    for name in SERVICE_ORDER:
        up = s.services.get(name, False)
        off = "off" if name == "WSL" else "down"   # WSL being off is normal (its services start on demand)
        svc_items.append((name, "up" if up else off, "#4ade80" if up else "#64748b"))
    if s.self_cpu > 0 or s.self_ram_mb > 0:
        svc_items.append(("HUD", f"{s.self_cpu:.1f}%", "#38bdf8"))

    row_svc = {
        "title": "SERVICES",
        "items": svc_items
    }

    # Row 5: Storage & Backups
    storage_items = []
    for d in s.disks:
        if "drive" in d and "free_gb" in d:
            trend = s.disk_trends.get(d["drive"])
            arrow = f" ▼{-trend:.1f}" if trend is not None and trend <= -DISK_TREND_GB else f" ▲{trend:.1f}" if trend is not None and trend >= DISK_TREND_GB else ""
            storage_items.append((d["drive"], f"{d['free_gb']:.0f}G free{arrow}", disk_color(d, trend)))
    if s.backup_age_h is not None:
        b_str = f"{s.backup_age_h:.0f}h" if s.backup_age_h < 48 else f"{s.backup_age_h/24:.1f}d"
        b_col = "#f87171" if s.backup_stale else "#4ade80"
        storage_items.append(("Backup", b_str, b_col))
    if s.git_dirty_count > 0:
        storage_items.append(("Git", f"{s.git_dirty_count} dirty", "#fbbf24"))
    elif s.git_total_repos > 0:
        storage_items.append(("Git", "clean", "#4ade80"))
    if s.note_exists:
        note_col = "#4ade80" if s.note_words >= 250 else "#38bdf8"
        storage_items.append(("Note", f"{s.note_words}w", note_col))
    if s.net_down_kb > 0 or s.net_up_kb > 0:
        storage_items.append(("Net", f"↓{format_rate(s.net_down_kb)}", "#38bdf8"))

    row_storage = {
        "title": "SYSTEM",
        "items": storage_items or [("Disks", "available", "#94a3b8")]
    }

    return [row_hw, row_ai, row_chains, row_svc, row_storage]
