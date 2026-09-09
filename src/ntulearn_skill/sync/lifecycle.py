"""Conservative lifecycle transitions after an exact course inventory."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar

from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId, Coverage
from ntulearn_skill.core.models import to_storage_time
from ntulearn_skill.storage import Database, StorageError
from ntulearn_skill.sync.models import ScopeResult, SyncWarning
from ntulearn_skill.sync.state import ScopeKey

IdentifierT = TypeVar("IdentifierT", AttachmentId, ContentId)


class LifecycleWarning(StrEnum):
    INCOMPLETE_INVENTORY_PRESERVED = "incomplete_inventory_preserved"
    UNAVAILABLE_PARENT_PRESERVED = "unavailable_parent_preserved"
    REMOVAL_CANDIDATE = "removal_candidate"


@dataclass(frozen=True, slots=True)
class InventoryReconciliationResult:
    resources_marked_missing: int
    uncertainty_observations: int
    removal_candidates: int
    warning_codes: tuple[LifecycleWarning, ...]


@dataclass(frozen=True, slots=True)
class _ResourceRow:
    key: int
    remote_key: str
    availability: Availability
    last_observed_at: str
    parent_availability: Availability
    original_filename: str
    sanitized_metadata_json: str
    metadata_fingerprint: str
    candidate_modified_at: str | None
    candidate_revision: str | None


class ResourceInventoryReconciler:
    """Apply omission evidence without deleting or rewriting historical evidence."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def reconcile(
        self,
        run_key: int,
        course: CourseId,
        scope_result: ScopeResult,
        seen_resource_ids: Iterable[AttachmentId],
        *,
        scope: ScopeKey,
        seen_content_ids: Iterable[ContentId],
    ) -> InventoryReconciliationResult:
        if isinstance(run_key, bool) or not isinstance(run_key, int) or run_key <= 0:
            raise ValueError("run_key must be a positive integer")
        self._validate_scope(course, scope, scope_result)
        seen_resources = self._typed_ids(seen_resource_ids, AttachmentId, course.provider)
        seen_content = self._typed_ids(seen_content_ids, ContentId, course.provider)
        if len(seen_content) != scope_result.items_seen:
            raise ValueError("seen content does not match the scope item count")
        observed_at = to_storage_time(scope_result.observed_at)
        comparable = self._is_complete_comparable(scope, scope_result)

        try:
            with self.database.transaction() as connection:
                course_key = self._course_key(connection, course)
                self._validate_run(connection, run_key)
                self._validate_persisted_scope(
                    connection, run_key, course_key, scope_result, observed_at
                )
                seen_resource_keys = self._validate_resource_ownership(
                    connection, seen_resources, course_key
                )
                seen_content_keys = self._validate_content_ownership(
                    connection, seen_content, course_key
                )
                self._validate_seen_relationships(connection, seen_resource_keys, seen_content_keys)
                resources = self._resources(connection, course_key)
                resources_missing = 0
                uncertainty = 0
                candidates = 0
                unavailable_parent = False

                for resource in resources:
                    if (
                        resource.key in seen_resource_keys
                        or resource.availability is Availability.REMOVED_CONFIRMED
                    ):
                        continue
                    can_mark_missing = comparable and (
                        resource.parent_availability is not Availability.UNAVAILABLE
                    )
                    observation_status = "NOT_OBSERVED" if can_mark_missing else "UNKNOWN"
                    observation_availability = (
                        Availability.MISSING if can_mark_missing else resource.availability
                    )
                    inserted = self._append_resource_observation(
                        connection,
                        resource,
                        run_key,
                        observation_status,
                        observation_availability,
                        observed_at,
                    )
                    if not inserted:
                        continue
                    if not can_mark_missing:
                        uncertainty += 1
                        unavailable_parent = unavailable_parent or (
                            comparable and resource.parent_availability is Availability.UNAVAILABLE
                        )
                        continue
                    if resource.availability is Availability.MISSING and self._prior_omission(
                        connection, resource.key, run_key, observed_at
                    ):
                        candidates += 1
                    cursor = connection.execute(
                        """
                        UPDATE resource SET availability = 'MISSING'
                        WHERE resource_key = ?
                          AND availability != 'REMOVED_CONFIRMED'
                          AND availability != 'MISSING'
                          AND NOT EXISTS (
                              SELECT 1 FROM resource_observation authoritative
                              WHERE authoritative.resource_key = resource.resource_key
                                AND authoritative.observation_status IN (
                                    'OBSERVED', 'NOT_OBSERVED', 'UNAVAILABLE'
                                )
                                AND (
                                    authoritative.observed_at > ?
                                    OR (
                                        authoritative.observed_at = ?
                                        AND authoritative.sync_run_key > ?
                                    )
                                )
                          )
                        """,
                        (resource.key, observed_at, observed_at, run_key),
                    )
                    resources_missing += cursor.rowcount

                warnings: list[LifecycleWarning] = []
                if not comparable:
                    warnings.append(LifecycleWarning.INCOMPLETE_INVENTORY_PRESERVED)
                if unavailable_parent:
                    warnings.append(LifecycleWarning.UNAVAILABLE_PARENT_PRESERVED)
                if candidates:
                    warnings.append(LifecycleWarning.REMOVAL_CANDIDATE)
                return InventoryReconciliationResult(
                    resources_missing,
                    uncertainty,
                    candidates,
                    tuple(warnings),
                )
        except sqlite3.Error:
            raise StorageError("inventory lifecycle operation failed") from None

    @staticmethod
    def _validate_scope(course: CourseId, scope: ScopeKey, result: ScopeResult) -> None:
        if type(course) is not CourseId:
            raise TypeError("course must be a CourseId")
        if not isinstance(scope, ScopeKey) or not isinstance(result, ScopeResult):
            raise TypeError("inventory scope must use typed scope values")
        if (
            course.provider != scope.provider
            or scope.course_id != course
            or scope.data_kind != "content"
            or result.provider != course.provider
            or result.course_id != course
            or result.data_kind != "content"
        ):
            raise ValueError("inventory scope does not match the course")
        if (
            not isinstance(result.coverage, Coverage)
            or not isinstance(result.pagination_complete, bool)
            or isinstance(result.pages_seen, bool)
            or not isinstance(result.pages_seen, int)
            or result.pages_seen < 0
            or isinstance(result.items_seen, bool)
            or not isinstance(result.items_seen, int)
            or result.items_seen < 0
            or any(not isinstance(warning, SyncWarning) for warning in result.warnings)
        ):
            raise ValueError("inventory scope result is invalid")

    @staticmethod
    def _is_complete_comparable(scope: ScopeKey, result: ScopeResult) -> bool:
        return bool(
            scope.time_window is None
            and result.coverage is Coverage.COMPLETE
            and result.pagination_complete
            and result.pages_seen > 0
            and result.failure_category is None
            and not result.warnings
        )

    @staticmethod
    def _typed_ids(
        values: Iterable[IdentifierT], expected: type[IdentifierT], provider: str
    ) -> tuple[IdentifierT, ...]:
        copied = tuple(values)
        if len(copied) > 100_000:
            raise ValueError("inventory exceeds the configured entity bound")
        if any(type(value) is not expected or value.provider != provider for value in copied):
            raise ValueError("inventory identifiers use the wrong type or provider")
        if len(set(copied)) != len(copied):
            raise ValueError("inventory identifiers must be unique")
        return copied

    @staticmethod
    def _course_key(connection: sqlite3.Connection, course: CourseId) -> int:
        row = connection.execute(
            """
            SELECT course.course_key
            FROM course
            JOIN source_object USING (source_object_key)
            JOIN source_provider USING (provider_key)
            WHERE source_provider.name = ? AND source_object.object_kind = 'course'
              AND source_object.remote_key = ?
            """,
            (course.provider, course.value),
        ).fetchone()
        if row is None:
            raise ValueError("inventory course has not been observed")
        return int(row[0])

    @staticmethod
    def _validate_run(connection: sqlite3.Connection, run_key: int) -> None:
        row = connection.execute(
            "SELECT 1 FROM sync_run WHERE sync_run_key = ?", (run_key,)
        ).fetchone()
        if row is None:
            raise ValueError("inventory sync run does not exist")

    @staticmethod
    def _validate_persisted_scope(
        connection: sqlite3.Connection,
        run_key: int,
        course_key: int,
        result: ScopeResult,
        observed_at: str,
    ) -> None:
        rows = connection.execute(
            """
            SELECT provider.name AS provider, scope.*
            FROM sync_scope_result scope
            JOIN source_provider provider USING (provider_key)
            WHERE scope.sync_run_key = ? AND scope.data_kind = 'content'
            """,
            (run_key,),
        ).fetchall()
        if not rows:
            return
        if not any(
            str(row["provider"]) == result.provider
            and row["course_key"] is not None
            and int(row["course_key"]) == course_key
            and str(row["window_key"]) == ""
            and str(row["coverage"]) == result.coverage.value
            and bool(row["pagination_complete"]) == result.pagination_complete
            and int(row["pages_seen"]) == result.pages_seen
            and int(row["items_seen"]) == result.items_seen
            and row["failure_category"] == result.failure_category
            and str(row["observed_at"]) == observed_at
            for row in rows
        ):
            raise ValueError("inventory scope does not match the recorded sync run")

    @staticmethod
    def _validate_resource_ownership(
        connection: sqlite3.Connection,
        resources: tuple[AttachmentId, ...],
        course_key: int,
    ) -> set[int]:
        keys: set[int] = set()
        for remote_id in resources:
            row = connection.execute(
                """
                SELECT resource.resource_key, content_node.course_key
                FROM resource
                JOIN source_object object
                  ON object.source_object_key = resource.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                JOIN content_node ON content_node.content_key = resource.content_key
                WHERE provider.name = ? AND object.object_kind = 'attachment'
                  AND object.remote_key = ?
                """,
                (remote_id.provider, remote_id.value),
            ).fetchone()
            if row is None or int(row["course_key"]) != course_key:
                raise ValueError("seen resource does not belong to the inventory course")
            keys.add(int(row["resource_key"]))
        return keys

    @staticmethod
    def _validate_content_ownership(
        connection: sqlite3.Connection,
        content_ids: tuple[ContentId, ...],
        course_key: int,
    ) -> set[int]:
        keys: set[int] = set()
        for remote_id in content_ids:
            row = connection.execute(
                """
                SELECT content_node.content_key, content_node.course_key
                FROM content_node
                JOIN source_object object
                  ON object.source_object_key = content_node.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE provider.name = ? AND object.object_kind = 'content'
                  AND object.remote_key = ?
                """,
                (remote_id.provider, remote_id.value),
            ).fetchone()
            if row is None or int(row["course_key"]) != course_key:
                raise ValueError("seen content does not belong to the inventory course")
            keys.add(int(row["content_key"]))
        return keys

    @staticmethod
    def _validate_seen_relationships(
        connection: sqlite3.Connection,
        resource_keys: set[int],
        content_keys: set[int],
    ) -> None:
        if not resource_keys:
            return
        placeholders = ",".join("?" for _ in resource_keys)
        parents = {
            int(row[0])
            for row in connection.execute(
                f"SELECT content_key FROM resource WHERE resource_key IN ({placeholders})",
                tuple(resource_keys),
            )
        }
        if not parents.issubset(content_keys):
            raise ValueError("seen resources require their parent content in the inventory")

    @staticmethod
    def _resources(connection: sqlite3.Connection, course_key: int) -> tuple[_ResourceRow, ...]:
        rows = connection.execute(
            """
            SELECT resource.resource_key, object.remote_key, resource.availability,
                   resource.last_observed_at,
                   content_node.availability AS parent_availability,
                   observation.original_filename,
                   observation.sanitized_metadata_json,
                   observation.metadata_fingerprint,
                   observation.candidate_modified_at,
                   observation.candidate_revision
            FROM resource
            JOIN source_object object
              ON object.source_object_key = resource.source_object_key
            JOIN content_node ON content_node.content_key = resource.content_key
            JOIN resource_observation observation ON observation.observation_key = (
                SELECT latest.observation_key FROM resource_observation latest
                WHERE latest.resource_key = resource.resource_key
                ORDER BY latest.observed_at DESC, latest.sync_run_key DESC,
                         latest.observation_key DESC LIMIT 1
            )
            WHERE content_node.course_key = ?
            ORDER BY resource.resource_key
            """,
            (course_key,),
        ).fetchall()
        return tuple(
            _ResourceRow(
                int(row["resource_key"]),
                str(row["remote_key"]),
                Availability(str(row["availability"])),
                str(row["last_observed_at"]),
                Availability(str(row["parent_availability"])),
                str(row["original_filename"]),
                str(row["sanitized_metadata_json"]),
                str(row["metadata_fingerprint"]),
                None if row["candidate_modified_at"] is None else str(row["candidate_modified_at"]),
                None if row["candidate_revision"] is None else str(row["candidate_revision"]),
            )
            for row in rows
        )

    @staticmethod
    def _append_resource_observation(
        connection: sqlite3.Connection,
        resource: _ResourceRow,
        run_key: int,
        observation_status: str,
        availability: Availability,
        observed_at: str,
    ) -> bool:
        existing = connection.execute(
            """SELECT 1 FROM resource_observation
            WHERE resource_key = ? AND sync_run_key = ?
              AND observation_status IN ('NOT_OBSERVED', 'UNKNOWN') LIMIT 1""",
            (resource.key, run_key),
        ).fetchone()
        if existing is not None:
            return False
        connection.execute(
            """
            INSERT INTO resource_observation(
                resource_key, sync_run_key, observation_status, original_filename,
                sanitized_metadata_json, metadata_fingerprint, candidate_modified_at,
                candidate_revision, availability, observed_at, fetch_decision, version_key
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DEFERRED', NULL)
            """,
            (
                resource.key,
                run_key,
                observation_status,
                resource.original_filename,
                resource.sanitized_metadata_json,
                resource.metadata_fingerprint,
                resource.candidate_modified_at,
                resource.candidate_revision,
                availability.value,
                observed_at,
            ),
        )
        return True

    @staticmethod
    def _prior_omission(
        connection: sqlite3.Connection,
        resource_key: int,
        run_key: int,
        observed_at: str,
    ) -> bool:
        row = connection.execute(
            """
            SELECT 1 FROM resource_observation
            WHERE resource_key = ? AND observation_status = 'NOT_OBSERVED'
              AND availability = 'MISSING'
              AND (observed_at < ? OR (observed_at = ? AND sync_run_key < ?))
            LIMIT 1
            """,
            (resource_key, observed_at, observed_at, run_key),
        ).fetchone()
        return row is not None


__all__ = ["InventoryReconciliationResult", "LifecycleWarning", "ResourceInventoryReconciler"]
