"""Epic-3 response stamping — every completed response carries a host-decided
`review_status` in the 5-value vocab, reached through the real endpoints (requirement 5,
"under the right conditions").

Label reachability through the HTTP surface:
  * passed                      — resolve pipeline, audit finds nothing fixable
  * passed_with_findings_fixed  — resolve pipeline, audit finds+fixes, verify confirms
  * skipped                     — non-protocol terminals (design review-only, discover options)
  * (failed / unavailable are reached via test_review_gate / test_review_required)

Offline fake OpenRouter client; no network, no live model.

Runnable directly (`python tests/test_review_status_endpoints.py`) or under pytest.
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
from app.server import REVIEW_STATUS, app  # noqa: E402

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
        return self.queue.pop(0)


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


_PROTO = {"title": "P", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [{"instruction": "mix"}], "assumptions_log": []}

_DESIGN = {"question": "q", "hypothesis": "h",
           "variables": {"independent": [], "dependent": [], "controlled": []},
           "controls": [], "readout": {"measures": "m", "answers_question": True},
           "replication": {"rationale": "r"}, "expected_results": [], "interpretation_limits": []}

_ASSAYS = {"usable": True, "hypothesis_restated": "H",
           "assays": [{"id": "fp", "name": "FP", "measures": "m", "why_tests_hypothesis": "w",
                       "critical_comparison": "c", "throughput": "high", "difficulty": "low",
                       "materials_burden": "cheap", "key_limitation": "k",
                       "provenance": "best_practice"}],
           "recommended_assay_id": "fp", "recommendation_rationale": "r"}


def _seed_resolve(sid):
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", request_tool_use_id="c1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time())


def test_resolve_clean_audit_is_passed():
    saved = _config.AUTO_REVIEW
    _config.AUTO_REVIEW = True
    review = {"verdict": "sound", "summary": "ok", "findings": [], "strengths": []}
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1"),
                    _tool_msg("emit_correctness_review", review, "cr1")])
    sid = "rs_passed"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "passed"
        assert body["review_status"] in REVIEW_STATUS
        assert body.get("fixes_applied") == 0
    finally:
        _config.AUTO_REVIEW = saved
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_auto_review_restamp_is_passed_with_findings_fixed():
    saved = _config.AUTO_REVIEW
    _config.AUTO_REVIEW = True
    finding = {"severity": "major", "category": "missing_detail",
               "location": "steps[0]", "problem": "no volume", "fix": "specify 20 uL"}
    review = {"verdict": "issues_found", "summary": "s", "findings": [finding]}
    verify = {"verdict": "sound", "summary": "landed",
              "checks": [{"finding_key": finding_key(finding), "outcome": "confirmed_fixed",
                          "evidence": "20 uL now"}]}
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e1"),
                    _tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", dict(_PROTO, title="Fixed"), "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rs_fixed"
    _seed_resolve(sid)
    try:
        r = client.post("/api/resolve", json={"session_id": sid, "answers": []})
        assert r.status_code == 200
        body = r.json()
        assert body["fixes_applied"] == 1
        assert body["protocol"]["title"] == "Fixed"
        assert body["review_status"] == "passed_with_findings_fixed"
        assert body["fix_verification"]["status"] == "verified_clean"
    finally:
        _config.AUTO_REVIEW = saved
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_design_review_only_is_skipped():
    _install_agent([_tool_msg("emit_design_review", _DESIGN, "d1")])
    sid = "rs_design"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"})
    try:
        r = client.post("/api/design", json={"session_id": sid})
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "skipped"       # non-protocol terminal
        assert "protocol" not in body
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_discover_options_is_skipped():
    _install_agent([_tool_msg("emit_assay_options", _ASSAYS, "a1")])
    try:
        r = client.post("/api/discover", json={"hypothesis": "Does X increase Y binding?"})
        assert r.status_code == 200
        body = r.json()
        assert body["phase"] == "assays"
        assert body["review_status"] == "skipped"
    finally:
        srv._agent = None


def test_apply_path_carries_both_review_keys():
    finding = {"severity": "major", "category": "ordering",
               "location": "steps[0]", "problem": "p", "fix": "add buffer first"}
    verify = {"verdict": "sound", "summary": "landed",
              "checks": [{"finding_key": finding_key(finding), "outcome": "confirmed_fixed",
                          "evidence": "buffer first"}]}
    _install_agent([_tool_msg("emit_protocol", dict(_PROTO, title="Fixed"), "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rs_apply"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"},
        correctness_review={"verdict": "issues_found", "findings": [finding]})
    try:
        r = client.post("/api/apply_fixes", json={"session_id": sid})
        assert r.status_code == 200
        body = r.json()
        assert isinstance(body["review_status"], str) and body["review_status"] in REVIEW_STATUS
        assert isinstance(body["fix_verification"], dict)   # distinct object, unchanged vocab
        assert body["fix_verification"]["status"] in ("verified_clean", "issues_remain",
                                                       "not_reviewed")
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
