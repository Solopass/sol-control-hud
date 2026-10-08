"""Tests for sol_control_hud.gemini_solver."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from sol_control_hud import gemini_solver


def test_image_to_base64_in_memory():
    sample_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    encoded = gemini_solver.image_to_base64(sample_bytes)
    assert isinstance(encoded, str)
    assert len(encoded) > 0


def test_get_api_key_from_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")
    assert gemini_solver.get_api_key() == "test-key-123"


def test_get_api_key_from_settings(monkeypatch, tmp_path):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    settings_file = tmp_path / "exam-review.json"
    settings_file.write_text(json.dumps({"gemini_api_key": "file-key-456"}), encoding="utf-8")
    assert gemini_solver.get_api_key(settings_file) == "file-key-456"


def test_call_with_retry_succeeds_first_try():
    called = 0

    def work():
        nonlocal called
        called += 1
        return "success"

    res = gemini_solver.call_with_retry(work, max_retries=3, base_delay=0.01)
    assert res == "success"
    assert called == 1


def test_call_with_retry_backs_off_and_succeeds():
    attempts = 0

    def work():
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            req = httpx.Request("POST", "https://api.test")
            resp = httpx.Response(429, request=req, text="Rate limit exceeded")
            raise httpx.HTTPStatusError("429", request=req, response=resp)
        return {"ok": True}

    res = gemini_solver.call_with_retry(work, max_retries=3, base_delay=0.01)
    assert res == {"ok": True}
    assert attempts == 3


def test_call_with_retry_fails_after_max_retries():
    attempts = 0

    def work():
        nonlocal attempts
        attempts += 1
        req = httpx.Request("POST", "https://api.test")
        resp = httpx.Response(429, request=req, text="Rate limit exceeded")
        raise httpx.HTTPStatusError("429", request=req, response=resp)

    with pytest.raises(gemini_solver.GeminiRateLimitError):
        gemini_solver.call_with_retry(work, max_retries=3, base_delay=0.01)
    assert attempts == 3


def test_solve_with_gemini_mocked(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "answer_labels": ["B"],
                        "answer_text": "B. Correct option",
                        "topic": "DNS queries",
                        "explanation": "nslookup queries DNS records directly.",
                        "why_previous_wrong": "Choice A was ping, which checks ICMP echo, not DNS resolution."
                    })
                }]
            }
        }]
    }
    mock_client.post.return_value = mock_resp

    img1 = b"fake-png-1"
    img2 = b"fake-png-2"
    prev = {
        "question": "Which tool queries DNS?",
        "answer": "A. ping",
        "choices": [{"label": "A", "text": "ping"}, {"label": "B", "text": "nslookup"}]
    }

    result = gemini_solver.solve_with_gemini(
        [img1, img2],
        previous_item=prev,
        client=mock_client
    )

    assert result["answer_labels"] == ["B"]
    assert result["topic"] == "DNS queries"
    assert "ping" in result["why_previous_wrong"]

    # Verify post was called with multi-part images
    args, kwargs = mock_client.post.call_args
    payload = kwargs["json"]
    parts = payload["contents"][0]["parts"]
    assert len(parts) == 3  # 2 images + 1 prompt text
    assert parts[0]["inline_data"]["mime_type"] == "image/png"
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert "scrolling" in parts[2]["text"]


def test_solve_with_gemini_multipart_and_preamble(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [
                    {"text": "Here is the verified solution after analyzing both images:\n```json\n"},
                    {"text": json.dumps({"answer": "C", "explanation": "Detailed explanation."})},
                    {"text": "\n```\nHope that helps!"}
                ]
            }
        }]
    }
    mock_client.post.return_value = mock_resp

    jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 20
    res = gemini_solver.solve_with_gemini([jpeg_bytes], client=mock_client)
    assert res["answer"] == "C"
    assert res["explanation"] == "Detailed explanation."

    args, kwargs = mock_client.post.call_args
    parts = kwargs["json"]["contents"][0]["parts"]
    assert parts[0]["inline_data"]["mime_type"] == "image/jpeg"


def test_call_with_retry_network_timeout():
    attempts = 0

    def work():
        nonlocal attempts
        attempts += 1
        if attempts < 2:
            raise httpx.TimeoutException("Connection timed out")
        return "success_after_timeout"

    res = gemini_solver.call_with_retry(work, max_retries=3, base_delay=0.01)
    assert res == "success_after_timeout"
    assert attempts == 2

