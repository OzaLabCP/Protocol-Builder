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

import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import AgentError, GapFillerAgent, Session
from .render import (
    design_alignment_to_markdown,
    design_review_to_markdown,
    protocol_to_markdown,
)
from .validation import validate_and_finalize, validate_design_review

MAX_PDF_BYTES = 25 * 1024 * 1024  # API hard limit is 32 MB; leave headroom
MAX_TEXT_CHARS = int(os.environ.get("GAPFILLER_MAX_TEXT_CHARS", "200000"))
SESSION_TTL = int(os.environ.get("GAPFILLER_SESSION_TTL", "3600"))
MAX_SESSIONS = int(os.environ.get("GAPFILLER_MAX_SESSIONS", "500"))

app = FastAPI(title="Methods Gap-Filler")

_STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


@dataclass
class Store:
    session: Session
    created: float
    protocol: Optional[dict] = None
    design_review: Optional[dict] = None
    design_alignment: Optional[dict] = None


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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/healthz")
def healthz() -> dict:
    return {
        "status": "ok",
        "model": config.MODEL,
        "grounding": {
            "web_search": config.ENABLE_WEB_SEARCH,
            "pubmed": config.ENABLE_PUBMED,
            "preprints": config.ENABLE_PREPRINTS,
            "protocols_io": config.ENABLE_PROTOCOLS_IO,
        },
        "sessions": len(_SESSIONS),
    }


@app.post("/api/analyze")
def analyze(
    methods_text: str = Form(default=""),
    hypothesis: str = Form(default=""),
    file: Optional[UploadFile] = File(default=None),
) -> dict:
    _prune()
    agent = get_agent()
    pdf_bytes: Optional[bytes] = None

    if file is not None and file.filename:
        pdf_bytes = file.file.read()
        if not pdf_bytes.startswith(b"%PDF"):
            raise HTTPException(400, "That file doesn't look like a PDF.")
        if len(pdf_bytes) > MAX_PDF_BYTES:
            raise HTTPException(400, "PDF is too large (max ~25 MB). Paste the Methods section instead.")

    if pdf_bytes is None:
        text = (methods_text or "").strip()
        if len(text) < 40:
            raise HTTPException(400, "Paste a Methods section (a few sentences) or choose a PDF.")
        if len(text) > MAX_TEXT_CHARS:
            raise HTTPException(400, f"That's very long (> {MAX_TEXT_CHARS} chars). Paste just the Methods section, or upload the PDF.")

    hyp = (hypothesis or "").strip() or None
    try:
        session = (
            agent.analyze(pdf=pdf_bytes, hypothesis=hyp)
            if pdf_bytes is not None
            else agent.analyze(methods_text=methods_text.strip(), hypothesis=hyp)
        )
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


@app.post("/api/resolve")
def resolve(req: ResolveRequest) -> dict:
    return _resolve(req.session_id, [a.model_dump() for a in req.answers])


@app.post("/api/revise")
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


@app.post("/api/design")
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


@app.post("/api/align")
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


@app.get("/api/protocol/{session_id}.md")
def download_markdown(session_id: str) -> PlainTextResponse:
    store = _get(session_id)
    if store.protocol is None:
        raise HTTPException(404, "No protocol generated for this session yet.")
    md = protocol_to_markdown(store.protocol)
    if store.design_alignment is not None:
        md += "\n\n" + design_alignment_to_markdown(store.design_alignment)
    if store.design_review is not None:
        md += "\n\n" + design_review_to_markdown(store.design_review)
    fname = _slug(store.protocol.get("title", "protocol")) + ".md"
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
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
    report = validate_and_finalize(protocol)
    store.protocol = protocol
    return {
        "session_id": session_id,
        "phase": "complete",
        "protocol": protocol,
        "validation_report": report,
        "grounding_log": store.session.grounding_log,
        "markdown_url": f"/api/protocol/{session_id}.md",
    }


def _slug(title: str) -> str:
    keep = [c.lower() if c.isalnum() else "-" for c in (title or "protocol")]
    s = "".join(keep).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:60] or "protocol"


def _explain(exc: Exception) -> str:
    msg = str(exc)
    if "api_key" in msg.lower() or "authentication" in msg.lower():
        return (
            "Anthropic API auth failed. Set ANTHROPIC_API_KEY (or run `ant auth login`) "
            "and restart the server."
        )
    return f"{type(exc).__name__}: {msg}"
