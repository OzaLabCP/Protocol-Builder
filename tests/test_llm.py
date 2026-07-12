"""OpenRouter client tests — payload shape, auth guard, error handling. No network:
the internal httpx client is replaced with a stub that captures the request."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import config  # noqa: E402
from app.llm import LLMError, OpenRouterClient  # noqa: E402


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = str(payload)

    def json(self):
        return self._payload


class _StubHTTP:
    def __init__(self, resp):
        self.resp = resp
        self.last = None

    def post(self, url, json=None, headers=None):
        self.last = {"url": url, "json": json, "headers": headers}
        return self.resp


def _client(resp, api_key="sk-or-test"):
    c = OpenRouterClient(api_key=api_key, model="vendor/model-x")
    c._client = _StubHTTP(resp)
    return c


def test_missing_key_raises_before_any_request():
    c = OpenRouterClient(api_key="")
    try:
        c.chat(messages=[{"role": "user", "content": "hi"}])
        assert False, "expected LLMError"
    except LLMError as e:
        assert "OPENROUTER_API_KEY" in str(e)


def test_payload_and_headers_shape():
    c = _client(_Resp(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
    tools = [{"type": "function", "function": {"name": "t", "parameters": {"type": "object"}}}]
    out = c.chat(messages=[{"role": "user", "content": "hi"}], tools=tools, tool_choice="auto")
    req = c._client.last
    assert req["url"].endswith("/chat/completions")
    body = req["json"]
    assert body["model"] == "vendor/model-x"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["tools"] == tools and body["tool_choice"] == "auto"
    assert body["max_tokens"] == config.MAX_TOKENS
    # reasoning.effort is included when configured AND the provider accepts it
    if config.SEND_REASONING and config.REASONING_EFFORT:
        assert body["reasoning"] == {"effort": config.REASONING_EFFORT}
    assert req["headers"]["Authorization"] == "Bearer sk-or-test"
    assert out["choices"][0]["message"]["content"] == "ok"


def test_non_2xx_raises():
    c = _client(_Resp(429, {"error": "rate limited"}))
    try:
        c.chat(messages=[{"role": "user", "content": "hi"}])
        assert False, "expected LLMError"
    except LLMError as e:
        assert "429" in str(e)


def test_error_body_is_not_leaked():
    # The provider's response body can echo the prompt/key hints — it must never appear
    # in the exception (which flows up into client-facing errors). Only the code survives.
    secret = "SECRET-PROMPT-ECHO-do-not-leak"
    c = _client(_Resp(403, {"error": secret}))
    try:
        c.chat(messages=[{"role": "user", "content": "hi"}])
        assert False, "expected LLMError"
    except LLMError as e:
        assert secret not in str(e)
        assert "403" in str(e)


def test_inline_error_object_raises():
    c = _client(_Resp(200, {"error": {"message": "bad", "code": 400}}))
    try:
        c.chat(messages=[{"role": "user", "content": "hi"}])
        assert False, "expected LLMError"
    except LLMError as e:
        assert "error" in str(e).lower()


def test_effort_override_per_call():
    # explicit effort overrides the global; "" omits reasoning for that call
    c = _client(_Resp(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
    if config.SEND_REASONING:
        c.chat(messages=[{"role": "user", "content": "hi"}], effort="low")
        assert c._client.last["json"]["reasoning"] == {"effort": "low"}
        c.chat(messages=[{"role": "user", "content": "hi"}], effort="")
        assert "reasoning" not in c._client.last["json"]  # empty effort -> omitted


def test_reasoning_omitted_when_provider_disables_it():
    # e.g. Anthropic-direct: the OpenRouter-only reasoning field must not be sent.
    orig = config.SEND_REASONING
    config.SEND_REASONING = False
    try:
        c = _client(_Resp(200, {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}))
        c.chat(messages=[{"role": "user", "content": "hi"}])
        assert "reasoning" not in c._client.last["json"]
    finally:
        config.SEND_REASONING = orig


def test_provider_config_resolution_and_aliases():
    import importlib

    import app.config as cfg
    orig_env = dict(os.environ)
    keys = ("LLM_PROVIDER", "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL",
            "OPENROUTER_API_KEY", "OPENROUTER_BASE_URL", "OPENROUTER_MODEL",
            "GAPFILLER_SEND_REASONING")
    try:
        for k in keys:
            os.environ.pop(k, None)
        # anthropic-direct: base_url + model default to Anthropic; reasoning gated off.
        os.environ["LLM_PROVIDER"] = "anthropic"
        os.environ["LLM_API_KEY"] = "sk-ant-xyz"
        importlib.reload(cfg)
        assert cfg.LLM_PROVIDER == "anthropic"
        assert cfg.OPENROUTER_BASE_URL == "https://api.anthropic.com/v1"
        assert cfg.MODEL == "claude-opus-4-8"
        assert cfg.OPENROUTER_API_KEY == "sk-ant-xyz"  # LLM_API_KEY resolves the canonical attr
        assert cfg.SEND_REASONING is False
        # openrouter: defaults restore, OPENROUTER_* alias still resolves, reasoning on.
        for k in keys:
            os.environ.pop(k, None)
        os.environ["LLM_PROVIDER"] = "openrouter"
        os.environ["OPENROUTER_API_KEY"] = "sk-or-abc"  # legacy alias
        importlib.reload(cfg)
        assert cfg.OPENROUTER_BASE_URL == "https://openrouter.ai/api/v1"
        assert cfg.MODEL == "anthropic/claude-opus-4-8"
        assert cfg.OPENROUTER_API_KEY == "sk-or-abc"
        assert cfg.SEND_REASONING is True
    finally:
        os.environ.clear()
        os.environ.update(orig_env)
        importlib.reload(cfg)


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
