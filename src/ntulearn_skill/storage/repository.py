"""Explicit repositories for the M1 synthetic course/content slice."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from ntulearn_skill.core.identifiers import ContentId, CourseId, RemoteId, require_identifier
from ntulearn_skill.core.models import (
    Availability,
    from_storage_time,
    to_storage_time,
    utc_now,
)
from ntulearn_skill.core.text import sanitize_source_text
from ntulearn_skill.storage.database import Database, StorageError
from ntulearn_skill.storage.migration import MigrationRunner

_ALLOWED_CONTENT_METADATA_FIELDS = frozenset({"content_type", "display_style", "module_label"})
_JSON_SCALARS = (str, int, float, bool, type(None))


@dataclass(frozen=True, slots=True)
class CourseRecord:
    key: int
    remote_id: CourseId
    code: str
    title: str
    term: str | None
    availability: Availability
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class ContentNodeRecord:
    key: int
    remote_id: ContentId
    course_key: int
    parent_key: int | None
    handler_kind: str
    title: str
    position: int
    availability: Availability
    sanitized_metadata: dict[str, object]
    first_observed_at: datetime
    last_observed_at: datetime


def _safe_json(value: Mapping[str, object]) -> str:
    if not set(value).issubset(_ALLOWED_CONTENT_METADATA_FIELDS):
        raise ValueError("metadata contains a field outside the public allowlist")
    if any(not isinstance(item, _JSON_SCALARS) for item in value.values()):
        raise ValueError("metadata values must be JSON scalar values")
    sanitized = {
        key: sanitize_source_text(item) if isinstance(item, str) else item
        for key, item in value.items()
    }
    try:
        return json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("metadata must be JSON-serializable") from None


class DomainRepository:
    """Transactional access to courses and the general content tree."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> int:
        return MigrationRunner(self.database).migrate()

    @staticmethod
    def _provider_key(connection: sqlite3.Connection, provider: str, observed_at: str) -> int:
        connection.execute(
            """
            INSERT INTO source_provider(name, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO NOTHING
            """,
            (provider, observed_at, observed_at),
        )
        connection.execute(
            """
            UPDATE source_provider
            SET created_at = MIN(created_at, ?), updated_at = MAX(updated_at, ?)
            WHERE name = ?
            """,
            (observed_at, observed_at, provider),
        )
        row = connection.execute(
            "SELECT provider_key FROM source_provider WHERE name = ?", (provider,)
        ).fetchone()
        assert row is not None
        return int(row["provider_key"])

    @classmethod
    def _source_object_key(
        cls,
        connection: sqlite3.Connection,
        remote_id: RemoteId,
        object_kind: str,
        observed_at: str,
    ) -> int:
        provider_key = cls._provider_key(connection, remote_id.provider, observed_at)
        connection.execute(
            """
            INSERT INTO source_object(
                provider_key, object_kind, remote_key, first_observed_at, last_observed_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(provider_key, object_kind, remote_key)
            DO UPDATE SET
                first_observed_at = MIN(first_observed_at, excluded.first_observed_at),
                last_observed_at = MAX(last_observed_at, excluded.last_observed_at)
            """,
            (provider_key, object_kind, remote_id.value, observed_at, observed_at),
        )
        row = connection.execute(
            """
            SELECT source_object_key FROM source_object
            WHERE provider_key = ? AND object_kind = ? AND remote_key = ?
            """,
            (provider_key, object_kind, remote_id.value),
        ).fetchone()
        assert row is not None
        return int(row["source_object_key"])

    @staticmethod
    def _lookup_domain_key(
        connection: sqlite3.Connection,
        remote_id: RemoteId,
        object_kind: str,
        table: str,
    ) -> int:
        columns = {"course": "course_key", "content_node": "content_key"}
        if table not in columns:
            raise ValueError("unsupported domain table")
        key_column = columns[table]
        row = connection.execute(
            f"""
            SELECT domain.{key_column}
            FROM {table} domain
            JOIN source_object so ON so.source_object_key = domain.source_object_key
            JOIN source_provider p ON p.provider_key = so.provider_key
            WHERE p.name = ? AND so.object_kind = ? AND so.remote_key = ?
            """,
            (remote_id.provider, object_kind, remote_id.value),
        ).fetchone()
        if row is None:
            raise ValueError(f"referenced {object_kind} does not exist")
        return int(row[0])

    def put_course(
        self,
        remote_id: CourseId,
        *,
        code: str,
        title: str,
        term: str | None = None,
        availability: Availability = Availability.ACTIVE,
        observed_at: datetime | None = None,
    ) -> CourseRecord:
        require_identifier(remote_id, CourseId)
        code = sanitize_source_text(code)
        title = sanitize_source_text(title)
        term = None if term is None else sanitize_source_text(term) or None
        if not code or not title:
            raise ValueError("course code and title must be non-empty")
        timestamp = to_storage_time(observed_at or utc_now())
        try:
            with self.database.transaction() as connection:
                source_key = self._source_object_key(connection, remote_id, "course", timestamp)
                existing = connection.execute(
                    """
                    SELECT course_key, last_observed_at
                    FROM course WHERE source_object_key = ?
                    """,
                    (source_key,),
                ).fetchone()
                if existing is not None and timestamp < str(existing["last_observed_at"]):
                    connection.execute(
                        """
                        UPDATE course
                        SET first_observed_at = MIN(first_observed_at, ?)
                        WHERE course_key = ?
                        """,
                        (timestamp, int(existing["course_key"])),
                    )
                else:
                    connection.execute(
                        """
                        INSERT INTO course(
                            source_object_key, code, title, term, availability,
                            first_observed_at, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_object_key) DO UPDATE SET
                            code = excluded.code,
                            title = excluded.title,
                            term = excluded.term,
                            availability = excluded.availability,
                            first_observed_at = MIN(
                                first_observed_at, excluded.first_observed_at
                            ),
                            last_observed_at = MAX(last_observed_at, excluded.last_observed_at)
                        """,
                        (source_key, code, title, term, availability.value, timestamp, timestamp),
                    )
            record = self.get_course(remote_id)
            assert record is not None
            return record
        except sqlite3.Error:
            raise StorageError("course metadata operation failed") from None

    def get_course(self, remote_id: CourseId) -> CourseRecord | None:
        require_identifier(remote_id, CourseId)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT c.*, p.name AS provider, so.remote_key
                FROM course c
                JOIN source_object so ON so.source_object_key = c.source_object_key
                JOIN source_provider p ON p.provider_key = so.provider_key
                WHERE p.name = ? AND so.object_kind = 'course' AND so.remote_key = ?
                """,
                (remote_id.provider, remote_id.value),
            ).fetchone()
            if row is None:
                return None
            return CourseRecord(
                key=int(row["course_key"]),
                remote_id=CourseId(str(row["provider"]), str(row["remote_key"])),
                code=str(row["code"]),
                title=str(row["title"]),
                term=None if row["term"] is None else str(row["term"]),
                availability=Availability(row["availability"]),
                first_observed_at=from_storage_time(row["first_observed_at"]),
                last_observed_at=from_storage_time(row["last_observed_at"]),
            )
        except sqlite3.Error:
            raise StorageError("course metadata operation failed") from None
        finally:
            connection.close()

    def put_content_node(
        self,
        remote_id: ContentId,
        *,
        course_id: CourseId,
        handler_kind: str,
        title: str,
        position: int,
        parent_id: ContentId | None = None,
        availability: Availability = Availability.ACTIVE,
        sanitized_metadata: Mapping[str, object] | None = None,
        observed_at: datetime | None = None,
    ) -> ContentNodeRecord:
        require_identifier(remote_id, ContentId)
        require_identifier(course_id, CourseId)
        if parent_id is not None:
            require_identifier(parent_id, ContentId)
        handler_kind = sanitize_source_text(handler_kind)
        title = sanitize_source_text(title)
        if position < 0 or not handler_kind or not title:
            raise ValueError("content fields are invalid")
        metadata_json = _safe_json(sanitized_metadata or {})
        timestamp = to_storage_time(observed_at or utc_now())
        try:
            with self.database.transaction() as connection:
                source_key = self._source_object_key(connection, remote_id, "content", timestamp)
                existing = connection.execute(
                    """
                    SELECT content_key, course_key, last_observed_at
                    FROM content_node WHERE source_object_key = ?
                    """,
                    (source_key,),
                ).fetchone()
                if existing is not None and timestamp < str(existing["last_observed_at"]):
                    connection.execute(
                        """
                        UPDATE content_node
                        SET first_observed_at = MIN(first_observed_at, ?)
                        WHERE content_key = ?
                        """,
                        (timestamp, int(existing["content_key"])),
                    )
                else:
                    course_key = self._lookup_domain_key(connection, course_id, "course", "course")
                    if existing is not None and int(existing["course_key"]) != course_key:
                        raise ValueError("a content node cannot move to another course")
                    parent_key = None
                    if parent_id is not None:
                        parent_key = self._lookup_domain_key(
                            connection, parent_id, "content", "content_node"
                        )
                        parent_course = connection.execute(
                            "SELECT course_key FROM content_node WHERE content_key = ?",
                            (parent_key,),
                        ).fetchone()
                        if parent_course is None or int(parent_course[0]) != course_key:
                            raise ValueError("content parent must belong to the same course")
                        if existing is not None and self._parent_reaches_node(
                            connection, parent_key, int(existing["content_key"])
                        ):
                            raise ValueError("content parent would create a cycle")
                    connection.execute(
                        """
                        INSERT INTO content_node(
                            source_object_key, course_key, parent_content_key, handler_kind, title,
                            position, availability, sanitized_metadata_json,
                            first_observed_at, last_observed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(source_object_key) DO UPDATE SET
                            parent_content_key = excluded.parent_content_key,
                            handler_kind = excluded.handler_kind,
                            title = excluded.title,
                            position = excluded.position,
                            availability = excluded.availability,
                            sanitized_metadata_json = excluded.sanitized_metadata_json,
                            first_observed_at = MIN(
                                first_observed_at, excluded.first_observed_at
                            ),
                            last_observed_at = MAX(
                                last_observed_at, excluded.last_observed_at
                            )
                        """,
                        (
                            source_key,
                            course_key,
                            parent_key,
                            handler_kind,
                            title,
                            position,
                            availability.value,
                            metadata_json,
                            timestamp,
                            timestamp,
                        ),
                    )
            record = self.get_content_node(remote_id)
            assert record is not None
            return record
        except sqlite3.Error:
            raise StorageError("content metadata operation failed") from None

    @staticmethod
    def _parent_reaches_node(
        connection: sqlite3.Connection, parent_key: int, node_key: int
    ) -> bool:
        row = connection.execute(
            """
            WITH RECURSIVE ancestors(content_key, parent_content_key) AS (
                SELECT content_key, parent_content_key
                FROM content_node
                WHERE content_key = ?
                UNION ALL
                SELECT parent.content_key, parent.parent_content_key
                FROM content_node parent
                JOIN ancestors ON parent.content_key = ancestors.parent_content_key
            )
            SELECT 1 FROM ancestors WHERE content_key = ? LIMIT 1
            """,
            (parent_key, node_key),
        ).fetchone()
        return row is not None

    def get_content_node(self, remote_id: ContentId) -> ContentNodeRecord | None:
        require_identifier(remote_id, ContentId)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT n.*, p.name AS provider, so.remote_key
                FROM content_node n
                JOIN source_object so ON so.source_object_key = n.source_object_key
                JOIN source_provider p ON p.provider_key = so.provider_key
                WHERE p.name = ? AND so.object_kind = 'content' AND so.remote_key = ?
                """,
                (remote_id.provider, remote_id.value),
            ).fetchone()
            if row is None:
                return None
            return ContentNodeRecord(
                key=int(row["content_key"]),
                remote_id=ContentId(str(row["provider"]), str(row["remote_key"])),
                course_key=int(row["course_key"]),
                parent_key=None
                if row["parent_content_key"] is None
                else int(row["parent_content_key"]),
                handler_kind=str(row["handler_kind"]),
                title=str(row["title"]),
                position=int(row["position"]),
                availability=Availability(row["availability"]),
                sanitized_metadata=json.loads(row["sanitized_metadata_json"]),
                first_observed_at=from_storage_time(row["first_observed_at"]),
                last_observed_at=from_storage_time(row["last_observed_at"]),
            )
        except sqlite3.Error:
            raise StorageError("content metadata operation failed") from None
        finally:
            connection.close()
