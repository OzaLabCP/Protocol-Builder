"""Milestone-1 "Projects" layer — acceptance-criteria tests (NORMATIVE SPEC §4–§8).

Each test maps to one acceptance criterion:

  1. a project survives application restart
  2. changing the detected workflow preserves ALL submitted inputs
  3. multiple projects do not share transcripts / protocol state
  4. an invalid project id -> 404
  5. a concurrent update -> 409
  6. migration / version handling (DDL user_version + app-data schema_version)

No network is used: the intake path is model-free by contract, and the one place a
protocol version is written (`_finish`) is driven directly with a hand-built protocol
dict. All persistence goes through a throwaway SQLite file under a temp dir (or an
in-memory store), so the repo's ``./projects.db`` is never touched.

Runnable directly (``python tests/test_projects_acceptance.py``) or under pytest.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time as _time
from contextlib import contextmanager

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

import app.server as srv  # noqa: E402
from app.agent import Session  # noqa: E402
from app.projects import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    LifecycleStatus,
    WorkflowType,
    make_project,
)
from app.store import (  # noqa: E402
    CURRENT_USER_VERSION,
    ConcurrencyError,
    ProjectNotFound,
    SQLiteProjectStore,
    StoreError,
)

client = TestClient(srv.app)


@contextmanager
def tmp_store():
    """Point ``srv._STORE`` at a fresh SQLite file under a temp dir for the duration
    of the block, restoring (and closing) afterwards. Returns (store, db_path)."""
    tmpdir = tempfile.mkdtemp(prefix="proj_accept_")
    db_path = os.path.join(tmpdir, "projects.db")
    old = srv._STORE
    store = SQLiteProjectStore(db_path)
    srv._STORE = store
    try:
        yield store, db_path
    finally:
        srv._STORE = old
        try:
            store.close()
        except Exception:  # noqa: BLE001
            pass


def _make_session_store(project_id: str, session_id: str, source_kind: str = "paper") -> srv.Store:
    """A live in-memory Store linked to a durable project, ready for _finish()."""
    st = srv.Store(
        session=Session(source_kind=source_kind, messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(),
        project_id=project_id,
    )
    srv._SESSIONS[session_id] = st
    return st


def _protocol(title: str) -> dict:
    return {
        "title": title,
        "summary": "s",
        "estimated_duration": "1 h",
        "materials": [],
        "steps": [],
        "assumptions_log": [],
    }


# --- 1. restart survival ----------------------------------------------------

def test_project_survives_application_restart():
    with tmp_store() as (store, db_path):
        # Create a project through the real HTTP intake path (no model call).
        r = client.post("/api/project", data={"free_text": "reproduce https://doi.org/10.1234/abc.def"})
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        assert r.json()["detected_workflow"] == "reproduce"

        # Write a real ProtocolVersion via the server's persistence seam.
        sid = "restart-sess"
        st = _make_session_store(pid, sid)
        try:
            _finished = srv._finish(sid, st, _protocol("Persisted Protocol"))
            assert _finished["protocol_version_id"] is not None
        finally:
            srv._SESSIONS.pop(sid, None)

        # Simulate an application restart: close the store, open a fresh instance on
        # the SAME file, and swap it in as the module store.
        store.close()
        store2 = SQLiteProjectStore(db_path)
        srv._STORE = store2
        try:
            got = client.get(f"/api/project/{pid}")
            assert got.status_code == 200, got.text
            body = got.json()
            assert body["id"] == pid
            assert body["detected_workflow"] == "reproduce"
            assert body["lifecycle_status"] == "protocol_ready"
            assert body["version"] == 1
            # The full protocol snapshot rehydrates unchanged after restart.
            assert body["latest_result"]["protocol"]["title"] == "Persisted Protocol"
            assert len(body["protocol_versions"]) == 1
            # row_version persisted across the "restart".
            _p, rv = store2.get_project_versioned(pid)
            assert rv >= 2
        finally:
            store2.close()


# --- 2. changing workflow preserves all submitted inputs --------------------

def test_changing_workflow_preserves_all_inputs():
    with tmp_store() as (store, _db):
        submitted = {
            "free_text": "We want to characterize binding.",
            "hypothesis": "Compound X increases Y binding affinity.",
            "measurement_goal": "Determine the Kd of the interaction.",
            "existing_protocol_text": "1. Incubate 20 uL at 37 C.",
            "identifier": "PMID: 12345678",
            "constraints": '{"equipment": "plate reader", "time": "2 days", "skill": "intermediate"}',
        }
        r = client.post("/api/project", data=submitted)
        assert r.status_code == 200, r.text
        pid = r.json()["id"]
        detected = r.json()["detected_workflow"]

        # Snapshot the full server-side inputs BEFORE the workflow change.
        before = store.get_project(pid)
        inputs_before = dict(before.inputs)
        summary_before = dict(before.input_summary)
        assert inputs_before["hypothesis"] == submitted["hypothesis"]
        assert inputs_before["existing_protocol_text"] == submitted["existing_protocol_text"]
        assert inputs_before["constraints"]["equipment"] == "plate reader"

        # Change the workflow to something different from the detected one.
        new_wf = "scale" if detected != "scale" else "troubleshoot"
        w = client.post(f"/api/project/{pid}/workflow", json={"workflow": new_wf})
        assert w.status_code == 200, w.text
        assert w.json()["workflow"] == new_wf
        # detected_workflow is immutable.
        assert w.json()["detected_workflow"] == detected

        # EVERY submitted input is preserved verbatim; nothing cleared or truncated.
        after = store.get_project(pid)
        assert after.inputs == inputs_before, "raw inputs must be preserved across /workflow"
        assert after.input_summary == summary_before
        assert after.hypothesis == submitted["hypothesis"]
        assert after.measurement_objective == submitted["measurement_goal"]
        assert after.detected_workflow == WorkflowType(detected)
        assert after.workflow == WorkflowType(new_wf)
        assert after.confirmation_required is False
        # Idempotent re-apply keeps inputs intact too.
        w2 = client.post(f"/api/project/{pid}/workflow", json={"workflow": new_wf})
        assert w2.status_code == 200
        assert store.get_project(pid).inputs == inputs_before


# --- 3. multiple projects do not share transcript / protocol state ----------

def test_multiple_projects_do_not_share_state():
    with tmp_store() as (store, _db):
        a = client.post("/api/project", data={"free_text": "first project about assays"}).json()["id"]
        b = client.post("/api/project", data={"free_text": "second unrelated project"}).json()["id"]
        assert a != b

        # Each project gets its OWN live session and its OWN protocol version.
        sid_a, sid_b = "sess-A", "sess-B"
        st_a = _make_session_store(a, sid_a)
        st_b = _make_session_store(b, sid_b)
        srv._link_session(a, sid_a)
        srv._link_session(b, sid_b)
        try:
            ra = srv._finish(sid_a, st_a, _protocol("Alpha Protocol"))
            rb = srv._finish(sid_b, st_b, _protocol("Beta Protocol"))
        finally:
            srv._SESSIONS.pop(sid_a, None)
            srv._SESSIONS.pop(sid_b, None)

        # The two version ids are distinct and land in different projects.
        assert ra["protocol_version_id"] != rb["protocol_version_id"]

        da = client.get(f"/api/project/{a}").json()
        db = client.get(f"/api/project/{b}").json()

        # Protocol state is not shared.
        assert da["latest_result"]["protocol"]["title"] == "Alpha Protocol"
        assert db["latest_result"]["protocol"]["title"] == "Beta Protocol"
        assert da["current_protocol_version_id"] != db["current_protocol_version_id"]
        assert da["current_protocol_version_id"] == ra["protocol_version_id"]
        assert db["current_protocol_version_id"] == rb["protocol_version_id"]

        # Transcript / session linkage is not shared.
        assert da["session_id"] == sid_a
        assert db["session_id"] == sid_b
        assert da["session_id"] != db["session_id"]

        # Each project owns exactly one version; neither leaks into the other.
        va = {v["id"] for v in da["protocol_versions"]}
        vb = {v["id"] for v in db["protocol_versions"]}
        assert len(va) == 1 and len(vb) == 1
        assert va.isdisjoint(vb)
        assert store.list_protocol_versions(a)[0].title == "Alpha Protocol"
        assert store.list_protocol_versions(b)[0].title == "Beta Protocol"


# --- 4. invalid project id -> 404 -------------------------------------------

def test_invalid_project_id_returns_404():
    with tmp_store():
        for r in (
            client.get("/api/project/proj_does_not_exist"),
            client.get("/api/project/proj_does_not_exist/protocol.md"),
            client.get("/api/project/proj_does_not_exist/materials.csv"),
        ):
            assert r.status_code == 404, r.text
            assert r.json()["detail"] == srv._PROJECT_404 or "protocol" in r.json()["detail"].lower()
        # workflow override on a missing project is also a 404 (valid workflow, unknown id).
        w = client.post("/api/project/proj_missing/workflow", json={"workflow": "reproduce"})
        assert w.status_code == 404
        assert w.json()["detail"] == srv._PROJECT_404


# --- 5. concurrent update -> 409 --------------------------------------------

def test_concurrent_update_returns_409_at_store():
    """The store raises ConcurrencyError when a writer holds a stale row_version."""
    with tmp_store() as (store, _db):
        p = store.create_project(make_project(
            title="C", workflow="reproduce", detected_workflow="reproduce"))
        proj, rv = store.get_project_versioned(p.project_id)  # rv == 1

        # First writer wins, bumping row_version to 2.
        proj.title = "winner"
        store.update_project(proj, expected_row_version=rv)

        # Second writer still holds the stale rv == 1 -> conflict.
        proj.title = "loser"
        raised = None
        try:
            store.update_project(proj, expected_row_version=rv)
        except ConcurrencyError as e:
            raised = e
        assert raised is not None
        assert raised.expected == 1 and raised.actual == 2


def test_concurrent_update_returns_409_over_http():
    """The /workflow endpoint maps a ConcurrencyError (after the one retry) to HTTP 409."""
    with tmp_store() as (store, _db):
        p = store.create_project(make_project(
            title="C", workflow="reproduce", detected_workflow="reproduce"))
        pid = p.project_id

        # Force a persistent lost race: every optimistic write conflicts, so even after
        # _update_with_retry's single retry the endpoint sees ConcurrencyError -> 409.
        real_update = store.update_project

        def always_conflict(project, *, expected_row_version):
            raise ConcurrencyError(project.project_id, expected_row_version, expected_row_version + 99)

        store.update_project = always_conflict  # type: ignore[method-assign]
        try:
            r = client.post(f"/api/project/{pid}/workflow", json={"workflow": "adapt"})
            assert r.status_code == 409, r.text
            assert r.json()["detail"] == srv._PROJECT_409
        finally:
            store.update_project = real_update  # type: ignore[method-assign]


# --- 6. migration / version handling ----------------------------------------

def test_ddl_migration_sets_user_version():
    """A fresh DB is migrated 0 -> CURRENT_USER_VERSION and the tables exist."""
    tmpdir = tempfile.mkdtemp(prefix="proj_migrate_")
    db_path = os.path.join(tmpdir, "projects.db")
    store = SQLiteProjectStore(db_path)
    try:
        raw = sqlite3.connect(db_path)
        try:
            uv = raw.execute("PRAGMA user_version").fetchone()[0]
            assert uv == CURRENT_USER_VERSION == 2
            names = {row[0] for row in raw.execute(
                "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            assert {"projects", "protocol_versions"} <= names
        finally:
            raw.close()
    finally:
        store.close()

    # Re-opening an already-migrated DB is a no-op (idempotent runner).
    store2 = SQLiteProjectStore(db_path)
    try:
        raw = sqlite3.connect(db_path)
        try:
            assert raw.execute("PRAGMA user_version").fetchone()[0] == CURRENT_USER_VERSION
        finally:
            raw.close()
    finally:
        store2.close()


def test_ddl_forward_guard_raises_on_newer_db():
    """A DB stamped with a newer user_version than we support fails closed."""
    tmpdir = tempfile.mkdtemp(prefix="proj_future_")
    db_path = os.path.join(tmpdir, "projects.db")
    raw = sqlite3.connect(db_path)
    raw.execute(f"PRAGMA user_version = {CURRENT_USER_VERSION + 1}")
    raw.commit()
    raw.close()
    raised = None
    try:
        SQLiteProjectStore(db_path)
    except StoreError as e:
        raised = e
    assert raised is not None
    assert "newer than supported" in str(raised)


def test_appdata_schema_version_fail_closed_on_newer_payload():
    """A stored project whose JSON schema_version is newer than supported fails closed
    on read (the app-data migration layer's forward guard)."""
    with tmp_store() as (store, _db):
        p = store.create_project(make_project(
            title="V", workflow="design", detected_workflow="design"))
        # Hand-corrupt the row's data JSON to claim a future app-data schema version.
        conn = store._conn
        with store._lock:
            row = conn.execute(
                "SELECT data FROM projects WHERE project_id=?", (p.project_id,)).fetchone()
            import json as _json
            obj = _json.loads(row["data"])
            obj["schema_version"] = CURRENT_SCHEMA_VERSION + 1
            conn.execute(
                "UPDATE projects SET data=? WHERE project_id=?",
                (_json.dumps(obj), p.project_id))
            conn.commit()
        raised = None
        try:
            store.get_project(p.project_id)
        except StoreError as e:
            raised = e
        assert raised is not None
        assert "newer than supported" in str(raised)


def test_appdata_schema_version_stamped_on_create():
    """New projects carry the current app-data schema_version end-to-end."""
    with tmp_store() as (store, _db):
        r = client.post("/api/project", data={"free_text": "a fresh intake"})
        assert r.status_code == 200
        got = store.get_project(r.json()["id"])
        assert got.schema_version == CURRENT_SCHEMA_VERSION


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
