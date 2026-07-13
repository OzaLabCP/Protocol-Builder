"""Epic-3 review failure policy — degrade vs. block (`GAPFILLER_REVIEW_REQUIRED`).

Covers requirements (3) and (4), plus the lock/rollback invariants (7):

  * REVIEW_REQUIRED false (default): a review that raises DEGRADES — review_status becomes
    `unavailable`, a human `review_warning` is attached, a deduped `[REVIEW UNAVAILABLE]`
    line is appended to the protocol's open_questions, and the protocol is STILL returned
    (HTTP 200).
  * REVIEW_REQUIRED true: the same failure BLOCKS delivery with an actionable HTTP 424 that
    names GAPFILLER_REVIEW_REQUIRED and carries NO protocol body.
  * a REQUIRED 424 is never recorded in the idempotency cache (stays retryable); the lock
    is released (no deadlock); and a genuine state conflict is still a 409, never a 424.

Offline: a fake OpenRouter client with a call counter drives the endpoints; a review is
made to "raise" simply by leaving its tool response un-queued (the fake then pops an empty
queue). No network, no live model.

Runnable directly (`python tests/test_review_required.py`) or under pytest.
"""

from __future__ import annotations

import json as _json
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.config as _config  # noqa: E402
import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402
from app.server import app, on_review_failure  # noqa: E402

client = TestClient(app)


def _tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        self.calls.append({"model": model})
        return self.queue.pop(0)   # empty queue -> IndexError: the "review raised" path


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


_PROTO = {"title": "Draft", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [{"instruction": "mix"}], "assumptions_log": []}


def _seed_resolve(sid, **kw):
    """A session parked at the pending clarification, ready for /api/resolve to emit."""
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), **kw)


# --- (3) degrade: REQUIRED false -----------------------------------------------

def test_degrade_default_sets_unavailable_and_returns_protocol():
    # Only the emit is queued; the auto-review's correctness_review then pops an empty
    # queue and raises. Under the default policy the protocol is still delivered.
    assert _config.REVIEW_REQUIRED is False
    saved = _config.AUTO_REVIEW
    _config.AUTO_REVIEW = True
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1")])
    sid = "req_degrade"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 200                       # NOT a 424, NOT a 500
        body = r.json()
        assert body["protocol"]["title"] == "Draft"       # protocol delivered
        assert body["review_status"] == "unavailable"
        assert body["audit_status"] == "unavailable"
        assert body.get("review_warning", "").startswith("[REVIEW UNAVAILABLE]")
        oq = [q for q in (body["protocol"].get("open_questions") or [])
              if isinstance(q, str) and q.startswith("[REVIEW UNAVAILABLE]")]
        assert len(oq) == 1                               # annotated, exactly once
    finally:
        _config.AUTO_REVIEW = saved
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_degrade_open_questions_dedup():
    # on_review_failure must not stack duplicate [REVIEW UNAVAILABLE] lines on re-failure.
    assert _config.REVIEW_REQUIRED is False
    proto = {"open_questions": ["keep me"]}
    on_review_failure(RuntimeError("x"), result={}, protocol=proto, stage="correctness review")
    on_review_failure(RuntimeError("y"), result={}, protocol=proto, stage="correctness review")
    banners = [q for q in proto["open_questions"] if q.startswith("[REVIEW UNAVAILABLE]")]
    assert len(banners) == 1
    assert "keep me" in proto["open_questions"]           # pre-existing entries preserved


def test_verifier_error_degrades_to_unavailable():
    # apply_fixes: emit lands, but the fresh fix-verification pops an empty queue and raises.
    # REQUIRED false -> fix_verification.reason=verifier_error -> review_status unavailable.
    assert _config.REVIEW_REQUIRED is False
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="Fixed"), "e2")])
    sid = "req_verr"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"},
        correctness_review={"verdict": "issues_found", "findings": [
            {"severity": "major", "category": "ordering", "problem": "p", "fix": "add buffer first"}]})
    try:
        r = client.post("/api/apply_fixes", json={"session_id": sid})
        assert r.status_code == 200
        body = r.json()
        assert body["protocol"]["title"] == "Fixed"       # corrected protocol delivered
        assert body["fix_verification"]["reason"] == "verifier_error"
        assert body["review_status"] == "unavailable"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


# --- (4) block: REQUIRED true --------------------------------------------------

def test_required_blocks_with_424_and_no_protocol():
    saved_req, saved_auto = _config.REVIEW_REQUIRED, _config.AUTO_REVIEW
    _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = True, True
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1")])  # emit ok; review then raises
    sid = "req_block"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 424                        # Failed Dependency, not 200/500
        body = r.json()
        assert "GAPFILLER_REVIEW_REQUIRED" in body["detail"]
        assert "NOT delivered" in body["detail"]
        assert "protocol" not in body                      # no protocol body on a block
    finally:
        _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = saved_req, saved_auto
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_required_block_not_recorded_in_idem():
    # A blocked op must never poison the idempotency cache — the key stays retryable.
    saved_req, saved_auto = _config.REVIEW_REQUIRED, _config.AUTO_REVIEW
    _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = True, True
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1")])
    sid = "req_noidem"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": [],
                                              "idempotency_key": "x"})
        assert r.status_code == 424
        assert "resolve:x" not in srv._SESSIONS[sid].idem  # raise precedes idem_put
    finally:
        _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = saved_req, saved_auto
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_required_block_releases_lock_and_preserves_emit():
    # The 424 raises inside `with store.lock` and propagates out, so the lock is released
    # (no deadlock) and the successful emit that preceded the failed review is preserved.
    saved_req, saved_auto = _config.REVIEW_REQUIRED, _config.AUTO_REVIEW
    _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = True, True
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1")])
    sid = "req_lock"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 424
        store = srv._SESSIONS[sid]
        got = store.lock.acquire(timeout=1)               # lock must be free
        assert got is True
        store.lock.release()
        assert store.protocol is not None                 # emit preserved, not rolled back
    finally:
        _config.REVIEW_REQUIRED, _config.AUTO_REVIEW = saved_req, saved_auto
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_state_conflict_is_409_not_424():
    # A genuine per-session state conflict stays 409; 424 is reserved for the review block.
    sid = "req_409"
    srv._SESSIONS[sid] = srv.Store(session=Session(messages=[]), created=_time.time())  # no protocol
    try:
        r = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells"})
        assert r.status_code == 409
        assert r.status_code != 424
    finally:
        srv._SESSIONS.pop(sid, None)


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
