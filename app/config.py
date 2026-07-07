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
