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
FALLBACK_MODELS = ("gemini-3.6-flash", "gemini-3-flash-preview")
API_URL_TEMPLATE = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SOLVE_PROMPT_TEMPLATE = """You are an expert exam tutor. Re-examine this practice exam question carefully and provide the definitive correct answer.

{context_note}

{multi_part_note}

Format instructions:
- The question may be multiple-choice, drag-and-drop matching, fill-in-the-blank (CLI command or numeric/text value), or step ordering.
- If there is an exhibit, diagram, routing table (e.g. show ip route), or topology, analyze it thoroughly.
- Classify the question and return the appropriate response fields.

Required Response Schema (JSON only):
{{
  "question_type": "multiple_choice | matching | fill_in_the_blank | ordering",
  "question": "Verbatim question text transcribed from the image",
  "choices": [
    {{"label": "A", "text": "choice text"}}
  ],
  "answer_labels": ["A"],
  "answer_text": "human-readable summary of the answer",
  "matching_pairs": [
    {{"source": "item", "target": "slot/definition"}}
  ],
  "blank_answers": [
    "exact command or value"
  ],
  "ordered_sequence": [
    "1. First step", "2. Second step"
  ],
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
    paths_to_check = ([settings_path] if settings_path else []) + [DATA_DIR / "exam-review.json", DATA_DIR / "hub-settings.json"]
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


def get_model(settings_path: Path | None = None) -> str:
    """Find Gemini model in env or exam-review / hub settings, defaulting to DEFAULT_MODEL."""
    model = os.environ.get("GEMINI_MODEL")
    if model and model.strip():
        return model.strip()

    paths_to_check = ([settings_path] if settings_path else []) + [DATA_DIR / "exam-review.json", DATA_DIR / "hub-settings.json"]
    for p in paths_to_check:
        if p and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                m = data.get("gemini_model")
                if m and isinstance(m, str) and m.strip():
                    return m.strip()
            except (OSError, ValueError):
                pass
    return DEFAULT_MODEL


def call_with_retry(func: Callable[[], Any], max_retries: int = 3, base_delay: float = 3.0) -> Any:
    """Executes func with exponential backoff on 429 rate limit errors and network glitches."""
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
            elif e.response.status_code in (500, 502, 503, 504):
                if attempt < max_retries - 1:
                    wait_time = (2 ** attempt) * base_delay
                    time.sleep(wait_time)
                else:
                    raise GeminiError(f"Gemini API temporarily unavailable ({e.response.status_code}): {e}") from e
            else:
                raise GeminiError(f"HTTP error {e.response.status_code}: {e}") from e
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) * base_delay
                time.sleep(wait_time)
            else:
                raise GeminiError(f"Network error communicating with Gemini API: {e}") from e
        except Exception as e:
            err_str = str(e).lower()
            if ("429" in err_str or "503" in err_str or "resource_exhausted" in err_str or "quota" in err_str or "unavailable" in err_str) and attempt < max_retries - 1:
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
        mime = "image/jpeg" if img.startswith(b"\xff\xd8") else "image/png"
        parts.append({
            "inline_data": {
                "mime_type": mime,
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

    primary_model = get_model() if (not model or model == DEFAULT_MODEL) else model
    models_to_try = [primary_model]
    for fb in FALLBACK_MODELS:
        if fb not in models_to_try:
            models_to_try.append(fb)

    last_err: Exception | None = None
    for attempt_model in models_to_try:
        url = f"{API_URL_TEMPLATE.format(model=attempt_model)}?key={key}"

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
                cand_parts = candidates[0].get("content", {}).get("parts", [])
                raw_text = "".join(p.get("text", "") for p in cand_parts if isinstance(p, dict) and p.get("text"))
                parsed = _clean_and_parse_json(raw_text)
                parsed["model"] = attempt_model
                return parsed
            finally:
                if client is None:
                    local_client.close()

        try:
            return call_with_retry(_execute)
        except (GeminiError, httpx.HTTPError) as e:
            last_err = e
            err_str = str(e).lower()
            is_transient = any(tok in err_str for tok in ("503", "500", "502", "504", "unavailable", "timeout", "timed out", "rate limit"))
            if not is_transient or attempt_model == models_to_try[-1]:
                raise

    if last_err:
        raise last_err
    raise GeminiError("No models available to fulfill request.")


def _scrub(text: str, key: str | None) -> str:
    """Never let the API key reach an error message, a log line or a response."""
    out = str(text)
    if key:
        out = out.replace(key, "***")
    return re.sub(r"([?&]key=)[^&\s'\"]+", r"\1***", out)


def chat_text(
    prompt: str,
    system: str = "",
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = 60.0,
    client: httpx.Client | None = None,
) -> str:
    """Plain text chat with Gemini (the Playground). Returns the reply text; raises GeminiError.
    The key goes in the x-goog-api-key header, not the URL, so it can't show up in an httpx error message."""
    key = api_key or get_api_key()
    if not key:
        raise GeminiError("no Gemini API key set")
    if not str(prompt).strip():
        raise GeminiError("empty prompt")
    use_model = (model or "").strip() or get_model()
    payload: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.7, "maxOutputTokens": 2048},
    }
    if system.strip():
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    url = API_URL_TEMPLATE.format(model=use_model)
    headers = {"x-goog-api-key": key}

    def _execute() -> str:
        local_client = client or httpx.Client(timeout=timeout)
        try:
            resp = local_client.post(url, json=payload, headers=headers)
            if resp.status_code == 429:
                raise GeminiRateLimitError("Rate limited (429)")
            resp.raise_for_status()
            data = resp.json()
            candidates = data.get("candidates") or []
            if not candidates:
                raise GeminiError("Gemini returned no answer (blocked or empty)")
            cand_parts = (candidates[0].get("content") or {}).get("parts") or []
            return "".join(p.get("text", "") for p in cand_parts if isinstance(p, dict) and p.get("text") and not p.get("thought"))
        finally:
            if client is None:
                local_client.close()

    try:
        return call_with_retry(_execute, max_retries=2)
    except GeminiError as e:
        raise GeminiError(_scrub(str(e), key)) from None
    except httpx.HTTPError as e:
        raise GeminiError(_scrub(f"{type(e).__name__}: {e}", key)) from None


def _clean_and_parse_json(text: str) -> dict:
    """Strips markdown fences and parses JSON securely."""
    clean = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", clean, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", clean)
        clean = re.sub(r"\s*```$", "", clean)
    try:
        return json.loads(clean.strip())
    except json.JSONDecodeError as e:
        match = re.search(r"\{.*\}", clean, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                pass
        raise GeminiError(f"Failed to parse Gemini response as JSON: {text[:200]}") from e
