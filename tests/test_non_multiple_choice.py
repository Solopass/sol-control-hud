"""Tests for non-multiple-choice exam formats and exhibits in SOL Control HUD.

Covers:
- Drag-and-Drop / Matching
- Fill-in-the-Blank / CLI Commands
- Step Ordering / Sequencing
- Exhibit & Diagram synthesis
- Floating HUD & Clipboard copy
- Ticker slides
- Obsidian note markdown generation
"""
import json
from contextlib import contextmanager
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from sol_control_hud import exam_review as er
from sol_control_hud.chains.llm import ChatResult
from sol_control_hud.data import snapshot as sn
from sol_control_hud.views import exam_hud


@contextmanager
def no_lock(what):
    yield


class MockClient:
    def __init__(self, read_response: dict, solve_response: dict):
        self.read_response = read_response
        self.solve_response = solve_response
        self.calls = []

    def chat(self, model, messages, **kw):
        self.calls.append((model, messages, kw))
        user = messages[-1]["content"]
        if isinstance(user, list):
            return ChatResult(content=json.dumps(self.read_response), finish_reason="stop")
        return ChatResult(content=json.dumps(self.solve_response), finish_reason="stop")


def make_watcher(tmp_path, read_resp, solve_resp):
    client = MockClient(read_resp, solve_resp)
    w = er.ReviewWatcher(
        grab_thumb=lambda r: b"\x00" * 64,
        grab_png=lambda r: b"png",
        client_factory=lambda: client,
        lock=no_lock,
        mode_fn=lambda: "desk",
        settings_path=tmp_path / "s.json",
        notes_dir=tmp_path / "School",
    )
    return w, client


# ---- 1. Matching / Drag-and-Drop
def test_matching_drag_and_drop_solve_and_note(tmp_path):
    read_resp = {
        "status": "ok",
        "question_type": "matching",
        "question": "Match the OSPF packet type to its primary function.",
        "choices": [],
        "your_labels": [],
        "correct_labels": [],
        "matching_items": {
            "sources": ["Hello", "DBD", "LSR", "LSU", "LSAck"],
            "targets": ["Builds adjacencies", "Checks database sync", "Requests records", "Sends records", "Acknowledges"],
        },
    }
    solve_resp = {
        "question_type": "matching",
        "topic": "OSPF Packet Types",
        "matching_pairs": [
            {"source": "Hello", "target": "Builds adjacencies"},
            {"source": "DBD", "target": "Checks database sync"},
            {"source": "LSR", "target": "Requests records"},
            {"source": "LSU", "target": "Sends records"},
            {"source": "LSAck", "target": "Acknowledges"},
        ],
        "answer_text": "5 pairs matched",
        "explanation": "Hello packets discover neighbors and establish bidirectional adjacencies. DBD exchanges summary LSAs.",
    }

    w, client = make_watcher(tmp_path, read_resp, solve_resp)
    assert w._process(b"png", force=True) is True

    s = w.state()
    assert s["answered"] == 1
    assert s["current"]["question_type"] == "matching"
    assert len(s["current"]["matching_pairs"]) == 5
    assert "Hello ➔ Builds adjacencies" in s["current"]["answer"]

    # Check Ticker slide
    slide = sn.review_slide(s)
    assert "Match: 5 pairs" in slide["text"]
    assert "OSPF Packet Types" in slide["text"]

    # Check Obsidian note
    note_file = (tmp_path / "School").glob("* practice exam.md").__next__()
    note_content = note_file.read_text(encoding="utf-8")
    assert "## OSPF Packet Types (Matching)" in note_content
    assert "| Item | Target / Category |" in note_content
    assert "| **Hello** | Builds adjacencies |" in note_content
    assert "| **DBD** | Checks database sync |" in note_content


# ---- 2. Fill-in-the-Blank / CLI Commands
def test_fill_in_the_blank_cli_command_solve_and_note(tmp_path):
    read_resp = {
        "status": "ok",
        "question_type": "fill_in_the_blank",
        "question": "Configure a default static IPv4 route forwarding traffic out interface GigabitEthernet0/0.",
        "choices": [],
        "your_labels": [],
        "correct_labels": [],
        "exhibit_text": "Router# configure terminal\nRouter(config)#",
    }
    solve_resp = {
        "question_type": "fill_in_the_blank",
        "topic": "Default Route CLI",
        "blank_answers": ["ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0"],
        "answer_text": "ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0",
        "explanation": "A default static route uses prefix 0.0.0.0 with subnet mask 0.0.0.0 pointing to the egress interface.",
    }

    w, client = make_watcher(tmp_path, read_resp, solve_resp)
    assert w._process(b"png", force=True) is True

    s = w.state()
    assert s["answered"] == 1
    assert s["current"]["question_type"] == "fill_in_the_blank"
    assert s["current"]["blank_answers"] == ["ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0"]
    assert s["current"]["answer"] == "ip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0"

    # Check Ticker slide
    slide = sn.review_slide(s)
    assert "Input: ip route 0.0.0.0 0." in slide["text"]
    assert "Default Route CLI" in slide["text"]

    # Check Obsidian note
    note_file = (tmp_path / "School").glob("* practice exam.md").__next__()
    note_content = note_file.read_text(encoding="utf-8")
    assert "## Default Route CLI (Fill in Blank)" in note_content
    assert "```cisco\nip route 0.0.0.0 0.0.0.0 GigabitEthernet0/0\n```" in note_content
    assert "Exhibit / Diagram" in note_content


# ---- 3. Step Ordering / Sequencing
def test_step_ordering_solve_and_note(tmp_path):
    read_resp = {
        "status": "ok",
        "question_type": "ordering",
        "question": "Place the TCP three-way handshake and connection establishment steps in chronological order.",
        "choices": [],
        "your_labels": [],
        "correct_labels": [],
        "ordered_items": [
            "Client sends SYN",
            "Server receives SYN and sends SYN-ACK",
            "Client sends ACK",
            "Connection is ESTABLISHED",
        ],
    }
    solve_resp = {
        "question_type": "ordering",
        "topic": "TCP Handshake Order",
        "ordered_sequence": [
            "1. Client sends SYN",
            "2. Server responds with SYN-ACK",
            "3. Client replies with ACK",
            "4. Socket transitions to ESTABLISHED",
        ],
        "answer_text": "4 steps sequenced",
        "explanation": "TCP begins with SYN from client, server replies with SYN-ACK, client acknowledges with ACK.",
    }

    w, client = make_watcher(tmp_path, read_resp, solve_resp)
    assert w._process(b"png", force=True) is True

    s = w.state()
    assert s["answered"] == 1
    assert s["current"]["question_type"] == "ordering"
    assert len(s["current"]["ordered_sequence"]) == 4

    # Check Ticker slide
    slide = sn.review_slide(s)
    assert "Order: 4 steps" in slide["text"]
    assert "TCP Handshake Order" in slide["text"]

    # Check Obsidian note
    note_file = (tmp_path / "School").glob("* practice exam.md").__next__()
    note_content = note_file.read_text(encoding="utf-8")
    assert "## TCP Handshake Order (Ordering)" in note_content
    assert "**Correct Sequence:**" in note_content
    assert "1. Client sends SYN" in note_content
    assert "2. Server responds with SYN-ACK" in note_content


# ---- 4. Exhibits & Diagrams Synthesis
def test_exhibits_and_diagrams_synthesis(tmp_path):
    read_resp = {
        "status": "ok",
        "question_type": "multiple_choice",
        "question": "Refer to the exhibit. Which route will Router1 select to reach destination 10.1.1.15?",
        "choices": [
            {"label": "A", "text": "10.1.0.0/16 via 192.168.1.1"},
            {"label": "B", "text": "10.1.1.0/24 via 192.168.1.2"},
            {"label": "C", "text": "10.0.0.0/8 via 192.168.1.3"},
            {"label": "D", "text": "0.0.0.0/0 via 192.168.1.254"},
        ],
        "your_labels": ["A"],
        "correct_labels": [],
        "exhibit_text": "Gateway of last resort is 192.168.1.254\nO    10.1.0.0/16 [110/20] via 192.168.1.1\nD    10.1.1.0/24 [90/3072] via 192.168.1.2\nS*   0.0.0.0/0 [1/0] via 192.168.1.254",
    }
    solve_resp = {
        "question_type": "multiple_choice",
        "topic": "Longest Prefix Match",
        "answer_labels": ["B"],
        "answer_text": "B. 10.1.1.0/24 via 192.168.1.2",
        "explanation": "Routers always prefer the most specific route (longest prefix match). /24 is longer than /16 or /8.",
    }

    w, client = make_watcher(tmp_path, read_resp, solve_resp)
    assert w._process(b"png", force=True) is True

    s = w.state()
    assert s["current"]["correct_labels"] == ["B"]
    assert "10.1.1.0/24" in s["current"]["answer"]

    # Check that exhibit is sent in prompt
    solve_call = client.calls[1]
    prompt_sent = solve_call[1][-1]["content"]
    assert "Gateway of last resort" in prompt_sent


# ---- 5. Floating HUD Component Tests
def test_exam_hud_update_item_and_clipboard():
    parent_mock = MagicMock()
    parent_mock.winfo_screenwidth.return_value = 1920

    hud = exam_hud.ExamHudWindow(parent=parent_mock)
    win_mock = MagicMock()
    hud.win = win_mock
    hud.topic_lbl = MagicMock()
    hud.ans_lbl = MagicMock()
    hud.badge_lbl = MagicMock()
    hud.bad_btn = MagicMock()
    hud.copy_btn = MagicMock()
    hud.txt = MagicMock()
    hud.show = MagicMock()

    # Test Fill-in-the-Blank update
    fib_item = {
        "question_type": "fill_in_the_blank",
        "topic": "VLAN Config",
        "question": "Enter the command to name the VLAN 'ENGINEERING'.",
        "blank_answers": ["name ENGINEERING"],
        "answer": "name ENGINEERING",
        "explanation": "Use 'name <string>' in VLAN configuration mode.",
    }
    hud.update_item(fib_item)

    hud.topic_lbl.config.assert_called_with(text="Topic: VLAN Config · Fill-in-Blank")
    hud.ans_lbl.config.assert_called_with(text="Ans: name ENGINEERING", fg="#4ade80")
    hud.copy_btn.pack.assert_called()

    # Test Clipboard copy
    hud._on_click_copy()
    win_mock.clipboard_clear.assert_called_once()
    win_mock.clipboard_append.assert_called_once_with("name ENGINEERING")
    hud.copy_btn.config.assert_called_with(text="✓ Copied!")

    # Test Matching update
    matching_item = {
        "question_type": "matching",
        "topic": "Port Numbers",
        "question": "Match the protocols to standard port numbers.",
        "matching_pairs": [
            {"source": "HTTP", "target": "80"},
            {"source": "HTTPS", "target": "443"},
        ],
        "explanation": "Standard well-known ports.",
    }
    hud.update_item(matching_item)
    hud.topic_lbl.config.assert_called_with(text="Topic: Port Numbers · Matching")
    hud.ans_lbl.config.assert_called_with(text="Ans: 2 Pairs Matched", fg="#4ade80")
    hud.copy_btn.pack_forget.assert_called()


# ---- 6. Gemini Retry with Non-Multiple-Choice
def test_gemini_retry_non_multiple_choice(monkeypatch, tmp_path):
    from sol_control_hud import gemini_solver

    w, client = make_watcher(tmp_path, {}, {})
    # Set current to a bad/failed local attempt for a matching question
    w._s["current"] = {
        "question_type": "matching",
        "question": "Match the OSI layer to its PDU.",
        "topic": "OSI Layers",
        "answer": "Bad guess",
        "correct": "Bad guess",
    }
    w._last_png_bytes = b"fake_png"

    # Mock solve_with_gemini
    fake_gemini_sol = {
        "question_type": "matching",
        "topic": "OSI Layer PDUs",
        "matching_pairs": [
            {"source": "Transport", "target": "Segment"},
            {"source": "Network", "target": "Packet"},
            {"source": "Data Link", "target": "Frame"},
        ],
        "answer_text": "3 pairs matched",
        "explanation": "Transport layer PDU is segment, Network is packet, Data Link is frame.",
        "why_previous_wrong": "Local vision model confused layer 3 with layer 4.",
    }

    monkeypatch.setattr(gemini_solver, "solve_with_gemini", lambda *a, **kw: fake_gemini_sol)

    res = w.retry_gemini()
    assert res["ok"] is True
    item = res["item"]
    assert item["question_type"] == "matching"
    assert len(item["matching_pairs"]) == 3
    assert "Transport ➔ Segment" in item["answer"]
    assert item["gemini_retried"] is True

    # Verify Obsidian retry note formatting
    note_file = (tmp_path / "School").glob("* practice exam.md").__next__()
    content = note_file.read_text(encoding="utf-8")
    assert "Marked Bad · Re-attempt via Gemini 3.8 Flash" in content
    assert "> **Format:** Matching" in content
    assert "> | Item | Target |" in content
    assert "> | **Transport** | Segment |" in content
    assert "Why previous answer was wrong" in content

