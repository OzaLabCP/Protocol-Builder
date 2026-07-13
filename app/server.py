"""FastAPI app: paste-text or PDF in, provenance-tagged protocol out, with
export and edit-and-regenerate.

Endpoints:
  GET  /                       -> the UI
  GET  /healthz                -> liveness + config
  POST /api/analyze            -> Phase 1 (multipart: methods_text field or PDF file)
  POST /api/resolve            -> Phase 2 + 3 + validation
  POST /api/revise             -> apply a correction and re-emit
  GET  /api/protocol/{sid}.md  -> download the current protocol as Markdown

Sessions are held in memory (run one worker) and expire after SESSION_TTL.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import AgentError, GapFillerAgent, Session, _pdf_text
from .checks import assign_stable_ids, ensure_ids
from .projects import (
    WORKFLOWS,
    LifecycleStatus,
    ProtocolVersion,
    ValidationSummary,
    WorkflowType,
    build_input_summary,
    detect_workflow,
    entry_mode_for,
    make_project,
    new_version_id,
)
from .store import (
    ConcurrencyError,
    ProjectNotFound,
    ProjectStore,
    SQLiteProjectStore,
)
from .render import (
    assay_selection_to_markdown,
    correctness_review_to_markdown,
    design_alignment_to_markdown,
    design_review_to_markdown,
    grounding_log_to_markdown,
    materials_to_csv,
    protocol_to_markdown,
)
from .validation import (
    build_review_status,
    validate_and_finalize,
    validate_assay_options,
    validate_correctness_review,
    validate_design_alignment,
    validate_design_review,
)

MAX_PDF_BYTES = 25 * 1024 * 1024  # API hard limit is 32 MB; leave headroom
MAX_TEXT_CHARS = int(os.environ.get("GAPFILLER_MAX_TEXT_CHARS", "200000"))
SESSION_TTL = int(os.environ.get("GAPFILLER_SESSION_TTL", "3600"))
MAX_SESSIONS = int(os.environ.get("GAPFILLER_MAX_SESSIONS", "500"))

_log = logging.getLogger("gapfiller")

app = FastAPI(title="Methods Gap-Filler")

_STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# --- Access control (env-gated; no-ops when unconfigured) -------------------

_RATE: dict[str, list] = {}  # client -> recent request timestamps (in-memory)


def _client_ip(request: Request) -> str:
    if config.TRUST_PROXY:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            # Trust only the rightmost hop — the address our own proxy appended.
            # The leftmost entries are client-supplied and trivially spoofable, so
            # keying the rate limiter on them lets an attacker mint unlimited buckets.
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _supplied_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key", "").strip()


def require_auth(request: Request) -> None:
    """Reject requests without the configured token (constant-time compare)."""
    if not config.AUTH_TOKEN:
        return  # auth disabled
    supplied = _supplied_token(request)
    if not supplied or not secrets.compare_digest(supplied, config.AUTH_TOKEN):
        raise HTTPException(401, "Missing or invalid access token.")


def rate_limit(request: Request) -> None:
    """Fixed 60s window, per client. In-memory (single worker), pruned as it goes."""
    if config.RATE_LIMIT <= 0:
        return  # rate limiting disabled
    now = time.time()
    cutoff = now - 60.0
    ip = _client_ip(request)
    bucket = [t for t in _RATE.get(ip, []) if t >= cutoff]
    if len(bucket) >= config.RATE_LIMIT:
        raise HTTPException(429, "Rate limit exceeded. Please slow down and retry shortly.",
                            headers={"Retry-After": "60"})
    bucket.append(now)
    _RATE[ip] = bucket
    if len(_RATE) > 10000:  # bound memory
        # Drop clients with no recent activity first.
        for k in [k for k, v in _RATE.items() if not v or v[-1] < cutoff]:
            _RATE.pop(k, None)
        # If a flood of distinct fresh IPs still blows the cap, hard-evict the
        # least-recently-active keys so the map can't grow without bound.
        if len(_RATE) > 10000:
            for k, _v in sorted(_RATE.items(), key=lambda kv: kv[1][-1])[: len(_RATE) - 10000]:
                _RATE.pop(k, None)


# Dependency bundles applied per route.
_MUTATING = [Depends(require_auth), Depends(rate_limit)]  # POSTs that drive the model
_READONLY = [Depends(require_auth)]  # GET downloads (auth header only; cheap, not rate-limited)


@dataclass
class Store:
    session: Session
    created: float
    protocol: Optional[dict] = None
    design_review: Optional[dict] = None
    design_alignment: Optional[dict] = None
    correctness_review: Optional[dict] = None
    fix_verification: Optional[dict] = None  # last independent fix-verification outcome
    assay_options: Optional[dict] = None
    chosen_assay: Optional[dict] = None
    # Live activity feed for the current long op: [{seq, msg}], polled by the client.
    progress: list = field(default_factory=list)
    _plock: Any = field(default_factory=threading.Lock)
    # Serializes mutating ops on this session. Sync endpoints run in Starlette's threadpool,
    # so two concurrent requests on one session_id (e.g. a double-click) would otherwise race
    # on session.messages and corrupt the transcript — the transactional restores assume
    # single-threaded access. Reentrant so nested helper calls (choose→resolve) don't deadlock.
    lock: Any = field(default_factory=threading.RLock)
    # Optional second sink for notes (e.g. a pre-session pid feed the client is already
    # polling on the auto_pick path, where discover→choose→build share one request).
    mirror: Any = None
    # Links this in-memory session to a durable ExperimentProject (projects layer).
    # None for sessions that never went through the projects bridge; when set, _finish
    # persists a ProtocolVersion and advances the project's lifecycle.
    project_id: Optional[str] = None

    def start_progress(self) -> None:
        """Clear the feed at the start of a new long op so the client (polling from 0)
        sees only this op's steps."""
        with self._plock:
            self.progress = []

    def note(self, msg: str) -> None:
        """Append one activity line. Thread-safe: the op runs in Starlette's worker thread
        while the client polls concurrently."""
        m = str(msg)
        with self._plock:
            self.progress.append({"seq": len(self.progress) + 1, "msg": m})
        if self.mirror:  # outside the lock — mirror takes its own
            self.mirror(m)

    def steps_after(self, seq: int) -> list:
        with self._plock:
            return [s for s in self.progress if s["seq"] > seq]


_SESSIONS: dict[str, Store] = {}
_agent: Optional[GapFillerAgent] = None

# Durable projects layer (SQLite). Instantiated at import (parallel to _agent, but eager
# so the DB/schema is ready before the first request). The in-memory _SESSIONS dict and
# per-session RLock discipline are unchanged; _STORE has its own lock for DB writes.
_STORE: ProjectStore = SQLiteProjectStore()

# Pre-session activity feeds: discover/analyze mint their session mid-call, so the client
# can't key progress on a session id yet. It supplies a short-lived progress_id instead, and
# these functions back a feed the same shape the client already polls. Bounded by count.
_PRE: dict[str, dict] = {}
_PRE_LOCK = threading.Lock()
_MAX_PRE = 200


def _pre_start(pid: str) -> None:
    if not pid:
        return
    with _PRE_LOCK:
        _PRE[pid] = {"steps": [], "created": time.time()}
        if len(_PRE) > _MAX_PRE:  # evict oldest feeds
            for k, _v in sorted(_PRE.items(), key=lambda kv: kv[1]["created"])[: len(_PRE) - _MAX_PRE]:
                _PRE.pop(k, None)


def _pre_note(pid: str, msg: str) -> None:
    if not pid:
        return
    with _PRE_LOCK:
        buf = _PRE.get(pid)
        if buf is not None:
            buf["steps"].append({"seq": len(buf["steps"]) + 1, "msg": str(msg)})


def _pre_steps(pid: str, after: int) -> list:
    with _PRE_LOCK:
        buf = _PRE.get(pid)
        return [s for s in buf["steps"] if s["seq"] > after] if buf else []


def get_agent() -> GapFillerAgent:
    global _agent
    if _agent is None:
        _agent = GapFillerAgent()
    return _agent


def _now() -> float:
    return time.time()


def _prune() -> None:
    cutoff = _now() - SESSION_TTL
    for sid in [s for s, st in _SESSIONS.items() if st.created < cutoff]:
        _SESSIONS.pop(sid, None)
    # Hard cap on live sessions (bounded memory) — evict the oldest beyond the cap.
    if len(_SESSIONS) > MAX_SESSIONS:
        for sid, _st in sorted(_SESSIONS.items(), key=lambda kv: kv[1].created)[: len(_SESSIONS) - MAX_SESSIONS]:
            _SESSIONS.pop(sid, None)


def _get(session_id: str) -> Store:
    _prune()
    store = _SESSIONS.get(session_id)
    if store is None:
        raise HTTPException(404, "Unknown or expired session. Start over.")
    # Refresh on access: the TTL is measured from LAST use, not creation, so an active
    # multi-step session (each phase is a slow model call) isn't pruned out from under a
    # user still working it. This also makes the MAX_SESSIONS cap evict least-recently-used.
    store.created = _now()
    return store


# ---------------------------------------------------------------------------

class Answer(BaseModel):
    id: str
    value: Any = None
    skipped: bool = False


class ResolveRequest(BaseModel):
    session_id: str
    answers: list[Answer] = []


class ReviseRequest(BaseModel):
    session_id: str
    instruction: str


class DesignRequest(BaseModel):
    session_id: str


class CritiqueRequest(BaseModel):
    session_id: str
    apply: bool = False  # True: audit, then auto-apply the fixes and return the rebuilt protocol


class AlignRequest(BaseModel):
    session_id: str
    hypothesis: Optional[str] = None


class DiscoverRequest(BaseModel):
    hypothesis: str
    constraints: Optional[dict] = None
    auto_pick: bool = False
    progress_id: Optional[str] = None  # client-supplied key for the pre-session activity feed
    project_id: Optional[str] = None  # optional link into the durable projects layer


class ChooseAssayRequest(BaseModel):
    session_id: str
    assay_id: str


class EditPatchRequest(BaseModel):
    target_id: str
    field: str
    value: Any = None
    unit: Optional[str] = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/healthz")
def healthz(request: Request) -> dict:
    auth_required = bool(config.AUTH_TOKEN)
    # On a locked instance, don't disclose config (model, key state, grounding, session
    # count) to an unauthenticated caller — just liveness + that a token is needed.
    supplied = _supplied_token(request)
    if auth_required and not (supplied and secrets.compare_digest(supplied, config.AUTH_TOKEN)):
        return {"status": "ok", "auth_required": True}
    return {
        "status": "ok",
        "provider": config.LLM_PROVIDER,
        "base_url": config.OPENROUTER_BASE_URL,
        "model": config.MODEL,
        "model_fast": config.MODEL_FAST or config.MODEL,
        "api_key_set": bool(config.OPENROUTER_API_KEY),
        "prompt_cache": config.PROMPT_CACHE,
        "reasoning_effort": {"heavy": config.REASONING_EFFORT, "light": config.REASONING_EFFORT_FAST},
        "auto_review": config.AUTO_REVIEW,
        "auth_required": auth_required,
        "grounding": {
            "pubmed": config.ENABLE_PUBMED,
            "preprints": config.ENABLE_PREPRINTS,
            "protocols_io": config.ENABLE_PROTOCOLS_IO,
        },
        "sessions": len(_SESSIONS),
    }


@app.post("/api/analyze", dependencies=_MUTATING)
def analyze(
    methods_text: str = Form(default=""),
    hypothesis: str = Form(default=""),
    progress_id: str = Form(default=""),
    project_id: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
) -> dict:
    _prune()
    agent = get_agent()
    is_full_paper = False
    text: str

    if file is not None and file.filename:
        # Reject on declared size BEFORE buffering the whole upload — a single in-memory
        # worker holds every session, so blindly reading a huge body could OOM the process
        # and wipe all live sessions.
        if file.size is not None and file.size > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")
        pdf_bytes = file.file.read(MAX_PDF_BYTES + 1)  # cap the read
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(400, "That file doesn't look like a PDF.")
        # Extract text host-side so this works behind any model (no native-PDF reliance).
        extracted = _pdf_text(pdf_bytes)
        if not extracted:
            raise HTTPException(400, "Couldn't read text from that PDF (it may be scanned or "
                                     "image-only). Paste the Methods section instead.")
        text = extracted[:MAX_TEXT_CHARS]
        is_full_paper = True
    else:
        text = (methods_text or "").strip()
        if len(text) < 40:
            raise HTTPException(400, "Paste a Methods section (a few sentences) or choose a PDF.")
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"That's very long (> {MAX_TEXT_CHARS} chars). Paste just the Methods section, or upload the PDF.")

    hyp = (hypothesis or "").strip() or None
    pid = (progress_id or "").strip()
    _pre_start(pid)
    note = (lambda m: _pre_note(pid, m)) if pid else None
    if note:
        note("Reading the methods and reconstructing the protocol…")
    try:
        session = agent.analyze(methods_text=text, hypothesis=hyp,
                                is_full_paper=is_full_paper, progress=note)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))

    session_id = uuid.uuid4().hex
    store = Store(session=session, created=_now())
    _SESSIONS[session_id] = store
    # Bridge: attach to (or create) a durable project so this generation is persisted.
    # A stale/unknown project_id never 404s a generation — try_get_project → None → fresh.
    _bridge_attach(
        store, session_id, (project_id or "").strip(),
        analyze=True, is_full_paper=is_full_paper,
        seed_inputs={
            "free_text": "" if is_full_paper else text,
            "pdf_text": text if is_full_paper else "",
            "pdf_present": is_full_paper,
            "hypothesis": hyp or "",
            "pdf_filename": (file.filename if (file is not None and is_full_paper) else None),
        },
    )
    phase1 = session.phase1 or {}

    if not phase1.get("usable", False):
        # Rejected: neutralize the pending clarification so a stray /api/resolve can't try
        # to build a protocol from a non-usable session.
        session.request_tool_use_id = None
        return {"session_id": session_id, "phase": "rejected", "phase1": phase1}
    if not (phase1.get("gaps") or []):
        # No clarifications -> build straight away in this same request. Mirror the build's
        # session-keyed notes into the pre-session feed the client is polling (like auto_pick),
        # so the activity feed doesn't go silent for the whole build.
        if note:
            store.mirror = note
        return _resolve(session_id, [])
    return {"session_id": session_id, "phase": "questions", "phase1": phase1}


@app.post("/api/resolve", dependencies=_MUTATING)
def resolve(req: ResolveRequest) -> dict:
    store = _get(req.session_id)
    # Client-state guard (e.g. a double-clicked "Build protocol"): the clarification was
    # already consumed. Return a clean 409 instead of a 502 that blames the model.
    if store.session.request_tool_use_id is None:
        if store.protocol is not None:
            raise HTTPException(409, "This protocol is already built — use Refine to change "
                                     "it, or start over for a new one.")
        raise HTTPException(409, "No pending questions to answer for this session. Start over.")
    store.start_progress()  # fresh activity feed for this build
    return _resolve(req.session_id, [a.model_dump() for a in req.answers])


@app.post("/api/revise", dependencies=_MUTATING)
def revise(req: ReviseRequest) -> dict:
    instruction = (req.instruction or "").strip()
    if len(instruction) < 3:
        raise HTTPException(400, "Describe the correction you'd like.")
    store = _get(req.session_id)
    if store.protocol is None:  # parity with design/critique/align/apply_fixes
        raise HTTPException(409, "Generate a protocol first, then request a revision.")
    with store.lock:  # serialize against any concurrent op on this session
        store.start_progress()
        store.note("Applying your correction and rebuilding the protocol…")
        agent = get_agent()
        try:
            protocol = agent.revise(store.session, instruction, progress=store.note)
        except AgentError as exc:
            _mark_failed(store.project_id)
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            _mark_failed(store.project_id)
            raise HTTPException(500, _explain(exc))
        store.note("Done.")
        return _finish(req.session_id, store, protocol, source_op="revise")


@app.post("/api/design", dependencies=_MUTATING)
def design(req: DesignRequest) -> dict:
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then request a design review.")
    with store.lock:  # serialize against any concurrent op on this session
        store.start_progress()
        store.note("Reasoning about the experiment design…")
        agent = get_agent()
        try:
            review = agent.design_review(store.session, progress=store.note)
        except AgentError as exc:
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, _explain(exc))
        report = validate_design_review(review)
        store.design_review = review
        return {"session_id": req.session_id, "design_review": review, "validation_report": report}


@app.post("/api/critique", dependencies=_MUTATING)
def critique(req: CritiqueRequest) -> dict:
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then run a correctness review.")
    with store.lock:  # serialize against any concurrent op on this session
        store.start_progress()
        store.note("Auditing the protocol for correctness & practicality…")
        agent = get_agent()
        try:
            review = agent.correctness_review(store.session, progress=store.note)
        except AgentError as exc:
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, _explain(exc))
        report = validate_correctness_review(review)
        findings = review.get("findings") or []
        fixable = [f for f in findings if isinstance(f, dict) and f.get("fix")]

        # One-step mode: audit AND apply, returning the findings + the corrected protocol.
        if req.apply and fixable:
            store.note(f"Applying {len(fixable)} fix(es) and rebuilding…")
            try:
                protocol = agent.apply_correctness_fixes(store.session, findings, progress=store.note)
            except AgentError as exc:
                raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(500, _explain(exc))
            # Independent, fresh-context re-review of the corrected protocol against the
            # ORIGINAL findings. Computed BEFORE _finish so the review gate feeds the
            # persisted ProtocolVersion.result and the project's validation_summary.
            fixv = _verify_fixes(store, findings)
            result = _finish(req.session_id, store, protocol,
                             source_op="critique_apply", fix_verification=fixv)
            store.correctness_review = None  # stale as an actionable to-apply list
            store.fix_verification = fixv    # retain the verification outcome
            # result already carries fix_verification + review_status via _finish.
            return {**result,
                    "correctness_review": review, "review_validation_report": report,
                    "applied": True, "fixes_applied": len(fixable)}

        store.correctness_review = review
        return {"session_id": req.session_id, "correctness_review": review,
                "validation_report": report, "applied": False}


@app.post("/api/apply_fixes", dependencies=_MUTATING)
def apply_fixes(req: DesignRequest) -> dict:
    """Close the loop: apply the correctness review's fixes and rebuild the protocol."""
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then run a correctness review.")
    findings = (store.correctness_review or {}).get("findings") or []
    if not any(isinstance(f, dict) and f.get("fix") for f in findings):
        raise HTTPException(409, "Run a correctness review that finds fixable issues first.")
    with store.lock:  # serialize against any concurrent op on this session
        store.start_progress()
        store.note("Applying the review's fixes and rebuilding…")
        agent = get_agent()
        try:
            protocol = agent.apply_correctness_fixes(store.session, findings, progress=store.note)
        except AgentError as exc:
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, _explain(exc))
        # Independent re-review of the corrected protocol against the ORIGINAL findings,
        # computed BEFORE _finish so the review gate feeds the persisted version/summary.
        fixv = _verify_fixes(store, findings)
        result = _finish(req.session_id, store, protocol,
                         source_op="apply_fixes", fix_verification=fixv)
        store.correctness_review = None  # stale as an actionable to-apply list
        store.fix_verification = fixv    # retain the verification outcome
        return result  # already carries fix_verification + review_status


@app.post("/api/align", dependencies=_MUTATING)
def align(req: AlignRequest) -> dict:
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then test it against your hypothesis.")
    with store.lock:  # serialize against any concurrent op on this session
        store.start_progress()
        store.note("Checking whether the protocol directly tests the hypothesis…")
        agent = get_agent()
        try:
            alignment = agent.design_alignment(store.session, req.hypothesis, progress=store.note)
        except AgentError as exc:
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, _explain(exc))
        # design_alignment() sets session.hypothesis when the caller supplied one, so a
        # truthy value here (now or from analyze/discover) is the host truth for `inferred`.
        report = validate_design_alignment(
            alignment, hypothesis_supplied=bool(store.session.hypothesis))
        store.design_alignment = alignment
        return {"session_id": req.session_id, "design_alignment": alignment,
                "validation_report": report}


@app.post("/api/discover", dependencies=_MUTATING)
def discover(req: DiscoverRequest) -> dict:
    _prune()
    hyp = (req.hypothesis or "").strip()
    if len(hyp) < 12:
        raise HTTPException(400, "State a hypothesis or goal to test (a sentence).")
    agent = get_agent()
    pid = (req.progress_id or "").strip()
    _pre_start(pid)
    note = (lambda m: _pre_note(pid, m)) if pid else None
    if note:
        note("Searching the literature for candidate assays…")
    try:
        session = agent.discover(hyp, req.constraints, progress=note)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))

    session_id = uuid.uuid4().hex
    store = Store(session=session, created=_now())
    _SESSIONS[session_id] = store
    _bridge_attach(
        store, session_id, (req.project_id or "").strip(),
        analyze=False, is_full_paper=False,
        seed_inputs={"hypothesis": hyp, "constraints": req.constraints or {}},
    )
    opts = session.assay_options or {}

    if not opts.get("usable", True) or not (opts.get("assays") or []):
        return {"session_id": session_id, "phase": "rejected", "assay_options": opts}

    report = validate_assay_options(opts)
    store.assay_options = opts
    if req.auto_pick:
        # This request also runs choose+build; mirror those session-keyed notes into the
        # pre-session feed the client is polling so the feed doesn't go silent mid-build.
        store.mirror = note
        return _choose(session_id, opts.get("recommended_assay_id"))
    return {"session_id": session_id, "phase": "assays",
            "assay_options": opts, "validation_report": report}


@app.post("/api/choose_assay", dependencies=_MUTATING)
def choose_assay(req: ChooseAssayRequest) -> dict:
    store = _get(req.session_id)
    opts = store.session.assay_options
    if not opts or opts.get("usable") is False or not (opts.get("assays") or []):
        raise HTTPException(409, "Start from a usable hypothesis first, then choose an assay.")
    ids = [a.get("id") for a in opts.get("assays", [])]
    if req.assay_id not in ids:
        raise HTTPException(400, "Unknown assay for this session.")  # before any model call
    store.start_progress()  # fresh activity feed for the build this kicks off
    return _choose(req.session_id, req.assay_id)


@app.get("/api/progress/{session_id}", dependencies=_READONLY)
def progress(session_id: str, after: int = 0) -> dict:
    """Live activity feed for the in-flight long op, keyed by session id (build phases) or a
    client-supplied progress_id (pre-session discover/analyze). Polled concurrently with the
    blocking POST, which runs in a Starlette worker thread. Returns only steps newer than
    `after`. Unknown/expired key -> empty (a poll shouldn't 404)."""
    store = _SESSIONS.get(session_id)
    if store is not None:
        return {"steps": store.steps_after(after)}
    return {"steps": _pre_steps(session_id, after)}


def _choose(session_id: str, assay_id: str) -> dict:
    store = _get(session_id)
    agent = get_agent()
    with store.lock:  # serialize against any concurrent op on this session
        store.note("Setting up the protocol for this assay…")
        try:
            phase1 = agent.choose_assay(store.session, assay_id, progress=store.note)
        except AgentError as exc:
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(500, _explain(exc))
        store.chosen_assay = store.session.chosen_assay
        if not (phase1.get("gaps") or []):
            return _resolve(session_id, [])  # no gaps -> straight to emit (reentrant lock)
        return {"session_id": session_id, "phase": "questions", "phase1": phase1}


@app.get("/api/protocol/{session_id}.md", dependencies=_READONLY)
def download_markdown(session_id: str) -> PlainTextResponse:
    store = _get(session_id)
    if store.protocol is None:
        raise HTTPException(404, "No protocol generated for this session yet.")
    md = ""
    if store.chosen_assay is not None:
        md += assay_selection_to_markdown(store.chosen_assay, store.assay_options or {}) + "\n\n"
    md += protocol_to_markdown(store.protocol)
    if store.design_alignment is not None:
        md += "\n\n" + design_alignment_to_markdown(store.design_alignment)
    if store.design_review is not None:
        md += "\n\n" + design_review_to_markdown(store.design_review)
    if store.correctness_review is not None:
        md += "\n\n" + correctness_review_to_markdown(store.correctness_review)
    log_md = grounding_log_to_markdown(store.session.grounding_log)
    if log_md:
        md += "\n\n" + log_md
    fname = _slug(store.protocol.get("title", "protocol")) + ".md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/protocol/{session_id}/materials.csv", dependencies=_READONLY)
def download_materials_csv(session_id: str) -> PlainTextResponse:
    store = _get(session_id)
    if store.protocol is None:
        raise HTTPException(404, "No protocol generated for this session yet.")
    csv_text = materials_to_csv(store.protocol)
    fname = _slug(store.protocol.get("title", "protocol")) + "-materials.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _resolve(session_id: str, answers: list) -> dict:
    store = _get(session_id)
    agent = get_agent()
    with store.lock:  # serialize against any concurrent op on this session
        store.note("Drafting the protocol from your answers…")
        try:
            protocol = agent.continue_with_answers(store.session, answers, progress=store.note)
        except AgentError as exc:
            _mark_failed(store.project_id)
            raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
        except Exception as exc:  # noqa: BLE001
            _mark_failed(store.project_id)
            raise HTTPException(500, _explain(exc))
        store.note("Verifying citations and finalizing…")
        result = _finish(session_id, store, protocol)
        if config.AUTO_REVIEW:
            result = _auto_review(session_id, store, result)
        store.note("Done.")
        return result


def _auto_review(session_id: str, store: Store, result: dict) -> dict:
    """Pipeline stage: adversarially audit the just-emitted protocol for correctness AND
    practicality and apply the fixes automatically, so the user receives an already-corrected
    protocol with the findings attached (transparency). Never lets the audit break delivery —
    any failure falls back to the un-audited-but-valid protocol."""
    agent = get_agent()
    store.note("Auditing the protocol for correctness & practicality…")
    try:
        review = agent.correctness_review(store.session, progress=store.note)
        validate_correctness_review(review)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort; never block the protocol
        _log.warning("auto-review skipped: %s", exc)  # log the message, not just the type
        store.note("Audit couldn't run — deliver the protocol as drafted; use Re-review & fix.")
        # Tell the client the audit was attempted-and-failed, so the UI can say so instead of
        # implying an audit ran and found nothing.
        return {**result, "auto_review": True, "audit_status": "unavailable"}
    findings = review.get("findings") or []
    fixable = [f for f in findings if isinstance(f, dict) and f.get("fix")]
    applied = 0
    if fixable:
        store.note(f"Audit found {len(fixable)} issue(s) — applying fixes…")
        try:
            fixed = agent.apply_correctness_fixes(store.session, findings, progress=store.note)
            result = _finish(session_id, store, fixed, source_op="auto_review")  # re-validate + replace
            applied = len(fixable)
            store.correctness_review = None  # applied — stale against the corrected protocol
        except Exception as exc:  # noqa: BLE001
            _log.warning("auto-fix skipped: %s", type(exc).__name__)
            store.correctness_review = review  # keep for a manual apply
    else:
        store.note("Audit found no fixable issues — the draft holds.")
        store.correctness_review = review
    return {**result, "correctness_review": review, "auto_review": True, "fixes_applied": applied}


def _verify_fixes(store: Store, original_findings: list) -> dict:
    """Best-effort independent re-review of the CORRECTED protocol against the ORIGINAL
    (pre-fix) findings. Runs on a fresh, ephemeral transcript (the apply turn is never
    shown to it). Never blocks delivery — any failure yields a `not_reviewed`
    fix_verification and the corrected protocol is still returned (mirrors the
    _persist_protocol_version / _auto_review best-effort discipline)."""
    fixable = [f for f in (original_findings or []) if isinstance(f, dict) and f.get("fix")]
    if not fixable:
        return build_review_status([], {}, checked=False)  # -> not_reviewed / no_original_findings
    try:
        # Feed the corrected protocol (already stored by _finish) to the independent verifier.
        store.session.protocol = store.protocol
        store.note("Independently re-checking that each fix actually landed…")
        verification = get_agent().verify_fixes(store.session, fixable, progress=store.note)
        return build_review_status(fixable, verification, checked=True)
    except Exception as exc:  # noqa: BLE001 — best-effort, never block delivery
        _log.warning("fix-verification skipped: %s", type(exc).__name__)
        store.note("Independent re-check couldn't run — protocol delivered as corrected.")
        fv = build_review_status(fixable, {}, checked=False)
        fv["reason"] = "verifier_error"
        return fv


def _finish(
    session_id: str,
    store: Store,
    protocol: dict,
    source_op: str = "resolve",
    fix_verification: dict | None = None,
) -> dict:
    report = validate_and_finalize(
        protocol,
        allow_stated=(store.session.source_kind != "hypothesis"),
        source_text=store.session.source_text,
        source_exact=store.session.source_exact,
    )
    store.protocol = protocol
    # The complete, opaque payload. Persisted verbatim as ProtocolVersion.result so a
    # restore rehydrates renderResult unchanged. The two projects-layer keys are always
    # present (null when this session isn't linked to a durable project).
    r: dict = {
        "session_id": session_id,
        "phase": "complete",
        "protocol": protocol,
        "validation_report": report,
        "grounding_log": store.session.grounding_log,
        "markdown_url": f"/api/protocol/{session_id}.md",
        "materials_csv_url": f"/api/protocol/{session_id}/materials.csv",
        "chosen_assay": store.chosen_assay,
        "project_id": store.project_id,
        "protocol_version_id": None,
    }
    if fix_verification is not None:
        # Epic-3 additive keys: host-owned object + compact string mirror.
        r["fix_verification"] = fix_verification
        r["review_status"] = fix_verification.get("status")
    if store.project_id:
        # Server-authoritative persistence: one _finish == one ProtocolVersion. Best-effort —
        # a store failure never breaks protocol delivery (the response is still returned).
        try:
            vid = new_version_id()
            r["protocol_version_id"] = vid  # embed before snapshotting so result is complete
            _persist_protocol_version(
                store, protocol, report, r, source_op, vid, fix_verification
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("protocol-version persist skipped: %s", type(exc).__name__)
            r["protocol_version_id"] = None
    return r


ALLOWED_FIELDS = {
    "material": {"amount", "unit", "concentration"},
    "critical_parameter": {"value", "unit"},
}


def _coerce_scalar(v):
    if v is None:
        return ""
    return str(v).strip()


def _find_entity_by_id(protocol: dict, target_id: str):
    """Return (entity_dict, kind) or (None, None). kind is 'material' /
    'critical_parameter' for editable kinds, else a non-editable kind label
    so the handler can distinguish unknown-id (404) from known-but-locked (422)."""
    # An entity resolves by its positional _id OR its additive stable id
    # (material_id/step_id/…). _id is tested first; the two id schemes have disjoint
    # prefixes (mat:/step: vs m_/s_), so there is no ambiguity.
    for mat in protocol.get("materials", []) or []:
        if mat.get("_id") == target_id or mat.get("material_id") == target_id:
            return mat, "material"
    for step in protocol.get("steps", []) or []:
        if step.get("_id") == target_id or step.get("step_id") == target_id:
            return step, "step"
        for cp in step.get("critical_parameters", []) or []:
            if cp.get("_id") == target_id or cp.get("parameter_id") == target_id:
                return cp, "critical_parameter"
        for ss in step.get("substeps", []) or []:
            if ss.get("_id") == target_id or ss.get("substep_id") == target_id:
                return ss, "substep"
    ts = protocol.get("titration_series")
    if ts:
        if ts.get("_id") == target_id:
            return ts, "titration"
        for pt in ts.get("points", []) or []:
            if pt.get("_id") == target_id or pt.get("point_id") == target_id:
                return pt, "titration"
            for comp in pt.get("components", []) or []:
                if comp.get("_id") == target_id or comp.get("component_id") == target_id:
                    return comp, "titration"
    return None, None


@app.post("/api/protocol/{session_id}/edit", dependencies=_MUTATING)
def edit_value(session_id: str, req: EditPatchRequest) -> dict:
    store = _get(session_id)  # 404 unknown/expired session
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then edit a value.")
    with store.lock:
        ensure_ids(store.protocol)  # idempotent
        # Additive stable ids, so an /edit by material_id/step_id/… resolves and
        # the edited entity keeps its stable id through the value/provenance flip
        # (preservation), even though its positional _id may later shift.
        assign_stable_ids(store.protocol)  # idempotent, preserving
        entity, kind = _find_entity_by_id(store.protocol, req.target_id)
        if entity is None:
            raise HTTPException(404, "Unknown value id for this protocol.")
        if kind not in ALLOWED_FIELDS:
            raise HTTPException(422, "This value type is not inline-editable.")
        if req.field not in ALLOWED_FIELDS[kind]:
            raise HTTPException(422, f"Field '{req.field}' cannot be edited on this value.")
        new_val = _coerce_scalar(req.value)
        if new_val == "":
            raise HTTPException(422, "Value cannot be empty.")
        old_val = entity.get(req.field)
        entity[req.field] = new_val
        if req.field != "unit" and req.unit is not None:
            u = _coerce_scalar(req.unit)
            entity["unit"] = u or None
        # HONESTY: the user is now the source. Flip provenance, strip stale grounding.
        entity["provenance"] = "user_input"
        entity["provenance_note"] = f"Corrected by you (was {old_val!r})."  # SET, not append
        entity["citation"] = None
        entity["citation_verified"] = False
        entity["quote_verified"] = False
        entity["needs_user_input"] = False
        entity.pop("evidence", None)
        return _finish(session_id, store, store.protocol, source_op="edit")


def _slug(title: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in (title or "protocol")]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:60] or "protocol"


def _explain(exc: Exception) -> str:
    """Client-safe message. Full detail (including any upstream body) is logged
    server-side only — never echoed to the client, which could otherwise leak
    provider/internal error text."""
    _log.warning("request failed: %s", type(exc).__name__)
    low = str(exc).lower()
    if "openrouter_api_key" in low or "401" in low or "no auth credentials" in low or (
        "auth" in low and "openrouter" in low
    ):
        return "The model provider rejected the request (authentication). Check the server's LLM_API_KEY."
    if "timeout" in low or "timed out" in low or "readtimeout" in low or "connecttimeout" in low:
        return ("The model took too long and the request timed out. This usually means a very "
                "large or complex build — try a narrower scope or fewer conditions, then retry.")
    return "Something went wrong while building the protocol. Please try again; if it persists, check the server logs."


# ===========================================================================
# Projects layer (Milestone-1) — additive. See NORMATIVE SPEC §5.
# ===========================================================================

_PROJECT_404 = "Unknown or expired project. Start a new one."
_PROJECT_409 = "This project was modified by another request. Reload and retry."


class ConfirmWorkflowRequest(BaseModel):
    workflow: str


# --- store-mutation helpers (get_versioned → mutate → update, retry once) ---

def _update_with_retry(project_id: str, mutate) -> None:
    """Read the project with its row_version, apply ``mutate`` (in place), and write it
    back with optimistic concurrency. Retries once on a lost race. Propagates
    ProjectNotFound and (after the retry) ConcurrencyError to the caller."""
    last: Optional[ConcurrencyError] = None
    for attempt in range(2):
        project, rv = _STORE.get_project_versioned(project_id)
        mutate(project)
        try:
            _STORE.update_project(project, expected_row_version=rv)
            return
        except ConcurrencyError as exc:
            last = exc
            continue
    if last is not None:
        raise last


def _link_session(project_id: str, session_id: str) -> None:
    """Bind a live in-memory session to the durable project and mark it building.
    protocol_ready → building is an allowed (revise/retry) transition."""
    def mutate(p):
        p.session_id = session_id
        p.lifecycle_status = LifecycleStatus.building
    _update_with_retry(project_id, mutate)


def _set_workflow(project_id: str, workflow: str) -> None:
    """Set/override the confirmed workflow. MUST NOT touch inputs, protocol_versions,
    session_id, current_protocol_version_id, or detected_workflow. Idempotent."""
    def mutate(p):
        p.workflow = WorkflowType(workflow)
        p.confirmation_required = False
        if p.lifecycle_status in (
            LifecycleStatus.intake_received,
            LifecycleStatus.awaiting_confirmation,
        ):
            p.lifecycle_status = LifecycleStatus.workflow_confirmed
    _update_with_retry(project_id, mutate)


def _mark_failed(project_id: Optional[str]) -> None:
    """Best-effort: flag a project-linked generation that raised. A store error here
    never masks the original HTTP error the caller is about to raise."""
    if not project_id:
        return
    try:
        def mutate(p):
            p.lifecycle_status = LifecycleStatus.failed
        _update_with_retry(project_id, mutate)
    except Exception as exc:  # noqa: BLE001
        _log.warning("mark_failed suppressed: %s", type(exc).__name__)


def _validation_summary_from_report(
    report: dict, fix_verification: dict | None = None
) -> ValidationSummary:
    """Denormalize the validation report into the project's ValidationSummary. Counts are
    best-effort (the report has no top-level status); the full report is retained.

    The optional `fix_verification` (Epic-3) is a DISTINCT report axis from the Epic-2
    `quality_gate`; both feed the single `status` via a monotone-toward-blocked combiner.
    Legacy callers pass no `fix_verification` ⇒ escalation branches are dead and the
    derived status/counts are byte-identical to before."""
    report = report or {}
    downgraded = len(report.get("downgraded") or [])
    stated = len(report.get("stated_downgrades") or [])
    quotes = report.get("quotes") or {}
    unverified = len(quotes.get("unverified") or [])
    support = report.get("support") or {}
    mismatch = len(support.get("mismatch") or [])

    gate = report.get("quality_gate") or {}
    gate_counts = gate.get("counts") or {}
    error_count = int(gate_counts.get("errors", 0))
    gate_warnings = int(gate_counts.get("warnings", 0))

    warning_count = downgraded + stated + mismatch + unverified + gate_warnings
    unverified_citation_count = downgraded + unverified

    if error_count > 0 or gate.get("status") == "blocked":
        status = "blocked"
    elif warning_count > 0:
        status = "warnings"
    else:
        status = "clean"

    # --- Epic-3 review gate: monotone escalation toward `blocked` --------------
    fv = fix_verification or {}
    review_status = fv.get("status", "unknown")
    unresolved = int(fv.get("unresolved_count", 0) or 0)
    if fv.get("unresolved_blocking"):
        status = "blocked"  # an unresolved critical/major review finding blocks
    elif review_status == "issues_remain" and status == "clean":
        status = "warnings"  # minor-only unresolved → warn, never block

    return ValidationSummary(
        status=status,
        error_count=error_count,
        warning_count=warning_count,
        unverified_citation_count=unverified_citation_count,
        report=report,
        checked_at=datetime.now(timezone.utc),
        review_status=review_status,
        unresolved_finding_count=unresolved,
    )


_PROVENANCE_BY_OP = {
    "resolve": "initial",
    "auto_review": "auto_review",
    "apply_fixes": "apply_fixes",
    "critique_apply": "critique_apply",
    "revise": "revise",
    "edit": "user_edit",
}


def _persist_protocol_version(
    store: Store,
    protocol: dict,
    report: dict,
    result: dict,
    source_op: str,
    version_id: str,
    fix_verification: dict | None = None,
) -> None:
    """Write a ProtocolVersion row, then update the project (append version_id, set
    current_protocol_version_id, refresh validation_summary/title, advance lifecycle to
    protocol_ready). One _finish == one version; current always points at the newest."""
    project, _rv = _STORE.get_project_versioned(store.project_id)
    version_number = len(project.protocol_versions) + 1
    provenance = _PROVENANCE_BY_OP.get(source_op, source_op)
    ver = ProtocolVersion(
        version_id=version_id,
        project_id=store.project_id,
        version_number=version_number,
        provenance=provenance,
        source_op=source_op,
        title=(protocol.get("title") or "protocol"),
        result=result,
        validation_summary=report,
        chosen_assay=store.chosen_assay,
        created_at=datetime.now(timezone.utc),
    )
    _STORE.save_protocol_version(ver)

    summary = _validation_summary_from_report(report, fix_verification)
    title = (protocol.get("title") or "").strip()

    def mutate(p):
        if ver.version_id not in p.protocol_versions:
            p.protocol_versions.append(ver.version_id)
        p.current_protocol_version_id = ver.version_id
        p.lifecycle_status = LifecycleStatus.protocol_ready
        p.validation_summary = summary
        if title:
            p.title = title
    _update_with_retry(store.project_id, mutate)


# --- intake bridge (analyze/discover attach-or-create) ----------------------

def _bridge_attach(
    store: Store, session_id: str, project_id: str, *,
    analyze: bool, is_full_paper: bool, seed_inputs: dict,
) -> None:
    """Attach the session to an existing project (by id) or create a fresh one, then link
    the live session. Wholly best-effort: a stale/unknown project_id never 404s a
    generation, and any store error leaves the generation untouched (store.project_id None)."""
    try:
        proj = None
        if project_id:
            proj = _STORE.try_get_project(project_id)
        if proj is None:
            detected = (
                "reproduce" if (analyze and is_full_paper)
                else "adapt" if analyze
                else "design"
            )
            try:
                summary = build_input_summary(seed_inputs)
            except Exception:  # noqa: BLE001
                summary = {}
            title = (summary.get("title") if isinstance(summary, dict) else None) or "Untitled project"
            constraints = seed_inputs.get("constraints") or None
            new = make_project(
                title=title,
                workflow=detected,
                detected_workflow=detected,
                confirmation_required=False,
                hypothesis=(seed_inputs.get("hypothesis") or None),
                inputs=seed_inputs,
                input_summary=summary if isinstance(summary, dict) else {},
                constraints=constraints,
            )
            proj = _STORE.create_project(new)
        store.project_id = proj.project_id
        _link_session(proj.project_id, session_id)
    except Exception as exc:  # noqa: BLE001
        _log.warning("project bridge skipped: %s", type(exc).__name__)


# --- API serializers --------------------------------------------------------

def _constraints_api(c) -> Optional[dict]:
    d: dict = {}
    if c.equipment:
        d["equipment"] = c.equipment
    if c.time:
        d["time"] = c.time
    if c.skill:
        d["skill"] = c.skill
    if c.extra:
        d.update(c.extra)
    return d or None


def _report_ok(report: dict) -> bool:
    report = report or {}
    quotes = report.get("quotes") or {}
    support = report.get("support") or {}
    return not (
        (report.get("downgraded") or [])
        or (report.get("stated_downgrades") or [])
        or (quotes.get("unverified") or [])
        or (support.get("mismatch") or [])
    )


def _version_summary(v: ProtocolVersion) -> dict:
    return {
        "id": v.version_id,
        "version_number": v.version_number,
        "created_at": v.created_at.isoformat(),
        "provenance": v.provenance.value,
        "source_op": v.source_op,
        "title": v.title,
        "validation_ok": _report_ok(v.validation_summary or {}),
    }


def _current_version(project, versions: list) -> Optional[ProtocolVersion]:
    if project.current_protocol_version_id:
        for v in versions:
            if v.version_id == project.current_protocol_version_id:
                return v
    return versions[-1] if versions else None


def _project_detail(project) -> dict:
    """Assemble the GET /api/project/{id} restore payload (§5.2). Never exposes raw
    `inputs` or full pdf_text; download URLs on latest_result are rewritten to the
    project-stable forms so restore-after-restart downloads work."""
    versions = _STORE.list_protocol_versions(project.project_id)
    cur = _current_version(project, versions)
    latest_result: Optional[dict] = None
    if cur is not None:
        latest_result = dict(cur.result)
        latest_result["markdown_url"] = f"/api/project/{project.project_id}/protocol.md"
        latest_result["materials_csv_url"] = f"/api/project/{project.project_id}/materials.csv"
    version_number = cur.version_number if cur is not None else 0
    session_live = bool(project.session_id and project.session_id in _SESSIONS)
    summary = project.input_summary or {}
    seed_text = summary.get("preview", "") if isinstance(summary, dict) else ""
    seed_filename = summary.get("pdf_filename") if isinstance(summary, dict) else None
    return {
        "id": project.project_id,
        "title": project.title,
        "workflow": project.workflow.value,
        "detected_workflow": project.detected_workflow.value,
        "entry_mode": entry_mode_for(project.workflow),
        "detection_confidence": project.detection_confidence,
        "detection_reason": project.detection_reason,
        "confirmation_required": project.confirmation_required,
        "lifecycle_status": project.lifecycle_status.value,
        "version": version_number,
        "created_at": project.created_at.isoformat(),
        "updated_at": project.updated_at.isoformat(),
        "session_id": project.session_id,
        "session_live": session_live,
        "hypothesis": project.hypothesis,
        "scientific_goal": project.scientific_goal,
        "measurement_objective": project.measurement_objective,
        "constraints": _constraints_api(project.constraints),
        "input_summary": project.input_summary,
        "seed_input": {"text": seed_text, "filename": seed_filename},
        "current_protocol_version_id": project.current_protocol_version_id,
        "validation_summary": (
            project.validation_summary.model_dump(mode="json")
            if project.validation_summary else None
        ),
        "protocol_versions": [_version_summary(v) for v in versions],
        "latest_result": latest_result,
    }


def _summary_api(s) -> dict:
    return {
        "id": s.project_id,
        "title": s.title,
        "workflow": s.workflow.value,
        "detected_workflow": s.detected_workflow.value,
        "entry_mode": entry_mode_for(s.workflow),
        "lifecycle_status": s.lifecycle_status.value,
        "detection_confidence": s.detection_confidence,
        "confirmation_required": s.confirmation_required,
        "version": s.version,
        "has_protocol": s.has_protocol,
        "current_protocol_version_id": s.current_protocol_version_id,
        "created_at": s.created_at.isoformat(),
        "updated_at": s.updated_at.isoformat(),
    }


# --- endpoints --------------------------------------------------------------

@app.post("/api/project", dependencies=_MUTATING)
@app.post("/api/intake", dependencies=_MUTATING)  # legacy alias
def create_project(
    free_text: str = Form(default=""),
    text: str = Form(default=""),  # C-frontend alias for free_text
    identifier: str = Form(default=""),
    hypothesis: str = Form(default=""),
    measurement_goal: str = Form(default=""),
    existing_protocol_text: str = Form(default=""),
    requested_workflow: str = Form(default=""),
    workflow: str = Form(default=""),  # C-frontend alias for requested_workflow
    constraints: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
) -> dict:
    """Create + classify a DRAFT project with a cheap heuristic (NO model call)."""
    _prune()
    free_text = (free_text or "").strip() or (text or "").strip()
    identifier = (identifier or "").strip()
    hypothesis = (hypothesis or "").strip()
    measurement_goal = (measurement_goal or "").strip()
    existing_protocol_text = (existing_protocol_text or "").strip()
    requested = (requested_workflow or "").strip() or (workflow or "").strip()
    if requested and requested not in WORKFLOWS:
        raise HTTPException(400, "Unknown workflow.")

    try:
        constraints_dict = json.loads(constraints) if constraints.strip() else {}
        if not isinstance(constraints_dict, dict):
            constraints_dict = {}
    except Exception:  # noqa: BLE001
        constraints_dict = {}

    # PDF extraction (host-side; NO model). Validated exactly as /api/analyze.
    pdf_text = ""
    pdf_filename = None
    if file is not None and file.filename:
        if file.size is not None and file.size > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")
        pdf_bytes = file.file.read(MAX_PDF_BYTES + 1)
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(400, "That file doesn't look like a PDF.")
        extracted = _pdf_text(pdf_bytes)
        if not extracted:
            raise HTTPException(400, "Couldn't read text from that PDF (it may be scanned or "
                                     "image-only). Paste the Methods section instead.")
        pdf_text = extracted[:MAX_TEXT_CHARS]
        pdf_filename = file.filename

    if not any([free_text, identifier, hypothesis, measurement_goal,
                existing_protocol_text, pdf_text]):
        raise HTTPException(
            400,
            "Describe what you want to build, paste a protocol, give an identifier, "
            "or upload a PDF.",
        )

    inputs = {
        "requested_workflow": requested,
        "identifier": identifier,
        "free_text": free_text,
        "hypothesis": hypothesis,
        "measurement_goal": measurement_goal,
        "existing_protocol_text": existing_protocol_text,
        "pdf_present": bool(pdf_text),
        "pdf_text": pdf_text,
        "pdf_filename": pdf_filename,
        "constraints": constraints_dict,
    }
    detection = detect_workflow(inputs)
    input_summary = build_input_summary(inputs)
    title = input_summary.get("title") or "Untitled project"

    project = make_project(
        title=title,
        workflow=detection.detected_workflow,
        detected_workflow=detection.detected_workflow,
        detection_confidence=detection.detection_confidence,
        detection_reason=detection.detection_reason,
        confirmation_required=detection.confirmation_required,
        scientific_goal=(free_text or None),
        hypothesis=(hypothesis or None),
        measurement_objective=(measurement_goal or None),
        constraints=constraints_dict or None,
        inputs=inputs,
        input_summary=input_summary,
    )
    project = _STORE.create_project(project)

    return {
        "id": project.project_id,
        "detected_workflow": project.detected_workflow.value,
        "workflow": project.workflow.value,
        "entry_mode": entry_mode_for(project.workflow),
        "detection_confidence": project.detection_confidence,
        "detection_reason": project.detection_reason,
        "confirmation_required": project.confirmation_required,
        "lifecycle_status": project.lifecycle_status.value,
        "seed_input": {"text": input_summary.get("preview", ""), "filename": pdf_filename},
        "input_summary": input_summary,
    }


@app.get("/api/projects", dependencies=_READONLY)
def list_projects(limit: int = 20) -> dict:
    limit = max(1, min(100, int(limit)))
    summaries = _STORE.list_projects(limit=limit, order_by="updated_at", descending=True)
    return {"projects": [_summary_api(s) for s in summaries]}


@app.get("/api/project/{project_id}", dependencies=_READONLY)
def get_project(project_id: str) -> dict:
    try:
        project = _STORE.get_project(project_id)
    except ProjectNotFound:
        raise HTTPException(404, _PROJECT_404)
    return _project_detail(project)


@app.post("/api/project/{project_id}/workflow", dependencies=_MUTATING)
@app.post("/api/project/{project_id}/confirm_workflow", dependencies=_MUTATING)  # legacy alias
def confirm_workflow(project_id: str, req: ConfirmWorkflowRequest) -> dict:
    wf = (req.workflow or "").strip()
    if wf not in WORKFLOWS:
        raise HTTPException(400, "Unknown workflow.")
    try:
        _set_workflow(project_id, wf)
        project = _STORE.get_project(project_id)
    except ProjectNotFound:
        raise HTTPException(404, _PROJECT_404)
    except ConcurrencyError:
        raise HTTPException(409, _PROJECT_409)
    return _project_detail(project)


@app.get("/api/project/{project_id}/protocol.md", dependencies=_READONLY)
def project_markdown(project_id: str) -> PlainTextResponse:
    _project, cur = _current_version_or_404(project_id)
    protocol = cur.result.get("protocol") or {}
    md = ""
    if cur.chosen_assay is not None:
        md += assay_selection_to_markdown(cur.chosen_assay, {}) + "\n\n"
    md += protocol_to_markdown(protocol)
    log = cur.result.get("grounding_log")
    log_md = grounding_log_to_markdown(log) if log else ""
    if log_md:
        md += "\n\n" + log_md
    fname = _slug(protocol.get("title", "protocol")) + ".md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/api/project/{project_id}/materials.csv", dependencies=_READONLY)
def project_materials_csv(project_id: str) -> PlainTextResponse:
    _project, cur = _current_version_or_404(project_id)
    protocol = cur.result.get("protocol") or {}
    csv_text = materials_to_csv(protocol)
    fname = _slug(protocol.get("title", "protocol")) + "-materials.csv"
    return PlainTextResponse(
        csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


def _current_version_or_404(project_id: str):
    try:
        project = _STORE.get_project(project_id)
    except ProjectNotFound:
        raise HTTPException(404, _PROJECT_404)
    versions = _STORE.list_protocol_versions(project_id)
    cur = _current_version(project, versions)
    if cur is None:
        raise HTTPException(404, "No protocol generated for this project yet.")
    return project, cur
