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

import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import AgentError, GapFillerAgent, Session, _pdf_text
from .render import (
    assay_selection_to_markdown,
    design_alignment_to_markdown,
    design_review_to_markdown,
    grounding_log_to_markdown,
    materials_to_csv,
    protocol_to_markdown,
)
from .validation import (
    validate_and_finalize,
    validate_assay_options,
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
            return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _supplied_token(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-key", "").strip()
            or request.query_params.get("t", "").strip())


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
    if len(_RATE) > 10000:  # bound memory: drop clients with no recent activity
        for k in [k for k, v in _RATE.items() if not v or v[-1] < cutoff]:
            _RATE.pop(k, None)


# Dependency bundles applied per route.
_MUTATING = [Depends(require_auth), Depends(rate_limit)]  # POSTs that drive the model
_READONLY = [Depends(require_auth)]  # GET downloads (auth via ?t=; cheap, not rate-limited)


@dataclass
class Store:
    session: Session
    created: float
    protocol: Optional[dict] = None
    design_review: Optional[dict] = None
    design_alignment: Optional[dict] = None
    assay_options: Optional[dict] = None
    chosen_assay: Optional[dict] = None


_SESSIONS: dict[str, Store] = {}
_agent: Optional[GapFillerAgent] = None


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


class AlignRequest(BaseModel):
    session_id: str
    hypothesis: Optional[str] = None


class DiscoverRequest(BaseModel):
    hypothesis: str
    constraints: Optional[dict] = None
    auto_pick: bool = False


class ChooseAssayRequest(BaseModel):
    session_id: str
    assay_id: str


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "provider": "openrouter",
        "model": config.MODEL,
        "api_key_set": bool(config.OPENROUTER_API_KEY),
        "auth_required": bool(config.AUTH_TOKEN),
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
    file: Optional[UploadFile] = File(default=None),
) -> dict:
    _prune()
    agent = get_agent()
    is_full_paper = False
    text: str

    if file is not None and file.filename:
        pdf_bytes = file.file.read()
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(400, "That file doesn't look like a PDF.")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")
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
    try:
        session = agent.analyze(methods_text=text, hypothesis=hyp, is_full_paper=is_full_paper)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))

    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = Store(session=session, created=_now())
    phase1 = session.phase1 or {}

    if not phase1.get("usable", False):
        return {"session_id": session_id, "phase": "rejected", "phase1": phase1}
    if not (phase1.get("gaps") or []):
        return _resolve(session_id, [])  # no gaps -> straight to emit
    return {"session_id": session_id, "phase": "questions", "phase1": phase1}


@app.post("/api/resolve", dependencies=_MUTATING)
def resolve(req: ResolveRequest) -> dict:
    return _resolve(req.session_id, [a.model_dump() for a in req.answers])


@app.post("/api/revise", dependencies=_MUTATING)
def revise(req: ReviseRequest) -> dict:
    instruction = (req.instruction or "").strip()
    if len(instruction) < 3:
        raise HTTPException(400, "Describe the correction you'd like.")
    store = _get(req.session_id)
    agent = get_agent()
    try:
        protocol = agent.revise(store.session, instruction)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))
    return _finish(req.session_id, store, protocol)


@app.post("/api/design", dependencies=_MUTATING)
def design(req: DesignRequest) -> dict:
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then request a design review.")
    agent = get_agent()
    try:
        review = agent.design_review(store.session)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))
    report = validate_design_review(review)
    store.design_review = review
    return {"session_id": req.session_id, "design_review": review, "validation_report": report}


@app.post("/api/align", dependencies=_MUTATING)
def align(req: AlignRequest) -> dict:
    store = _get(req.session_id)
    if store.protocol is None:
        raise HTTPException(409, "Generate a protocol first, then test it against your hypothesis.")
    agent = get_agent()
    try:
        alignment = agent.design_alignment(store.session, req.hypothesis)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))
    store.design_alignment = alignment
    return {"session_id": req.session_id, "design_alignment": alignment}


@app.post("/api/discover", dependencies=_MUTATING)
def discover(req: DiscoverRequest) -> dict:
    _prune()
    hyp = (req.hypothesis or "").strip()
    if len(hyp) < 12:
        raise HTTPException(400, "State a hypothesis or goal to test (a sentence).")
    agent = get_agent()
    try:
        session = agent.discover(hyp, req.constraints)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))

    session_id = uuid.uuid4().hex
    store = Store(session=session, created=_now())
    _SESSIONS[session_id] = store
    opts = session.assay_options or {}

    if not opts.get("usable", True) or not (opts.get("assays") or []):
        return {"session_id": session_id, "phase": "rejected", "assay_options": opts}

    report = validate_assay_options(opts)
    store.assay_options = opts
    if req.auto_pick:
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
    return _choose(req.session_id, req.assay_id)


def _choose(session_id: str, assay_id: str) -> dict:
    store = _get(session_id)
    agent = get_agent()
    try:
        phase1 = agent.choose_assay(store.session, assay_id)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))
    store.chosen_assay = store.session.chosen_assay
    if not (phase1.get("gaps") or []):
        return _resolve(session_id, [])  # no gaps -> straight to emit
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
    try:
        protocol = agent.continue_with_answers(store.session, answers)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))
    return _finish(session_id, store, protocol)


def _finish(session_id: str, store: Store, protocol: dict) -> dict:
    report = validate_and_finalize(
        protocol,
        allow_stated=(store.session.source_kind != "hypothesis"),
        source_text=store.session.source_text,
        source_exact=store.session.source_exact,
    )
    store.protocol = protocol
    return {
        "session_id": session_id,
        "phase": "complete",
        "protocol": protocol,
        "validation_report": report,
        "grounding_log": store.session.grounding_log,
        "markdown_url": f"/api/protocol/{session_id}.md",
        "materials_csv_url": f"/api/protocol/{session_id}/materials.csv",
        "chosen_assay": store.chosen_assay,
    }


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
    _log.warning("request failed: %s: %s", type(exc).__name__, exc)
    low = str(exc).lower()
    if "openrouter_api_key" in low or "401" in low or "no auth credentials" in low or (
        "auth" in low and "openrouter" in low
    ):
        return "The model provider rejected the request (authentication). Check the server's OPENROUTER_API_KEY."
    return "Something went wrong while building the protocol. Please try again; if it persists, check the server logs."
