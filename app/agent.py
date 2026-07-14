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
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Optional

from . import config, literature
from .checks import finding_key
from .llm import OpenRouterClient
from .models import coerce_emit_payload
from .prompts import (
    CHOOSE_ASSAY_INSTRUCTION,
    CORRECTNESS_REVIEW_INSTRUCTION,
    DESIGN_ALIGNMENT_INSTRUCTION,
    DESIGN_REVIEW_INSTRUCTION,
    DISCOVERY_SYSTEM_PROMPT,
    FIX_VERIFICATION_INSTRUCTION,
    SYSTEM_ASK,
    SYSTEM_EMIT,
    SYSTEM_PROMPT,
)
from .schemas import (
    EMIT_ASSAY_OPTIONS_TOOL,
    EMIT_CORRECTNESS_REVIEW_TOOL,
    EMIT_DESIGN_ALIGNMENT_TOOL,
    EMIT_DESIGN_REVIEW_TOOL,
    EMIT_FIX_VERIFICATION_TOOL,
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
    protocol: Optional[dict] = None  # last emitted/corrected protocol (post-apply, ensure_ids'd)
    grounding_log: list = field(default_factory=list)  # queries the app ran
    grounding_call_ids: list = field(default_factory=list)  # tool_call_ids of grounding results (for compaction)
    decisions: list = field(default_factory=list)  # host-captured user intent (clarification Q&A + chosen assay)


@dataclass
class RunState:
    """Per-call mutable state. Kept off the (shared) agent instance so concurrent
    requests through one GapFillerAgent don't clobber each other's session or budget."""

    session: Session
    searches_left: int
    lock: Any = field(default_factory=threading.Lock)  # guards budget/log under parallel dispatch
    progress: Any = None  # optional Callable[[str], None] for a live activity feed


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


IMAGE_ONLY_MIN_CHARS = 16


def _pdf_extract(pdf: bytes) -> "tuple[Optional[str], int]":
    """Host-side (NO model). Returns (text_or_None, page_count). text is None when the
    PDF has no readable text layer OR the extractor is unavailable/unparseable. Single parse."""
    try:
        import io

        import pypdf

        reader = pypdf.PdfReader(io.BytesIO(pdf))
        pages = len(reader.pages)
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return (text if text.strip() else None), pages
    except Exception:  # noqa: BLE001
        return None, 0


def _ocr_available() -> bool:
    """True once an OCR backend is wired into _ocr_pdf. Seam only."""
    return False


def _ocr_pdf(pdf: bytes) -> str:
    """OCR integration SEAM — NOT implemented (no OCR dep bundled). Frozen signature
    (bytes -> str). Callers must catch NotImplementedError and fall back to paste."""
    raise NotImplementedError(
        "OCR is not available in this build. This PDF has no selectable text "
        "(it looks scanned or image-only). Paste the Methods section as text instead.")


_CRITICAL_KEYWORDS = frozenset({
    "concentration", "dose", "dosage", "volume", "temperature", "time", "duration",
    "ph", "molarity", "ratio", "cycles", "dilution", "incubat", "antibiotic",
    "selection", "readout", "control", "seeding", "density", "moi", "voltage",
    "flow rate", "gradient", "wavelength", "exposure", "od600", "confluence",
})


def _is_outcome_critical(gap: dict) -> bool:
    if gap.get("outcome_critical") is True:
        return True
    cls = gap.get("classification")
    if cls == "deferred":
        return False
    if cls == "user_dependent":
        return True
    text = " ".join(str(gap.get(k, "")) for k in
                    ("parameter", "why_it_matters", "question")).lower()
    if any(kw in text for kw in _CRITICAL_KEYWORDS):
        return True
    if gap.get("answer_type") == "number" and (
            gap.get("plausible_min") is not None or gap.get("plausible_max") is not None):
        return True
    return False


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
        return "".join((p.get("text") or "") for p in content if isinstance(p, dict))
    return str(content)


def _normalize_tool_calls(raw: Any) -> list:
    """Clean the assistant message's tool_calls into a stable shape with guaranteed,
    unique ids (so the tool responses we echo back line up). Some providers hand back
    `arguments` as an object rather than a JSON string — stringify it so the turn we
    re-send is a valid OpenAI assistant message."""
    out = []
    for tc in raw or []:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, dict):
            args = json.dumps(args)
        elif not isinstance(args, str) or not args:
            args = "{}"
        out.append(
            {
                "id": tc.get("id") or f"call_{uuid.uuid4().hex}",
                "type": "function",
                "function": {"name": fn.get("name", ""), "arguments": args},
            }
        )
    return out


def _parse_args(arguments: Any) -> dict:
    if isinstance(arguments, dict):
        return arguments
    try:
        val = json.loads(arguments or "{}")
        return val if isinstance(val, dict) else {}
    except (ValueError, TypeError):
        return {}


def _cache_text(text: str) -> Any:
    """Wrap a large, stable text block as an OpenAI content part carrying a cache
    breakpoint, so the multi-phase loop re-reads the prefix (system prompt + source) from
    the provider's prompt cache instead of re-billing it. A breakpoint on the source
    message caches the whole `[system, source]` prefix. Returns a plain string when
    caching is off — the model sees identical content either way."""
    if not config.PROMPT_CACHE:
        return text
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


# Locate a paper's Methods/Materials section so we don't ship the whole PDF every call.
_METHODS_START_RE = re.compile(
    r"(?im)^\s*(?:\d+\.?\s*)?(?:materials\s+and\s+methods|methods\s+and\s+materials|"
    r"materials\s*&\s*methods|experimental\s+(?:procedures|section|methods)|methods)\b.*$"
)
_METHODS_END_RE = re.compile(
    r"(?im)^\s*(?:\d+\.?\s*)?(?:results(?:\s+and\s+discussion)?|discussion|conclusions?|"
    r"acknowledge?ments?|references|bibliography|supplementary|author\s+contributions|"
    r"data\s+availability|competing\s+interests)\b.*$"
)


def _extract_methods_section(text: str) -> Optional[str]:
    """Best-effort trim of extracted full-paper text down to just its Methods/Materials
    section, so the abstract/intro/results/references aren't shipped to the model on every
    call. Conservative — returns None (caller falls back to full text) unless it finds a
    Methods heading at a line start, a plausible end heading after it, and the slice is
    both non-trivial and materially smaller than the whole (so a false match can't quietly
    drop most of the paper). Removes noise, not signal: the reconstruction only uses this
    section, and host-side quote verification then runs against exactly what the model read."""
    m = _METHODS_START_RE.search(text)
    if not m:
        return None
    start = m.start()
    end_m = _METHODS_END_RE.search(text, m.end())
    end = end_m.start() if end_m else len(text)
    section = text[start:end].strip()
    if len(section) < 200 or len(section) > 0.9 * len(text):
        return None
    return section


def _compact_grounding(session: Session) -> None:
    """Once a protocol exists, replace the bulky raw grounding-search results carried in
    the transcript with a short placeholder. Follow-ups (revise/design/align) don't re-read
    raw hits — the grounded values and their citations are already in the emitted protocol
    — so this trims their input cost without changing what the model can use."""
    ids = set(session.grounding_call_ids)
    if not ids:
        return
    for msg in session.messages:
        if (msg.get("role") == "tool" and msg.get("tool_call_id") in ids
                and isinstance(msg.get("content"), str) and len(msg["content"]) > 400):
            msg["content"] = ("[earlier grounding-search results omitted to save tokens; "
                              "the grounded values and their citations are in the protocol above]")


def _render_verification_input(corrected: dict, prior_findings: list) -> str:
    """Build the ONE user message for an independent fix-verification run.

    Contains exactly: (1) the corrected protocol as JSON (already ensure_ids'd by the
    caller); (2) the prior findings to verify, each rendered with its host ``_key`` as
    ``finding_key`` (verbatim, must be echoed) plus severity/category/location/problem and
    the ``fix`` supposedly applied; (3) FIX_VERIFICATION_INSTRUCTION. It deliberately omits
    the apply-instruction turn, the review's verdict/summary/strengths, and the original
    pre-fix protocol body, so the verifier judges only the corrected artifact."""
    findings_view = []
    for f in prior_findings or []:
        if not isinstance(f, dict):
            continue
        findings_view.append({
            "finding_key": f.get("_key") or finding_key(f),
            "severity": f.get("severity"),
            "category": f.get("category"),
            "location": f.get("location"),
            "problem": f.get("problem"),
            "fix": f.get("fix"),
        })
    parts = [
        "=== CORRECTED PROTOCOL (JSON) ===",
        json.dumps(corrected, indent=2, ensure_ascii=False, default=str),
        "",
        "=== PRIOR FINDINGS TO VERIFY ===",
        "Each finding below was raised against an EARLIER draft and someone claims to have "
        "fixed it. Echo each finding_key VERBATIM; return exactly one check per key.",
        json.dumps(findings_view, indent=2, ensure_ascii=False, default=str),
        "",
        FIX_VERIFICATION_INSTRUCTION,
    ]
    return "\n".join(parts)


_SOURCE_CAP = 12000  # chars; longer source is head+tail summarized so the reviewer isn't flooded


def _render_review_input(protocol: dict, *, source_text=None, source_exact=True,
                         decisions=None, grounding_log=None, quality_gate=None) -> str:
    """Build the ONE user message for a FRESH-context adversarial correctness review.

    Assembles EXACTLY five whitelisted blocks and, BY CONSTRUCTION, includes nothing from
    ``session.messages`` — no authoring assistant turns, no chain-of-thought, no prior
    self-justification, no SYSTEM_EMIT reasoning. The reviewer sees only the artifact and
    the host-owned ground truth, so it audits independently rather than re-reading (and
    trusting) the reasoning that produced the protocol.

    Blocks, in order: SOURCE (methods), PROTOCOL (JSON), USER DECISIONS, RETRIEVED EVIDENCE
    (searches run), DETERMINISTIC VALIDATION FINDINGS (quality_gate). Trailing:
    CORRECTNESS_REVIEW_INSTRUCTION."""
    parts: list = []

    # 1. SOURCE (methods) — full when short; head+tail summary when oversized; a marker
    #    when absent (hypothesis-first). Always declare source_exact so lossy PDF text is
    #    not over-trusted.
    if source_text:
        parts.append("=== SOURCE (methods) ===")
        parts.append(f"source_exact: {'true' if source_exact else 'false'}")
        text = str(source_text)
        if len(text) > _SOURCE_CAP:
            head = text[: _SOURCE_CAP // 2]
            tail = text[-(_SOURCE_CAP // 2):]
            parts.append("source_summary (summarized): head+tail of an oversized source")
            parts.append(head)
            parts.append("… [middle omitted] …")
            parts.append(tail)
        else:
            parts.append(text)
    else:
        parts.append("=== SOURCE === (none — hypothesis-first draft)")
    parts.append("")

    # 2. PROTOCOL (JSON) — the artifact under audit (already ensure_ids'd / validated).
    parts.append("=== PROTOCOL (JSON) ===")
    parts.append(json.dumps(protocol, indent=2, default=str))
    parts.append("")

    # 3. USER DECISIONS — host-captured intent (clarification Q&A + chosen assay). Never
    #    the transcript.
    parts.append("=== USER DECISIONS ===")
    parts.append(json.dumps(decisions or [], indent=2, default=str))
    parts.append("")

    # 4. RETRIEVED EVIDENCE (searches run) — the grounding log; cited excerpts already live
    #    inline in the protocol's citation.evidence.
    parts.append("=== RETRIEVED EVIDENCE (searches run) ===")
    parts.append("\n".join(str(q) for q in (grounding_log or [])) or "(no searches run)")
    parts.append("")

    # 5. DETERMINISTIC VALIDATION FINDINGS (quality_gate) — the host's deterministic verdict
    #    handed to the reviewer as ground truth.
    parts.append("=== DETERMINISTIC VALIDATION FINDINGS (quality_gate) ===")
    parts.append(json.dumps(quality_gate or {}, indent=2, default=str))
    parts.append("")

    parts.append(CORRECTNESS_REVIEW_INSTRUCTION)
    return "\n".join(parts)


class GapFillerAgent:
    def __init__(self, client: Optional[OpenRouterClient] = None, model: Optional[str] = None,
                 model_fast: Optional[str] = None, review_model: Optional[str] = None):
        self.client = client or OpenRouterClient()
        self.model = model or config.MODEL
        # Fast tier for the light phases (analyze/clarifications, discovery); falls back
        # to the main model when unset. The heavy emit always uses the main model.
        self.model_fast = model_fast or config.MODEL_FAST or self.model
        # Reviewer tier for the adversarial correctness review + post-fix verify. Defaults
        # to REVIEW_MODEL (== MODEL unless GAPFILLER_REVIEW_MODEL is set), so a byte-identical
        # run by default and an independent second-opinion model when configured.
        self.review_model = review_model or config.REVIEW_MODEL or self.model
        # Reasoning effort: full on the heavy phases, lower on the light ones — spend
        # expensive thinking tokens only where they add value.
        self.effort = config.REASONING_EFFORT          # heavy phases (None -> this default)
        self.effort_fast = config.REASONING_EFFORT_FAST  # light phases
        self.system_prompt = SYSTEM_PROMPT      # full — design review/alignment, fallback
        self.system_ask = SYSTEM_ASK            # phase 1 (clarifications): no emit rules
        self.system_emit = SYSTEM_EMIT          # phase 3 (emit): no asking rules
        self.discovery_prompt = DISCOVERY_SYSTEM_PROMPT

    def _client_tool_names(self) -> set:
        return set(_enabled_client_tools().keys())

    # -- client-tool dispatch ---------------------------------------------------
    def _dispatch(self, name: str, tool_input: dict, state: RunState) -> str:
        enabled = _enabled_client_tools()
        if name not in enabled:
            return f"Unknown or disabled tool: {name}"
        # Reserve budget + record the query atomically (calls in one turn run in parallel);
        # the network search itself happens outside the lock so searches overlap.
        with state.lock:
            if state.searches_left <= 0:
                return (
                    "Search budget exhausted. Do not search again; fill any remaining "
                    "values from best_practice or default_verify and proceed to emit."
                )
            state.searches_left -= 1
            query = str(tool_input.get("query", "")).strip()
            state.session.grounding_log.append(f"{name}: {query}")
        if state.progress and query:  # live feed: surface the literature search underway
            src = name.replace("search_", "").replace("_", " ")
            state.progress(f"Searching {src} for “{query}”…")
        search_fn, formatter = enabled[name]
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
             system: Optional[str] = None, force_terminal: bool = False,
             model: Optional[str] = None, effort: Optional[str] = None) -> Optional[_ToolCall]:
        """Loop until the model calls `terminal_name`, executing client tools in
        between. Returns the terminal call (parsed), or None if the model ended with
        plain text. `system` overrides the default prompt; `model` overrides the tier;
        `force_terminal` pins tool_choice to the terminal tool (used for the emit-only
        nudge). When no grounding tool is available this round, the terminal is forced on
        the first call too — there's nothing to search for, so skip the auto→text→nudge
        round-trip."""
        openai_tools = [as_openai_tool(t) for t in tools]
        client_names = self._client_tool_names()
        has_grounding = bool({t.get("name") for t in tools} & client_names)
        force = force_terminal or not has_grounding
        tool_choice: Any = (
            {"type": "function", "function": {"name": terminal_name}}
            if force else "auto"
        )
        sys_msg = {"role": "system", "content": system or self.system_prompt}

        budget = config.MAX_TOKENS or 0  # escalates on truncation; growth persists across rounds
        for _round in range(config.MAX_TOOL_ROUNDS):
            # Heartbeat: a model round can take many seconds on a large context. Without a
            # per-round note the live feed looks frozen for the whole call, so a slow run
            # reads as a hang. The search dispatches add their own, more specific lines.
            if state.progress and _round > 0:
                state.progress(f"Working through the results… (step {_round + 1})")
            # A truncated response is an incomplete (broken-JSON) tool call. Rather than
            # hard-failing — a 502 telling the user to raise an env var they can't reach
            # mid-run — retry the SAME call with a doubled output budget up to MAX_TOKENS_CAP.
            # Normal emits fit the base budget and never escalate; only oversized protocols do.
            while True:
                resp = self.client.chat(
                    messages=[sys_msg] + messages,
                    tools=openai_tools,
                    tool_choice=tool_choice,
                    model=model or self.model,
                    effort=effort,
                    max_tokens=budget or None,
                )
                try:
                    choice = resp["choices"][0]
                    finish = choice.get("finish_reason")
                    msg = choice["message"]
                    if not isinstance(msg, dict):
                        raise TypeError("message is not an object")
                    raw_calls = msg.get("tool_calls")
                    if raw_calls is not None and not isinstance(raw_calls, list):
                        raise TypeError("tool_calls is not a list")
                except (KeyError, IndexError, TypeError) as exc:
                    raise AgentError(f"Malformed provider response: {exc}")

                if finish == "length" and budget and budget < config.MAX_TOKENS_CAP:
                    budget = min(budget * 2, config.MAX_TOKENS_CAP)
                    state.session.grounding_log.append(
                        f"output truncated — retrying with max_tokens={budget}")
                    continue
                if finish == "length":
                    # Even at the cap the response didn't fit — fail with a user-actionable
                    # message (no server-tuning internals leaked to the client).
                    raise AgentError(
                        "The protocol was too large to finish generating. Try a narrower "
                        "scope — fewer conditions or a simpler assay — and rebuild."
                    )
                break

            tool_calls = _normalize_tool_calls(raw_calls)
            content = _coerce_content(msg.get("content"))
            # A no-tool-call assistant turn with null content is rejected by strict
            # providers when re-sent; coerce it to "" so the transcript stays valid.
            assistant: dict = {"role": "assistant",
                               "content": content if tool_calls else (content or "")}
            if tool_calls:
                assistant["tool_calls"] = tool_calls
            messages.append(assistant)

            if not tool_calls:
                return None  # ended with plain text — caller decides to nudge/error

            # Split off the terminal (leave it pending — a later entrypoint acks it) and
            # answer every other call. Multiple grounding calls in one turn run
            # concurrently, so N searches cost ~one network round-trip, not N.
            terminal: Optional[_ToolCall] = None
            pending: list = []
            for tc in tool_calls:
                name = tc["function"]["name"]
                if name == terminal_name and terminal is None:
                    terminal = _ToolCall(id=tc["id"], name=name,
                                         input=_parse_args(tc["function"]["arguments"]))
                    continue
                pending.append(tc)

            def _answer(tc: dict) -> tuple:
                name = tc["function"]["name"]
                content = (
                    self._dispatch(name, _parse_args(tc["function"]["arguments"]), state)
                    if name in client_names else f"Unknown or unavailable tool: {name}"
                )
                return tc["id"], name, content

            if len(pending) > 1:
                workers = min(len(pending), max(1, config.SEARCH_CONCURRENCY))
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    answered = list(ex.map(_answer, pending))
            else:
                answered = [_answer(tc) for tc in pending]

            for cid, name, content in answered:  # append in original order
                messages.append({"role": "tool", "tool_call_id": cid, "content": content})
                if name in client_names:  # a grounding result — mark it for later compaction
                    state.session.grounding_call_ids.append(cid)

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
        progress: Optional[Any] = None,
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

        # For a PDF (full paper), trim host-side to just the Methods section when we can
        # find it, so the abstract/intro/results/references aren't shipped on every call.
        # Falls back to the whole text if the section can't be confidently located.
        extracted_methods = _extract_methods_section(text) if is_full_paper else None
        effective_text = extracted_methods or text
        session.source_text = effective_text  # verify quotes against exactly what the model read
        session.source_exact = not is_full_paper  # PDF-derived text is lossy vs the paper
        # Analyze only scopes gaps — a small search budget keeps it fast and bounds the
        # tool-round loop so a large/off-topic doc can't grind past the browser's timeout.
        state = RunState(session=session, searches_left=config.ANALYZE_PUBMED_BUDGET, progress=progress)

        hyp_preamble = (
            f"The student's hypothesis (what they want to test) is:\n{session.hypothesis}\n\n"
            "Orient your reconstruction and your clarifying questions toward directly "
            "testing this hypothesis.\n\n"
            if session.hypothesis else ""
        )
        if is_full_paper and not extracted_methods:
            body = (
                "The text below is the FULL TEXT of a research paper (extracted from a "
                "PDF). Locate its experimental Methods / Materials-and-Methods section "
                "(ignore abstract, intro, results, and references). Reconstruct the "
                "protocol from that section, classify every parameter, scope ambiguous "
                "gaps with a light literature search, then call request_clarifications. "
                "If the paper has no experimental methods section (e.g. a review), "
                "return usable=false.\n\n=== PAPER TEXT ===\n" + effective_text
            )
        elif is_full_paper:
            body = (
                "The text below is the Methods / Materials-and-Methods section extracted "
                "from a research paper's PDF (the surrounding sections were trimmed out "
                "host-side, so wording may be slightly lossy). Reconstruct the protocol, "
                "classify every parameter, scope ambiguous gaps with a light literature "
                "search, then call request_clarifications.\n\n=== METHODS (extracted) ===\n"
                + effective_text
            )
        else:
            body = (
                "Here is a published Methods section. Reconstruct the protocol, classify "
                "every parameter, scope any ambiguous gaps with a light literature "
                "search, then call request_clarifications.\n\n=== METHODS ===\n" + effective_text
            )

        session.messages.append({"role": "user", "content": _cache_text(hyp_preamble + body)})
        tools = _grounding_tools() + [REQUEST_CLARIFICATIONS_TOOL]
        block = self._run(session.messages, tools, "request_clarifications", state,
                          system=self.system_ask, model=self.model_fast, effort=self.effort_fast)
        if block is None:
            raise AgentError("Phase 1 ended without calling request_clarifications.")
        session.request_tool_use_id = block.id
        session.phase1 = dict(block.input)
        for _g in (session.phase1.get("gaps") or []):
            _g["outcome_critical"] = _is_outcome_critical(_g)
        return session

    # -- Phase 0 (hypothesis-first): discover candidate assays -------------------
    def discover(self, hypothesis: str, constraints: Optional[dict] = None,
                 progress: Optional[Any] = None) -> Session:
        """Hypothesis-first entry: recommend literature-grounded candidate assays.
        Runs under the discovery prompt so the paper-first Methods-section input guard
        cannot misfire. Parks the emitted call so choose_assay can ack it via _followup."""
        session = Session(source_kind="hypothesis")
        session.hypothesis = (hypothesis or "").strip() or None
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET, progress=progress)

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
        session.messages.append({"role": "user", "content": _cache_text("\n".join(parts))})

        tools = _grounding_tools() + [EMIT_ASSAY_OPTIONS_TOOL]
        block = self._run(session.messages, tools, "emit_assay_options", state,
                          system=self.discovery_prompt, model=self.model_fast, effort=self.effort_fast)
        if block is None:
            raise AgentError("Discovery ended without calling emit_assay_options.")
        session.assay_options = dict(block.input)
        session.pending_tool_use_id = block.id  # parked for _followup
        return session

    def choose_assay(self, session: Session, assay_id: str,
                     progress: Optional[Any] = None) -> dict:
        """The student picked an assay: ack the parked emit_assay_options and drive the
        UNTOUCHED request_clarifications phase, leaving the session byte-identical to
        what analyze() produces so the rest of the pipeline is reused."""
        assays = (session.assay_options or {}).get("assays") or []
        chosen = next((a for a in assays if a.get("id") == assay_id), None)
        if chosen is None:
            raise AgentError(f"Unknown assay id: {assay_id}")
        session.chosen_assay = chosen
        session.decisions.append({"decision": "chose_assay", "assay": chosen.get("name")})
        brief = CHOOSE_ASSAY_INSTRUCTION.format(
            hypothesis=session.hypothesis or "(not explicitly stated)",
            assay_name=chosen.get("name", assay_id),
            measures=chosen.get("measures", ""),
            critical_comparison=chosen.get("critical_comparison", ""),
        )
        phase1 = self._followup(session, brief, "request_clarifications",
                                REQUEST_CLARIFICATIONS_TOOL,
                                system=self.system_ask, model=self.model_fast,
                                effort=self.effort_fast, progress=progress)
        session.request_tool_use_id = session.pending_tool_use_id
        session.pending_tool_use_id = None
        session.phase1 = phase1
        for _g in (session.phase1.get("gaps") or []):
            _g["outcome_critical"] = _is_outcome_critical(_g)
        return phase1

    # -- Phase 2 + 3 ------------------------------------------------------------
    # -- Emit-boundary bounded repair (§I.5) ------------------------------------
    def _gate_emit_payload(self, session: Session, block: "_ToolCall", state: "RunState",
                           system: Optional[str] = None, model: Optional[str] = None,
                           effort: Optional[str] = None) -> tuple:
        """Gate an ``emit_protocol`` terminal at the boundary. If the payload is
        structurally UNUSABLE (the FATAL class: not a dict / empty title / steps not
        a list), re-emit exactly ONCE — appending a user turn quoting the errors and
        forcing emit_protocol with no search tools (mirrors the nudge-once block) —
        then re-check. A still-unusable result raises AgentError (surfaced by the
        transactional rollback as a normal error, never a partial render).

        Per-entry gaps are NOT fatal: they are repaired deterministically downstream
        by repair_structure + STRUCT_* blocker, so model calls stay bounded. Returns
        ``(raw, block)`` on success."""
        raw = dict(block.input)
        usable, fatal, _ = coerce_emit_payload(raw)
        if usable:
            return raw, block
        summary = "; ".join(f"{e['loc']}: {e['msg']}" for e in fatal) or "structurally unusable"
        session.messages.append(
            {"role": "user", "content": (
                "Your emit_protocol payload is structurally unusable and cannot be "
                f"rendered ({summary}). Call emit_protocol ONCE more with a valid protocol: "
                "a non-empty title and a steps array with at least one step. Do not search.")}
        )
        block2 = self._run(session.messages, [EMIT_PROTOCOL_TOOL], "emit_protocol", state,
                           system=system or self.system_emit, force_terminal=True,
                           model=model or self.model,
                           effort=effort if effort is not None else self.effort)
        if block2 is None:
            raise AgentError(
                "emit_protocol produced a structurally unusable protocol: " + summary)
        raw2 = dict(block2.input)
        usable2, fatal2, _ = coerce_emit_payload(raw2)
        if not usable2:
            summary2 = "; ".join(f"{e['loc']}: {e['msg']}" for e in fatal2) or summary
            raise AgentError(
                "emit_protocol produced a structurally unusable protocol: " + summary2)
        return raw2, block2

    def continue_with_answers(self, session: Session, answers: list,
                              progress: Optional[Any] = None) -> dict:
        if session.request_tool_use_id is None:
            raise AgentError("Session has no pending clarification to answer.")
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET, progress=progress)

        # Transactional (mirrors _followup): if the emit fails, restore the pending
        # clarification and drop the appended answer so retrying "Build protocol" works
        # instead of dying with "Session has no pending clarification to answer".
        saved_request = session.request_tool_use_id
        saved_len = len(session.messages)
        # Capture user intent host-side (never scraped from the transcript) so the fresh
        # correctness review can be shown the clarification Q&A. Guarded: if phase1 carries
        # no questions, decisions stays empty rather than raising.
        questions = (session.phase1 or {}).get("questions") or []
        if questions:
            session.decisions = [{"question": q, "answer": a.get("value"), "mode": a.get("mode")}
                                 for q, a in zip(questions, answers)]
        # Answer the request_clarifications call with the user's answers.
        session.messages.append(
            {"role": "tool", "tool_call_id": session.request_tool_use_id,
             "content": json.dumps({"answers": answers})}
        )
        session.request_tool_use_id = None  # prevent a second answer submission
        # Host directive: honor the explicit per-gap modes exactly (empty is NEVER a default).
        # Guard: no answers -> no directive (preserves the no-gap fast path).
        if answers:
            lines = []
            for a in answers:
                if a.get("mode") == "default":
                    lines.append(f"- {a['id']}: USE its suggested_default; tag provenance "
                                 f"'default_verify' and add an open_question noting it was accepted unverified.")
                elif a.get("mode") == "unresolved":
                    lines.append(f"- {a['id']}: LEAVE UNRESOLVED — do not fabricate a value; emit "
                                 f"it as default_verify with an explicit open_question asking the user to supply it.")
                else:
                    lines.append(f"- {a['id']}: use the provided value.")
            directive = ("The user made an explicit choice per gap. Honor these modes exactly "
                         "(an empty field is NOT a default):\n" + "\n".join(lines))
            session.messages.append({"role": "user", "content": directive})
        try:
            tools = _grounding_tools() + [EMIT_PROTOCOL_TOOL]
            block = self._run(session.messages, tools, "emit_protocol", state,
                              system=self.system_emit, model=self.model, effort=self.effort)
            if block is None:
                # Model stopped with text — nudge once, forcing emit_protocol (no search tools).
                session.messages.append(
                    {"role": "user", "content": "Your research is complete. Call emit_protocol "
                     "now with the finalized, provenance-tagged protocol."}
                )
                block = self._run(session.messages, [EMIT_PROTOCOL_TOOL], "emit_protocol", state,
                                  system=self.system_emit, force_terminal=True, model=self.model,
                                  effort=self.effort)
                if block is None:
                    raise AgentError("Phase 3 ended without calling emit_protocol.")
            # Emit-boundary bounded repair: at most one re-emit on a FATAL payload,
            # else AgentError (rolled back below). Runs inside the try so a failure
            # restores the pending clarification and drops the appended answer.
            raw, block = self._gate_emit_payload(session, block, state)
        except Exception:
            del session.messages[saved_len:]
            session.request_tool_use_id = saved_request
            session.pending_tool_use_id = None
            raise
        session.pending_tool_use_id = block.id
        return raw

    # -- Continue after an emit (ack the pending tool call) ---------------------
    def _followup(self, session: Session, instruction: str, terminal: str, tool: dict,
                  compact: bool = False, system: Optional[str] = None,
                  model: Optional[str] = None, effort: Optional[str] = None,
                  progress: Optional[Any] = None) -> dict:
        """Ack the last emit (answer its dangling tool call), append an instruction, and
        run to a new terminal tool. Shared by revise/design_review/design_alignment and
        choose_assay — answering whatever call is pending lets these interleave freely.
        `compact=True` (post-emit follow-ups) trims the bulky raw grounding results first.
        `system`/`model` scope the prompt + tier to the follow-up's kind."""
        if session.pending_tool_use_id is None:
            raise AgentError("Nothing to build on yet — emit a protocol first.")
        if compact:
            _compact_grounding(session)
        # Transactional: a follow-up that fails mid-run (tool-round ceiling, a transient
        # provider error) must leave the session exactly as it found it. Otherwise the acked
        # emit id is gone and the appended instruction dangles, bricking EVERY later follow-up
        # (retry, re-review, revise) with "Nothing to build on yet". Snapshot, restore on error.
        saved_pending = session.pending_tool_use_id
        saved_len = len(session.messages)
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET, progress=progress)
        session.messages.append(
            {"role": "tool", "tool_call_id": session.pending_tool_use_id, "content": "Received."}
        )
        session.messages.append({"role": "user", "content": instruction})
        session.pending_tool_use_id = None
        try:
            block = self._run(session.messages, _grounding_tools() + [tool], terminal, state,
                              system=system, model=model, effort=effort)
            if block is None:
                raise AgentError(f"Model ended without calling {terminal}.")
            # Only emit_protocol follow-ups (revise/apply_fixes) pass through the
            # emit-boundary gate; other terminals (design review/alignment) are not
            # protocols. Runs inside the try so a fatal re-emit failure rolls back.
            if terminal == "emit_protocol":
                raw, block = self._gate_emit_payload(
                    session, block, state, system=system, model=model, effort=effort)
            else:
                raw = dict(block.input)
        except Exception:
            del session.messages[saved_len:]           # drop the ack + instruction we appended
            session.pending_tool_use_id = saved_pending  # re-arm the emit so a retry works
            raise
        session.pending_tool_use_id = block.id
        return raw

    # -- Revise (edit-and-regenerate) -------------------------------------------
    def revise(self, session: Session, instruction: str, progress: Optional[Any] = None) -> dict:
        """Feed a correction and re-emit, keeping all prior context and grounding."""
        return self._followup(
            session,
            "Apply this correction and call emit_protocol again with the full, updated "
            "protocol (keep everything else unchanged; ground any newly filled "
            "values):\n\n" + instruction.strip(),
            "emit_protocol",
            EMIT_PROTOCOL_TOOL,
            compact=True,
            system=self.system_emit,
            model=self.model,
            effort=self.effort,
            progress=progress,
        )

    # -- Design review (teach the experiment around the protocol) ---------------
    def design_review(self, session: Session, progress: Optional[Any] = None) -> dict:
        """Produce an experiment-design review of the emitted protocol."""
        return self._followup(session, DESIGN_REVIEW_INSTRUCTION, "emit_design_review",
                               EMIT_DESIGN_REVIEW_TOOL, compact=True, progress=progress)

    # -- Design alignment (does it directly test the hypothesis?) ---------------
    def design_alignment(self, session: Session, hypothesis: Optional[str] = None,
                         progress: Optional[Any] = None) -> dict:
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
                               EMIT_DESIGN_ALIGNMENT_TOOL, compact=True, progress=progress)

    # -- Adversarial correctness review (attack the emitted protocol) -----------
    def correctness_review(self, session: Session, quality_gate: dict = None,
                           progress: Optional[Any] = None) -> dict:
        """Skeptical, independent audit of the emitted protocol for logic/value/ordering/
        control errors. Runs in a FRESH, ephemeral reviewer context: the live session's
        authoring transcript (its reasoning, self-justification, SYSTEM_EMIT turns) is NEVER
        shown to the reviewer and is NOT mutated. The reviewer sees only the whitelisted
        blocks assembled by ``_render_review_input`` (source/summary, normalized protocol,
        user decisions, retrieved evidence, deterministic findings). Model-generated
        reasoning (not a host guarantee); its citations are host-verified. Runs on the
        review model — reasoning-heavy, with grounding bounded by PUBMED_BUDGET so an
        implausible_value / unit_or_scaling claim can be re-derived."""
        protocol = session.protocol
        if protocol is None:
            raise AgentError("Nothing to review yet — emit a protocol first.")
        if quality_gate is None:
            quality_gate = ((protocol.get("validation_report") or {}).get("quality_gate"))
        payload = _render_review_input(
            protocol,
            source_text=session.source_text, source_exact=session.source_exact,
            decisions=session.decisions, grounding_log=session.grounding_log,
            quality_gate=quality_gate,
        )
        messages = [{"role": "user", "content": payload}]  # throwaway transcript
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET, progress=progress)
        block = self._run(
            messages, _grounding_tools() + [EMIT_CORRECTNESS_REVIEW_TOOL],
            "emit_correctness_review", state,
            system=self.system_prompt, model=self.review_model, effort=self.effort,
        )
        if block is None:
            raise AgentError("Reviewer ended without calling emit_correctness_review.")
        return dict(block.input)

    def apply_correctness_fixes(self, session: Session, findings: list,
                                progress: Optional[Any] = None) -> dict:
        """Close the loop: feed the correctness review's fixes back in and re-emit a
        CORRECTED protocol. Keeps everything already right, preserves provenance, grounds
        any newly filled values. Returns the new protocol (host-validated by the caller)."""
        lines = []
        for f in findings or []:
            if not isinstance(f, dict):
                continue
            fix = str(f.get("fix") or "").strip()
            if not fix:
                continue
            loc = str(f.get("location") or "").strip()
            prob = str(f.get("problem") or "").strip()
            head = f"- [{f.get('severity', '')}] {loc + ': ' if loc else ''}{fix}"
            lines.append(head + (f"  (fixes: {prob})" if prob else ""))
        if not lines:
            raise AgentError("No applicable fixes in the correctness review.")
        instruction = (
            "A correctness review of the protocol above found the issues listed below. "
            "Apply EVERY fix and call emit_protocol again with the full, corrected protocol: "
            "keep everything that was already right, preserve the provenance discipline "
            "(quotes for stated values, real citations for literature_grounded), ground any "
            "newly introduced values, and do not reintroduce the problems. Note the "
            "corrections in the relevant provenance_note / open_questions.\n\n" + "\n".join(lines)
        )
        return self._followup(session, instruction, "emit_protocol", EMIT_PROTOCOL_TOOL,
                              compact=True, system=self.system_emit, model=self.model,
                              effort=self.effort, progress=progress)

    # -- Independent fix verification (fresh, ephemeral context) ----------------
    def verify_fixes(self, session: Session, prior_findings: list,
                     progress: Optional[Any] = None) -> dict:
        """Independent, fresh-context re-review of the CORRECTED protocol against the
        prior findings. Builds its OWN throwaway transcript via ``_run`` — the live
        session's apply turn and armed emit are NEVER shown to it and are NOT mutated,
        so this is safe to run after apply. Returns the raw verification dict
        (``dict(block.input)``); the host adjudicates it into ``fix_verification``.

        Grounding tools are included so an implausible_value / unit_or_scaling claim can
        be re-derived independently, bounded by PUBMED_BUDGET."""
        corrected = session.protocol                      # post-apply, already ensure_ids'd
        payload = _render_verification_input(corrected, prior_findings)
        messages = [{"role": "user", "content": payload}]  # throwaway local list
        state = RunState(session=session, searches_left=config.PUBMED_BUDGET, progress=progress)
        block = self._run(
            messages,
            _grounding_tools() + [EMIT_FIX_VERIFICATION_TOOL],
            "emit_fix_verification",
            state,
            system=self.system_prompt,
            model=self.review_model,
            effort=self.effort,
        )
        if block is None:
            raise AgentError("Verifier ended without calling emit_fix_verification.")
        return dict(block.input)
