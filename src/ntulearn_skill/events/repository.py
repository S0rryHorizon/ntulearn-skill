"""Immutable structured-source observations for M6 event extraction."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from ntulearn_skill.client.contracts import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    DueSourceRecord,
    ScheduleSourceRecord,
)
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    CalendarItemId,
    ContentId,
    CourseId,
    GradingColumnId,
    RemoteId,
    SourceTime,
    sanitize_source_text,
)
from ntulearn_skill.core.identifiers import require_identifier
from ntulearn_skill.core.models import from_storage_time, to_storage_time, utc_now
from ntulearn_skill.events.models import (
    CandidateSourceKind,
    JsonValue,
    ObservedSourceRecord,
    SourceObservationRecord,
)
from ntulearn_skill.storage import Database, DomainRepository, StorageError


class EventStorageError(StorageError):
    """Privacy-safe event source persistence failure."""


def _json(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _source_time(value: SourceTime | None) -> dict[str, JsonValue] | None:
    if value is None:
        return None
    return {
        "instant": None if value.instant is None else to_storage_time(value.instant),
        "precision": value.precision.value,
        "source_text": value.source_text,
        "source_timezone": value.source_timezone,
    }


def _time_json(value: SourceTime | None) -> str | None:
    payload = _source_time(value)
    return None if payload is None else _json(payload)


@dataclass(frozen=True, slots=True)
class _ObservationInput:
    remote_id: RemoteId
    course_id: CourseId
    kind: CandidateSourceKind
    snapshot: dict[str, JsonValue]
    raw_wording: str


class EventRepository:
    """Store source-specific current rows without erasing immutable observations."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def observe_announcement(
        self,
        record: AnnouncementSourceRecord,
        *,
        sync_run_key: int,
        observed_at: datetime | None = None,
    ) -> ObservedSourceRecord:
        require_identifier(record.remote_id, AnnouncementId)
        title = sanitize_source_text(record.title, maximum_characters=4096)
        body = sanitize_source_text(record.body)
        self._validate_common(record.course_id, title, body)
        snapshot: dict[str, JsonValue] = {
            "availability": record.availability.value,
            "available_from": _source_time(record.available_from),
            "available_until": _source_time(record.available_until),
            "body": body,
            "created_at": _source_time(record.created_at),
            "modified_at": _source_time(record.modified_at),
            "published_at": _source_time(record.published_at),
            "title": title,
        }
        source = _ObservationInput(
            record.remote_id,
            record.course_id,
            CandidateSourceKind.ANNOUNCEMENT,
            snapshot,
            body,
        )
        columns = {
            "title": title,
            "body": body,
            "availability": record.availability.value,
            "created_time_json": _time_json(record.created_at),
            "modified_time_json": _time_json(record.modified_at),
            "published_time_json": _time_json(record.published_at),
            "available_from_json": _time_json(record.available_from),
            "available_until_json": _time_json(record.available_until),
        }
        return self._observe(source, sync_run_key, observed_at, "announcement", columns)

    def observe_assessment(
        self,
        record: AssessmentSourceRecord,
        *,
        sync_run_key: int,
        observed_at: datetime | None = None,
    ) -> ObservedSourceRecord:
        require_identifier(record.remote_id, AssessmentId)
        require_identifier(record.content_id, ContentId)
        if record.grading_column_id is not None:
            require_identifier(record.grading_column_id, GradingColumnId)
        title = sanitize_source_text(record.title, maximum_characters=4096)
        instructions = sanitize_source_text(record.instructions)
        self._validate_common(record.course_id, title, instructions)
        if not isinstance(record.subtype, AssessmentSubtype):
            raise TypeError("assessment subtype must be typed")
        snapshot: dict[str, JsonValue] = {
            "availability": record.availability.value,
            "available_from": _source_time(record.available_from),
            "available_until": _source_time(record.available_until),
            "close_at": _source_time(record.close_at),
            "created_at": _source_time(record.created_at),
            "due_at": _source_time(record.due_at),
            "generic_due_at": _source_time(record.generic_due_at),
            "grading_due_at": _source_time(record.grading_due_at),
            "instructions": instructions,
            "modified_at": _source_time(record.modified_at),
            "open_at": _source_time(record.open_at),
            "subtype": record.subtype.value,
            "title": title,
        }
        source = _ObservationInput(
            record.remote_id,
            record.course_id,
            CandidateSourceKind.ASSESSMENT,
            snapshot,
            instructions,
        )
        columns: dict[str, object] = {
            "title": title,
            "subtype": record.subtype.value,
            "instructions": instructions,
            "availability": record.availability.value,
            "created_time_json": _time_json(record.created_at),
            "modified_time_json": _time_json(record.modified_at),
            "available_from_json": _time_json(record.available_from),
            "available_until_json": _time_json(record.available_until),
            "open_time_json": _time_json(record.open_at),
            "close_time_json": _time_json(record.close_at),
            "due_time_json": _time_json(record.due_at),
            "grading_due_time_json": _time_json(record.grading_due_at),
            "generic_due_time_json": _time_json(record.generic_due_at),
            "content_id": record.content_id,
            "grading_column_id": record.grading_column_id,
        }
        return self._observe(source, sync_run_key, observed_at, "assessment", columns)

    def observe_schedule(
        self,
        record: ScheduleSourceRecord,
        *,
        sync_run_key: int,
        observed_at: datetime | None = None,
    ) -> ObservedSourceRecord:
        require_identifier(record.remote_id, CalendarItemId)
        title = sanitize_source_text(record.title, maximum_characters=4096)
        location = (
            None
            if record.location is None
            else sanitize_source_text(record.location, maximum_characters=4096)
        )
        self._validate_common(record.course_id, title, location or "")
        snapshot: dict[str, JsonValue] = {
            "availability": record.availability.value,
            "end_at": _source_time(record.end_at),
            "location": location,
            "start_at": _source_time(record.start_at),
            "title": title,
        }
        source = _ObservationInput(
            record.remote_id,
            record.course_id,
            CandidateSourceKind.SCHEDULE,
            snapshot,
            title,
        )
        columns = {
            "title": title,
            "availability": record.availability.value,
            "start_time_json": _time_json(record.start_at),
            "end_time_json": _time_json(record.end_at),
            "location": location,
        }
        return self._observe(source, sync_run_key, observed_at, "schedule_item", columns)

    def observe_due_item(
        self,
        record: DueSourceRecord,
        *,
        sync_run_key: int,
        observed_at: datetime | None = None,
    ) -> ObservedSourceRecord:
        require_identifier(record.remote_id, CalendarItemId)
        title = sanitize_source_text(record.title, maximum_characters=4096)
        self._validate_common(record.course_id, title, "")
        snapshot: dict[str, JsonValue] = {
            "availability": record.availability.value,
            "calendar_id": record.calendar_id,
            "due_at": _source_time(record.due_at),
            "item_source_id": record.item_source_id,
            "item_source_type": record.item_source_type,
            "source_end_at": _source_time(record.source_end_at),
            "source_start_at": _source_time(record.source_start_at),
            "title": title,
        }
        source = _ObservationInput(
            record.remote_id,
            record.course_id,
            CandidateSourceKind.DUE_ITEM,
            snapshot,
            title,
        )
        columns = {
            "title": title,
            "calendar_id": record.calendar_id,
            "item_source_id": record.item_source_id,
            "item_source_type": record.item_source_type,
            "availability": record.availability.value,
            "source_start_time_json": _time_json(record.source_start_at),
            "source_end_time_json": _time_json(record.source_end_at),
            "due_time_json": _time_json(record.due_at),
        }
        return self._observe(source, sync_run_key, observed_at, "due_item", columns)

    def get_observation(self, observation_key: int) -> SourceObservationRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM source_observation WHERE observation_key = ?",
                (observation_key,),
            ).fetchone()
            return None if row is None else self._observation(row)
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise EventStorageError("event source observation lookup failed") from None
        finally:
            connection.close()

    def _observe(
        self,
        source: _ObservationInput,
        sync_run_key: int,
        observed_at: datetime | None,
        table: str,
        columns: Mapping[str, object],
    ) -> ObservedSourceRecord:
        if sync_run_key <= 0 or table not in {
            "announcement",
            "assessment",
            "schedule_item",
            "due_item",
        }:
            raise ValueError("event observation target is invalid")
        timestamp = to_storage_time(observed_at or utc_now())
        snapshot_json = _json(source.snapshot)
        observation_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if len(source.raw_wording) > 256 * 1024:
            raise ValueError("source wording exceeds the configured bound")
        try:
            with self.database.transaction() as connection:
                run = connection.execute(
                    "SELECT 1 FROM sync_run WHERE sync_run_key = ?", (sync_run_key,)
                ).fetchone()
                if run is None:
                    raise ValueError("sync run does not exist")
                course_key = DomainRepository._lookup_domain_key(
                    connection, source.course_id, "course", "course"
                )
                source_key = DomainRepository._source_object_key(
                    connection,
                    source.remote_id,
                    "calendar_item"
                    if source.kind in {CandidateSourceKind.SCHEDULE, CandidateSourceKind.DUE_ITEM}
                    else source.kind.value,
                    timestamp,
                )
                self._validate_course_ownership(connection, source_key, course_key)
                if source.kind is CandidateSourceKind.ASSESSMENT:
                    content_id = columns.get("content_id")
                    assert isinstance(content_id, ContentId)
                    self._validate_assessment_content(connection, content_id, course_key)
                connection.execute(
                    """
                    INSERT INTO source_observation(
                        source_object_key, sync_run_key, data_kind, observed_at,
                        observation_hash, snapshot_json, raw_wording
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(source_object_key, sync_run_key, observation_hash) DO NOTHING
                    """,
                    (
                        source_key,
                        sync_run_key,
                        source.kind.value,
                        timestamp,
                        observation_hash,
                        snapshot_json,
                        source.raw_wording,
                    ),
                )
                observation_row = connection.execute(
                    """SELECT * FROM source_observation
                    WHERE source_object_key = ? AND sync_run_key = ? AND observation_hash = ?""",
                    (source_key, sync_run_key, observation_hash),
                ).fetchone()
                assert observation_row is not None
                observation_key = int(observation_row["observation_key"])
                entity_key = self._upsert_current(
                    connection,
                    table,
                    source_key,
                    course_key,
                    observation_key,
                    timestamp,
                    columns,
                )
                result = ObservedSourceRecord(entity_key, self._observation(observation_row))
            return result
        except sqlite3.Error:
            raise EventStorageError("event source observation storage failed") from None

    @staticmethod
    def _upsert_current(
        connection: sqlite3.Connection,
        table: str,
        source_key: int,
        course_key: int,
        observation_key: int,
        observed_at: str,
        columns: Mapping[str, object],
    ) -> int:
        values = dict(columns)
        content_id = values.pop("content_id", None)
        grading_id = values.pop("grading_column_id", None)
        if table == "assessment":
            assert isinstance(content_id, ContentId)
            content_key = DomainRepository._lookup_domain_key(
                connection, content_id, "content", "content_node"
            )
            content_owner = connection.execute(
                "SELECT course_key FROM content_node WHERE content_key = ?", (content_key,)
            ).fetchone()
            assert content_owner is not None
            if int(content_owner["course_key"]) != course_key:
                raise ValueError("assessment content belongs to another course")
            values["content_key"] = content_key
            if grading_id is not None:
                assert isinstance(grading_id, GradingColumnId)
                values["grading_column_source_object_key"] = DomainRepository._source_object_key(
                    connection, grading_id, "grading_column", observed_at
                )
            else:
                values["grading_column_source_object_key"] = None
        row = connection.execute(
            f"SELECT {table}_key, course_key, current_observation_key "
            f"FROM {table} WHERE source_object_key = ?",
            (source_key,),
        ).fetchone()
        if row is not None:
            if int(row["course_key"]) != course_key:
                raise ValueError("event source belongs to another course")
            current_time = connection.execute(
                "SELECT observed_at FROM source_observation WHERE observation_key = ?",
                (int(row["current_observation_key"]),),
            ).fetchone()
            assert current_time is not None
            if observed_at < str(current_time["observed_at"]):
                return int(row[f"{table}_key"])
        fixed = {
            "source_object_key": source_key,
            "course_key": course_key,
            "current_observation_key": observation_key,
            **values,
        }
        names = tuple(fixed)
        updates = ", ".join(
            f"{name} = excluded.{name}"
            for name in names
            if name not in {"source_object_key", "course_key"}
        )
        connection.execute(
            f"""INSERT INTO {table}({", ".join(names)})
            VALUES ({", ".join("?" for _ in names)})
            ON CONFLICT(source_object_key) DO UPDATE SET {updates}""",
            tuple(fixed[name] for name in names),
        )
        result = connection.execute(
            f"SELECT {table}_key FROM {table} WHERE source_object_key = ?", (source_key,)
        ).fetchone()
        assert result is not None
        return int(result[0])

    @staticmethod
    def _validate_course_ownership(
        connection: sqlite3.Connection, source_key: int, course_key: int
    ) -> None:
        owners = connection.execute(
            """
            SELECT course_key FROM announcement WHERE source_object_key = ?
            UNION ALL SELECT course_key FROM assessment WHERE source_object_key = ?
            UNION ALL SELECT course_key FROM schedule_item WHERE source_object_key = ?
            UNION ALL SELECT course_key FROM due_item WHERE source_object_key = ?
            """,
            (source_key, source_key, source_key, source_key),
        ).fetchall()
        if any(int(owner["course_key"]) != course_key for owner in owners):
            raise ValueError("event source belongs to another course")

    @staticmethod
    def _validate_assessment_content(
        connection: sqlite3.Connection, content_id: ContentId, course_key: int
    ) -> None:
        content_key = DomainRepository._lookup_domain_key(
            connection, content_id, "content", "content_node"
        )
        owner = connection.execute(
            "SELECT course_key FROM content_node WHERE content_key = ?", (content_key,)
        ).fetchone()
        assert owner is not None
        if int(owner["course_key"]) != course_key:
            raise ValueError("assessment content belongs to another course")

    @staticmethod
    def _validate_common(course: CourseId, title: str, body: str) -> None:
        require_identifier(course, CourseId)
        if not title.strip() or len(title) > 4096 or len(body) > 256 * 1024:
            raise ValueError("event source text exceeds the configured bound")

    @staticmethod
    def _observation(row: sqlite3.Row) -> SourceObservationRecord:
        snapshot = json.loads(str(row["snapshot_json"]))
        if not isinstance(snapshot, dict):
            raise ValueError("source observation snapshot is invalid")
        return SourceObservationRecord(
            key=int(row["observation_key"]),
            source_object_key=int(row["source_object_key"]),
            sync_run_key=int(row["sync_run_key"]),
            data_kind=CandidateSourceKind(str(row["data_kind"])),
            observed_at=from_storage_time(str(row["observed_at"])),
            observation_hash=str(row["observation_hash"]),
            snapshot=snapshot,
            raw_wording=str(row["raw_wording"]),
        )
