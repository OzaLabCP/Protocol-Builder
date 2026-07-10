"""The multi-phase tool-use loop, provider-agnostic via OpenRouter.

The model talks to us in OpenAI/OpenRouter shape: it emits `tool_calls`, we answer
each with a `role:"tool"` message, and the structured `emit_*` / `request_*` call is
the terminal signal for each phase. Every phase runs under `tool_choice:"auto"` with
the grounding tools + that phase's structured tool available; the final nudge forces
the terminal tool. Forcing is never combined with searching.

Grounding is done entirely by app-run client tools (search_pubmed/preprints/protocols)
against public APIs, so it works behind ANY model. There is no provider-hosted web
search — that keeps the loop identical across providers.

Phases:
- analyze()               -> request_clarifications   (paper-first, phase 1)
- discover()              -> emit_assay_options        (hypothesis-first, phase 0)
- choose_assay()          -> request_clarifications    (after an assay pick)
- continue_with_answers() -> emit_protocol             (phases 2 + 3)
- revise/design_review/design_alignment() reuse _followup()
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, literature
from .llm import OpenRouterClient
from .prompts import (
    CHOOSE_ASSAY_INSTRUCTION,
    DESIGN_ALIGNMENT_INSTRUCTION,
    DESIGN_REVIEW_INSTRUCTION,
    DISCOVERY_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
)
from .schemas import (
    EMIT_ASSAY_OPTIONS_TOOL,
    EMIT_DESIGN_ALIGNMENT_TOOL,
    EMIT_DESIGN_REVIEW_TOOL,
    EMIT_PROTOCOL_TOOL,
    REQUEST_CLARIFICATIONS_TOOL,
    SEARCH_PREPRINTS_TOOL,
    SEARCH_PROTOCOLS_TOOL,
    SEARCH_PUBMED_TOOL,
    as_openai_tool,
)

# name -> (tool schema, search-fn attr on `literature`, formatter attr, config flag)
# Functions are resolved from `literature` at call time so tests can monkeypatch them.
_CLIENT_TOOLS = {
    "search_pubmed": (SEARCH_PUBMED_TOOL, "search_pubmed", "format_results", "ENABLE_PUBMED"),
    "search_preprints": (SEARCH_PREPRINTS_TOOL, "search_preprints", "format_preprints", "ENABLE_PREPRINTS"),
    "search_protocols": (SEARCH_PROTOCOLS_TOOL, "search_protocols", "format_protocols", "ENABLE_PROTOCOLS_IO"),
}


class AgentError(RuntimeError):
    """Raised when the model does not follow the tool contract."""


@dataclass
class Session:
    messages: list = field(default_factory=list)  # OpenAI-shape turns (no system message)
    request_tool_use_id: Optional[str] = None  # request_clarifications call awaiting answers
    pending_tool_use_id: Optional[str] = None  # last emit_* call awaiting ack (revise/design)
    phase1: Optional[dict] = None
    hypothesis: Optional[str] = None  # what the student wants to test (optional)
    source_kind: str = "paper"  # "paper" or "hypothesis" — set by code, not pasteable text
    source_text: Optional[str] = None  # retained source, for host-side quote verification
    source_exact: bool = True  # True: source_text is exactly what the model read (paste);
    #                            False: lossy host extraction (PDF) — confirm-only, don't accuse
    assay_options: Optional[dict] = None  # validated emit_assay_options payload
    chosen_assay: Optional[dict] = None  # the picked assay dict (for brief + export)
    grounding_log: list = field(default_factory=list)  # queries the app ran


@dataclass
class RunState:
    """Per-call mutable state. Kept off the (shared) agent instance so concurrent
    requests through one GapFillerAgent don't clobber each other's session or budget."""

    session: Session
    searches_left: int


@dataclass
class _ToolCall:
    """A parsed tool call the loop returns to callers (mirrors how they used the old
    Anthropic tool_use block: `.id` and `dict(.input)`)."""

    id: str
    name: str
    input: dict


def _pdf_text(pdf: bytes) -> Optional[str]:
    """Best-effort host-side text extraction from a PDF. The extracted text is what we
    feed the model (so this works behind any provider) AND what the host checks 'stated'
    quotes against. Returns None if no extractor is available or the PDF has no text
    layer (scanned/OCR-free)."""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text if text.strip() else None
    except Exception:  # noqa: BLE001 — extractor missing or PDF unparseable
        return None


def _enabled_client_tools() -> dict:
    """{name: (search_fn, formatter)} for every grounding tool switched on.
    Resolves fns from the literature module at call time (monkeypatch-friendly)."""
    return {
        name: (getattr(literature, fn_attr), getattr(literature, fmt_attr))
        for name, (_schema, fn_attr, fmt_attr, flag) in _CLIENT_TOOLS.items()
        if getattr(config, flag)
    }


def _grounding_tools() -> list:
    """The `{name, description, input_schema}` grounding tools that are enabled."""
    return [schema for _n, (schema, _f, _fmt, flag) in _CLIENT_TOOLS.items() if getattr(config, flag)]


def _coerce_content(content: Any) -> Optional[str]:
    """Assistant content may be a string, null (tool-only turn), or a list of parts
    (some providers). Reduce to a plain string (or None)."""
    if content is None or isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


def _normalize_tool_calls(raw: Any) -> list:
    """Clean the assistant message's tool_calls into a stable shape with guaranteed ids
    (so the tool responses we echo back line up)."""
    out = []
    for i, tc in enumerate(raw or []):
        fn = tc.get("function") or {}
        out.append(
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": fn.get("arguments") or "{}"},
            }
        )
    return out


def _parse_args(arguments: str) -> dict:
    try:
        val = json.loads(arguments or "{}")
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


class GapFillerAgent:
    def __init__(self, client: Optional[OpenRouterClient] = None, model: Optional[str] = None):
        self.client = client or OpenRouterClient()
        self.model = model or config.MODEL
        self.system_prompt = SYSTEM_PROMPT
        self.discovery_prompt = DISCOVERY_SYSTEM_PROMPT

    def _client_tool_names(self) -> set:
        return set(_enabled_client_tools().keys())

    # -- client-tool dispatch ---------------------------------------------------
    def _dispatch(self, name: str, tool_input: dict, state: RunState) -> str:
        enabled = _enabled_client_tools()
        if name not in enabled:
            return f"Unknown or disabled tool: {name}"
        if state.searches_left <= 0:
            return (
                "Search budget exhausted. Do not search again; fill any remaining "
                "values from best_practice or default_verify and proceed to emit."
            )
        state.searches_left -= 1
        search_fn, formatter = enabled[name]
        query = str(tool_input.get("query", "")).strip()
        state.session.grounding_log.append(f"{name}: {query}")
        try:
            results = search_fn(query, tool_input.get("retmax", 5))
        except Exception as exc:  # noqa: BLE001
            return (
                f"{name} failed ({type(exc).__name__}). Fill from best_practice or "
                f"default_verify instead; do not fabricate a citation."
            )
        return formatter(results)

    # -- one terminal-seeking run -----------------------------------------------
    def _run(self, messages: list, tools: list, terminal_name: str, state: RunState,
             system: Optional[str] = None, force_terminal: bool = False) -> Optional[_ToolCall]:
        """Loop until the model calls `terminal_name`, executing client tools in
        between. Returns the terminal call (parsed), or None if the model ended with
        plain text. `system` overrides the default prompt; `force_terminal` pins
        tool_choice to the terminal tool (used for the emit-only nudge)."""
        openai_tools = [as_openai_tool(t) for t in tools]
        tool_choice: Any = (
            {"type": "function", "function": {"name": terminal_name}}
            if force_terminal else "auto"
        )
        sys_msg = {"role": "system", "content": system or self.system_prompt}
        client_names = self._client_tool_names()

        for _round in range(config.MAX_TOOL_ROUNDS):
            resp = self.client.chat(
                messages=[sys_msg] + messages,
                tools=openai_tools,
                tool_choice=tool_choice,
                model=self.model,
            )
            try:
                msg = resp["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise AgentError(f"Malformed provider response: {exc}")

            tool_calls = _normalize_tool_calls(msg.get("tool_calls"))
            assistant: dict = {"role": "assistant", "content": _coerce_content(msg.get("content"))}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)

            if not tool_calls:
                return None  # ended with plain text — caller decides to nudge/error

            # Answer every non-terminal tool call now; leave the terminal call pending
            # (a later entrypoint acks it) so exactly one dangling call remains.
            terminal: Optional[_ToolCall] = None
            for tc in tool_calls:
                name = tc["function"]["name"]
                if name == terminal_name and terminal is None:
                    terminal = _ToolCall(id=tc["id"], name=name,
                                         input=_parse_args(tc["function"]["arguments"]))
                    continue
                content = (
                    self._dispatch(name, _parse_args(tc["function"]["arguments"]), state)
                    if name in client_names else f"Unknown or unavailable tool: {name}"
                )
                messages.append({"role": "tool", "tool_call_id": tc["id"], "content": content})

            if terminal is not None:
                return terminal
            # No terminal this round: all tool calls answered — loop for the next turn.

        raise AgentError(
            f"Model exceeded {config.MAX_TOOL_ROUNDS} tool rounds without calling {terminal_name}."
        )

    # -- Phase 1 (paper-first) --------------------------------------------------
    def analyze(
        self,
        methods_text: Optional[str] = None,
        hypothesis: Optional[str] = None,
        is_full_paper: bool = False,
    ) -> Session:
        """Reconstruct a protocol from a Methods section (pasted) or the full text of a
        paper (extracted from a PDF host-side — `is_full_paper=True`, in which case the
        model must locate the Methods section within it). An optional hypothesis orients
        the reconstruction and the clarifying questions."""
        text = (methods_text or "").strip()
        if not text:
            raise AgentError("analyze() needs methods_text.")
        session = Session()
        session.hypothesis = (hypothesis or "").strip() or None
        session.source_text = text
        session.source_exact = not is_full_paper  # PDF-extracted text is lossy vs the paper
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)

        hyp_preamble = (
            f"The student's hypothesis (what they want to test) is:\n{session.hypothesis}\n\n"
            "Orient your reconstruction and your clarifying questions toward directly "
            "testing this hypothesis.\n\n"
            if session.hypothesis else ""
        )
        if is_full_paper:
            body = (
                "The text below is the FULL TEXT of a research paper (extracted from a "
                "PDF). Locate its experimental Methods / Materials-and-Methods section "
                "(ignore abstract, intro, results, and references). Reconstruct the "
                "protocol from that section, classify every parameter, scope ambiguous "
                "gaps with a light literature search, then call request_clarifications. "
                "If the paper has no experimental methods section (e.g. a review), "
                "return usable=false.\n\n=== PAPER TEXT ===\n" + text
            )
        else:
            body = (
                "Here is a published Methods section. Reconstruct the protocol, classify "
                "every parameter, scope any ambiguous gaps with a light literature "
                "search, then call request_clarifications.\n\n=== METHODS ===\n" + text
            )

        session.messages.append({"role": "user", "content": hyp_preamble + body})
        tools = _grounding_tools() + [REQUEST_CLARIFICATIONS_TOOL]
        block = self._run(session.messages, tools, "request_clarifications", state)
        if block is None:
            raise AgentError("Phase 1 ended without calling request_clarifications.")
        session.request_tool_use_id = block.id
        session.phase1 = dict(block.input)
        return session

    # -- Phase 0 (hypothesis-first): discover candidate assays -------------------
    def discover(self, hypothesis: str, constraints: Optional[dict] = None) -> Session:
        """Hypothesis-first entry: recommend literature-grounded candidate assays.
        Runs under the discovery prompt so the paper-first Methods-section input guard
        cannot misfire. Parks the emitted call so choose_assay can ack it via _followup."""
        session = Session(source_kind="hypothesis")
        session.hypothesis = (hypothesis or "").strip() or None
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)

        parts = [
            "A student wants to test a hypothesis but has no protocol and does not know "
            "which assay to run. Recommend the best assay(s) to DIRECTLY test it, "
            "grounded in the literature, then call emit_assay_options.",
            "",
            f"Hypothesis / goal:\n{session.hypothesis or hypothesis}",
        ]
        con = {k: str(v).strip() for k, v in (constraints or {}).items() if str(v or "").strip()}
        if con:
            parts.append("")
            parts.append("Constraints to weight the recommendation toward:")
            for k, v in con.items():
                parts.append(f"- {k}: {v}")
        session.messages.append({"role": "user", "content": "\n".join(parts)})

        tools = _grounding_tools() + [EMIT_ASSAY_OPTIONS_TOOL]
        block = self._run(session.messages, tools, "emit_assay_options", state,
                          system=self.discovery_prompt)
        if block is None:
            raise AgentError("Discovery ended without calling emit_assay_options.")
        session.assay_options = dict(block.input)
        session.pending_tool_use_id = block.id  # parked for _followup
        return session

    def choose_assay(self, session: Session, assay_id: str) -> dict:
        """The student picked an assay: ack the parked emit_assay_options and drive the
        UNTOUCHED request_clarifications phase, leaving the session byte-identical to
        what analyze() produces so the rest of the pipeline is reused."""
        assays = (session.assay_options or {}).get("assays") or []
        chosen = next((a for a in assays if a.get("id") == assay_id), None)
        if chosen is None:
            raise AgentError(f"Unknown assay id: {assay_id}")
        session.chosen_assay = chosen
        brief = CHOOSE_ASSAY_INSTRUCTION.format(
            hypothesis=session.hypothesis or "(not explicitly stated)",
            assay_name=chosen.get("name", assay_id),
            measures=chosen.get("measures", ""),
            critical_comparison=chosen.get("critical_comparison", ""),
        )
        phase1 = self._followup(session, brief, "request_clarifications",
                                REQUEST_CLARIFICATIONS_TOOL)
        session.request_tool_use_id = session.pending_tool_use_id
        session.pending_tool_use_id = None
        session.phase1 = phase1
        return phase1

    # -- Phase 2 + 3 ------------------------------------------------------------
    def continue_with_answers(self, session: Session, answers: list) -> dict:
        if session.request_tool_use_id is None:
            raise AgentError("Session has no pending clarification to answer.")
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)

        # Answer the request_clarifications call with the user's answers.
        session.messages.append(
            {"role": "tool", "tool_call_id": session.request_tool_use_id,
             "content": json.dumps({"answers": answers})}
        )
        session.request_tool_use_id = None  # prevent a second answer submission

        tools = _grounding_tools() + [EMIT_PROTOCOL_TOOL]
        block = self._run(session.messages, tools, "emit_protocol", state)
        if block is not None:
            session.pending_tool_use_id = block.id
            return dict(block.input)

        # Model stopped with text — nudge once, forcing emit_protocol (no search tools).
        session.messages.append(
            {"role": "user", "content": "Your research is complete. Call emit_protocol now "
             "with the finalized, provenance-tagged protocol."}
        )
        block = self._run(session.messages, [EMIT_PROTOCOL_TOOL], "emit_protocol", state,
                          force_terminal=True)
        if block is None:
            raise AgentError("Phase 3 ended without calling emit_protocol.")
        session.pending_tool_use_id = block.id
        return dict(block.input)

    # -- Continue after an emit (ack the pending tool call) ---------------------
    def _followup(self, session: Session, instruction: str, terminal: str, tool: dict) -> dict:
        """Ack the last emit (answer its dangling tool call), append an instruction, and
        run to a new terminal tool. Shared by revise/design_review/design_alignment and
        choose_assay — answering whatever call is pending lets these interleave freely."""
        if session.pending_tool_use_id is None:
            raise AgentError("Nothing to build on yet — emit a protocol first.")
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)
        session.messages.append(
            {"role": "tool", "tool_call_id": session.pending_tool_use_id, "content": "Received."}
        )
        session.messages.append({"role": "user", "content": instruction})
        session.pending_tool_use_id = None
        block = self._run(session.messages, _grounding_tools() + [tool], terminal, state)
        if block is None:
            raise AgentError(f"Model ended without calling {terminal}.")
        session.pending_tool_use_id = block.id
        return dict(block.input)

    # -- Revise (edit-and-regenerate) -------------------------------------------
    def revise(self, session: Session, instruction: str) -> dict:
        """Feed a correction and re-emit, keeping all prior context and grounding."""
        return self._followup(
            session,
            "Apply this correction and call emit_protocol again with the full, updated "
            "protocol (keep everything else unchanged; ground any newly filled "
            "values):\n\n" + instruction.strip(),
            "emit_protocol",
            EMIT_PROTOCOL_TOOL,
        )

    # -- Design review (teach the experiment around the protocol) ---------------
    def design_review(self, session: Session) -> dict:
        """Produce an experiment-design review of the emitted protocol."""
        return self._followup(session, DESIGN_REVIEW_INSTRUCTION, "emit_design_review",
                               EMIT_DESIGN_REVIEW_TOOL)

    # -- Design alignment (does it directly test the hypothesis?) ---------------
    def design_alignment(self, session: Session, hypothesis: Optional[str] = None) -> dict:
        """Assess whether the protocol directly tests the hypothesis and recommend
        concrete protocol changes. A hypothesis passed here overrides/sets the one
        captured at analyze time."""
        hypothesis = (hypothesis or "").strip()
        if hypothesis:
            session.hypothesis = hypothesis
        instruction = DESIGN_ALIGNMENT_INSTRUCTION
        if session.hypothesis:
            instruction = (
                f"The student states their hypothesis is:\n{session.hypothesis}\n\n"
                + instruction
            )
        return self._followup(session, instruction, "emit_design_alignment",
                               EMIT_DESIGN_ALIGNMENT_TOOL)
