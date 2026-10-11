"""Tests for ai_ops collector: router.log telemetry and live thinking stream reader."""
import json
import time

from sol_control_hud.data.collectors import ai_ops


SAMPLE_ROUTER_LOG = """
[58251] 190.00.464.359 I slot launch_slot_: id  0 | task 880 | processing task, is_child = 0
[58251] 190.03.665.512 I slot print_timing: id  0 | task 880 | n_gen =    234, tg =  74.92 t/s, tg_3s =  75.24 t/s
[58251] 190.04.130.684 I slot print_timing: id  0 | task 880 | prompt eval time =      91.19 ms /     5 tokens (   18.24 ms per token,    54.83 tokens per second)
[58251] 190.04.130.696 I slot print_timing: id  0 | task 880 |        eval time =    3575.10 ms /   246 tokens (   14.59 ms per token,    68.53 tokens per second)
[58251] 190.04.130.697 I slot print_timing: id  0 | task 880 |       total time =    3666.28 ms /   251 tokens
[58251] 190.04.130.703 I slot print_timing: id  0 | task 880 | draft acceptance = 0.57770 (  171 accepted /   296 generated), mean len =  3.31
[58251] 190.04.130.757 I slot      release: id  0 | task 880 | stop processing: n_tokens = 287, truncated = 0
"""

ACTIVE_ROUTER_LOG = """
190.00.572.949 I srv  proxy_reques: proxying request to model sol-smart on port 58251
[58251] 190.00.464.359 I slot launch_slot_: id  0 | task 999 | processing task, is_child = 0
[58251] 190.04.130.684 I slot print_timing: id  0 | task 999 | prompt eval time =      120.00 ms /    50 tokens (    2.40 ms per token,   416.67 tokens per second)
[58251] 190.03.665.512 I slot print_timing: id  0 | task 999 | n_gen =    112, tg =  64.20 t/s, tg_3s =  65.10 t/s
"""


def test_parse_completed_router_log():
    res = ai_ops.parse_router_events(SAMPLE_ROUTER_LOG)
    assert not res["active"]
    assert res["current"] is None
    last = res["last"]
    assert last is not None
    assert last["slot"] == 0
    assert last["task"] == 880
    assert last["total_tokens"] == 287
    assert last["eval_tps"] == 68.5
    assert last["draft_rate"] == 0.578
    assert last["draft_accepted"] == 171
    assert last["draft_generated"] == 296
    assert last["draft_mean_len"] == 3.31
    assert len(res["completed_history"]) == 1
    assert res["completed_history"][0]["eval_tps"] == 68.5
    assert res["completed_history"][0]["draft_rate"] == 0.578


def test_parse_active_router_log():
    res = ai_ops.parse_router_events(ACTIVE_ROUTER_LOG)
    assert res["active"]
    cur = res["current"]
    assert cur is not None
    assert cur["slot"] == 0
    assert cur["task"] == 999
    assert cur["phase"] == "generating"
    assert cur["n_gen"] == 112
    assert cur["tg_tps"] == 64.2
    assert cur["prompt_tokens"] == 50
    assert cur["prompt_tps"] == 416.7
    assert res["proxied_model"] == "sol-smart"


def test_live_thought_roundtrip(tmp_path):
    f = tmp_path / "live_thought.json"
    assert ai_ops.read_live_thought(f)["active"] is False

    now = time.time()
    ai_ops.write_live_thought({
        "active": True,
        "phase": "thinking",
        "model": "sol-smart",
        "task": "Test task",
        "thinking": "Step 1: checking bounds...",
        "preview": "",
        "tokens": 42,
        "started_at": now,
        "updated_at": now,
    }, path=f)

    read_back = ai_ops.read_live_thought(f)
    assert read_back["active"] is True
    assert read_back["phase"] == "thinking"
    assert read_back["model"] == "sol-smart"
    assert "checking bounds" in read_back["thinking"]
    assert read_back["tokens"] == 42


def test_stale_live_thought_becomes_inactive(tmp_path):
    f = tmp_path / "live_thought.json"
    ai_ops.write_live_thought({
        "active": True,
        "phase": "thinking",
        "model": "sol-smart",
        "task": "Died task",
        "thinking": "old thoughts",
        "updated_at": time.time() - 100.0,
    }, path=f)

    res = ai_ops.read_live_thought(f, max_age_s=30.0)
    assert res["active"] is False
    assert res["phase"] == "idle"


def test_sample_ai_ops_aggregate(tmp_path):
    log_file = tmp_path / "router.log"
    thought_file = tmp_path / "live_thought.json"
    log_file.write_text(ACTIVE_ROUTER_LOG, encoding="utf-8")

    now = time.time()
    ai_ops.write_live_thought({
        "active": True,
        "phase": "thinking",
        "model": "sol-smart",
        "task": "Chain Code Review",
        "thinking": "Reviewing ast nodes...",
        "tokens": 88,
        "updated_at": now,
    }, path=thought_file)

    procs = {"sol-smart": {"port": 58251}}
    ops = ai_ops.sample_ai_ops(procs, router_log=log_file, thought_file=thought_file)

    assert ops["available"] is True
    assert ops["active"] is True
    assert ops["model"] == "sol-smart"
    assert ops["speed"]["tg_tps"] == 64.2
    assert ops["speed"]["prompt_tps"] == 416.7
    assert ops["thinking"]["active"] is True
    assert "Reviewing ast nodes" in ops["thinking"]["text"]
    assert ops["thinking"]["tokens"] == 88
