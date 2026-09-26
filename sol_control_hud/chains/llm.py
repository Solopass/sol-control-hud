"""Chat clients for local engines.

`OllamaNativeClient` (default) talks to Ollama's native /api/chat; `OpenAICompatClient` stays for llama-swap.
Lessons baked in (2026-09-13/14 evals and audit):
- Ollama's /v1 API can't set num_ctx and only documents JSON mode, so structured output goes through native
  /api/chat with `format` = JSON schema (SETUP_PLAN I1/I14);
- every model has exactly one num_ctx (D:\\OBVLT\\tools\\models.json) - a different num_ctx reloads the model (I15);
- don't force temperature 0 (thinking models loop) - only the seed is fixed;
- thinking tokens count against max_tokens, so a cut-off answer is reported as `length`, not as a wrong answer.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

REGISTRY_PATH = Path(os.environ.get("SOL_MODELS", r"D:\OBVLT\tools\models.json"))


class LLMError(Exception):
    """The engine could not produce an answer (down, timeout, HTTP error)."""


@dataclass
class ChatResult:
    content: str
    finish_reason: str | None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    tool_calls: list = field(default_factory=list)
    thinking: str = ""
    model: str | None = None  # the engine model actually used (alias resolved)


class ChatClient(Protocol):
    def chat(self, model: str, messages: list[dict], *, schema: dict | None = None,
             max_tokens: int = 8192, seed: int | None = None, tools: list | None = None) -> ChatResult: ...


def load_registry(path: Path = REGISTRY_PATH) -> dict[str, dict]:
    """{name: settings} from models.json; empty if the file is missing (callers then use plain model names)."""
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))["models"]
    except FileNotFoundError:
        return {}


class OllamaNativeClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11434", timeout: float = 1800.0,
                 registry: dict[str, dict] | None = None, installed: set[str] | None = None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.registry = load_registry() if registry is None else registry
        self._installed = installed

    def installed(self) -> set[str]:
        if self._installed is None:
            try:
                tags = httpx.get(f"{self.base_url}/api/tags", timeout=10).json().get("models") or []
            except (httpx.HTTPError, ValueError) as e:
                raise LLMError(f"{type(e).__name__}: {e}") from e
            names = {t["name"] for t in tags}
            self._installed = names | {n.removesuffix(":latest") for n in names}
        return self._installed

    def resolve(self, model: str) -> tuple[str, dict]:
        """Registry name -> (engine model, settings). Uses the sol-* alias once it exists (runbook step 2), else its base."""
        settings = self.registry.get(model)
        if settings is None:
            return model, {}
        return (model if model in self.installed() else settings["base"]), settings

    def build_body(self, model, messages, *, schema=None, max_tokens=8192, seed=None, tools=None) -> dict:
        engine_model, cfg = self.resolve(model)
        options: dict = {"num_predict": max_tokens}
        if cfg.get("num_ctx"):
            options["num_ctx"] = cfg["num_ctx"]
        if seed is not None:
            options["seed"] = seed
        body: dict = {"model": engine_model, "messages": messages, "stream": False, "options": options}
        if cfg.get("think") is not None:
            body["think"] = cfg["think"]
        if schema is not None:
            body["format"] = schema
        if tools:
            body["tools"] = tools
        return body

    def chat(self, model, messages, *, schema=None, max_tokens=8192, seed=None, tools=None) -> ChatResult:
        body = self.build_body(model, messages, schema=schema, max_tokens=max_tokens, seed=seed, tools=tools)
        check_fits(body)
        try:
            r = httpx.post(f"{self.base_url}/api/chat", json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        return parse_native(data)


CHARS_PER_TOKEN = 3.0  # conservative: the eval transcript measured ~3.4 chars/token on qwen3.8
MIN_ANSWER_ROOM = 1024


def estimate_tokens(body: dict) -> int:
    text = sum(len(str(m.get("content") or "")) for m in body.get("messages") or [])
    extra = len(json.dumps(body.get("tools") or [])) + len(json.dumps(body.get("format") or {}))
    return int((text + extra) / CHARS_PER_TOKEN)


def check_fits(body: dict) -> None:
    """Refuse a request whose prompt + answer budget can't fit the model's context.
    Ollama doesn't error on an oversized prompt: it silently cuts it to about half the context, which can drop the
    instructions or the question (seen 2026-09-14: 16.6k-token prompt at num_ctx 16384 -> truncated to 8194 -> HTTP 500)."""
    ctx = (body.get("options") or {}).get("num_ctx")
    if not ctx:
        return
    # Only the prompt must fit: num_predict is a ceiling, not a reservation (a 16.6k prompt + 12k budget worked at
    # ctx 24576 because the answer was short). An answer that runs out of room comes back as done_reason=length.
    prompt = estimate_tokens(body)
    room = min(int(body["options"].get("num_predict") or MIN_ANSWER_ROOM), MIN_ANSWER_ROOM)
    if prompt + room > ctx:
        raise LLMError(f"prompt too big for {body['model']}: ~{prompt} prompt tokens leaves < {room} answer tokens "
                       f"in num_ctx {ctx}; use a model with a larger context or split the input")


def parse_native(data: dict) -> ChatResult:
    msg = data.get("message") or {}
    calls = []
    for c in msg.get("tool_calls") or []:  # native args are objects; normalize to the OpenAI shape the runner expects
        fn = c.get("function") or {}
        args = fn.get("arguments")
        calls.append({"type": "function", "function": {"name": fn.get("name"),
                      "arguments": args if isinstance(args, str) else json.dumps(args or {})}})
    return ChatResult(
        content=msg.get("content") or "",
        finish_reason=data.get("done_reason") or ("stop" if data.get("done") else None),
        prompt_tokens=data.get("prompt_eval_count"),
        completion_tokens=data.get("eval_count"),
        tool_calls=calls,
        thinking=msg.get("thinking") or "",
        model=data.get("model"),
    )


class OpenAICompatClient:
    def __init__(self, base_url: str = "http://127.0.0.1:11440/v1", timeout: float = 1800.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def chat(self, model, messages, *, schema=None, max_tokens=8192, seed=None, tools=None) -> ChatResult:
        body: dict = {"model": model, "messages": messages, "max_tokens": max_tokens, "stream": False}
        if seed is not None:
            body["seed"] = seed
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "output", "strict": True, "schema": schema}}
        if tools:
            body["tools"] = tools
        try:
            r = httpx.post(f"{self.base_url}/chat/completions", json=body, timeout=self.timeout)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise LLMError(f"{type(e).__name__}: {e}") from e
        choice = data["choices"][0]
        msg = choice.get("message") or {}
        usage = data.get("usage") or {}
        return ChatResult(
            content=msg.get("content") or "",
            finish_reason=choice.get("finish_reason"),
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            tool_calls=msg.get("tool_calls") or [],
        )
