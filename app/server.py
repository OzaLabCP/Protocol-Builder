"""FastAPI app: paste-text in, provenance-tagged protocol out.

Two endpoints mirror the phase boundary:
  POST /api/analyze  -> Phase 1 (questions, or straight to emit if no gaps)
  POST /api/resolve  -> Phase 2 + 3 + host-side validation

Sessions are held in memory (single-process demo). The browser only carries a
session_id; the message history stays server-side.
"""

from __future__ import annotations

import uuid
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from pathlib import Path

from .agent import AgentError, GapFillerAgent, Session
from .validation import validate_and_finalize

app = FastAPI(title="Methods Gap-Filler")

_STATIC = Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_SESSIONS: dict[str, Session] = {}
_agent: Optional[GapFillerAgent] = None


def get_agent() -> GapFillerAgent:
    global _agent
    if _agent is None:
        _agent = GapFillerAgent()
    return _agent


class AnalyzeRequest(BaseModel):
    methods_text: str


class Answer(BaseModel):
    id: str
    value: Any = None
    skipped: bool = False


class ResolveRequest(BaseModel):
    session_id: str
    answers: list[Answer] = []


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest) -> dict:
    text = (req.methods_text or "").strip()
    if len(text) < 40:
        raise HTTPException(400, "Please paste a Methods section (at least a few sentences).")
    agent = get_agent()
    try:
        session = agent.analyze(text)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001 - surface API/setup errors to the client
        raise HTTPException(500, _explain(exc))

    session_id = uuid.uuid4().hex
    _SESSIONS[session_id] = session
    phase1 = session.phase1 or {}

    if not phase1.get("usable", False):
        return {"session_id": session_id, "phase": "rejected", "phase1": phase1}

    gaps = phase1.get("gaps") or []
    if not gaps:
        # No gaps: skip the question step, go straight to emit.
        return _resolve(session_id, [])

    return {"session_id": session_id, "phase": "questions", "phase1": phase1}


@app.post("/api/resolve")
def resolve(req: ResolveRequest) -> dict:
    answers = [a.model_dump() for a in req.answers]
    return _resolve(req.session_id, answers)


def _resolve(session_id: str, answers: list) -> dict:
    session = _SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown or expired session. Start over.")
    agent = get_agent()
    try:
        protocol = agent.continue_with_answers(session, answers)
    except AgentError as exc:
        raise HTTPException(502, f"Model did not follow the tool contract: {exc}")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(500, _explain(exc))

    report = validate_and_finalize(protocol)
    return {
        "session_id": session_id,
        "phase": "complete",
        "protocol": protocol,
        "validation_report": report,
    }


def _explain(exc: Exception) -> str:
    msg = str(exc)
    if "api_key" in msg.lower() or "authentication" in msg.lower():
        return (
            "Anthropic API auth failed. Set ANTHROPIC_API_KEY (or run `ant auth login`) "
            "and restart the server."
        )
    return f"{type(exc).__name__}: {msg}"
