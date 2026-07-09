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


def _pdf_text(pdf: bytes) -> Optional[str]:
    """Best-effort host-side text extraction from a PDF, for quote verification. The
    model still reads the PDF natively; this is only so the host can confirm a 'stated'
    quote actually appears in the paper. Returns None if no extractor is available."""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf))
        text = "\n".join((page.extract_text() or "") for page in reader.pages)
        return text if text.strip() else None  # blank -> None (scanned/OCR-free PDF)
    except Exception:  # noqa: BLE001 — extractor missing or PDF unparseable
        return None


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
        self.discovery_system = [
            {"type": "text", "text": DISCOVERY_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]

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
             system: Optional[list] = None) -> Optional[Any]:
        """Loop until the model calls `terminal_name`. Resumes across server-tool
        pauses and executes client tools (search_pubmed) in between. Returns the
        terminal tool_use block, or None if the model ended without calling it.
        `system` overrides the default protocol-engineer prompt (used by discover())."""
        while True:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=config.MAX_TOKENS,
                system=system or self.system,
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
                            "content": self._dispatch(b.name, dict(b.input), state),
                        }
                    )
            if not results:
                return None  # tool_use we don't handle and not terminal — bail
            messages.append({"role": "user", "content": results})

    def _client_tool_names(self) -> set:
        return set(_enabled_client_tools().keys())

    # -- Phase 1 ----------------------------------------------------------------
    def analyze(
        self,
        methods_text: Optional[str] = None,
        pdf: Optional[bytes] = None,
        hypothesis: Optional[str] = None,
    ) -> Session:
        """Start from pasted Methods text OR a full-paper PDF (read natively by the
        API). Exactly one of `methods_text` / `pdf` should be provided. An optional
        hypothesis orients the reconstruction and the clarifying questions."""
        session = Session()
        session.hypothesis = (hypothesis or "").strip() or None
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)

        hyp_preamble = (
            f"The student's hypothesis (what they want to test) is:\n{session.hypothesis}\n\n"
            "Orient your reconstruction and your clarifying questions toward directly "
            "testing this hypothesis.\n\n"
            if session.hypothesis else ""
        )

        if pdf is not None:
            import base64

            session.source_text = _pdf_text(pdf)  # None if no extractor -> quotes unverifiable
            session.source_exact = False  # extraction is lossy vs the model's native read
            b64 = base64.standard_b64encode(pdf).decode("ascii")
            content = [
                {
                    "type": "document",
                    "source": {"type": "base64", "media_type": "application/pdf", "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        hyp_preamble
                        + "The attached PDF is a full research paper. Locate its experimental "
                        "Methods / Materials-and-Methods section (ignore abstract, intro, "
                        "results, and references). Reconstruct the protocol from that section, "
                        "classify every parameter, scope ambiguous gaps with a light literature "
                        "search, then call request_clarifications. If the paper has no "
                        "experimental methods section (e.g. a review), return usable=false."
                    ),
                },
            ]
        elif methods_text and methods_text.strip():
            session.source_text = methods_text.strip()
            content = (
                hyp_preamble
                + "Here is a published Methods section. Reconstruct the protocol, "
                "classify every parameter, scope any ambiguous gaps with a light "
                "literature search, then call request_clarifications.\n\n"
                "=== METHODS ===\n" + methods_text.strip()
            )
        else:
            raise AgentError("analyze() needs methods_text or pdf.")

        session.messages.append({"role": "user", "content": content})
        tools = _grounding_tools() + [REQUEST_CLARIFICATIONS_TOOL]
        block = self._run(session.messages, tools, "request_clarifications", state)
        if block is None:
            raise AgentError("Phase 1 ended without calling request_clarifications.")
        session.request_tool_use_id = block.id
        session.phase1 = dict(block.input)
        return session

    # -- Phase 0 (hypothesis-first): discover candidate assays -------------------
    def discover(self, hypothesis: str, constraints: Optional[dict] = None) -> Session:
        """Hypothesis-first entry: recommend literature-grounded candidate assays that
        directly test the hypothesis. Runs under DISCOVERY_SYSTEM_PROMPT so the
        paper-first Methods-section input guard cannot misfire. Parks the emitted block
        on pending_tool_use_id so choose_assay can ack it via the shared _followup."""
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
                          system=self.discovery_system)
        if block is None:
            raise AgentError("Discovery ended without calling emit_assay_options.")
        session.assay_options = dict(block.input)
        session.pending_tool_use_id = block.id  # parked on the emit slot for _followup
        return session

    def choose_assay(self, session: Session, assay_id: str) -> dict:
        """The student picked an assay: ack the parked emit_assay_options and drive the
        UNTOUCHED request_clarifications phase for the chosen assay. Leaves the session
        byte-identical to what analyze() produces, so the rest of the pipeline is reused."""
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
        # Re-thread exactly as analyze() leaves state for continue_with_answers().
        session.request_tool_use_id = session.pending_tool_use_id
        session.pending_tool_use_id = None
        session.phase1 = phase1
        return phase1

    # -- Phase 2 + 3 ------------------------------------------------------------
    def continue_with_answers(self, session: Session, answers: list) -> dict:
        if session.request_tool_use_id is None:
            raise AgentError("Session has no pending clarification to answer.")
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)

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
        block = self._run(session.messages, tools, "emit_protocol", state)
        if block is not None:
            session.pending_tool_use_id = block.id
            return dict(block.input)

        # Model stopped without emitting — nudge once, emit-only (no search tools).
        session.messages.append(
            {
                "role": "user",
                "content": "Your research is complete. Call emit_protocol now with the "
                "finalized, provenance-tagged protocol.",
            }
        )
        block = self._run(session.messages, [EMIT_PROTOCOL_TOOL], "emit_protocol", state)
        if block is None:
            raise AgentError("Phase 3 ended without calling emit_protocol.")
        session.pending_tool_use_id = block.id
        return dict(block.input)

    # -- Continue after an emit (ack the pending tool_use) ----------------------
    def _followup(self, session: Session, instruction: str, terminal: str, tool: dict) -> dict:
        """Ack the last emit, append an instruction, and run to a new terminal tool.
        Shared by revise (re-emit protocol) and design_review (emit design review).
        Responding to whatever tool_use is pending lets protocol edits and design
        reviews interleave in any order without breaking the conversation."""
        if session.pending_tool_use_id is None:
            raise AgentError("Nothing to build on yet — emit a protocol first.")
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET)
        session.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": session.pending_tool_use_id,
                     "content": "Received."},
                    {"type": "text", "text": instruction},
                ],
            }
        )
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
