"""Runtime configuration, read from the environment with sensible defaults.

The model runs over the OpenAI-compatible chat-completions protocol, so the app works
with **any provider that speaks it** — pick one with `LLM_PROVIDER`:

- `openrouter` (default): one key, any model OpenRouter serves — set `LLM_MODEL` to its
  slug ('anthropic/claude-opus-4-8', 'openai/gpt-5', 'x-ai/grok-4', 'google/gemini-2.5-pro').
- `anthropic`: talk to the Claude API directly with an `sk-ant-…` key (its OpenAI-compat
  endpoint), e.g. `LLM_MODEL=claude-opus-4-8`.

Configure with the provider-neutral `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`
(the older `OPENROUTER_*` names still work as aliases). Whatever the provider, the model
must support tool/function calling — that is the mechanism the whole protocol loop relies
on. Switching providers is a change of `LLM_PROVIDER` + `LLM_API_KEY` (+ `LLM_MODEL`);
the base URL and provider-specific params follow automatically.
"""

from __future__ import annotations

import os


def _first(*vals: str, default: str = "") -> str:
    """First non-empty value (provider-neutral name, then OPENROUTER_* alias, then default)."""
    for v in vals:
        if v:
            return v
    return default


# --- LLM provider ----------------------------------------------------------
# Which OpenAI-compatible provider to talk to. Extend _PROVIDER_DEFAULTS to add more.
LLM_PROVIDER = (os.environ.get("LLM_PROVIDER", "openrouter").strip().lower() or "openrouter")

_PROVIDER_DEFAULTS = {
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "anthropic/claude-opus-4-8"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "model": "claude-opus-4-8"},
}
_defaults = _PROVIDER_DEFAULTS.get(LLM_PROVIDER, _PROVIDER_DEFAULTS["openrouter"])

# Canonical attrs keep the OPENROUTER_* names (referenced across the app), but resolve
# from the provider-neutral LLM_* names first, then the OPENROUTER_* aliases, then the
# selected provider's default.
OPENROUTER_API_KEY = _first(os.environ.get("LLM_API_KEY", ""), os.environ.get("OPENROUTER_API_KEY", ""))
OPENROUTER_BASE_URL = _first(
    os.environ.get("LLM_BASE_URL", ""), os.environ.get("OPENROUTER_BASE_URL", ""),
    default=_defaults["base_url"],
)
MODEL = _first(
    os.environ.get("LLM_MODEL", ""), os.environ.get("OPENROUTER_MODEL", ""),
    default=_defaults["model"],
)
# Optional attribution headers OpenRouter surfaces on your dashboard (OpenRouter only).
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "")
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "Methods Gap-Filler")

# Output token ceiling per response.
MAX_TOKENS = int(os.environ.get("GAPFILLER_MAX_TOKENS", "16000"))
# Per-request HTTP timeout (seconds). Reasoning models can take a while.
REQUEST_TIMEOUT = float(os.environ.get("GAPFILLER_REQUEST_TIMEOUT", "600"))
# Reasoning effort, passed as OpenRouter's `reasoning.effort` for models that support
# it (ignored otherwise). Set to "" to omit the field entirely.
REASONING_EFFORT = os.environ.get("GAPFILLER_REASONING_EFFORT", "high")
# `reasoning.effort` is an OpenRouter extension; other providers' compat endpoints (e.g.
# Anthropic's) can reject an unknown field, so only send it on OpenRouter by default.
# Override with GAPFILLER_SEND_REASONING=1/0 if your gateway differs.
SEND_REASONING = (
    os.environ.get("GAPFILLER_SEND_REASONING", "1" if LLM_PROVIDER == "openrouter" else "0") != "0"
)
# Safety valve against a model that loops on tool calls without ever finishing.
MAX_TOOL_ROUNDS = int(os.environ.get("GAPFILLER_MAX_TOOL_ROUNDS", "16"))

# --- Literature grounding (provider-agnostic; run by this app) --------------
# These are app-run client tools against public APIs — they work regardless of
# which LLM is behind OpenRouter. Each can be disabled.
ENABLE_PUBMED = os.environ.get("GAPFILLER_ENABLE_PUBMED", "1") != "0"
# Europe PMC preprint search (bioRxiv/medRxiv) — no auth.
ENABLE_PREPRINTS = os.environ.get("GAPFILLER_ENABLE_PREPRINTS", "1") != "0"

# Optional NCBI API key raises the E-utilities rate limit (3 -> 10 req/s).
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

# protocols.io grounding needs a developer token; the tool is offered only when set.
PROTOCOLS_IO_TOKEN = os.environ.get("PROTOCOLS_IO_TOKEN", "")
ENABLE_PROTOCOLS_IO = (
    os.environ.get("GAPFILLER_ENABLE_PROTOCOLS_IO", "1") != "0"
) and bool(PROTOCOLS_IO_TOKEN)

# Max literature searches the agent may run per phase, and max hits per search.
PUBMED_BUDGET = int(os.environ.get("GAPFILLER_PUBMED_BUDGET", "12"))
PUBMED_RETMAX_CAP = int(os.environ.get("GAPFILLER_PUBMED_RETMAX", "8"))

# --- Access control (all OFF by default so local dev is unaffected) ---------
# When set, the /api/* endpoints require this token (Authorization: Bearer,
# X-API-Key header, or ?t= query param for download links). Protects the
# token-spending endpoints from arbitrary callers.
AUTH_TOKEN = os.environ.get("GAPFILLER_AUTH_TOKEN", "")
# Per-client requests/minute on the model-driving endpoints; 0 disables.
RATE_LIMIT = int(os.environ.get("GAPFILLER_RATE_LIMIT", "0"))
# Only trust X-Forwarded-For for the client IP when behind a proxy you control.
TRUST_PROXY = os.environ.get("GAPFILLER_TRUST_PROXY", "0") != "0"
