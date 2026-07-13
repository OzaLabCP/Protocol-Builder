"""Restore + recovery backend — POST /api/protocol/{sid}/restore (host-only, NO model) rolls
targeted entities back to the pre-op snapshot ``op_base`` with provenance intact, and a
failed/cancelled op leaves the last valid protocol untouched. NO network, NO live LLM.

Covers:
- restore rolls a target id back to its op_base value while an unrelated change persists;
- an unknown target id (in neither base nor current) is a clean 404;
- restore preserves provenance (unlike /edit, which flips it to user_input);
- a model op that RAISES leaves store.protocol == the last valid version (recovery).

Runnable directly (``python tests/test_restore.py``) or under pytest.
"""

from __future__ import annotations

import copy
import json as _json
import os
import sys
import time as _time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import GapFillerAgent, Session  # noqa: E402
from app.checks import assign_stable_ids, ensure_ids  # noqa: E402

client = TestClient(srv.app)


def _two_param_proto(temp="50", time_="30"):
    """Two lockable/targetable critical params with distinct identities."""
    return {
        "title": "P", "summary": "s", "estimated_duration": "1 h",
        "materials": [],
        "steps": [{"title": "PCR", "instruction": "cycle", "critical_parameters": [
            {"name": "Denaturation temp", "value": temp, "unit": "C", "provenance": "stated"},
            {"name": "Extension time", "value": time_, "unit": "s", "provenance": "best_practice"}]}],
        "assumptions_log": [],
    }


def _pids(proto):
    probe = copy.deepcopy(proto)
    ensure_ids(probe); assign_stable_ids(probe)
    cps = probe["steps"][0]["critical_parameters"]
    return cps[0]["parameter_id"], cps[1]["parameter_id"]


def _seed(sid, base, current):
    st = srv.Store(session=Session(source_kind="paper",
                                   messages=[{"role": "user", "content": "seed"}]),
                   created=_time.time(), protocol=copy.deepcopy(current))
    st.op_base = copy.deepcopy(base)  # the pre-op (last-valid) snapshot
    srv._SESSIONS[sid] = st
    return st


def test_restore_rolls_back_target():
    sid = "restoretarget"
    base = _two_param_proto(temp="50", time_="30")
    temp_id, _ = _pids(base)
    # Current: BOTH params changed by a prior revise (temp 50->95, time 30->60).
    current = _two_param_proto(temp="95", time_="60")
    _seed(sid, base, current)
    try:
        r = client.post(f"/api/protocol/{sid}/restore",
                        json={"session_id": sid, "target_ids": [temp_id]})
        assert r.status_code == 200, r.text
        cps = r.json()["protocol"]["steps"][0]["critical_parameters"]
        by_name = {c["name"]: c["value"] for c in cps}
        assert by_name["Denaturation temp"] == "50"   # target rolled back to op_base
        assert by_name["Extension time"] == "60"      # unrelated (non-target) change persists
    finally:
        srv._SESSIONS.pop(sid, None)


def test_restore_404_unknown_id():
    sid = "restore404"
    base = _two_param_proto()
    _seed(sid, base, _two_param_proto(temp="95"))
    try:
        r = client.post(f"/api/protocol/{sid}/restore",
                        json={"session_id": sid, "target_ids": ["p_nonexistent"]})
        assert r.status_code == 404
    finally:
        srv._SESSIONS.pop(sid, None)


def test_restore_preserves_provenance():
    sid = "restoreprov"
    base = _two_param_proto(temp="50")  # temp provenance == "stated"
    temp_id, _ = _pids(base)
    current = _two_param_proto(temp="95")
    # simulate the model having also changed provenance on the current copy
    current["steps"][0]["critical_parameters"][0]["provenance"] = "literature_grounded"
    _seed(sid, base, current)
    try:
        r = client.post(f"/api/protocol/{sid}/restore",
                        json={"session_id": sid, "target_ids": [temp_id]})
        assert r.status_code == 200, r.text
        cp0 = r.json()["protocol"]["steps"][0]["critical_parameters"][0]
        assert cp0["value"] == "50"
        assert cp0["provenance"] == "stated"  # exact pre-op provenance, NOT user_input
    finally:
        srv._SESSIONS.pop(sid, None)


# --- recovery: a failed model op preserves the last valid protocol ----------

def _tool_msg(name, args, cid):
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _BoomLLM:
    """Raises on the first chat() — simulates a provider failure mid-op."""
    def chat(self, *a, **k):
        raise RuntimeError("simulated provider outage")


def test_failed_revise_preserves_last_valid_protocol():
    sid = "reviseboom"
    last_valid = {"title": "Good", "summary": "s", "estimated_duration": "1 h",
                  "materials": [], "steps": [], "assumptions_log": []}
    srv._agent = GapFillerAgent(client=_BoomLLM(), model="fake")
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol=copy.deepcopy(last_valid))
    try:
        r = client.post("/api/revise", json={
            "session_id": sid, "instruction": "make the annealing step longer"})
        assert r.status_code >= 500  # the op failed
        # The last valid protocol is untouched — recovery invariant.
        assert srv._SESSIONS[sid].protocol == last_valid
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
