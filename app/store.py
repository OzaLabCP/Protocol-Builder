"""Milestone-1 "Projects" layer persistence — SQLite-backed ProjectStore.

Durable, restart-surviving persistence for ``ExperimentProject`` aggregates and
their ``ProtocolVersion`` rows. Optimistic concurrency via a ``row_version``
counter; DDL migrations via ``PRAGMA user_version``; app-data migrations via the
JSON ``schema_version`` field.

See NORMATIVE SPEC §4.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from app.projects import (
    CURRENT_SCHEMA_VERSION,
    ExperimentProject,
    LifecycleStatus,
    ProtocolVersion,
    WorkflowType,
)

# ---------------------------------------------------------------------------
# DB path resolution (§4.4)
# ---------------------------------------------------------------------------
DB_PATH = os.environ.get("GAPFILLER_DB_PATH", "").strip() or "./projects.db"

# DDL migration layer (§4.6.1)
CURRENT_USER_VERSION: int = 1


# ---------------------------------------------------------------------------
# 4.1 Exceptions
# ---------------------------------------------------------------------------
class StoreError(Exception):
    """Base class for store-layer errors."""


class ProjectNotFound(StoreError):
    """Raised when a project or version id is absent. Server -> 404."""

    def __init__(self, identifier: str):
        self.identifier = identifier
        super().__init__(f"Not found: {identifier}")


class ConcurrencyError(StoreError):
    """Optimistic-concurrency conflict on update. Server -> 409.

    ``actual=None`` means the row vanished between read and write.
    """

    def __init__(self, project_id: str, expected: int, actual: int | None):
        self.project_id = project_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Concurrency conflict on {project_id}: "
            f"expected row_version={expected}, actual={actual}"
        )


# ---------------------------------------------------------------------------
# 4.2 ProjectSummary
# ---------------------------------------------------------------------------
class ProjectSummary(BaseModel):
    project_id: str
    title: str
    workflow: WorkflowType
    detected_workflow: WorkflowType
    lifecycle_status: LifecycleStatus
    confirmation_required: bool
    detection_confidence: float
    current_protocol_version_id: str | None
    version: int
    has_protocol: bool
    created_at: datetime
    updated_at: datetime
    row_version: int


# ---------------------------------------------------------------------------
# 4.3 ProjectStore (ABC)
# ---------------------------------------------------------------------------
from abc import ABC, abstractmethod


class ProjectStore(ABC):
    @abstractmethod
    def create_project(self, project: ExperimentProject) -> ExperimentProject:
        ...

    @abstractmethod
    def get_project(self, project_id: str) -> ExperimentProject:
        ...

    @abstractmethod
    def get_project_versioned(self, project_id: str) -> tuple[ExperimentProject, int]:
        ...

    @abstractmethod
    def try_get_project(self, project_id: str) -> ExperimentProject | None:
        ...

    @abstractmethod
    def update_project(
        self, project: ExperimentProject, *, expected_row_version: int
    ) -> tuple[ExperimentProject, int]:
        ...

    @abstractmethod
    def list_projects(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        lifecycle_status: LifecycleStatus | str | None = None,
        order_by: str = "updated_at",
        descending: bool = True,
    ) -> list[ProjectSummary]:
        ...

    @abstractmethod
    def save_protocol_version(self, version: ProtocolVersion) -> ProtocolVersion:
        ...

    @abstractmethod
    def get_protocol_version(self, version_id: str) -> ProtocolVersion:
        ...

    @abstractmethod
    def list_protocol_versions(self, project_id: str) -> list[ProtocolVersion]:
        ...


# ---------------------------------------------------------------------------
# App-data (JSON payload) migration chain (§4.6.2)
# ---------------------------------------------------------------------------
def _upgrade_project_json(obj: dict[str, Any], from_sv: int) -> dict[str, Any]:
    """Upgrade a persisted project JSON dict from ``from_sv`` to current.

    At v1 the chain is empty; only the fail-closed guards below are active.
    """
    # for v in range(from_sv + 1, CURRENT_SCHEMA_VERSION + 1): apply _upgrade_to_vN
    return obj


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_project(project: ExperimentProject) -> str:
    return json.dumps(project.model_dump(mode="json"), separators=(",", ":"))


def _load_project(data: str) -> ExperimentProject:
    obj = json.loads(data)
    sv = obj.get("schema_version", 0)
    if sv > CURRENT_SCHEMA_VERSION:
        raise StoreError(
            f"Project schema_version {sv} is newer than supported "
            f"{CURRENT_SCHEMA_VERSION}."
        )
    if sv < CURRENT_SCHEMA_VERSION:
        obj = _upgrade_project_json(obj, sv)
    return ExperimentProject.model_validate(obj)


# ---------------------------------------------------------------------------
# 4.5 DDL (v1 baseline)
# ---------------------------------------------------------------------------
_DDL_V1 = """
CREATE TABLE IF NOT EXISTS projects (
  project_id       TEXT PRIMARY KEY,
  row_version      INTEGER NOT NULL DEFAULT 1,
  schema_version   INTEGER NOT NULL,
  title            TEXT NOT NULL,
  workflow         TEXT NOT NULL,
  detected_workflow TEXT NOT NULL,
  lifecycle_status TEXT NOT NULL,
  data             TEXT NOT NULL,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_projects_lifecycle  ON projects(lifecycle_status);
CREATE INDEX IF NOT EXISTS idx_projects_updated_at ON projects(updated_at DESC);

CREATE TABLE IF NOT EXISTS protocol_versions (
  version_id     TEXT PRIMARY KEY,
  project_id     TEXT NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
  version_number INTEGER NOT NULL,
  provenance     TEXT NOT NULL,
  source_op      TEXT NOT NULL,
  title          TEXT NOT NULL,
  result         TEXT NOT NULL,
  created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pv_project ON protocol_versions(project_id, version_number);
"""


def _migrate_to_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL_V1)


_MIGRATIONS = {1: _migrate_to_v1}


# ---------------------------------------------------------------------------
# 4.4 SQLiteProjectStore
# ---------------------------------------------------------------------------
class SQLiteProjectStore(ProjectStore):
    def __init__(self, db_path: str | None = None):
        raw = db_path if db_path is not None else DB_PATH
        if raw == ":memory:":
            self.db_path = ":memory:"
        else:
            self.db_path = os.path.abspath(raw)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread=False,
            detect_types=0,
        )
        self._conn.row_factory = sqlite3.Row
        self._configure()
        self._run_migrations()

    def _configure(self) -> None:
        conn = self._conn
        if self.db_path != ":memory:":
            conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")

    def _run_migrations(self) -> None:
        with self._lock:
            conn = self._conn
            cur = conn.execute("PRAGMA user_version")
            current = int(cur.fetchone()[0])
            if current > CURRENT_USER_VERSION:
                raise StoreError(
                    f"DB user_version {current} is newer than supported "
                    f"{CURRENT_USER_VERSION}."
                )
            try:
                conn.execute("BEGIN IMMEDIATE")
                for v in range(current + 1, CURRENT_USER_VERSION + 1):
                    _MIGRATIONS[v](conn)
                    # PRAGMA takes no placeholders — integer interpolation only.
                    conn.execute(f"PRAGMA user_version = {int(v)}")
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # -- helpers -----------------------------------------------------------
    @staticmethod
    def _current_version_number(conn: sqlite3.Connection, project: ExperimentProject) -> int:
        vid = project.current_protocol_version_id
        if not vid:
            return 0
        row = conn.execute(
            "SELECT version_number FROM protocol_versions WHERE version_id=?",
            (vid,),
        ).fetchone()
        return int(row["version_number"]) if row else 0

    def _row_to_project(self, row: sqlite3.Row) -> ExperimentProject:
        return _load_project(row["data"])

    def _row_to_summary(
        self, conn: sqlite3.Connection, row: sqlite3.Row
    ) -> ProjectSummary:
        project = _load_project(row["data"])
        version = self._current_version_number(conn, project)
        return ProjectSummary(
            project_id=project.project_id,
            title=project.title,
            workflow=project.workflow,
            detected_workflow=project.detected_workflow,
            lifecycle_status=project.lifecycle_status,
            confirmation_required=project.confirmation_required,
            detection_confidence=project.detection_confidence,
            current_protocol_version_id=project.current_protocol_version_id,
            version=version,
            has_protocol=project.current_protocol_version_id is not None,
            created_at=project.created_at,
            updated_at=project.updated_at,
            row_version=int(row["row_version"]),
        )

    # -- ProjectStore API --------------------------------------------------
    def create_project(self, project: ExperimentProject) -> ExperimentProject:
        with self._lock:
            conn = self._conn
            data = _serialize_project(project)
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO projects (project_id, row_version, schema_version, "
                    "title, workflow, detected_workflow, lifecycle_status, data, "
                    "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (
                        project.project_id,
                        1,
                        project.schema_version,
                        project.title,
                        project.workflow.value,
                        project.detected_workflow.value,
                        project.lifecycle_status.value,
                        data,
                        project.created_at.isoformat(),
                        project.updated_at.isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return self.get_project(project.project_id)

    def get_project(self, project_id: str) -> ExperimentProject:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFound(project_id)
            return self._row_to_project(row)

    def get_project_versioned(self, project_id: str) -> tuple[ExperimentProject, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data, row_version FROM projects WHERE project_id=?",
                (project_id,),
            ).fetchone()
            if row is None:
                raise ProjectNotFound(project_id)
            return self._row_to_project(row), int(row["row_version"])

    def try_get_project(self, project_id: str) -> ExperimentProject | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM projects WHERE project_id=?", (project_id,)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_project(row)

    def update_project(
        self, project: ExperimentProject, *, expected_row_version: int
    ) -> tuple[ExperimentProject, int]:
        with self._lock:
            conn = self._conn
            new_version = expected_row_version + 1
            # column and JSON must agree — stamp updated_at then re-serialize.
            project.updated_at = datetime.now(timezone.utc)
            data = _serialize_project(project)
            try:
                conn.execute("BEGIN IMMEDIATE")
                cur = conn.execute(
                    "UPDATE projects SET row_version=?, schema_version=?, title=?, "
                    "workflow=?, detected_workflow=?, lifecycle_status=?, data=?, "
                    "updated_at=? WHERE project_id=? AND row_version=?",
                    (
                        new_version,
                        project.schema_version,
                        project.title,
                        project.workflow.value,
                        project.detected_workflow.value,
                        project.lifecycle_status.value,
                        data,
                        project.updated_at.isoformat(),
                        project.project_id,
                        expected_row_version,
                    ),
                )
                if cur.rowcount == 0:
                    row = conn.execute(
                        "SELECT row_version FROM projects WHERE project_id=?",
                        (project.project_id,),
                    ).fetchone()
                    conn.rollback()
                    actual = int(row["row_version"]) if row is not None else None
                    raise ConcurrencyError(
                        project.project_id, expected_row_version, actual
                    )
                conn.commit()
            except ConcurrencyError:
                raise
            except Exception:
                conn.rollback()
                raise
            return self._row_to_project_by_id(project.project_id), new_version

    def _row_to_project_by_id(self, project_id: str) -> ExperimentProject:
        row = self._conn.execute(
            "SELECT data FROM projects WHERE project_id=?", (project_id,)
        ).fetchone()
        if row is None:
            raise ProjectNotFound(project_id)
        return self._row_to_project(row)

    def list_projects(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        lifecycle_status: LifecycleStatus | str | None = None,
        order_by: str = "updated_at",
        descending: bool = True,
    ) -> list[ProjectSummary]:
        allowed = {"updated_at", "created_at", "title"}
        col = order_by if order_by in allowed else "updated_at"
        direction = "DESC" if descending else "ASC"
        sql = "SELECT data, row_version FROM projects"
        params: list[Any] = []
        if lifecycle_status is not None:
            status_val = (
                lifecycle_status.value
                if isinstance(lifecycle_status, LifecycleStatus)
                else str(lifecycle_status)
            )
            sql += " WHERE lifecycle_status=?"
            params.append(status_val)
        sql += f" ORDER BY {col} {direction} LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        with self._lock:
            conn = self._conn
            rows = conn.execute(sql, params).fetchall()
            return [self._row_to_summary(conn, r) for r in rows]

    def save_protocol_version(self, version: ProtocolVersion) -> ProtocolVersion:
        with self._lock:
            conn = self._conn
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT OR IGNORE INTO protocol_versions (version_id, project_id, "
                    "version_number, provenance, source_op, title, result, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        version.version_id,
                        version.project_id,
                        version.version_number,
                        version.provenance.value,
                        version.source_op,
                        version.title,
                        json.dumps(version.result, separators=(",", ":")),
                        version.created_at.isoformat(),
                    ),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return self.get_protocol_version(version.version_id)

    def get_protocol_version(self, version_id: str) -> ProtocolVersion:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM protocol_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if row is None:
                raise ProjectNotFound(version_id)
            return self._row_to_version(row)

    def list_protocol_versions(self, project_id: str) -> list[ProtocolVersion]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM protocol_versions WHERE project_id=? "
                "ORDER BY created_at ASC, version_number ASC",
                (project_id,),
            ).fetchall()
            return [self._row_to_version(r) for r in rows]

    @staticmethod
    def _row_to_version(row: sqlite3.Row) -> ProtocolVersion:
        result = json.loads(row["result"])
        validation_summary = result.get("validation_report") or {}
        return ProtocolVersion(
            version_id=row["version_id"],
            project_id=row["project_id"],
            version_number=int(row["version_number"]),
            provenance=row["provenance"],
            source_op=row["source_op"],
            title=row["title"],
            result=result,
            validation_summary=validation_summary,
            chosen_assay=result.get("chosen_assay"),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
