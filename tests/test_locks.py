"""Value-lock enforcement backend — pure ``_enforce_locks`` + the /api/revise end-to-end
restore path. NO network, NO live LLM.

Policy under test: RESTORE a locked entity's value fields on any model change (matched by
stable id), ABORT (409) when the revision makes the locked id disappear, and IGNORE an
unknown lock (forward-compatible). End-to-end: a fake agent mutating a locked parameter,
driven through /api/revise with ``locked_ids``, delivers the ORIGINAL value back and reports
``restored_locked_ids``. Omitting ``locked_ids`` reproduces today's behavior exactly.

Runnable directly (``python tests/test_locks.py``) or under pytest.
"""

from __future__ import annotations

import copy
import json as _json
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402
from app.checks import assign_stable_ids, ensure_ids  # noqa: E402

client = TestClient(srv.app)


# --- fixtures ---------------------------------------------------------------

def _proto_with_param(value="50", unit="%"):
    """A protocol whose single critical parameter (identity 'Denaturation temp') is lockable.
    Identity (name) drives the stable id; the value is what a lock protects."""
    return {
        "title": "P", "summary": "s", "estimated_duration": "1 h",
        "materials": [],
        "steps": [{"title": "PCR", "instruction": "cycle",
                   "critical_parameters": [
                       {"name": "Denaturation temp", "value": value, "unit": unit,
                        "provenance": "best_practice"}]}],
        "assumptions_log": [],
    }


def _param_stable_id(proto):
    probe = copy.deepcopy(proto)
    ensure_ids(probe); assign_stable_ids(probe)
    return probe["steps"][0]["critical_parameters"][0]["parameter_id"]


# --- pure _enforce_locks ----------------------------------------------------

def test_enforce_locks_restores_value():
    base = _proto_with_param(value="50", unit="%")
    pid = _param_stable_id(base)
    new = _proto_with_param(value="150", unit="%")   # model changed the value
    srv._enforce_locks(base, new, [pid])
    assert new["steps"][0]["critical_parameters"][0]["value"] == "50"  # restored from base


def test_enforce_locks_returns_changed_ids():
    base = _proto_with_param(value="50")
    pid = _param_stable_id(base)
    changed = srv._enforce_locks(base, _proto_with_param(value="150"), [pid])
    assert changed == [pid]
    # No actual change -> id NOT reported as restored.
    unchanged = srv._enforce_locks(base, _proto_with_param(value="50"), [pid])
    assert unchanged == []


def test_enforce_locks_409_on_disappearance():
    base = _proto_with_param(value="50")
    pid = _param_stable_id(base)
    # New protocol dropped the parameter entirely -> identity gone -> cannot preserve.
    new = _proto_with_param(value="50")
    new["steps"][0]["critical_parameters"] = []
    try:
        srv._enforce_locks(base, new, [pid])
        assert False, "expected HTTPException(409)"
    except HTTPException as e:
        assert e.status_code == 409
        assert "locked" in e.detail.lower()


def test_enforce_locks_ignores_unknown_lock():
    base = _proto_with_param(value="50")
    new = _proto_with_param(value="150")
    # A lock id that never existed in base is skipped (no raise, no restore).
    changed = srv._enforce_locks(base, new, ["p_deadbeef"])
    assert changed == []
    assert new["steps"][0]["critical_parameters"][0]["value"] == "150"  # untouched


# --- end-to-end /api/revise -------------------------------------------------

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


def _seed_revise(sid, base_proto, model_returns):
    srv._agent = GapFillerAgent(client=_FakeLLM([_tool_msg("emit_protocol", model_returns, "e2")]),
                                model="fake")
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol=copy.deepcopy(base_proto))


def test_revise_end_to_end_lock():
    sid = "reviselock"
    base = _proto_with_param(value="50", unit="%")
    pid = _param_stable_id(base)
    mutated = _proto_with_param(value="150", unit="%")  # the model tries to change it
    _seed_revise(sid, base, mutated)
    try:
        r = client.post("/api/revise", json={
            "session_id": sid, "instruction": "make the annealing step longer",
            "locked_ids": [pid]})
        assert r.status_code == 200, r.text
        body = r.json()
        got = body["protocol"]["steps"][0]["critical_parameters"][0]["value"]
        assert got == "50"  # locked value preserved across the revise
        assert body["restored_locked_ids"] == [pid]
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_revise_no_locks_identical():
    sid = "revisenolock"
    base = _proto_with_param(value="50", unit="%")
    mutated = _proto_with_param(value="150", unit="%")
    _seed_revise(sid, base, mutated)
    try:
        r = client.post("/api/revise", json={
            "session_id": sid, "instruction": "make the annealing step longer"})
        assert r.status_code == 200, r.text
        body = r.json()
        # No locks -> the model's change stands, and no restored_locked_ids key appears.
        assert body["protocol"]["steps"][0]["critical_parameters"][0]["value"] == "150"
        assert "restored_locked_ids" not in body
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
