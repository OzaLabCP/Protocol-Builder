"""Epic-3 per-session idempotency keys (`app/server.py`).

Requirement (6): a repeated idempotency_key returns the identical prior result WITHOUT
invoking the model again; a new key does fresh work; a failed op does not poison the key;
the cache is per-session and bounded (LRU).

Offline: a call-counting fake OpenRouter client drives the endpoints. `_calls()` is the
number of model round-trips so far — a cache HIT must NOT increase it. A "failure" is
produced by leaving the emit un-queued so the fake pops an empty queue and raises.

Runnable directly (`python tests/test_idempotency.py`) or under pytest.
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
from app.checks import finding_key  # noqa: E402
from app.server import IDEM_CAP, app  # noqa: E402

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
        return self.queue.pop(0)   # empty queue -> IndexError: the failure path


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


def _calls():
    return len(srv._agent.client.calls)


_PROTO = {"title": "P", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [{"instruction": "mix"}], "assumptions_log": []}


def _seed_revise(sid, **kw):
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e0",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"}, **kw)


# --- replay: identical result, no second model call ----------------------------

def test_repeated_key_replays_identical_and_no_model_call():
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="P2"), "e1")])
    sid = "idem_replay"
    _seed_revise(sid)
    try:
        r1 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells",
                                              "idempotency_key": "k1"})
        assert r1.status_code == 200
        body1 = r1.json()
        n = _calls()
        assert n >= 1
        # queue is now empty: a real second model call would raise. A cache HIT must not.
        r2 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells",
                                              "idempotency_key": "k1"})
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2 == body1                              # byte-identical replay
        assert body2["protocol_version_id"] == body1["protocol_version_id"]
        assert _calls() == n                               # NO extra model round-trip
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_new_key_does_fresh_work():
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="P2"), "e1"),
                    _tool_msg("emit_protocol", dict(_PROTO, title="P3"), "e2")])
    sid = "idem_newkey"
    _seed_revise(sid)
    try:
        client.post("/api/revise", json={"session_id": sid, "instruction": "first correction",
                                        "idempotency_key": "k1"})
        n = _calls()
        r2 = client.post("/api/revise", json={"session_id": sid, "instruction": "second correction",
                                             "idempotency_key": "k2"})
        assert r2.status_code == 200
        assert _calls() == n + 1                           # a new key runs the model again
        idem = srv._SESSIONS[sid].idem
        assert "revise:k1" in idem and "revise:k2" in idem
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_failed_op_does_not_poison_key():
    _install_agent([])   # empty queue -> the first attempt raises
    sid = "idem_poison"
    _seed_revise(sid)
    try:
        r1 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells",
                                              "idempotency_key": "k1"})
        assert r1.status_code >= 500                        # model contract failure
        assert "revise:k1" not in srv._SESSIONS[sid].idem   # never recorded on failure
        # a retry after the model is healthy succeeds and is now cached
        _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="P2"), "e1")])
        r2 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells",
                                             "idempotency_key": "k1"})
        assert r2.status_code == 200
        assert "revise:k1" in srv._SESSIONS[sid].idem
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_cache_is_per_session():
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="A"), "e1"),
                    _tool_msg("emit_protocol", dict(_PROTO, title="B"), "e2")])
    sidA, sidB = "idem_A", "idem_B"
    _seed_revise(sidA)
    _seed_revise(sidB)
    try:
        rA = client.post("/api/revise", json={"session_id": sidA, "instruction": "corr",
                                             "idempotency_key": "shared"})
        bodyA = rA.json()
        n = _calls()
        # SAME key string on a DIFFERENT session must miss (cache is per-session) -> fresh work
        rB = client.post("/api/revise", json={"session_id": sidB, "instruction": "corr",
                                             "idempotency_key": "shared"})
        bodyB = rB.json()
        assert _calls() == n + 1                           # B did its own model call
        assert bodyB["session_id"] == sidB != bodyA["session_id"]
        assert bodyB["protocol"]["title"] == "B" != bodyA["protocol"]["title"]
        assert "revise:shared" in srv._SESSIONS[sidA].idem  # A's entry intact
        assert "revise:shared" in srv._SESSIONS[sidB].idem
    finally:
        srv._SESSIONS.pop(sidA, None); srv._SESSIONS.pop(sidB, None); srv._agent = None


def test_missing_key_never_caches():
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="P2"), "e1")])
    sid = "idem_nokey"
    _seed_revise(sid)
    try:
        r1 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells"})
        assert r1.status_code == 200
        assert len(srv._SESSIONS[sid].idem) == 0           # nothing cached without a key
        # so a second keyless call is non-idempotent: empty queue -> failure
        r2 = client.post("/api/revise", json={"session_id": sid, "instruction": "use 150 uL wells"})
        assert r2.status_code >= 500
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_resolve_replay_short_circuits_before_409_guard():
    # The first resolve consumes request_tool_use_id; a keyed replay must return the cached
    # result, NOT the "already built" 409 — the cache check precedes the state guard.
    saved = _config.AUTO_REVIEW
    _config.AUTO_REVIEW = False                            # keep it a single model call
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1")])
    sid = "idem_resolve"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time())
    try:
        r1 = client.post("/api/resolve", json={"session_id": sid, "answers": [],
                                              "idempotency_key": "r1"})
        assert r1.status_code == 200
        body1 = r1.json()
        n = _calls()
        r2 = client.post("/api/resolve", json={"session_id": sid, "answers": [],
                                              "idempotency_key": "r1"})
        assert r2.status_code == 200                        # replay, NOT 409 "already built"
        assert r2.json() == body1
        assert _calls() == n                                # no second emit
    finally:
        _config.AUTO_REVIEW = saved
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_critique_apply_idempotent_no_second_version():
    finding = {"severity": "major", "category": "missing_detail",
               "location": "steps[0]", "problem": "no volume", "fix": "specify 20 uL"}
    review = {"verdict": "issues_found", "summary": "s", "findings": [finding]}
    verify = {"verdict": "sound", "summary": "landed",
              "checks": [{"finding_key": finding_key(finding), "outcome": "confirmed_fixed",
                          "evidence": "20 uL now"}]}
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", dict(_PROTO, title="Fixed"), "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "idem_critique"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"})
    try:
        r1 = client.post("/api/critique", json={"session_id": sid, "apply": True,
                                               "idempotency_key": "c1"})
        assert r1.status_code == 200
        body1 = r1.json()
        n = _calls()
        r2 = client.post("/api/critique", json={"session_id": sid, "apply": True,
                                               "idempotency_key": "c1"})
        assert r2.status_code == 200
        body2 = r2.json()
        assert body2 == body1                              # identical, incl. fix_verification
        assert body2["review_status"] == body1["review_status"]
        assert body2["protocol_version_id"] == body1["protocol_version_id"]
        assert _calls() == n                               # no re-audit / re-emit / re-verify
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


# --- bounded LRU (unit) --------------------------------------------------------

def test_lru_bound_evicts_oldest():
    store = srv.Store(session=Session(), created=_time.time())
    for i in range(IDEM_CAP + 1):                          # 17 keys into a 16-slot cache
        store.idem_put(f"k{i}", {"i": i})
    assert len(store.idem) == IDEM_CAP
    assert store.idem_get("k0") is None                    # oldest evicted
    assert store.idem_get(f"k{IDEM_CAP}") == {"i": IDEM_CAP}  # newest retained


def test_lru_touch_protects_recently_used():
    store = srv.Store(session=Session(), created=_time.time())
    for i in range(IDEM_CAP):                              # fill exactly
        store.idem_put(f"k{i}", {"i": i})
    store.idem_get("k1")                                   # touch k1 -> most-recently-used
    store.idem_put("k_new", {"i": -1})                    # forces one eviction
    assert store.idem_get("k1") == {"i": 1}               # protected by the touch
    assert store.idem_get("k0") is None                   # k0 (now oldest) evicted instead


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
