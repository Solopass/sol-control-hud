"""Client for the Local AI v2 engine: the llama.cpp router, OpenAI /v1 API (127.0.0.1:11440).

OBVLT plans\\LOCAL_AI_AUTOMATION_PLAN.md phase 1.1. Measured on llama.cpp b11175 (2026-09-25):
- `reasoning_effort` "none" turns Gemma's thinking off and sets gpt-oss's effort (gpt-oss can't go below "low");
- a JSON schema works with thinking on, and thinking on was more accurate -> the default is to leave it on;
- POST /tokenize counts tokens exactly; GET /models says what's loaded; GET /slots?model=X says if it's busy.
Model names depend on the mode: Desk serves sol-fast/sol-vision/sol-smart; Away also serves sol-specialist/sol-coder.
Plain workflows (transcript-note) may fall back to sol-smart in Desk like the old translator did; chains never do.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

from .llm import CHARS_PER_TOKEN, ChatResult, LLMError, load_registry

ROUTER_URL = os.environ.get("SOL_ROUTER", "http://127.0.0.1:11440")
STATE_FILE = Path(os.environ.get("SOL_LLM_STATE", r"D:\AI\Cache\llm\state.json"))
DESK_FALLBACK = {"sol-specialist": "sol-smart", "sol-coder": "sol-smart", "sol-coder-next": "sol-smart"}
REASONING = ("off", "on", "low", "medium", "high")
ANSWER_ROOM = 2048        # a thinking model needs room to think before it answers
PER_MESSAGE_TOKENS = 16   # chat template overhead per message


class ModelUnavailable(LLMError):
    """The model isn't served right now (an Away model in Desk mode, or the game guard has the AI off). Wait; don't fail."""


def engine_mode() -> str:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8-sig")).get("mode") or "unknown"
    except (OSError, ValueError):
        return "unknown"


class RouterClient:
    def __init__(self, base_url: str = ROUTER_URL, timeout: float = 900.0, registry: dict[str, dict] | None = None,
                 allow_fallback: bool = False, http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.registry = load_registry() if registry is None else registry
        self.allow_fallback = allow_fallback
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))
        self._models: tuple[float, dict[str, dict]] | None = None

    # ---- what the router serves
    def models(self, max_age: float = 15.0) -> dict[str, dict]:
        """{name or alias: {"id": main name, "status": loaded|unloaded|loading|...}}"""
        if self._models and time.monotonic() - self._models[0] < max_age:
            return self._models[1]
        try:
            data = self.http.get(f"{self.base_url}/models", timeout=10).json().get("data") or []
        except (httpx.HTTPError, ValueError) as e:  # restarting (mode switch, wake) or down: wait, don't fail
            raise ModelUnavailable(f"the engine isn't reachable right now ({type(e).__name__}); it continues when it's back") from e
        out: dict[str, dict] = {}
        for m in data:
            info = {"id": m["id"], "status": (m.get("status") or {}).get("value") or "unknown"}
            for name in [m["id"], *(m.get("aliases") or [])]:
                out[name] = info
        self._models = (time.monotonic(), out)
        return out

    def resolve(self, model: str) -> str:
        if engine_mode() == "off":
            raise ModelUnavailable("the local AI is off (game guard); it comes back by itself after the game")
        served = self.models()
        if model in served:
            return model
        alt = DESK_FALLBACK.get(model)
        if self.allow_fallback and alt in served:
            return alt
        raise ModelUnavailable(f"model '{model}' isn't served in {engine_mode()} mode (Away models run in Away mode)")

    def loaded(self) -> str | None:
        """Main name of the model that is loaded or loading (the router keeps at most one), else None."""
        for info in self.models(max_age=0).values():
            if info["status"] in ("loaded", "loading"):
                return info["id"]
        return None

    def busy(self, model: str) -> bool:
        try:
            slots = self.http.get(f"{self.base_url}/slots", params={"model": model}, timeout=10).json()
            return any(s.get("is_processing") for s in slots) if isinstance(slots, list) else False
        except (httpx.HTTPError, ValueError):
            return False

    # ---- context budget (phase 1.4)
    def context(self, model: str) -> int | None:
        return (self.registry.get(model) or {}).get("num_ctx")

    def count_tokens(self, model: str, text: str) -> int:
        """Exact when the model is already loaded (never load a model just to count); otherwise a safe estimate."""
        try:
            info = self.models().get(model)
            if info and info["status"] == "loaded":
                r = self.http.post(f"{self.base_url}/tokenize", json={"model": model, "content": text}, timeout=60)
                r.raise_for_status()
                return len(r.json().get("tokens") or [])
        except (httpx.HTTPError, ValueError, LLMError):
            pass
        return int(len(text) / CHARS_PER_TOKEN) + 1

    def fits(self, model: str, messages: list[dict], max_tokens: int) -> tuple[bool, int, int | None]:
        """(fits, prompt tokens, context). The prompt plus room to think/answer must fit; max_tokens is only a ceiling."""
        ctx = self.context(model)
        text = "\n".join(str(m.get("content") or "") for m in messages)
        prompt = self.count_tokens(model, text) + PER_MESSAGE_TOKENS * len(messages)
        if not ctx:
            return True, prompt, None
        return prompt + min(max_tokens, ANSWER_ROOM) <= ctx, prompt, ctx

    # ---- chat
    def reasoning_effort(self, model: str, reasoning: str | None) -> str | None:
        if reasoning in (None, "on"):
            return None
        if reasoning not in REASONING:
            raise LLMError(f"reasoning must be one of {', '.join(REASONING)}, not '{reasoning}'")
        if reasoning == "off":
            return "low" if (self.registry.get(model) or {}).get("think") else "none"
        return reasoning

    def build_body(self, model, messages, *, schema=None, max_tokens=8192, seed=None, reasoning=None,
                   temperature=None, stream=True) -> dict:
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": stream}
        if stream:
            body["stream_options"] = {"include_usage": True}
        if seed is not None:
            body["seed"] = seed
        if temperature is not None:
            body["temperature"] = temperature
        effort = self.reasoning_effort(model, reasoning)
        if effort is not None:
            body["reasoning_effort"] = effort
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "strict": True, "schema": schema}}
        return body

    def chat(self, model, messages, *, schema=None, max_tokens=8192, seed=None, tools=None, reasoning=None,
             temperature=None, on_progress=None) -> ChatResult:
        if tools:
            raise LLMError("the router client doesn't do tool calls; use a tool step")
        engine_model = self.resolve(model)
        body = self.build_body(engine_model, messages, schema=schema, max_tokens=max_tokens, seed=seed,
                               reasoning=reasoning, temperature=temperature)
        content: list[str] = []
        thinking: list[str] = []
        finish, usage, pieces, last = None, {}, 0, time.monotonic()
        try:
            with self.http.stream("POST", f"{self.base_url}/v1/chat/completions", json=body) as r:
                if r.status_code >= 400:
                    r.read()
                    if r.status_code in (502, 503):  # the model is (un)loading or the router is switching presets
                        raise ModelUnavailable(f"the engine is busy switching models (HTTP {r.status_code}); try again shortly")
                    raise LLMError(f"HTTP {r.status_code} from the router: {r.text[:300]}")
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    chunk = json.loads(payload)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        if delta.get("content"):
                            content.append(delta["content"]); pieces += 1
                        if delta.get("reasoning_content"):
                            thinking.append(delta["reasoning_content"]); pieces += 1
                        finish = choice.get("finish_reason") or finish
                    if on_progress and time.monotonic() - last >= 5:
                        on_progress(pieces); last = time.monotonic()
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.WriteError) as e:
            # the router restarted mid-answer (Away <-> Desk switch, game guard, wake): the step runs again later
            raise ModelUnavailable(f"the engine stopped mid-answer ({type(e).__name__}); the step runs again when it's back") from e
        except httpx.HTTPError as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        except ValueError as e:
            raise LLMError(f"bad reply from the router: {e}") from e
        return ChatResult(content="".join(content), finish_reason=finish, prompt_tokens=usage.get("prompt_tokens"),
                          completion_tokens=usage.get("completion_tokens"), thinking="".join(thinking), model=engine_model)
