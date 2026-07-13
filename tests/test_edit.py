"""Epic-4 inline value-editing — endpoint tests for POST /api/protocol/{id}/edit.

These exercise the whole edit path with NO network and NO live LLM: sessions are
seeded directly in ``srv._SESSIONS`` with hand-built protocol dicts, and the edit
handler runs the pure ``_finish -> validate_and_finalize -> run_checks`` pipeline.
Gate flips use the deterministic ``PHYS_PCT_OVER_100`` check (``checks.py``): a
critical-parameter ``value:"150" unit:"%"`` blocks the quality gate and ``"50"``
passes, so an edit can flip ``blocked -> ok`` and back with no model in the loop.

Each test pops the session/project it created in ``finally``; the durable-project
test points ``srv._STORE`` at a throwaway SQLite file so ``./projects.db`` is never
touched. All tests are additive — the existing ~198 script-run tests are untouched.

Runnable directly (``python tests/test_edit.py``) or under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time as _time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import Session  # noqa: E402
from app.projects import make_project  # noqa: E402
from app.store import SQLiteProjectStore  # noqa: E402
from app.validation import validate_and_finalize  # noqa: E402

client = TestClient(srv.app)


# --- fixtures ---------------------------------------------------------------

def _material_protocol(amount="5", unit="uL", provenance="stated", **extra):
    """A minimal protocol whose single material (mat:0) is editable."""
    mat = {"name": "Buffer", "amount": amount, "unit": unit, "provenance": provenance}
    mat.update(extra)
    return {
        "title": "P", "summary": "s", "estimated_duration": "1 h",
        "materials": [mat],
        "steps": [{"title": "Mix", "instruction": "d", "critical_parameters": []}],
        "assumptions_log": [],
    }


def _cp_protocol(value, unit="%", provenance="stated", **extra):
    """A protocol whose single critical parameter (step:0/param:0) is editable.
    With ``value='150' unit='%'`` the quality gate blocks (PHYS_PCT_OVER_100)."""
    cp = {"name": "final concentration", "value": value, "unit": unit,
          "provenance": provenance}
    cp.update(extra)
    return {
        "title": "P", "summary": "s", "estimated_duration": "1 h",
        "materials": [],
        "steps": [{"title": "Dilute", "instruction": "d", "critical_parameters": [cp]}],
        "assumptions_log": [],
    }


def _seed(sid, protocol, source_kind="paper", project_id=None):
    st = srv.Store(
        session=Session(source_kind=source_kind,
                        messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), protocol=protocol, project_id=project_id)
    srv._SESSIONS[sid] = st
    return st


def _edit(sid, target_id, field, value, unit=None):
    body = {"target_id": target_id, "field": field, "value": value}
    if unit is not None:
        body["unit"] = unit
    return client.post(f"/api/protocol/{sid}/edit", json=body)


@contextmanager
def _tmp_store():
    """Point ``srv._STORE`` at a fresh SQLite file for the duration of the block."""
    tmpdir = tempfile.mkdtemp(prefix="edit_persist_")
    db_path = os.path.join(tmpdir, "projects.db")
    old = srv._STORE
    store = SQLiteProjectStore(db_path)
    srv._STORE = store
    try:
        yield store
    finally:
        srv._STORE = old
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


# --- error paths ------------------------------------------------------------

def test_edit_unknown_session_is_404():
    r = _edit("nope", "mat:0", "amount", "10")
    assert r.status_code == 404


def test_edit_no_protocol_is_409():
    sid = "editnoproto"
    srv._SESSIONS[sid] = srv.Store(session=Session(), created=_time.time())  # protocol None
    try:
        r = _edit(sid, "mat:0", "amount", "10")
        assert r.status_code == 409
        assert "Generate a protocol first" in r.json()["detail"]
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_unknown_id_is_404():
    sid = "editbadid"
    _seed(sid, _material_protocol())
    try:
        r = _edit(sid, "mat:99", "amount", "10")
        assert r.status_code == 404
        assert "Unknown value id" in r.json()["detail"]
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_disallowed_field_is_422():
    sid = "editbadfield"
    _seed(sid, _material_protocol())
    try:
        r = _edit(sid, "mat:0", "name", "Buffer2")
        assert r.status_code == 422
        assert "cannot be edited" in r.json()["detail"]
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_non_editable_kind_is_422():
    sid = "editbadkind"
    _seed(sid, _material_protocol())
    try:
        r = _edit(sid, "step:0", "value", "x")  # a step is not inline-editable
        assert r.status_code == 422
        assert "not inline-editable" in r.json()["detail"]
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_empty_value_is_422():
    sid = "editempty"
    _seed(sid, _material_protocol())
    try:
        r = _edit(sid, "mat:0", "amount", "")
        assert r.status_code == 422
        assert "cannot be empty" in r.json()["detail"]
    finally:
        srv._SESSIONS.pop(sid, None)


# --- valid edit: value + provenance flip ------------------------------------

def test_valid_edit_sets_user_input():
    sid = "editok"
    # A grounded material carrying a citation: editing it makes the user the source,
    # so the citation is dropped and citation_verified survives as False.
    _seed(sid, _material_protocol(amount="5", unit="uL", provenance="literature_grounded",
                                  citation={"pmid": "1"}, citation_verified=True))
    try:
        r = _edit(sid, "mat:0", "amount", "10", unit="mM")
        assert r.status_code == 200
        m = r.json()["protocol"]["materials"][0]
        assert m["amount"] == "10"
        assert m["unit"] == "mM"
        assert m["provenance"] == "user_input"
        assert m["provenance_note"] and "5" in m["provenance_note"]  # old value recorded
        assert m["citation"] is None
        # grounding is dropped: citation_verified is no longer True (finalization
        # strips the key once the citation is gone, so absent-or-False is the contract).
        assert m.get("citation_verified") is not True
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_unit_coedit():
    sid = "editcoedit"
    _seed(sid, _material_protocol(amount="5", unit="uL"))
    try:
        r = _edit(sid, "mat:0", "amount", "5", unit="uL")
        assert r.status_code == 200
        m = r.json()["protocol"]["materials"][0]
        assert m["amount"] == "5" and m["unit"] == "uL"
    finally:
        srv._SESSIONS.pop(sid, None)


def test_cp_edit_flips_provenance_badge_fields():
    sid = "editcp"
    _seed(sid, _cp_protocol("50", provenance="literature_grounded",
                            citation={"pmid": "1"}, citation_verified=True,
                            evidence={"quote": "x"}))
    try:
        r = _edit(sid, "step:0/param:0", "value", "60")
        assert r.status_code == 200
        cp = r.json()["protocol"]["steps"][0]["critical_parameters"][0]
        assert cp["provenance"] == "user_input"
        assert cp["citation"] is None
        assert "evidence" not in cp
    finally:
        srv._SESSIONS.pop(sid, None)


# --- gate re-runs on edit ---------------------------------------------------

def test_edit_fixes_gate_blocked_to_ok():
    # A bad dilution (150 %) blocks the gate; correcting it to 50 % flips blocked -> ok.
    seed = _cp_protocol("150", unit="%")
    pre = validate_and_finalize(seed, allow_stated=True, source_text=None, source_exact=None)
    assert pre["quality_gate"]["status"] == "blocked"

    sid = "editfix"
    _seed(sid, _cp_protocol("150", unit="%"))
    try:
        r = _edit(sid, "step:0/param:0", "value", "50")
        assert r.status_code == 200
        gate = r.json()["validation_report"]["quality_gate"]
        assert gate["status"] == "ok"
        assert all(e["code"] != "PHYS_PCT_OVER_100" for e in gate["errors"])
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_breaks_gate_ok_to_blocked():
    # A clean protocol (50 %) has an ok gate; a bad edit (150 %) flips ok -> blocked.
    seed = _cp_protocol("50", unit="%")
    pre = validate_and_finalize(seed, allow_stated=True, source_text=None, source_exact=None)
    assert pre["quality_gate"]["status"] == "ok"

    sid = "editbreak"
    _seed(sid, _cp_protocol("50", unit="%"))
    try:
        r = _edit(sid, "step:0/param:0", "value", "150")
        assert r.status_code == 200
        gate = r.json()["validation_report"]["quality_gate"]
        assert gate["status"] == "blocked"
        assert any(e["code"] == "PHYS_PCT_OVER_100" for e in gate["errors"])
    finally:
        srv._SESSIONS.pop(sid, None)


# --- idempotency ------------------------------------------------------------

def test_edit_idempotent():
    sid = "editidem"
    _seed(sid, _material_protocol(amount="5", unit="uL"))
    try:
        r1 = _edit(sid, "mat:0", "amount", "10", unit="mM")
        r2 = _edit(sid, "mat:0", "amount", "10", unit="mM")
        assert r1.status_code == r2.status_code == 200
        m1 = r1.json()["protocol"]["materials"][0]
        m2 = r2.json()["protocol"]["materials"][0]
        # Re-applying an identical patch is stable: same value/unit/provenance, the
        # note is SET not appended (single "was" clause, never stacked), gate identical.
        assert m1["amount"] == m2["amount"] == "10"
        assert m1["unit"] == m2["unit"] == "mM"
        assert m1["provenance"] == m2["provenance"] == "user_input"
        assert m2["provenance_note"].count("was") == 1  # not stacked across re-edits
        assert (r1.json()["validation_report"]["quality_gate"]["status"]
                == r2.json()["validation_report"]["quality_gate"]["status"])
    finally:
        srv._SESSIONS.pop(sid, None)


# --- provenance-by-op map ---------------------------------------------------

def test_edit_provenance_map_has_edit():
    assert srv._PROVENANCE_BY_OP["edit"] == "user_edit"


# --- durable persistence (Milestone-1 ProtocolVersion) ----------------------

def test_edit_persists_protocol_version():
    with _tmp_store() as store:
        proj = store.create_project(make_project(
            title="T", workflow="design", detected_workflow="design"))
        pid = proj.project_id
        sid = "editpersist"
        _seed(sid, _material_protocol(amount="5", unit="uL"), project_id=pid)
        try:
            r = _edit(sid, "mat:0", "amount", "10", unit="mM")
            assert r.status_code == 200
            vid = r.json()["protocol_version_id"]
            assert vid is not None, (
                "edit did not persist a ProtocolVersion — _persist_protocol_version "
                "raised (Provenance enum is missing the 'user_edit' member that "
                "_PROVENANCE_BY_OP['edit'] maps to)")
            ver = store.get_protocol_version(vid)
            assert ver.source_op == "edit"
            assert ver.provenance.value == "user_edit"
            assert ver.result["protocol"]["materials"][0]["amount"] == "10"
            assert store.get_project(pid).current_protocol_version_id == vid
        finally:
            srv._SESSIONS.pop(sid, None)


# --- additive / non-breaking guarantees -------------------------------------

def test_existing_endpoints_unchanged():
    # The edit route must not shadow existing routes or change their behavior.
    assert client.get("/healthz").status_code == 200
    assert client.post("/api/revise",
                       json={"session_id": "nope", "instruction": "change it"}).status_code == 404
    assert client.get("/api/protocol/deadbeef.md").status_code == 404


def test_edit_result_keys_unchanged():
    # The edit payload is the same _finish shape as /api/resolve and /api/revise:
    # no result key is renamed, removed, or added-required beyond the Epic-4 contract.
    sid = "editkeys"
    _seed(sid, _material_protocol())
    try:
        r = _edit(sid, "mat:0", "amount", "10", unit="mM")
        assert r.status_code == 200
        body = r.json()
        for k in ("session_id", "phase", "protocol", "validation_report",
                  "grounding_log", "markdown_url", "materials_csv_url",
                  "chosen_assay", "project_id", "protocol_version_id"):
            assert k in body, f"missing result key {k!r}"
        assert body["phase"] == "complete"
        assert body["markdown_url"] == f"/api/protocol/{sid}.md"
        assert body["materials_csv_url"] == f"/api/protocol/{sid}/materials.csv"
    finally:
        srv._SESSIONS.pop(sid, None)


# --- Epic 2 §II: /edit resolves a target by its content-stable id ------------

def test_edit_resolves_by_stable_id():
    from app.checks import assign_stable_ids, ensure_ids
    # two materials; target the SECOND by its content-derived material_id (not _id).
    proto = {
        "title": "P", "summary": "s", "estimated_duration": "1 h",
        "materials": [
            {"name": "Tris", "amount": "5", "unit": "uL", "provenance": "stated"},
            {"name": "Magnesium", "amount": "2", "unit": "mM", "provenance": "stated"},
        ],
        "steps": [{"title": "Mix", "instruction": "d", "critical_parameters": []}],
        "assumptions_log": [],
    }
    probe = {"materials": [dict(m) for m in proto["materials"]]}
    ensure_ids(probe); assign_stable_ids(probe)
    mg_stable = probe["materials"][1]["material_id"]
    assert mg_stable.startswith("m_")            # disjoint from the mat:1 positional id

    sid = "editstable"
    _seed(sid, proto)
    try:
        r = _edit(sid, mg_stable, "amount", "4", unit="mM")
        assert r.status_code == 200
        mats = r.json()["protocol"]["materials"]
        # the SECOND material was edited (resolved by stable id, not position).
        assert mats[1]["name"] == "Magnesium" and mats[1]["amount"] == "4"
        assert mats[0]["amount"] == "5"          # first material untouched
    finally:
        srv._SESSIONS.pop(sid, None)


def test_edit_by_stable_id_matches_edit_by_positional_id():
    from app.checks import assign_stable_ids, ensure_ids
    proto = _material_protocol(amount="5", unit="uL")
    probe = {"materials": [dict(proto["materials"][0])]}
    ensure_ids(probe); assign_stable_ids(probe)
    stable = probe["materials"][0]["material_id"]

    sid_a, sid_b = "editbypos", "editbystable"
    _seed(sid_a, _material_protocol(amount="5", unit="uL"))
    _seed(sid_b, _material_protocol(amount="5", unit="uL"))
    try:
        ra = _edit(sid_a, "mat:0", "amount", "9", unit="uL")
        rb = _edit(sid_b, stable, "amount", "9", unit="uL")
        assert ra.status_code == rb.status_code == 200
        ma = ra.json()["protocol"]["materials"][0]
        mb = rb.json()["protocol"]["materials"][0]
        assert ma["amount"] == mb["amount"] == "9"
        assert ma["provenance"] == mb["provenance"] == "user_input"
    finally:
        srv._SESSIONS.pop(sid_a, None)
        srv._SESSIONS.pop(sid_b, None)


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
