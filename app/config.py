"""Runtime configuration, read from the environment with sensible defaults."""

from __future__ import annotations

import os

# The model that runs the protocol-engineering loop. Opus 4.8 is the default:
# adaptive-thinking only, strong scientific reasoning, supports web_search.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

# Cap on Phase-2 web searches (enforced via the web_search tool's max_uses).
SEARCH_BUDGET = int(os.environ.get("GAPFILLER_SEARCH_BUDGET", "12"))

# Output token ceiling. Kept under the SDK's non-streaming timeout guard.
MAX_TOKENS = int(os.environ.get("GAPFILLER_MAX_TOKENS", "16000"))

# Reasoning effort for the loop.
EFFORT = os.environ.get("GAPFILLER_EFFORT", "high")

# The web_search server-tool version with dynamic filtering (Opus 4.6+ / 4.7 / 4.8).
WEB_SEARCH_TYPE = os.environ.get("GAPFILLER_WEB_SEARCH_TYPE", "web_search_20260209")

# Grounding tools. web_search is Anthropic-run; search_pubmed is run by this app
# against NCBI E-utilities (real PMIDs/DOIs). Either can be disabled.
ENABLE_WEB_SEARCH = os.environ.get("GAPFILLER_ENABLE_WEB_SEARCH", "1") != "0"
ENABLE_PUBMED = os.environ.get("GAPFILLER_ENABLE_PUBMED", "1") != "0"

# Optional NCBI API key raises the E-utilities rate limit (3 -> 10 req/s).
NCBI_API_KEY = os.environ.get("NCBI_API_KEY", "")

# Max PubMed searches the agent may run per phase, and max hits per search.
PUBMED_BUDGET = int(os.environ.get("GAPFILLER_PUBMED_BUDGET", "12"))
PUBMED_RETMAX_CAP = int(os.environ.get("GAPFILLER_PUBMED_RETMAX", "8"))
