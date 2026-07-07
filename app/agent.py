"""The three-phase tool-use loop.

Phase 1 (analyze): system + Methods text, with web_search + request_clarifications
available under tool_choice=auto. The model scopes ambiguous gaps with a light
search, then calls request_clarifications — that call is the terminal signal.

Phase 2/3 (continue_with_answers): the user's answers come back as the tool_result
for request_clarifications; the model grounds values with web_search (capped by the
tool's max_uses) and then calls emit_protocol. If it stops without emitting, we nudge
once with only emit_protocol available (auto — never a forced tool_choice on a call
that is also meant to search).

Sessions are just the running `messages` list plus the request_clarifications id.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import anthropic

from . import config
from .prompts import SYSTEM_PROMPT
from .schemas import EMIT_PROTOCOL_TOOL, REQUEST_CLARIFICATIONS_TOOL


class AgentError(RuntimeError):
    """Raised when the model does not follow the tool contract."""


@dataclass
class Session:
    messages: list = field(default_factory=list)
    request_tool_use_id: Optional[str] = None
    phase1: Optional[dict] = None


def _web_search_tool() -> dict:
    return {
        "type": config.WEB_SEARCH_TYPE,
        "name": "web_search",
        "max_uses": config.SEARCH_BUDGET,
    }


def _find_tool_use(content: list, name: str) -> Optional[Any]:
    for block in content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == name:
            return block
    return None


class GapFillerAgent:
    def __init__(self, client: Optional[anthropic.Anthropic] = None, model: Optional[str] = None):
        self.client = client or anthropic.Anthropic()
        self.model = model or config.MODEL
        self.system = [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]

    def _call(self, messages: list, tools: list) -> Any:
        """One model turn, resuming automatically across server-tool pauses."""
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
                continue  # server tool (web_search) hit its per-call loop cap; resume
            return resp

    # -- Phase 1 ----------------------------------------------------------------
    def analyze(self, methods_text: str) -> Session:
        session = Session()
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
        resp = self._call(
            session.messages, tools=[_web_search_tool(), REQUEST_CLARIFICATIONS_TOOL]
        )
        block = _find_tool_use(resp.content, "request_clarifications")
        if block is None:
            raise AgentError(
                "Phase 1 ended without calling request_clarifications "
                f"(stop_reason={resp.stop_reason})."
            )
        session.request_tool_use_id = block.id
        session.phase1 = dict(block.input)
        return session

    # -- Phase 2 + 3 ------------------------------------------------------------
    def continue_with_answers(self, session: Session, answers: list) -> dict:
        if session.request_tool_use_id is None:
            raise AgentError("Session has no pending clarification to answer.")

        payload = json.dumps({"answers": answers})
        session.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": session.request_tool_use_id,
                        "content": payload,
                    }
                ],
            }
        )
        # Prevent a second answer submission against the same clarification.
        session.request_tool_use_id = None

        resp = self._call(
            session.messages, tools=[_web_search_tool(), EMIT_PROTOCOL_TOOL]
        )
        block = _find_tool_use(resp.content, "emit_protocol")
        if block is not None:
            return dict(block.input)

        # Model stopped researching without emitting — nudge once, emit-only.
        session.messages.append(
            {
                "role": "user",
                "content": "Your research is complete. Call emit_protocol now with the "
                "finalized, provenance-tagged protocol.",
            }
        )
        resp = self._call(session.messages, tools=[EMIT_PROTOCOL_TOOL])
        block = _find_tool_use(resp.content, "emit_protocol")
        if block is None:
            raise AgentError(
                "Phase 3 ended without calling emit_protocol "
                f"(stop_reason={resp.stop_reason})."
            )
        return dict(block.input)
