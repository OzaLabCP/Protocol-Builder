"""Runtime configuration, read from the environment with sensible defaults.

The model runs through OpenRouter's OpenAI-compatible API, so ANY model OpenRouter
serves works — set OPENROUTER_MODEL to its slug (e.g. 'anthropic/claude-opus-4-8',
'openai/gpt-5', 'x-ai/grok-4', 'google/gemini-2.5-pro'). The model must support
tool/function calling — that is the mechanism the whole protocol loop relies on.
"""

from __future__ import annotations

import os

# --- LLM provider (OpenRouter) ---------------------------------------------
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
# Model slug, provider-prefixed. Default to a strong reasoning model; override freely.
MODEL = os.environ.get("OPENROUTER_MODEL", "anthropic/claude-opus-4-8")
# Optional attribution headers OpenRouter surfaces on your dashboard.
OPENROUTER_REFERER = os.environ.get("OPENROUTER_REFERER", "")
OPENROUTER_TITLE = os.environ.get("OPENROUTER_TITLE", "Methods Gap-Filler")

# Output token ceiling per response.
MAX_TOKENS = int(os.environ.get("GAPFILLER_MAX_TOKENS", "16000"))
# Per-request HTTP timeout (seconds). Reasoning models can take a while.
REQUEST_TIMEOUT = float(os.environ.get("GAPFILLER_REQUEST_TIMEOUT", "600"))
# Reasoning effort, passed as OpenRouter's `reasoning.effort` for models that support
# it (ignored otherwise). Set to "" to omit the field entirely.
REASONING_EFFORT = os.environ.get("GAPFILLER_REASONING_EFFORT", "high")
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
