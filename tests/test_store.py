"""Tests for app.store.SQLiteProjectStore (NORMATIVE SPEC §4).

Runnable directly: ``python tests/test_store.py``. Covers create/get/update/list,
save/get protocol_version, invalid id -> None, optimistic-concurrency conflict,
and restart survival across a fresh store instance on the same DB file.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timezone

# Ensure the repo root is importable when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.projects import (
    LifecycleStatus,
    Provenance,
    WorkflowType,
    make_project,
    new_version_id,
)
from app.store import (
    ConcurrencyError,
    ProjectNotFound,
    ProjectSummary,
    SQLiteProjectStore,
)


def _mk_project(title="Test project", workflow="reproduce"):
    return make_project(
        title=title,
        workflow=workflow,
        detected_workflow=workflow,
        detection_confidence=0.92,
        detection_reason="Found a DOI.",
    )


def _mk_version(project_id, version_number=1, source_op="resolve"):
    from app.projects import ProtocolVersion

    result = {
        "session_id": "sess123",
        "phase": "complete",
        "protocol": {"title": "My Protocol", "steps": []},
        "validation_report": {"status": "clean", "error_count": 0},
        "grounding_log": [],
        "markdown_url": "/api/protocol/sess123.md",
        "materials_csv_url": "/api/materials/sess123.csv",
        "chosen_assay": {"name": "assay-A"},
        "project_id": project_id,
        "protocol_version_id": None,
    }
    return ProtocolVersion(
        version_id=new_version_id(),
        project_id=project_id,
        version_number=version_number,
        provenance=Provenance.initial,
        source_op=source_op,
        title="My Protocol",
        result=result,
        validation_summary=result["validation_report"],
        chosen_assay=result["chosen_assay"],
        created_at=datetime.now(timezone.utc),
    )


def test_create_get():
    store = SQLiteProjectStore(":memory:")
    p = _mk_project()
    created = store.create_project(p)
    assert created.project_id == p.project_id
    got = store.get_project(p.project_id)
    assert got.project_id == p.project_id
    assert got.title == "Test project"
    assert got.workflow == WorkflowType.reproduce
    # versioned read starts at row_version 1
    got2, rv = store.get_project_versioned(p.project_id)
    assert rv == 1
    print("PASS test_create_get")


def test_update():
    store = SQLiteProjectStore(":memory:")
    p = store.create_project(_mk_project())
    proj, rv = store.get_project_versioned(p.project_id)
    assert rv == 1
    proj.title = "Renamed"
    proj.lifecycle_status = LifecycleStatus.workflow_confirmed
    updated, new_rv = store.update_project(proj, expected_row_version=rv)
    assert new_rv == 2
    assert updated.title == "Renamed"
    assert updated.lifecycle_status == LifecycleStatus.workflow_confirmed
    # updated_at bumped >= created_at
    assert updated.updated_at >= updated.created_at
    reread, rv3 = store.get_project_versioned(p.project_id)
    assert rv3 == 2
    assert reread.title == "Renamed"
    print("PASS test_update")


def test_list():
    store = SQLiteProjectStore(":memory:")
    a = store.create_project(_mk_project(title="Alpha", workflow="reproduce"))
    b = store.create_project(_mk_project(title="Beta", workflow="design"))
    rows = store.list_projects()
    assert len(rows) == 2
    assert all(isinstance(r, ProjectSummary) for r in rows)
    ids = {r.project_id for r in rows}
    assert a.project_id in ids and b.project_id in ids
    # filter by lifecycle_status
    filtered = store.list_projects(lifecycle_status=LifecycleStatus.intake_received)
    assert len(filtered) == 2
    none = store.list_projects(lifecycle_status=LifecycleStatus.protocol_ready)
    assert none == []
    # summary fields
    r = rows[0]
    assert r.version == 0
    assert r.has_protocol is False
    print("PASS test_list")


def test_protocol_version():
    store = SQLiteProjectStore(":memory:")
    p = store.create_project(_mk_project())
    v = _mk_version(p.project_id)
    saved = store.save_protocol_version(v)
    assert saved.version_id == v.version_id
    assert saved.result["protocol"]["title"] == "My Protocol"
    got = store.get_protocol_version(v.version_id)
    assert got.version_id == v.version_id
    assert got.provenance == Provenance.initial
    assert got.chosen_assay == {"name": "assay-A"}
    lst = store.list_protocol_versions(p.project_id)
    assert len(lst) == 1 and lst[0].version_id == v.version_id
    # idempotent on version_id (INSERT OR IGNORE)
    store.save_protocol_version(v)
    assert len(store.list_protocol_versions(p.project_id)) == 1

    # link version to project via update, then version-number surfaces in summary
    proj, rv = store.get_project_versioned(p.project_id)
    proj.protocol_versions.append(v.version_id)
    proj.current_protocol_version_id = v.version_id
    proj.lifecycle_status = LifecycleStatus.protocol_ready
    store.update_project(proj, expected_row_version=rv)
    summary = store.list_projects(lifecycle_status=LifecycleStatus.protocol_ready)
    assert len(summary) == 1
    assert summary[0].version == 1
    assert summary[0].has_protocol is True
    print("PASS test_protocol_version")


def test_invalid_id_returns_none():
    store = SQLiteProjectStore(":memory:")
    assert store.try_get_project("proj_does_not_exist") is None
    # raising variants still raise
    try:
        store.get_project("proj_missing")
        raise AssertionError("expected ProjectNotFound")
    except ProjectNotFound:
        pass
    try:
        store.get_protocol_version("ver_missing")
        raise AssertionError("expected ProjectNotFound")
    except ProjectNotFound:
        pass
    print("PASS test_invalid_id_returns_none")


def test_optimistic_concurrency_conflict():
    store = SQLiteProjectStore(":memory:")
    p = store.create_project(_mk_project())
    proj, rv = store.get_project_versioned(p.project_id)  # rv == 1

    # First writer wins, bumping row_version to 2.
    proj.title = "First"
    store.update_project(proj, expected_row_version=rv)

    # Second writer holds the stale rv == 1 -> conflict.
    proj.title = "Second"
    try:
        store.update_project(proj, expected_row_version=rv)
        raise AssertionError("expected ConcurrencyError")
    except ConcurrencyError as e:
        assert e.project_id == p.project_id
        assert e.expected == 1
        assert e.actual == 2

    # Vanished-row case: actual is None.
    ghost = _mk_project()
    try:
        store.update_project(ghost, expected_row_version=1)
        raise AssertionError("expected ConcurrencyError for missing row")
    except ConcurrencyError as e:
        assert e.actual is None
    print("PASS test_optimistic_concurrency_conflict")


def test_restart_survival():
    tmpdir = tempfile.mkdtemp(prefix="store_test_")
    db_path = os.path.join(tmpdir, "projects.db")

    store1 = SQLiteProjectStore(db_path)
    p = store1.create_project(_mk_project(title="Persistent", workflow="design"))
    v = _mk_version(p.project_id)
    store1.save_protocol_version(v)
    proj, rv = store1.get_project_versioned(p.project_id)
    proj.protocol_versions.append(v.version_id)
    proj.current_protocol_version_id = v.version_id
    proj.lifecycle_status = LifecycleStatus.protocol_ready
    store1.update_project(proj, expected_row_version=rv)
    store1.close()

    # Fresh store instance on the same DB file -> data survives.
    store2 = SQLiteProjectStore(db_path)
    got = store2.get_project(p.project_id)
    assert got.title == "Persistent"
    assert got.workflow == WorkflowType.design
    assert got.current_protocol_version_id == v.version_id
    got_v = store2.get_protocol_version(v.version_id)
    assert got_v.title == "My Protocol"
    _, rv2 = store2.get_project_versioned(p.project_id)
    assert rv2 == 2  # row_version persisted across restart
    summ = store2.list_projects()
    assert len(summ) == 1 and summ[0].version == 1
    store2.close()
    print("PASS test_restart_survival")


def test_concurrent_version_allocation_same_project():
    """BLOCKER-5: two near-simultaneous saves on ONE project each get a DISTINCT,
    sequential version_number allocated by the store (never a collision or a lost
    write), both rows survive, and the UNIQUE(project_id, version_number) index holds.

    Each worker passes version_number=0 (a placeholder) — the store is the sole
    authority for the number, so a caller can no longer race the count."""
    import threading

    store = SQLiteProjectStore(":memory:")
    p = store.create_project(_mk_project())

    barrier = threading.Barrier(2)
    results: dict[str, int] = {}
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker():
        try:
            v = _mk_version(p.project_id, version_number=0)
            barrier.wait()  # maximize the overlap of the two saves
            saved = store.save_protocol_version(v)
            with lock:
                results[saved.version_id] = saved.version_number
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start()
    t1.join(); t2.join()

    assert not errors, f"unexpected save error(s): {errors!r}"
    # Two distinct rows, numbered 1 and 2 — no collision, no lost write.
    assert len(results) == 2
    assert sorted(results.values()) == [1, 2]
    # Both rows are durable and the numbers reconcile with what the store lists back.
    lst = store.list_protocol_versions(p.project_id)
    assert len(lst) == 2
    assert {r.version_number for r in lst} == {1, 2}
    store.close()
    print("PASS test_concurrent_version_allocation_same_project")


def test_delete_project_removes_project_and_versions():
    """BLOCKER-4 (store happy path): delete drops the project row AND its versions;
    a subsequent read raises ProjectNotFound and the version list is empty."""
    store = SQLiteProjectStore(":memory:")
    p = store.create_project(_mk_project())
    v1 = store.save_protocol_version(_mk_version(p.project_id, version_number=0))
    v2 = store.save_protocol_version(_mk_version(p.project_id, version_number=0))
    assert len(store.list_protocol_versions(p.project_id)) == 2
    assert {v1.version_number, v2.version_number} == {1, 2}

    store.delete_project(p.project_id)

    raised = None
    try:
        store.get_project(p.project_id)
    except ProjectNotFound as e:
        raised = e
    assert raised is not None
    assert store.list_protocol_versions(p.project_id) == []
    # The version rows themselves are gone (not merely unlinked).
    for vid in (v1.version_id, v2.version_id):
        try:
            store.get_protocol_version(vid)
            raise AssertionError("expected ProjectNotFound for deleted version")
        except ProjectNotFound:
            pass
    store.close()
    print("PASS test_delete_project_removes_project_and_versions")


def test_delete_unknown_project_raises_not_found():
    """BLOCKER-4 (store 404 path): deleting an absent project raises ProjectNotFound
    (the deterministic signal the endpoint maps to HTTP 404)."""
    store = SQLiteProjectStore(":memory:")
    raised = None
    try:
        store.delete_project("proj_does_not_exist")
    except ProjectNotFound as e:
        raised = e
    assert raised is not None
    store.close()
    print("PASS test_delete_unknown_project_raises_not_found")


def test_delete_project_retains_other_projects():
    """BLOCKER-4 (correct-retention): deleting one project leaves every OTHER project
    and its versions fully intact."""
    store = SQLiteProjectStore(":memory:")
    a = store.create_project(_mk_project(title="Keep me"))
    b = store.create_project(_mk_project(title="Delete me"))
    va = store.save_protocol_version(_mk_version(a.project_id, version_number=0))
    store.save_protocol_version(_mk_version(b.project_id, version_number=0))

    store.delete_project(b.project_id)

    # A untouched: project readable, its one version survives with its number.
    kept = store.get_project(a.project_id)
    assert kept.title == "Keep me"
    lst = store.list_protocol_versions(a.project_id)
    assert len(lst) == 1 and lst[0].version_id == va.version_id
    # B gone.
    try:
        store.get_project(b.project_id)
        raise AssertionError("expected ProjectNotFound for deleted project B")
    except ProjectNotFound:
        pass
    assert store.list_protocol_versions(b.project_id) == []
    store.close()
    print("PASS test_delete_project_retains_other_projects")


def main():
    test_create_get()
    test_update()
    test_list()
    test_protocol_version()
    test_invalid_id_returns_none()
    test_optimistic_concurrency_conflict()
    test_restart_survival()
    test_concurrent_version_allocation_same_project()
    test_delete_project_removes_project_and_versions()
    test_delete_unknown_project_raises_not_found()
    test_delete_project_retains_other_projects()
    print("\nAll store tests passed.")


if __name__ == "__main__":
    main()
