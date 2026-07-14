"""Project deletion (BLOCKER-4) and session-less read-only guarantee (BLOCKER-1).

BLOCKER-4 — the ONLY new endpoint: ``DELETE /api/project/{id}`` permanently removes a
project row AND its ``protocol_versions`` rows, returns ``{"deleted": id}``, evicts any
bound live session, and 404s on an unknown id. Other projects are untouched (retention).

BLOCKER-1 — a project loaded WITHOUT a live session exposes no working mutate path. The
frontend gates this in the UI, but at the API layer the guarantee is testable in two
load-bearing ways: (a) there is NO ``/resume`` route that could re-arm a stale snapshot,
and (b) every session-mutating endpoint 404s when handed an unknown/expired session id
(the only handle a session-less snapshot could try to use).

No network: the intake path is model-free and deletion is pure store/HTTP.

Runnable directly (``python tests/test_delete_project.py``) or under pytest.
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
from app.store import ProjectNotFound, SQLiteProjectStore  # noqa: E402

client = TestClient(srv.app)


@contextmanager
def tmp_store():
    """Point ``srv._STORE`` at a fresh SQLite file for the block, restoring afterwards."""
    tmpdir = tempfile.mkdtemp(prefix="del_proj_")
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


def _protocol(title: str) -> dict:
    return {"title": title, "summary": "s", "estimated_duration": "1 h",
            "materials": [], "steps": [], "assumptions_log": []}


def _make_session_store(project_id: str, session_id: str) -> srv.Store:
    st = srv.Store(
        session=Session(source_kind="paper", messages=[{"role": "user", "content": "seed"}]),
        created=_time.time(), project_id=project_id)
    srv._SESSIONS[session_id] = st
    return st


# --- BLOCKER-4: endpoint happy path -----------------------------------------

def test_delete_endpoint_removes_project_and_versions():
    with tmp_store() as (store, _db):
        pid = client.post("/api/project", data={"free_text": "reproduce this assay"}).json()["id"]
        # Persist two real ProtocolVersions through the server's write seam.
        sid = "del-sess"
        st = _make_session_store(pid, sid)
        try:
            srv._finish(sid, st, _protocol("V one"))
            srv._finish(sid, st, _protocol("V two"))
        finally:
            srv._SESSIONS.pop(sid, None)
        assert len(store.list_protocol_versions(pid)) == 2

        r = client.delete(f"/api/project/{pid}")
        assert r.status_code == 200, r.text
        assert r.json() == {"deleted": pid}

        # The project and its versions are gone at the store AND over HTTP.
        assert client.get(f"/api/project/{pid}").status_code == 404
        assert store.list_protocol_versions(pid) == []
        raised = None
        try:
            store.get_project(pid)
        except ProjectNotFound as e:
            raised = e
        assert raised is not None


def test_delete_unknown_project_404():
    with tmp_store():
        r = client.delete("/api/project/proj_does_not_exist")
        assert r.status_code == 404, r.text
        assert r.json()["detail"] == srv._PROJECT_404


def test_delete_evicts_bound_live_session():
    with tmp_store() as (store, _db):
        pid = client.post("/api/project", data={"free_text": "reproduce this too"}).json()["id"]
        sid = "del-live-sess"
        _make_session_store(pid, sid)
        # Bind the session onto the durable project (what the real bridge does).
        srv._link_session(pid, sid)
        assert getattr(store.get_project(pid), "session_id", None) == sid
        assert sid in srv._SESSIONS
        try:
            r = client.delete(f"/api/project/{pid}")
            assert r.status_code == 200, r.text
            # The bound in-memory session is evicted so no stale handle survives the project.
            assert sid not in srv._SESSIONS
        finally:
            srv._SESSIONS.pop(sid, None)


def test_delete_retains_other_projects():
    """Deleting one project leaves every OTHER project + its versions fully intact."""
    with tmp_store() as (store, _db):
        keep = client.post("/api/project", data={"free_text": "keep this one"}).json()["id"]
        drop = client.post("/api/project", data={"free_text": "drop this one"}).json()["id"]
        sid_k, sid_d = "keep-sess", "drop-sess"
        stk = _make_session_store(keep, sid_k)
        std = _make_session_store(drop, sid_d)
        try:
            srv._finish(sid_k, stk, _protocol("Kept Protocol"))
            srv._finish(sid_d, std, _protocol("Dropped Protocol"))
        finally:
            srv._SESSIONS.pop(sid_k, None)
            srv._SESSIONS.pop(sid_d, None)

        assert client.delete(f"/api/project/{drop}").status_code == 200

        # keep survives whole; drop is gone.
        got = client.get(f"/api/project/{keep}")
        assert got.status_code == 200
        assert got.json()["latest_result"]["protocol"]["title"] == "Kept Protocol"
        assert len(store.list_protocol_versions(keep)) == 1
        assert client.get(f"/api/project/{drop}").status_code == 404
        assert store.list_protocol_versions(drop) == []


# --- BLOCKER-1: session-less snapshot has no working mutate path -------------

def test_no_resume_route_exists():
    """A session-less snapshot cannot be re-armed: no route path mentions 'resume', and a
    POST to a resume-shaped URL is an unrouted 404 (the control + endpoint were removed)."""
    paths = {getattr(r, "path", "") for r in srv.app.routes}
    assert not any("resume" in p for p in paths), f"unexpected resume route(s): {paths}"
    # Both plausible shapes the old UI could have called are unrouted.
    assert client.post("/api/project/some-id/resume").status_code == 404
    assert client.post("/api/session/some-sid/resume").status_code == 404


def test_mutating_an_unknown_session_404s():
    """Every session-mutating endpoint 404s on an unknown/expired session id — the only
    handle a read-only (session-less) snapshot could attempt to mutate through."""
    unknown = "sess_does_not_exist"
    # /api/critique and /api/apply_fixes take a bare session_id body -> _get -> 404.
    for url, body in (
        ("/api/critique", {"session_id": unknown, "apply": True}),
        ("/api/apply_fixes", {"session_id": unknown}),
        ("/api/revise", {"session_id": unknown, "instruction": "change x"}),
        ("/api/resolve", {"session_id": unknown, "answers": []}),
    ):
        r = client.post(url, json=body)
        assert r.status_code == 404, f"{url} -> {r.status_code}: {r.text}"
        assert "session" in r.json()["detail"].lower()
    # Inline value edit keyed by an unknown session id is likewise a 404 (valid body).
    e = client.post(f"/api/protocol/{unknown}/edit",
                    json={"target_id": "mat:0", "field": "amount", "value": "10"})
    assert e.status_code == 404, e.text
    assert "session" in e.json()["detail"].lower()


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
