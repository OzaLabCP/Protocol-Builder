"""Server surface — clarification/preflight HTML markers + /api/resolve mode acceptance.

TestClient, NO network, NO LLM. Part 1 asserts on the served static/index.html text that
the three-choice control, submit gate, preflight dialog and mode-emitting collectAnswer
are present (graceful-degradation markers). Part 2 drives /api/resolve with a fake agent to
prove an ``Answer`` carrying ``mode`` is accepted (not 422) and that a legacy answer with no
``mode`` still resolves (back-compat).

Runnable directly (``python tests/test_resolve_modes.py``) or under pytest.
"""

from __future__ import annotations

import json as _json
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402

client = TestClient(srv.app)

_HTML = client.get("/").text


# --- Part 1: served-HTML markers (Bucket-1 clarification/preflight) ---------

def test_index_has_mode_radiogroup():
    assert "radiogroup" in _HTML
    assert "_mode_answer" in _HTML
    assert "Leave unresolved" in _HTML


def test_index_has_preflight_panel():
    assert "preflight-panel" in _HTML
    assert "Generate protocol" in _HTML
    assert "aria-modal" in _HTML


def test_index_has_submit_gate():
    assert "updateSubmitGate" in _HTML
    assert "gate-error" in _HTML
    assert "aria-live" in _HTML


def test_index_collect_answer_emits_mode():
    # collectAnswer must emit an explicit per-gap mode; empty -> unresolved (never default).
    assert 'mode: "unresolved"' in _HTML
    assert 'mode: "default"' in _HTML
    # the empty-vs-answered branch: empty typed value maps to unresolved, never default.
    assert 'empty ? "unresolved" : "answered"' in _HTML
    assert 'defRadio.value = "default"' in _HTML  # the default radio (only w/ suggested_default)


# --- Part 2: /api/resolve accepts the mode field ---------------------------

def _tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue)

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        return self.queue.pop(0)


_PROTO = {"title": "P", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [], "assumptions_log": []}


def _seed_pending_session(sid):
    srv._agent = GapFillerAgent(client=_FakeLLM([_tool_msg("emit_protocol", _PROTO, "e1")]), model="fake")
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}],
                        phase1={"questions": ["what temperature?"]}),
        created=_time.time())


def test_resolve_accepts_mode_field():
    sid = "resolvemode"
    _seed_pending_session(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": [
            {"id": "g1", "value": None, "mode": "unresolved"}]})
        assert r.status_code == 200, r.text  # NOT 422 — mode is an accepted field
        assert r.json()["protocol"]["title"] == "P"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_resolve_legacy_answer_no_mode_still_works():
    sid = "resolvelegacy"
    _seed_pending_session(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": [
            {"id": "g1", "value": "37 C"}]})  # no mode field at all (back-compat)
        assert r.status_code == 200, r.text
        assert r.json()["protocol"]["title"] == "P"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


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
