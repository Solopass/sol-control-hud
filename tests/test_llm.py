import json

import httpx
import pytest

from sol_control_hud.chains.llm import LLMError, OllamaNativeClient, load_registry, parse_native

REG = {
    "sol-fast": {"base": "gpt-oss:20b", "num_ctx": 32768, "think": "medium"},
    "sol-coder": {"base": "qwen3-coder:30b", "num_ctx": 32768, "think": None},
}
SCHEMA = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}


def client(installed=frozenset({"gpt-oss:20b", "qwen3-coder:30b"})):
    return OllamaNativeClient(registry=REG, installed=set(installed))


def test_body_uses_native_options_schema_and_registry_ctx():
    body = client().build_body("sol-fast", [{"role": "user", "content": "hi"}], schema=SCHEMA, max_tokens=512, seed=7)
    assert body["model"] == "gpt-oss:20b"  # alias not created yet -> base
    assert body["options"] == {"num_predict": 512, "num_ctx": 32768, "seed": 7}
    assert body["format"] == SCHEMA and body["think"] == "medium" and body["stream"] is False
    assert "response_format" not in body and "max_tokens" not in body


def test_alias_used_once_it_exists_and_think_none_is_omitted():
    c = client({"sol-coder", "qwen3-coder:30b"})
    body = c.build_body("sol-coder", [])
    assert body["model"] == "sol-coder" and "think" not in body


def test_unknown_model_passes_through_without_ctx():
    body = client().build_body("llama3:8b", [])
    assert body["model"] == "llama3:8b" and "num_ctx" not in body["options"]


def test_one_ctx_per_model_whatever_the_caller_asks():
    c = client()
    ctxs = {c.build_body("sol-fast", [], max_tokens=m)["options"]["num_ctx"] for m in (100, 8192, 16000)}
    assert ctxs == {32768}  # no reload-triggering ctx changes (I15)


def test_parse_native_response_including_tool_calls_and_length():
    data = {"model": "gpt-oss:20b", "done": True, "done_reason": "length", "prompt_eval_count": 40, "eval_count": 512,
            "message": {"role": "assistant", "content": "", "thinking": "hmm",
                        "tool_calls": [{"function": {"name": "get_order", "arguments": {"id": "A1"}}}]}}
    r = parse_native(data)
    assert (r.finish_reason, r.prompt_tokens, r.completion_tokens, r.thinking) == ("length", 40, 512, "hmm")
    assert r.tool_calls[0]["function"]["name"] == "get_order"
    assert json.loads(r.tool_calls[0]["function"]["arguments"]) == {"id": "A1"}


def test_http_failure_becomes_llm_error(monkeypatch):
    def boom(*a, **k):
        raise httpx.ConnectError("refused")
    monkeypatch.setattr(httpx, "post", boom)
    with pytest.raises(LLMError, match="ConnectError"):
        client().chat("sol-fast", [{"role": "user", "content": "x"}])


def test_oversized_prompt_is_refused_before_ollama_silently_truncates_it(monkeypatch):
    called = []
    monkeypatch.setattr(httpx, "post", lambda *a, **k: called.append(1))
    small = {"sol-fast": {"base": "gpt-oss:20b", "num_ctx": 16384, "think": None}}
    c = OllamaNativeClient(registry=small, installed={"gpt-oss:20b"})
    doc = "Speaker 1: filler line about the coffee machine.\n" * 1100   # ~55k chars, like the Extract20 transcript
    with pytest.raises(LLMError, match="prompt too big"):
        c.chat("sol-fast", [{"role": "user", "content": doc}], max_tokens=4096)
    assert not called  # never sent


def test_prompt_that_fits_is_sent_even_if_answer_budget_is_large():
    from sol_control_hud.chains.llm import check_fits
    # measured case: ~16.6k-token prompt + 12288 budget at ctx 24576 worked (short answer)
    check_fits({"model": "m", "messages": [{"role": "user", "content": "x" * 50000}], "options": {"num_ctx": 24576, "num_predict": 12288}})
    with pytest.raises(LLMError):  # ~16.7k prompt can't fit ctx 16384
        check_fits({"model": "m", "messages": [{"role": "user", "content": "x" * 50000}], "options": {"num_ctx": 16384, "num_predict": 12288}})


def test_real_registry_file_is_consistent():
    reg = load_registry()
    if not reg:
        pytest.skip("models.json not present")
    for name, cfg in reg.items():
        assert name.startswith("sol-") and cfg["base"] and isinstance(cfg["num_ctx"], int), name
        assert cfg["think"] in (None, True, False, "low", "medium", "high"), name
