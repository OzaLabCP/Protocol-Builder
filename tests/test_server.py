"""HTTP-surface tests via FastAPI TestClient. These exercise everything that
does NOT require a live LLM provider (health, validation, error paths, routing);
flow tests inject a fake OpenRouter client."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from app.server import app  # noqa: E402

client = TestClient(app)


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "grounding" in body and "pubmed" in body["grounding"]


def test_index_served():
    r = client.get("/")
    assert r.status_code == 200
    assert "Methods Gap-Filler" in r.text


def test_analyze_empty_is_400():
    r = client.post("/api/analyze", data={"methods_text": ""})
    assert r.status_code == 400


def test_analyze_non_pdf_is_400():
    r = client.post("/api/analyze", files={"file": ("x.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 400
    assert "PDF" in r.json()["detail"]


def test_unknown_session_markdown_is_404():
    r = client.get("/api/protocol/deadbeef.md")
    assert r.status_code == 404


def test_revise_unknown_session_is_404():
    r = client.post("/api/revise", json={"session_id": "nope", "instruction": "change it"})
    assert r.status_code == 404


def test_revise_empty_instruction_is_400():
    r = client.post("/api/revise", json={"session_id": "nope", "instruction": ""})
    assert r.status_code == 400


def test_design_unknown_session_is_404():
    r = client.post("/api/design", json={"session_id": "nope"})
    assert r.status_code == 404


def test_align_unknown_session_is_404():
    r = client.post("/api/align", json={"session_id": "nope"})
    assert r.status_code == 404


def test_discover_short_is_400():
    r = client.post("/api/discover", json={"hypothesis": "too short"})
    assert r.status_code == 400


def test_discover_accepts_constraints_shape():
    # constraints + auto_pick are accepted (a 400 on the short hypothesis, not a 422)
    r = client.post("/api/discover", json={"hypothesis": "x", "constraints": {"equipment": "plate reader"}, "auto_pick": True})
    assert r.status_code == 400


def test_choose_assay_unknown_session_is_404():
    r = client.post("/api/choose_assay", json={"session_id": "nope", "assay_id": "x"})
    assert r.status_code == 404


def test_choose_assay_wrong_flow_is_409():
    # a session that never went through discovery has no assay_options -> 409
    import time as _t

    from app.agent import Session
    from app.server import Store, _SESSIONS
    sid = "flowtest"
    _SESSIONS[sid] = Store(session=Session(), created=_t.time())
    try:
        r = client.post("/api/choose_assay", json={"session_id": sid, "assay_id": "x"})
        assert r.status_code == 409
    finally:
        _SESSIONS.pop(sid, None)


def test_choose_assay_bad_id_is_400():
    # endpoint validates assay_id against stored options BEFORE any model call
    import time as _t

    from app.agent import Session
    from app.server import Store, _SESSIONS
    sid = "idtest"
    sess = Session(source_kind="hypothesis", assay_options={"assays": [{"id": "x"}]})
    _SESSIONS[sid] = Store(session=sess, created=_t.time())
    try:
        r = client.post("/api/choose_assay", json={"session_id": sid, "assay_id": "bogus"})
        assert r.status_code == 400
    finally:
        _SESSIONS.pop(sid, None)


def test_analyze_accepts_hypothesis_field():
    # hypothesis is optional; with empty text the endpoint still 400s on the text,
    # proving the field is accepted (not a 422 unprocessable-entity from an unknown form field).
    r = client.post("/api/analyze", data={"methods_text": "", "hypothesis": "X increases Y"})
    assert r.status_code == 400


# --- flows that need a fake LLM client (no network / API key) ---------------

import json as _json  # noqa: E402
import time as _time  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402


def _tool_msg(name, args, cid):
    """An OpenAI/OpenRouter-shaped assistant turn with a single tool call."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue); self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None):
        self.calls.append({"tool_choice": tool_choice}); return self.queue.pop(0)


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


def test_finish_downgrades_stated_on_hypothesis_session():
    proto = {"title": "P", "summary": "s", "estimated_duration": "1 h",
             "materials": [{"name": "Buffer", "provenance": "stated"}],
             "steps": [], "assumptions_log": []}
    _install_agent([_tool_msg("emit_protocol", proto, "e1")])
    sid = "hypfin"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="hypothesis", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time())
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 200
        body = r.json()
        assert body["protocol"]["materials"][0]["provenance"] == "default_verify"
        assert len(body["validation_report"]["stated_downgrades"]) >= 1
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_finish_keeps_stated_on_paper_session():
    proto = {"title": "P", "summary": "s", "estimated_duration": "1 h",
             "materials": [{"name": "Buffer", "provenance": "stated"}],
             "steps": [], "assumptions_log": []}
    _install_agent([_tool_msg("emit_protocol", proto, "e1")])
    sid = "paperfin"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time())
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 200
        assert r.json()["protocol"]["materials"][0]["provenance"] == "stated"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_discover_autopick_runs_full_flow():
    opts = {"usable": True, "hypothesis_restated": "H",
            "assays": [{"id": "fp", "name": "FP", "measures": "m", "why_tests_hypothesis": "w",
                        "critical_comparison": "c", "throughput": "high", "difficulty": "low",
                        "materials_burden": "cheap", "key_limitation": "k", "provenance": "best_practice"}],
            "recommended_assay_id": "fp", "recommendation_rationale": "r"}
    proto = {"title": "P", "summary": "s", "estimated_duration": "1 h",
             "materials": [], "steps": [], "assumptions_log": []}
    _install_agent([
        _tool_msg("emit_assay_options", opts, "a1"),
        _tool_msg("request_clarifications", {"usable": True, "gaps": []}, "c1"),
        _tool_msg("emit_protocol", proto, "e1"),
    ])
    try:
        r = client.post("/api/discover", json={"hypothesis": "Does X increase Y binding?", "auto_pick": True})
        assert r.status_code == 200
        body = r.json()
        assert body["phase"] == "complete"  # auto_pick drove through _choose to emit
        assert body["protocol"]["title"] == "P"
        assert body["chosen_assay"]["id"] == "fp"
    finally:
        srv._agent = None


def test_discover_rejected_flow_returns_rejected():
    _install_agent([_tool_msg("emit_assay_options", {"usable": False, "reason": "not testable"}, "a1")])
    try:
        r = client.post("/api/discover", json={"hypothesis": "banana banana banana"})
        assert r.status_code == 200
        body = r.json()
        assert body["phase"] == "rejected"
        assert body["assay_options"]["reason"] == "not testable"
    finally:
        srv._agent = None


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn(); print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1; print(f"FAIL {fn.__name__}"); traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
