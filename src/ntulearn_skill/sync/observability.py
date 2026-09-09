"""Persistence for the existing sync_run and sync_scope_result schema."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from datetime import datetime

from ntulearn_skill.core import CourseId, Coverage, SyncRunStatus
from ntulearn_skill.core.models import from_storage_time, to_storage_time, utc_now
from ntulearn_skill.storage import Database, StorageError
from ntulearn_skill.sync.models import ScopeResult, SyncRunResult, SyncWarning


class SyncRunRecorder:
    """Write privacy-safe run counts, categories, and coverage to the M1 schema."""

    def __init__(self, database: Database, provider_name: str) -> None:
        if not provider_name.strip():
            raise ValueError("provider_name must be non-empty")
        self.database = database
        self.provider_name = provider_name

    def start(self, *, mode: str, requested_scope: Mapping[str, bool | int]) -> int:
        if not mode.strip() or any(
            not isinstance(key, str) or not isinstance(value, (bool, int))
            for key, value in requested_scope.items()
        ):
            raise ValueError("sync request must contain only safe scalar controls")
        started_at = utc_now()
        payload = json.dumps(dict(requested_scope), sort_keys=True, separators=(",", ":"))
        try:
            with self.database.transaction() as connection:
                self._provider_key(connection, started_at)
                cursor = connection.execute(
                    """
                    INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
                    VALUES (?, ?, ?, 'RUNNING')
                    """,
                    (mode, payload, to_storage_time(started_at)),
                )
                assert cursor.lastrowid is not None
                return int(cursor.lastrowid)
        except sqlite3.Error:
            raise StorageError("sync run operation failed") from None

    def record_scope(self, run_key: int, scope: ScopeResult) -> None:
        if run_key <= 0 or scope.provider != self.provider_name:
            raise ValueError("sync scope does not belong to this run recorder")
        warning_json = json.dumps(
            [warning.value for warning in scope.warnings], separators=(",", ":")
        )
        try:
            with self.database.transaction() as connection:
                provider_key = self._provider_key(connection, scope.observed_at)
                course_key = self._course_key(connection, scope.course_id)
                connection.execute(
                    """
                    INSERT INTO sync_scope_result(
                        sync_run_key, provider_key, course_key, data_kind, coverage,
                        pages_seen, items_seen, pagination_complete, failure_category,
                        warnings_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_key,
                        provider_key,
                        course_key,
                        scope.data_kind,
                        scope.coverage.value,
                        scope.pages_seen,
                        scope.items_seen,
                        int(scope.pagination_complete),
                        scope.failure_category,
                        warning_json,
                        to_storage_time(scope.observed_at),
                    ),
                )
        except sqlite3.Error:
            raise StorageError("sync scope operation failed") from None

    def finish(
        self,
        run_key: int,
        *,
        status: SyncRunStatus,
        counts: Mapping[str, int],
        warnings: tuple[SyncWarning, ...],
        error_category: str | None = None,
    ) -> SyncRunResult:
        if run_key <= 0 or status is SyncRunStatus.RUNNING:
            raise ValueError("sync run cannot finish with this status")
        if any(
            not isinstance(key, str)
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in counts.items()
        ):
            raise ValueError("sync counts must be non-negative integers")
        ended_at = utc_now()
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE sync_run
                    SET ended_at = ?, status = ?, counts_json = ?, warnings_json = ?,
                        error_category = ?
                    WHERE sync_run_key = ? AND status = 'RUNNING'
                    """,
                    (
                        to_storage_time(ended_at),
                        status.value,
                        json.dumps(dict(counts), sort_keys=True, separators=(",", ":")),
                        json.dumps([item.value for item in warnings], separators=(",", ":")),
                        error_category,
                        run_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise StorageError("sync run operation failed")
            return self.get(run_key)
        except sqlite3.Error:
            raise StorageError("sync run operation failed") from None

    def get(self, run_key: int) -> SyncRunResult:
        connection = self.database.connect()
        try:
            run = connection.execute(
                "SELECT * FROM sync_run WHERE sync_run_key = ?", (run_key,)
            ).fetchone()
            if run is None or run["ended_at"] is None:
                raise StorageError("completed sync run was not found")
            rows = connection.execute(
                """
                SELECT sr.*, p.name AS provider, so.remote_key AS course_remote_key
                FROM sync_scope_result sr
                JOIN source_provider p ON p.provider_key = sr.provider_key
                LEFT JOIN course c ON c.course_key = sr.course_key
                LEFT JOIN source_object so ON so.source_object_key = c.source_object_key
                WHERE sr.sync_run_key = ? ORDER BY sr.scope_result_key
                """,
                (run_key,),
            ).fetchall()
            scopes = tuple(self._scope_from_row(row) for row in rows)
            counts = json.loads(str(run["counts_json"]))
            if not isinstance(counts, dict):
                raise StorageError("sync run operation failed")
            return SyncRunResult(
                key=run_key,
                mode=str(run["mode"]),
                started_at=from_storage_time(str(run["started_at"])),
                ended_at=from_storage_time(str(run["ended_at"])),
                status=SyncRunStatus(str(run["status"])),
                counts={str(key): int(value) for key, value in counts.items()},
                warnings=tuple(
                    SyncWarning(value) for value in json.loads(str(run["warnings_json"]))
                ),
                error_category=None
                if run["error_category"] is None
                else str(run["error_category"]),
                scopes=scopes,
            )
        except (json.JSONDecodeError, sqlite3.Error, ValueError, TypeError):
            raise StorageError("sync run operation failed") from None
        finally:
            connection.close()

    def _provider_key(self, connection: sqlite3.Connection, observed_at: datetime) -> int:
        timestamp = to_storage_time(observed_at)
        connection.execute(
            """
            INSERT INTO source_provider(name, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                created_at = MIN(created_at, excluded.created_at),
                updated_at = MAX(updated_at, excluded.updated_at)
            """,
            (self.provider_name, timestamp, timestamp),
        )
        row = connection.execute(
            "SELECT provider_key FROM source_provider WHERE name = ?", (self.provider_name,)
        ).fetchone()
        assert row is not None
        return int(row[0])

    def _course_key(self, connection: sqlite3.Connection, course_id: CourseId | None) -> int | None:
        if course_id is None:
            return None
        if course_id.provider != self.provider_name:
            raise ValueError("course provider does not match the sync provider")
        row = connection.execute(
            """
            SELECT c.course_key
            FROM course c
            JOIN source_object so ON so.source_object_key = c.source_object_key
            JOIN source_provider p ON p.provider_key = so.provider_key
            WHERE p.name = ? AND so.object_kind = 'course' AND so.remote_key = ?
            """,
            (course_id.provider, course_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("sync scope course has not been observed")
        return int(row[0])

    @staticmethod
    def _scope_from_row(row: sqlite3.Row) -> ScopeResult:
        provider = str(row["provider"])
        course_id = (
            None
            if row["course_remote_key"] is None
            else CourseId(provider, str(row["course_remote_key"]))
        )
        return ScopeResult(
            provider=provider,
            course_id=course_id,
            data_kind=str(row["data_kind"]),
            coverage=Coverage(str(row["coverage"])),
            pages_seen=int(row["pages_seen"]),
            items_seen=int(row["items_seen"]),
            pagination_complete=bool(row["pagination_complete"]),
            failure_category=None
            if row["failure_category"] is None
            else str(row["failure_category"]),
            warnings=tuple(SyncWarning(value) for value in json.loads(str(row["warnings_json"]))),
            observed_at=from_storage_time(str(row["observed_at"])),
        )
