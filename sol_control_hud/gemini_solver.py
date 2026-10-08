"""Gemini API solver for practice exam re-attempts and verification.

Provides fallback solving when the user flags a local AI answer as incorrect or bad.
Features:
- Pure in-memory image passing via io.BytesIO and base64: zero temp files on disk.
- Automatic exponential backoff for HTTP 429 / RESOURCE_EXHAUSTED rate limits.
- Multi-page / tall scrolled question synthesis (multi-image parts).
- Context-aware correction explaining why the previous attempt was mistaken.
- Model: gemini-3.8-flash (current generation multimodal reasoning).
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable

import httpx

from .paths import DATA_DIR, ROOT

DEFAULT_MODEL = "gemini-3.8-flash"
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SOLVE_PROMPT_TEMPLATE = """You are an expert exam tutor. Re-examine this practice exam question carefully and provide the definitive correct answer.

{context_note}

{multi_part_note}

Required Response Schema (JSON only):
{{
  "answer_labels": ["A"],
  "answer_text": "text of the correct choice",
  "topic": "Concept tested (2-5 words)",
  "explanation": "2-4 concise sentences explaining why this answer is correct and the underlying rule or calculation.",
  "why_previous_wrong": "Explanation of why the previous attempt was incorrect, if applicable (or empty string)."
}}
"""


class GeminiError(Exception):
    pass


class GeminiRateLimitError(GeminiError):
    pass


def get_api_key(settings_path: Path | None = None) -> str | None:
    """Find Gemini API key in env, .env, or exam-review / hub settings."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key and key.strip():
        return key.strip()

    # Check ROOT / .env or DATA_DIR / .env
    for env_path in (ROOT / ".env", DATA_DIR / ".env"):
        if env_path.exists():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    k, v = k.strip(), v.strip().strip("\"'")
                    if k in ("GEMINI_API_KEY", "GOOGLE_API_KEY") and v:
                        return v
            except OSError:
                pass

    # Check exam-review.json
    paths_to_check = [settings_path] if settings_path else [DATA_DIR / "exam-review.json", DATA_DIR / "hub-settings.json"]
    for p in paths_to_check:
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                k = data.get("gemini_api_key")
                if k and isinstance(k, str) and k.strip():
                    return k.strip()
            except (OSError, ValueError):
                pass
    return None


def call_with_retry(func: Callable[[], Any], max_retries: int = 3, base_delay: float = 3.0) -> Any:
    """Executes func with exponential backoff on 429 rate limit errors."""
    for attempt in range(max_retries):
        try:
            return func()
        except GeminiRateLimitError as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * base_delay
                time.sleep(wait_time)
            else:
                raise e
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 429:
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * base_delay
                    time.sleep(wait_time)
                else:
                    raise GeminiRateLimitError(f"Gemini API rate limit exceeded (429): {e}") from e
            else:
                raise GeminiError(f"HTTP error {e.response.status_code}: {e}") from e
        except Exception as e:
            err_str = str(e).lower()
            if ("429" in err_str or "resource_exhausted" in err_str or "quota" in err_str) and attempt < max_retries - 1:
                wait_time = (2 ** attempt) * base_delay
                time.sleep(wait_time)
            else:
                raise


def image_to_base64(img_bytes: bytes) -> str:
    """Ensures in-memory bytes are base64-encoded with zero disk I/O."""
    buf = io.BytesIO(img_bytes)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def solve_with_gemini(
    images: list[bytes],
    *,
    api_key: str | None = None,
    previous_item: dict | None = None,
    question_text: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = 30.0,
    client: httpx.Client | None = None,
) -> dict:
    """Sends question images directly in-memory to Gemini API and returns structured solution.
    
    Args:
        images: List of raw PNG bytes (supports multi-page / scrolling parts).
        api_key: Gemini API key (discovered if None).
        previous_item: Prior question dictionary if re-evaluating a bad answer.
        question_text: Optional transcription text if available.
        model: Gemini model identifier (defaults to gemini-3.8-flash).
        timeout: Request timeout in seconds.
        client: Optional httpx.Client for testing / connection reuse.
    """
    key = api_key or get_api_key()
    if not key:
        raise GeminiError(
            "GEMINI_API_KEY is not configured. Set it in your environment or data\\exam-review.json."
        )
    if not images:
        raise GeminiError("No image provided to Gemini solver.")

    # Multi-page / tall scrolling note
    if len(images) > 1:
        multi_part_note = (
            f"NOTE: The question was tall and required scrolling. {len(images)} consecutive images "
            "are attached representing the complete question, diagram, and choices. "
            "Synthesize all parts together."
        )
    else:
        multi_part_note = ""

    # Bad answer / correction context
    if previous_item:
        prev_ans = previous_item.get("answer") or previous_item.get("correct") or "(none)"
        prev_q = previous_item.get("question") or question_text or ""
        context_note = (
            f"CONTEXT: A previous local AI attempt suggested the following answer: '{prev_ans}'.\n"
            "The user marked this answer as INCORRECT or dubious.\n"
            "Carefully re-read every option, check for tricky wording, negations (e.g. 'NOT', 'EXCEPT'), "
            "or diagram details, and find the true correct choice."
        )
        if prev_q:
            context_note += f"\nTranscribed Question: {prev_q}"
    elif question_text:
        context_note = f"Transcribed Question: {question_text}"
    else:
        context_note = "Transcribe the question and all choices from the image, and solve it."

    prompt_text = SOLVE_PROMPT_TEMPLATE.format(
        context_note=context_note,
        multi_part_note=multi_part_note,
    ).strip()

    parts: list[dict] = []
    for img in images:
        parts.append({
            "inline_data": {
                "mime_type": "image/png",
                "data": image_to_base64(img),
            }
        })
    parts.append({"text": prompt_text})

    payload = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "temperature": 0.2,
            "maxOutputTokens": 2048,
        },
    }

    url = f"{API_URL_TEMPLATE.format(model=model)}?key={key}"

    def _execute() -> dict:
        local_client = client or httpx.Client(timeout=timeout)
        try:
            resp = local_client.post(url, json=payload)
            if resp.status_code == 429:
                raise GeminiRateLimitError(f"Rate limited (429): {resp.text}")
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise GeminiError(f"No response candidates returned: {data}")
            raw_text = candidates[0].get("content", {}).get("parts", [{}])[0].get("text", "")
            return _clean_and_parse_json(raw_text)
        finally:
            if client is None:
                local_client.close()

    return call_with_retry(_execute)


def _clean_and_parse_json(text: str) -> dict:
    """Strips markdown fences and parses JSON securely."""
    clean = text.strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean.strip())
    except json.JSONDecodeError as e:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            return json.loads(match.group(0))
        raise GeminiError(f"Failed to parse Gemini response as JSON: {text[:200]}") from e
