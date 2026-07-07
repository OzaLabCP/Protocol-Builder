"""The three-phase tool-use loop, with literature grounding.

Both phases run under tool_choice=auto with grounding tools + the phase's
structured tool all available; the structured call is the terminal signal, and
forcing is never combined with searching.

Grounding sources:
- web_search: Anthropic-run server tool (results resolved inside the API call).
- search_pubmed: a client tool the app executes against NCBI E-utilities, so the
  agent retrieves real PMIDs/DOIs. The loop dispatches it and feeds results back.

Phase 1 (analyze): scope ambiguous gaps, then call request_clarifications.
Phase 2/3 (continue_with_answers): ground values, then call emit_protocol; if the
model stops without emitting, nudge once with only emit_protocol available.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from . import config, literature
from .prompts import SYSTEM_PROMPT
from .schemas import (
    EMIT_PROTOCOL_TOOL,
    REQUEST_CLARIFICATIONS_TOOL,
    SEARCH_PREPRINTS_TOOL,
    SEARCH_PROTOCOLS_TOOL,
    SEARCH_PUBMED_TOOL,
)

# name -> (tool schema, search-fn attr on `literature`, formatter attr, config flag)
# Functions are referenced by attribute name and resolved at call time so tests can
# monkeypatch app.literature.* and the swap takes effect.
_CLIENT_TOOLS = {
    "search_pubmed": (SEARCH_PUBMED_TOOL, "search_pubmed", "format_results", "ENABLE_PUBMED"),
    "search_preprints": (SEARCH_PREPRINTS_TOOL, "search_preprints", "format_preprints", "ENABLE_PREPRINTS"),
    "search_protocols": (SEARCH_PROTOCOLS_TOOL, "search_protocols", "format_protocols", "ENABLE_PROTOCOLS_IO"),
}


class AgentError(RuntimeError):
    """Raised when the model does not follow the tool contract."""


@dataclass
class Session:
    messages: list = field(default_factory=list)
    request_tool_use_id: Optional[str] = None
    phase1: Optional[dict] = None
    grounding_log: list = field(default_factory=list)  # queries the app ran


def _web_search_tool() -> dict:
    return {
        "type": config.WEB_SEARCH_TYPE,
        "name": "web_search",
        "max_uses": config.SEARCH_BUDGET,
    }


def _enabled_client_tools() -> dict:
    """{name: (search_fn, formatter)} for every grounding tool switched on.
    Resolves fns from the literature module at call time (monkeypatch-friendly)."""
    return {
        name: (getattr(literature, fn_attr), getattr(literature, fmt_attr))
        for name, (_schema, fn_attr, fmt_attr, flag) in _CLIENT_TOOLS.items()
        if getattr(config, flag)
    }


def _grounding_tools() -> list:
    tools = []
    if config.ENABLE_WEB_SEARCH:
        tools.append(_web_search_tool())
    for _name, (schema, _fn, _fmt, flag) in _CLIENT_TOOLS.items():
        if getattr(config, flag):
            tools.append(schema)
    return tools


def _tool_use_blocks(content: list) -> list:
    return [b for b in content if getattr(b, "type", None) == "tool_use"]


def _find_tool_use(content: list, name: str) -> Optional[Any]:
    for b in _tool_use_blocks(content):
        if getattr(b, "name", None) == name:
            return b
    return None


class GapFillerAgent:
    def __init__(self, client: Optional[anthropic.Anthropic] = None, model: Optional[str] = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or config.MODEL
        self.system = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]
        self._searches_left = 0
        self._session: Optional[Session] = None

    # -- client-tool dispatch ---------------------------------------------------
    def _dispatch(self, name: str, tool_input: dict) -> str:
        enabled = _enabled_client_tools()
        if name not in enabled:
            return f"Unknown or disabled tool: {name}"
        if self._searches_left <= 0:
            return (
                "Search budget exhausted. Do not search again; fill any remaining "
                "values from best_practice or default_verify and proceed to emit."
            )
        self._searches_left -= 1
        search_fn, formatter = enabled[name]
        query = str(tool_input.get("query", "")).strip()
        if self._session is not None:
            self._session.grounding_log.append(f"{name}: {query}")
        try:
            results = search_fn(query, tool_input.get("retmax", 5))
        except Exception as exc:  # noqa: BLE001
            return (
                f"{name} failed ({type(exc).__name__}). Fill from best_practice or "
                f"default_verify instead; do not fabricate a citation."
            )
        return formatter(results)

    # -- one terminal-seeking run -----------------------------------------------
    def _run(self, messages: list, tools: list, terminal_name: str) -> Optional[Any]:
        """Loop until the model calls `terminal_name`. Resumes across server-tool
        pauses and executes client tools (search_pubmed) in between. Returns the
        terminal tool_use block, or None if the model ended without calling it."""
        while True:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=config.MAX_TOKENS,
                system=self.system,
                messages=messages,
                tools=tools,
                thinking={"type": "adaptive"},
                output_config={"effort": config.EFFORT},
            )
            messages.append({"role": "assistant", "content": resp.content})

            if resp.stop_reason == "pause_turn":
                continue  # server tool (web_search) hit its per-call cap; resume
            if resp.stop_reason != "tool_use":
                return None  # ended without a tool call

            terminal = _find_tool_use(resp.content, terminal_name)
            if terminal is not None:
                return terminal

            # Execute any client tools and feed results back.
            results = []
            for b in _tool_use_blocks(resp.content):
                if b.name in self._client_tool_names():
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": b.id,
                            "content": self._dispatch(b.name, dict(b.input)),
                        }
                    )
            if not results:
                return None  # tool_use we don't handle and not terminal — bail
            messages.append({"role": "user", "content": results})

    def _client_tool_names(self) -> set:
        return set(_enabled_client_tools().keys())

    # -- Phase 1 ----------------------------------------------------------------
    def analyze(self, methods_text: str) -> Session:
        session = Session()
        self._session = session
        self._searches_left = config.PUBMED_BUDGET
        session.messages.append(
            {
                "role": "user",
                "content": (
                    "Here is a published Methods section. Reconstruct the protocol, "
                    "classify every parameter, scope any ambiguous gaps with a light "
                    "literature search, then call request_clarifications.\n\n"
                    "=== METHODS ===\n" + methods_text.strip()
                ),
            }
        )
        tools = _grounding_tools() + [REQUEST_CLARIFICATIONS_TOOL]
        block = self._run(session.messages, tools, "request_clarifications")
        if block is None:
            raise AgentError("Phase 1 ended without calling request_clarifications.")
        session.request_tool_use_id = block.id
        session.phase1 = dict(block.input)
        return session

    # -- Phase 2 + 3 ------------------------------------------------------------
    def continue_with_answers(self, session: Session, answers: list) -> dict:
        if session.request_tool_use_id is None:
            raise AgentError("Session has no pending clarification to answer.")
        self._session = session
        self._searches_left = config.PUBMED_BUDGET

        session.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": session.request_tool_use_id,
                        "content": json.dumps({"answers": answers}),
                    }
                ],
            }
        )
        session.request_tool_use_id = None  # prevent a second answer submission

        tools = _grounding_tools() + [EMIT_PROTOCOL_TOOL]
        block = self._run(session.messages, tools, "emit_protocol")
        if block is not None:
            return dict(block.input)

        # Model stopped without emitting — nudge once, emit-only (no search tools).
        session.messages.append(
            {
                "role": "user",
                "content": "Your research is complete. Call emit_protocol now with the "
                "finalized, provenance-tagged protocol.",
            }
        )
        block = self._run(session.messages, [EMIT_PROTOCOL_TOOL], "emit_protocol")
        if block is None:
            raise AgentError("Phase 3 ended without calling emit_protocol.")
        return dict(block.input)
