"""Provider-agnostic chat client, via OpenRouter's OpenAI-compatible API.

One thin `POST /chat/completions` wrapper. Any model OpenRouter serves works —
the only requirement is tool/function calling, which the protocol loop depends on.
The client is injected into GapFillerAgent, so tests pass a fake with the same
`.chat(...)` surface and never touch the network.
"""

from __future__ import annotations

from typing import Any, Optional

import httpx

from . import config


class LLMError(RuntimeError):
    """Raised on a transport error or a non-2xx response from the provider."""


class OpenRouterClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        timeout: Optional[float] = None,
    ):
        self.api_key = api_key if api_key is not None else config.OPENROUTER_API_KEY
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        self.model = model or config.MODEL
        self.timeout = timeout or config.REQUEST_TIMEOUT
        # HTTP/2 + a keep-alive pool: the loop makes many calls to the same host (model
        # round-trips + grounding), so reusing connections avoids repeated TLS handshakes.
        # http2 falls back to 1.1 automatically if the 'h2' package isn't installed.
        try:
            self._client = httpx.Client(
                timeout=self.timeout, http2=True,
                limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=30.0),
            )
        except Exception:  # noqa: BLE001 — h2 missing, etc.
            self._client = httpx.Client(timeout=self.timeout)

    def chat(
        self,
        messages: list,
        tools: Optional[list] = None,
        tool_choice: Optional[Any] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """Call /chat/completions and return the parsed JSON dict (OpenAI shape). `effort`
        overrides the reasoning.effort for this call (None -> the heavy-phase default);
        pass "" to omit reasoning entirely for a call. `max_tokens` overrides the output
        ceiling for this call (None -> config.MAX_TOKENS)."""
        if not self.api_key:
            raise LLMError(
                "OPENROUTER_API_KEY is not set. Get a key at openrouter.ai and export it."
            )
        payload: dict = {"model": model or self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        mt = max_tokens if max_tokens is not None else config.MAX_TOKENS
        if mt:
            payload["max_tokens"] = mt
        eff = effort if effort is not None else config.REASONING_EFFORT
        if config.SEND_REASONING and eff:
            # OpenRouter's reasoning.effort — honored by reasoning-capable models there.
            # Gated off for providers whose compat endpoint would reject the unknown field.
            payload["reasoning"] = {"effort": eff}

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        # Attribution headers are an OpenRouter nicety; don't send them elsewhere.
        if config.LLM_PROVIDER == "openrouter":
            if config.OPENROUTER_REFERER:
                headers["HTTP-Referer"] = config.OPENROUTER_REFERER
            if config.OPENROUTER_TITLE:
                headers["X-Title"] = config.OPENROUTER_TITLE

        try:
            resp = self._client.post(
                f"{self.base_url}/chat/completions", json=payload, headers=headers
            )
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenRouter request failed ({type(exc).__name__}).") from exc

        # Keep the status code (the host uses it to classify auth failures) but never
        # surface the provider's response body — it can echo the prompt, key hints, or
        # internal detail, and it flows up into client-facing 500s.
        if resp.status_code >= 400:
            raise LLMError(f"OpenRouter returned HTTP {resp.status_code}.")
        try:
            data = resp.json()
        except ValueError as exc:
            raise LLMError(f"OpenRouter returned a non-JSON body (HTTP {resp.status_code}).") from exc
        if data.get("error"):  # OpenRouter can 200 with an inline error object
            raise LLMError(f"OpenRouter returned an error response (HTTP {resp.status_code}).")
        return data
