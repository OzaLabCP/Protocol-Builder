"""Epic-3 — Independent Fix-Verification Gate.

Covers the review-as-a-gate contract end to end, offline (no network, no live
model): a fake OpenRouter client drives the endpoints, and the host adjudicator
(`build_review_status`) plus the finding-key primitives are exercised directly.

Grouped by the spec's guarantees:

  (1) `finding_key` is deterministic + idempotent, severity-independent, and
      stable across list-position changes (`assign_finding_keys` order-invariant).
  (2) a genuinely fixed finding => `confirmed_fixed`, `verified_clean`, and the
      review does NOT gate the project status.
  (3) an unfixed CRITICAL finding => `still_present`, `issues_remain`, gates.
  (4) a fix that introduces a new critical defect => a new/regressed finding is
      caught and gates (`unresolved_blocking`).
  (5) the verify pass failing => `not_reviewed` AND the corrected protocol is
      STILL returned (HTTP 200, never a 500).
  (6) additive/non-breaking: the pre-existing critique/apply response keys are
      all still present alongside the new `fix_verification` / `review_status`.
  (7) `ValidationSummary` gains the review fields; an unresolved critical review
      finding drives the project status to `blocked`, while the existing
      status/count derivation is byte-identical when no verification is supplied.

Runnable directly (`python tests/test_review_gate.py`) or under pytest.
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
from app.checks import finding_key  # noqa: E402
from app.server import _validation_summary_from_report, app  # noqa: E402
from app.projects import ValidationSummary, make_project  # noqa: E402
from app.store import SQLiteProjectStore  # noqa: E402
from app.validation import assign_finding_keys, build_review_status  # noqa: E402

client = TestClient(app)


# --- fake LLM plumbing (mirrors tests/test_server.py) -----------------------

def _tool_msg(name, args, cid):
    """An OpenAI/OpenRouter-shaped assistant turn with a single tool call."""
    return {"choices": [{"finish_reason": "tool_calls", "message": {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": cid, "type": "function",
                        "function": {"name": name, "arguments": _json.dumps(args)}}]}}]}


class _FakeLLM:
    def __init__(self, queue):
        self.queue = list(queue)
        self.calls = []

    def chat(self, messages, tools=None, tool_choice=None, model=None, effort=None, max_tokens=None):
        self.calls.append({"tool_choice": tool_choice, "model": model})
        return self.queue.pop(0)   # empty queue -> IndexError, the "verifier failed" path


def _install_agent(queue):
    srv._agent = GapFillerAgent(client=_FakeLLM(queue), model="fake")


_PROTO = {"title": "Fixed", "summary": "s", "estimated_duration": "1 h",
          "materials": [], "steps": [], "assumptions_log": []}


def _blank_report():
    """The minimal report `_validation_summary_from_report` accepts -> clean."""
    return {"quality_gate": {"status": "ok", "counts":
            {"errors": 0, "warnings": 0, "assumptions": 0, "info": 0}}}


# ---------------------------------------------------------------------------
# (1) finding_key: deterministic, idempotent, position-stable
# ---------------------------------------------------------------------------

def _finding(sev="major", cat="missing_detail", loc="steps[0]", prob="no volume specified",
             fix="specify 20 uL"):
    return {"severity": sev, "category": cat, "location": loc, "problem": prob, "fix": fix}


def test_finding_key_deterministic_and_idempotent():
    f = _finding()
    k = finding_key(f)
    assert k.startswith("f_") and len(k) == 12          # "f_" + sha256[:10]
    assert finding_key(f) == k                          # repeated calls agree
    assert finding_key(dict(f)) == k                    # same content, fresh dict
    # same finding embedded in a "before" vs "after" protocol still keys identically
    before = {"steps": [f], "title": "A"}
    after = {"title": "B", "extra": 1, "steps": [dict(f)]}
    assert finding_key(before["steps"][0]) == finding_key(after["steps"][0]) == k


def test_finding_key_is_severity_independent():
    base = _finding(sev="minor")
    k = finding_key(base)
    for sev in ("critical", "major", "minor", "bogus", None):
        assert finding_key(_finding(sev=sev)) == k       # severity excluded from identity


def test_finding_key_changes_on_identity_fields():
    k = finding_key(_finding())
    assert finding_key(_finding(cat="ordering")) != k    # category matters
    assert finding_key(_finding(loc="steps[3]")) != k    # location matters
    assert finding_key(_finding(prob="wrong buffer pH")) != k  # problem matters


def test_finding_key_reword_tolerant():
    a = _finding(prob="No volume specified")
    b = _finding(prob="volume   specified")     # stopword 'no' + case + whitespace only
    assert finding_key(a) == finding_key(b)     # reworded description => same key


def test_assign_finding_keys_stable_across_position():
    """The key of a finding is a content hash — shuffling the list cannot change it."""
    a = _finding(loc="steps[0]", prob="alpha defect")
    b = _finding(loc="steps[1]", prob="beta defect")
    c = _finding(loc="steps[2]", prob="gamma defect")
    forward = assign_finding_keys([dict(a), dict(b), dict(c)])
    reverse = assign_finding_keys([dict(c), dict(b), dict(a)])
    by_loc_fwd = {f["location"]: f["_key"] for f in forward}
    by_loc_rev = {f["location"]: f["_key"] for f in reverse}
    assert by_loc_fwd == by_loc_rev                       # order-invariant assignment
    # and each equals the bare content hash (no collision suffix for distinct findings)
    assert by_loc_fwd["steps[0]"] == finding_key(a)
    assert by_loc_fwd["steps[1]"] == finding_key(b)


def test_assign_finding_keys_collision_disambiguates_without_position():
    """Two DISTINCT findings that collide on (cat,loc,problem_sig) get suffixed keys,
    invariant under input reordering (never by list index)."""
    x = _finding(prob="volume defect", fix="use 20 uL")
    y = _finding(prob="volume defect", fix="use 50 uL")   # same identity basis, different fix
    assert finding_key(x) == finding_key(y)               # they collide on the base key
    fwd = assign_finding_keys([dict(x), dict(y)])
    rev = assign_finding_keys([dict(y), dict(x)])
    fwd_by_fix = {f["fix"]: f["_key"] for f in fwd}
    rev_by_fix = {f["fix"]: f["_key"] for f in rev}
    assert fwd_by_fix == rev_by_fix                        # same mapping regardless of order
    assert fwd_by_fix["use 20 uL"] != fwd_by_fix["use 50 uL"]  # disambiguated
    assert all("." in v for v in fwd_by_fix.values())     # suffixed


# ---------------------------------------------------------------------------
# (2)-(4),(7-unit) build_review_status host adjudication (pure, offline)
# ---------------------------------------------------------------------------

def _check(key, outcome, evidence="e"):
    return {"finding_key": key, "outcome": outcome, "evidence": evidence}


def test_build_review_status_confirmed_clean_does_not_block():
    prior = [_finding(sev="major", loc="steps[0]", prob="a"),
             _finding(sev="minor", cat="ordering", loc="steps[1]", prob="b")]
    assign_finding_keys(prior)
    verify = {"verdict": "sound", "summary": "ok", "checks": [
        _check(prior[0]["_key"], "confirmed_fixed"),
        _check(prior[1]["_key"], "not_applicable")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["status"] == "verified_clean"
    assert fv["checked"] is True
    assert fv["counts"]["confirmed_fixed"] == 1 and fv["counts"]["not_applicable"] == 1
    assert fv["unresolved_count"] == 0 and fv["unresolved_blocking"] is False


def test_build_review_status_fills_omissions_still_present():
    """Silence is skeptical: an omitted key defaults to still_present and blocks."""
    prior = [_finding(sev="critical", loc="steps[0]", prob="missing control")]
    assign_finding_keys(prior)
    fv = build_review_status(prior, {"checks": []}, checked=True)
    only = fv["findings"][0]
    assert only["outcome"] == "still_present"
    assert fv["status"] == "issues_remain"
    assert fv["unresolved_blocking"] is True and fv["checked"] is True


def test_build_review_status_drops_forged_keys():
    """A model-supplied key not in the issued set is dropped; the real finding, left
    unaddressed, defaults to still_present."""
    prior = [_finding(sev="critical", loc="steps[0]", prob="real defect")]
    assign_finding_keys(prior)
    verify = {"checks": [_check("f_deadbeef00", "confirmed_fixed", "smuggled")]}
    fv = build_review_status(prior, verify, checked=True)
    assert len(fv["findings"]) == 1
    assert fv["findings"][0]["outcome"] == "still_present"
    assert fv["counts"]["confirmed_fixed"] == 0
    assert fv["status"] == "issues_remain" and fv["unresolved_blocking"] is True


def test_build_review_status_unfixed_critical_gates():
    prior = [_finding(sev="critical", loc="steps[0]", prob="wrong dilution")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "still_present", "still 50 uL")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["status"] == "issues_remain"
    assert fv["unresolved_blocking"] is True
    assert fv["unresolved_count"] == 1


def test_build_review_status_minor_only_surfaces_but_does_not_block():
    prior = [_finding(sev="minor", cat="logic", loc="steps[0]", prob="tiny nit")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "still_present")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["status"] == "issues_remain"           # surfaced
    assert fv["unresolved_blocking"] is False         # but minor-only never blocks
    assert fv["unresolved_count"] == 1


def test_build_review_status_new_critical_defect_gates():
    """A fix that introduces a brand-new critical defect is caught and blocks even
    though the prior finding was (claimed) fixed."""
    prior = [_finding(sev="major", cat="missing_detail", loc="steps[0]", prob="no volume")]
    assign_finding_keys(prior)
    new = {"severity": "critical", "category": "logic", "location": "steps[2]",
           "problem": "buffer now added twice", "fix": "add buffer once"}
    verify = {"checks": [_check(prior[0]["_key"], "confirmed_fixed")],
              "new_findings": [new]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["counts"]["new"] == 1
    assert fv["status"] == "issues_remain"
    assert fv["unresolved_blocking"] is True
    assert len(fv["new_findings"]) == 1 and fv["new_findings"][0]["finding_key"].startswith("f_")


def test_build_review_status_regressed_gates():
    prior = [_finding(sev="major", loc="steps[0]", prob="the defect")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "regressed", "fix broke step 1")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["counts"]["regressed"] == 1
    assert fv["status"] == "issues_remain" and fv["unresolved_blocking"] is True


def test_build_review_status_new_finding_dedup_no_double_count():
    """A 'new' finding whose recomputed key collides with a prior is reclassified as
    that prior's still_present — not listed as new, not double-counted."""
    prior = [_finding(sev="major", cat="missing_detail", loc="steps[0]", prob="no volume")]
    assign_finding_keys(prior)
    dupe = {"severity": "critical", "category": "missing_detail", "location": "steps[0]",
            "problem": "no volume", "fix": "specify 20 uL"}   # same identity basis as prior
    verify = {"checks": [_check(prior[0]["_key"], "confirmed_fixed")],
              "new_findings": [dupe]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["counts"]["new"] == 0                    # not counted as new
    assert fv["new_findings"] == []
    assert fv["findings"][0]["outcome"] == "still_present"   # folded back onto the prior


def test_build_review_status_not_reviewed_when_unchecked():
    prior = [_finding(sev="critical", loc="steps[0]", prob="x")]
    assign_finding_keys(prior)
    fv = build_review_status(prior, {}, checked=False)
    assert fv["status"] == "not_reviewed" and fv["checked"] is False


def test_build_review_status_no_priors_is_not_reviewed():
    fv = build_review_status([], {}, checked=False)
    assert fv["status"] == "not_reviewed"
    assert fv["reason"] == "no_original_findings"
    assert fv["checked"] is False


def test_build_review_status_is_pure_and_idempotent():
    prior = [_finding(sev="critical", loc="steps[0]", prob="x")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "still_present")]}
    a = build_review_status([dict(prior[0])], dict(verify), checked=True)
    b = build_review_status([dict(prior[0])], dict(verify), checked=True)
    a.pop("summary", None); b.pop("summary", None)     # (identical anyway)
    assert a == b                                      # equal inputs -> equal object


# ---------------------------------------------------------------------------
# `unconfirmed`: the genuine "cannot determine" outcome — never counts as fixed
# ---------------------------------------------------------------------------

def test_unconfirmed_blocks_verified_clean():
    """A critical finding the reviewer cannot confirm as fixed is `unconfirmed`, which is
    OUTSIDE the {confirmed_fixed, not_applicable} clean set — so it forces issues_remain
    and blocks. It is never silently promoted to fixed just because a re-emit completed."""
    prior = [_finding(sev="critical", loc="steps[0]", prob="dilution maybe wrong")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "unconfirmed", "cannot tell from the artifact")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["findings"][0]["outcome"] == "unconfirmed"
    assert fv["status"] == "issues_remain"
    assert fv["unresolved_count"] == 1
    assert fv["unresolved_blocking"] is True
    assert fv["counts"]["confirmed_fixed"] == 0     # NOT counted as fixed


def test_unconfirmed_skeptic_rank_beats_confirmed_fixed_on_dup():
    """Most-skeptical-wins on a duplicated key: confirmed_fixed + unconfirmed folds to
    unconfirmed (the reviewer's uncertain read is not overridden by its optimistic one)."""
    prior = [_finding(sev="major", loc="steps[0]", prob="a defect")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "confirmed_fixed", "looks fixed"),
                         _check(prior[0]["_key"], "unconfirmed", "actually can't tell")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["findings"][0]["outcome"] == "unconfirmed"
    assert fv["counts"]["unconfirmed"] == 1 and fv["counts"]["confirmed_fixed"] == 0
    assert fv["status"] == "issues_remain"


def test_unconfirmed_minor_surfaces_but_does_not_block():
    prior = [_finding(sev="minor", cat="logic", loc="steps[0]", prob="tiny nit")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "unconfirmed")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["status"] == "issues_remain"           # surfaced as unresolved
    assert fv["unresolved_count"] == 1
    assert fv["unresolved_blocking"] is False         # minor-only never blocks


def test_still_present_fix_is_never_marked_fixed():
    """Requirement (2): a proposed fix that remains present is `still_present`, never
    confirmed_fixed — the gate does not clear it."""
    prior = [_finding(sev="critical", loc="steps[0]", prob="buffer omitted", fix="add buffer")]
    assign_finding_keys(prior)
    verify = {"checks": [_check(prior[0]["_key"], "still_present", "buffer still missing")]}
    fv = build_review_status(prior, verify, checked=True)
    assert fv["findings"][0]["outcome"] == "still_present"
    assert fv["counts"]["confirmed_fixed"] == 0
    assert fv["status"] == "issues_remain" and fv["unresolved_blocking"] is True


def test_unconfirmed_does_not_disturb_existing_omission_fill():
    """Regression guard: an omitted key still defaults to still_present (not unconfirmed)."""
    prior = [_finding(sev="critical", loc="steps[0]", prob="missing control")]
    assign_finding_keys(prior)
    fv = build_review_status(prior, {"checks": []}, checked=True)
    assert fv["findings"][0]["outcome"] == "still_present"
    assert fv["counts"]["unconfirmed"] == 0


# ---------------------------------------------------------------------------
# (2),(3),(4),(5),(6) end-to-end through the apply endpoints
# ---------------------------------------------------------------------------

def _seed_store(sid, **kw):
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"}, **kw)


def test_critique_apply_confirmed_fixed_does_not_gate():
    finding = _finding(sev="major")
    review = {"verdict": "issues_found", "summary": "s", "findings": [finding]}
    key = finding_key(finding)
    verify = {"verdict": "sound", "summary": "all landed",
              "checks": [_check(key, "confirmed_fixed", "step 1 now says 20 uL")]}
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", _PROTO, "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rg_confirm"
    _seed_store(sid)
    try:
        r = client.post("/api/critique", json={"session_id": sid, "apply": True})
        assert r.status_code == 200
        body = r.json()
        fv = body["fix_verification"]
        assert fv["status"] == "verified_clean"
        assert fv["counts"]["confirmed_fixed"] == 1
        assert fv["unresolved_count"] == 0 and fv["unresolved_blocking"] is False
        # L3 response label (5-value): findings existed and were verified resolved.
        assert body["review_status"] == "passed_with_findings_fixed"
        assert body["applied"] is True and body["fixes_applied"] == 1
        assert body["protocol"]["title"] == "Fixed"
        # (6) additive: pre-existing critique/apply keys all still present
        for existing in ("correctness_review", "review_validation_report",
                         "applied", "fixes_applied", "protocol"):
            assert existing in body
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_critique_apply_unfixed_critical_still_present_gates():
    finding = _finding(sev="critical", cat="implausible_value",
                       prob="dilution off by 5x", fix="use 10 uL not 50 uL")
    review = {"verdict": "serious_issues", "summary": "s", "findings": [finding]}
    key = finding_key(finding)
    verify = {"verdict": "serious_issues", "summary": "still wrong",
              "checks": [_check(key, "still_present", "step still reads 50 uL")]}
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", _PROTO, "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rg_unfixed"
    _seed_store(sid)
    try:
        r = client.post("/api/critique", json={"session_id": sid, "apply": True})
        assert r.status_code == 200                      # protocol still delivered
        body = r.json()
        fv = body["fix_verification"]
        assert fv["status"] == "issues_remain"
        assert fv["unresolved_blocking"] is True
        # L3 label: an unresolved blocking finding -> failed.
        assert body["review_status"] == "failed"
        assert body["applied"] is True
        assert body["protocol"]["title"] == "Fixed"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_critique_apply_regression_introduces_new_critical_gates():
    finding = _finding(sev="major")
    review = {"verdict": "issues_found", "summary": "s", "findings": [finding]}
    key = finding_key(finding)
    verify = {"verdict": "serious_issues", "summary": "fix broke something",
              "checks": [_check(key, "confirmed_fixed", "step 1 fixed")],
              "new_findings": [{"severity": "critical", "category": "logic",
                                "location": "steps[2]", "problem": "buffer added twice",
                                "fix": "add buffer once"}]}
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", _PROTO, "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rg_regress"
    _seed_store(sid)
    try:
        r = client.post("/api/critique", json={"session_id": sid, "apply": True})
        assert r.status_code == 200
        body = r.json()
        fv = body["fix_verification"]
        assert fv["status"] == "issues_remain"
        assert fv["counts"]["new"] == 1
        assert fv["unresolved_blocking"] is True
        # L3 label: a new blocking defect -> failed.
        assert body["review_status"] == "failed"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_critique_apply_verifier_failure_is_not_reviewed_and_still_returns():
    """(5) The verifier never runs (no emit queued -> the fake raises). The corrected
    protocol is STILL returned with status not_reviewed — never a 500."""
    finding = _finding(sev="critical")
    review = {"verdict": "serious_issues", "summary": "s", "findings": [finding]}
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", _PROTO, "e2")])   # NO emit_fix_verification
    sid = "rg_fail"
    _seed_store(sid)
    try:
        r = client.post("/api/critique", json={"session_id": sid, "apply": True})
        assert r.status_code == 200                      # not a 500
        body = r.json()
        fv = body["fix_verification"]
        assert fv["status"] == "not_reviewed"
        assert fv["reason"] == "verifier_error"
        assert fv["checked"] is False
        # L3 label: the verifier raised -> unavailable (protocol still delivered).
        assert body["review_status"] == "unavailable"
        assert body["applied"] is True
        assert body["protocol"]["title"] == "Fixed"
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


def test_auto_review_verifies_and_persists_the_finalized_candidate():
    """BLOCKER-2: the DEFAULT auto-review path finalizes the fixed candidate, makes it the
    store/session-current protocol, THEN verifies THAT exact candidate, and persists it — in
    that order. The verifier must see the FINALIZED artifact (validation_report / schema_version
    / stamped material _id), and the persisted ProtocolVersion must embed precisely the verified,
    finalized candidate (same fix_verification status, same protocol)."""
    import os as _os
    import tempfile as _tempfile

    finding = _finding(sev="major")
    review = {"verdict": "issues_found", "summary": "s", "findings": [finding]}
    key = finding_key(finding)
    verify = {"verdict": "sound", "summary": "landed",
              "checks": [_check(key, "confirmed_fixed", "step 1 now says 20 uL")]}
    fixed = {"title": "Fixed", "summary": "s", "estimated_duration": "1 h",
             "materials": [{"name": "MgCl2", "amount": "10", "unit": "mM",
                            "provenance": "default_verify"}],
             "steps": [], "assumptions_log": []}
    # Tape: correctness_review -> apply (emit_protocol) -> verify_fixes (emit_fix_verification).
    _install_agent([_tool_msg("emit_correctness_review", review, "cr1"),
                    _tool_msg("emit_protocol", fixed, "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])

    tmpdir = _tempfile.mkdtemp(prefix="rg_autorev_")
    old_store = srv._STORE
    store_db = SQLiteProjectStore(_os.path.join(tmpdir, "projects.db"))
    srv._STORE = store_db
    project = store_db.create_project(make_project(
        title="P", workflow="reproduce", detected_workflow="reproduce"))

    sid = "rg_autorev"
    st = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(),
        protocol={"title": "Old", "summary": "s", "estimated_duration": "1 h",
                  "materials": [], "steps": [], "assumptions_log": []},
        project_id=project.project_id)
    st.session.protocol = st.protocol
    srv._SESSIONS[sid] = st

    # Spy: snapshot the store/session-current protocol at the INSTANT verification runs, to
    # prove finalize (report + ids stamped) happened BEFORE the verifier judged the candidate.
    seen: dict = {}
    real_verify = srv._verify_fixes

    def spy_verify(store, findings):
        p = store.session.protocol
        seen["finalized_at_verify"] = bool(
            p and p.get("validation_report") and p.get("schema_version")
            and any(m.get("_id") for m in (p.get("materials") or [])))
        seen["title_at_verify"] = (p or {}).get("title")
        return real_verify(store, findings)

    srv._verify_fixes = spy_verify
    try:
        result = srv._auto_review(sid, st, {"session_id": sid, "phase": "complete",
                                            "protocol": st.protocol})
        # finalize-before-verify: the verifier saw the finalized, store-current candidate.
        assert seen["finalized_at_verify"] is True
        assert seen["title_at_verify"] == "Fixed"
        # the verified outcome is the one that flows into the response.
        assert result["fix_verification"]["status"] == "verified_clean"
        assert result["review_status"] == "passed_with_findings_fixed"
        vid = result["protocol_version_id"]
        assert vid is not None
        # the PERSISTED version is exactly the verified, finalized candidate.
        ver = srv._STORE.get_protocol_version(vid)
        assert (ver.result["fix_verification"]["status"]
                == result["fix_verification"]["status"] == "verified_clean")
        assert ver.result["protocol"]["title"] == "Fixed"
        assert ver.result["protocol"].get("validation_report")
    finally:
        srv._SESSIONS.pop(sid, None)
        srv._agent = None
        srv._verify_fixes = real_verify
        srv._STORE = old_store
        store_db.close()


def test_apply_fixes_two_step_verifies_and_retains_outcome():
    finding = _finding(sev="major", fix="add buffer first")
    key = finding_key(finding)
    verify = {"verdict": "sound", "summary": "landed",
              "checks": [_check(key, "confirmed_fixed", "buffer is first now")]}
    _install_agent([_tool_msg("emit_protocol", _PROTO, "e2"),
                    _tool_msg("emit_fix_verification", verify, "fv1")])
    sid = "rg_twostep"
    srv._SESSIONS[sid] = srv.Store(
        session=Session(source_kind="paper", pending_tool_use_id="e1",
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol={"title": "Old"},
        correctness_review={"verdict": "issues_found", "findings": [finding]})
    try:
        r = client.post("/api/apply_fixes", json={"session_id": sid})
        assert r.status_code == 200
        body = r.json()
        assert body["fix_verification"]["status"] == "verified_clean"
        # L3 label (5-value): findings existed and were verified resolved.
        assert "review_status" in body and body["review_status"] == "passed_with_findings_fixed"
        assert body["protocol"]["title"] == "Fixed"     # protocol still delivered
        assert srv._SESSIONS[sid].correctness_review is None      # stale review cleared
        assert srv._SESSIONS[sid].fix_verification is not None    # outcome retained
    finally:
        srv._SESSIONS.pop(sid, None); srv._agent = None


# ---------------------------------------------------------------------------
# (7) ValidationSummary review fields + monotone gate escalation
# ---------------------------------------------------------------------------

def test_validation_summary_review_fields_default_when_absent():
    """No fix_verification supplied => new fields take their defaults and the existing
    status/count derivation is byte-identical to the legacy path."""
    vs_default = ValidationSummary()
    assert vs_default.review_status == "unknown"
    assert vs_default.unresolved_finding_count == 0

    summ = _validation_summary_from_report(_blank_report())          # legacy call, no fv
    assert summ.review_status == "unknown"
    assert summ.unresolved_finding_count == 0
    assert summ.status == "clean"
    assert summ.error_count == 0 and summ.warning_count == 0
    assert summ.unverified_citation_count == 0


def test_validation_summary_unchanged_error_still_blocks_without_fv():
    report = {"quality_gate": {"status": "blocked",
              "counts": {"errors": 1, "warnings": 0, "assumptions": 0, "info": 0}}}
    summ = _validation_summary_from_report(report)                   # no fv
    assert summ.status == "blocked"
    assert summ.error_count == 1
    assert summ.review_status == "unknown"                            # review axis untouched


def test_validation_summary_unresolved_critical_drives_status_blocked():
    fv = {"status": "issues_remain", "unresolved_count": 1, "unresolved_blocking": True}
    summ = _validation_summary_from_report(_blank_report(), fix_verification=fv)
    assert summ.status == "blocked"                                   # review gate escalates
    assert summ.review_status == "issues_remain"
    assert summ.unresolved_finding_count == 1


def test_validation_summary_verified_clean_leaves_clean():
    fv = {"status": "verified_clean", "unresolved_count": 0, "unresolved_blocking": False}
    summ = _validation_summary_from_report(_blank_report(), fix_verification=fv)
    assert summ.status == "clean"                                     # never loosens/tightens
    assert summ.review_status == "verified_clean"
    assert summ.unresolved_finding_count == 0


def test_validation_summary_minor_only_escalates_clean_to_warnings():
    fv = {"status": "issues_remain", "unresolved_count": 1, "unresolved_blocking": False}
    summ = _validation_summary_from_report(_blank_report(), fix_verification=fv)
    assert summ.status == "warnings"                                  # minor-only warns, no block
    assert summ.review_status == "issues_remain"
    assert summ.unresolved_finding_count == 1


def test_validation_summary_not_reviewed_never_changes_status():
    fv = {"status": "not_reviewed", "unresolved_count": 0, "unresolved_blocking": False}
    summ = _validation_summary_from_report(_blank_report(), fix_verification=fv)
    assert summ.status == "clean"                                     # not_reviewed is inert
    assert summ.review_status == "not_reviewed"


if __name__ == "__main__":
    import traceback

    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
