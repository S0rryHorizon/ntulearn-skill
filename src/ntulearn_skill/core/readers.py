"""SQL readers and row mapping for core library queries."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping

from ntulearn_skill.client import TimeWindow
from ntulearn_skill.core.identifiers import AttachmentId, CourseId
from ntulearn_skill.core.models import (
    AssessmentSubtype,
    Availability,
    from_storage_time,
    to_storage_time,
)
from ntulearn_skill.core.pagination import PageLoad
from ntulearn_skill.core.queries import (
    AnnouncementFilter,
    AnnouncementView,
    AssessmentFilter,
    AssessmentView,
    CourseFilter,
    CourseSummary,
    LibraryStatus,
    MaterialChangeView,
    MaterialFilter,
    MaterialSummary,
)
from ntulearn_skill.core.results import ProvenanceView
from ntulearn_skill.storage import Database


def _classification_sql(column: str) -> str:
    return f"""COALESCE(
        (SELECT classification.{column}
         FROM material_classification_selection selected
         JOIN material_classification classification USING(classification_key)
         JOIN classification_run run USING(classification_run_key)
         WHERE selected.resource_key = resource.resource_key
           AND (run.version_key IS NULL OR run.version_key IS version.version_key)
         ORDER BY selected.selection_key DESC LIMIT 1),
        (SELECT classification.{column}
         FROM classification_run run
         JOIN material_classification classification USING(classification_run_key)
         WHERE run.resource_key = resource.resource_key
           AND (run.version_key IS NULL OR run.version_key IS version.version_key)
           AND run.status <> 'FAILED'
         ORDER BY run.created_at DESC, run.classification_run_key DESC,
                  classification.rank LIMIT 1)
    )"""


def load_courses(
    database: Database, filter: CourseFilter, offset: int = 0
) -> PageLoad[CourseSummary]:
    clauses: list[str] = []
    values: list[object] = []
    if filter.availability:
        clauses.append(f"course.availability IN ({','.join('?' for _ in filter.availability)})")
        values.extend(
            item.value for item in sorted(filter.availability, key=lambda item: item.value)
        )
    values.extend((filter.limit + 1, offset))
    where = "" if not clauses else "WHERE " + " AND ".join(clauses)
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT course.*, object.source_object_key AS provenance_key,
                    object.remote_key, provider.name AS provider
                FROM course JOIN source_object object USING(source_object_key)
                JOIN source_provider provider USING(provider_key)
                {where} ORDER BY course.code, course.title, course.course_key
                LIMIT ? OFFSET ?""",
            values,
        ).fetchall()
    finally:
        connection.close()
    truncated = len(rows) > filter.limit
    rows = rows[: filter.limit]
    items = tuple(
        CourseSummary(
            int(row["course_key"]),
            CourseId(str(row["provider"]), str(row["remote_key"])),
            str(row["code"]),
            str(row["title"]),
            None if row["term"] is None else str(row["term"]),
            Availability(str(row["availability"])),
            from_storage_time(str(row["first_observed_at"])),
            from_storage_time(str(row["last_observed_at"])),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "source_object", int(row["provenance_key"]), item.course_id.provider, item.course_id
        )
        for item, row in zip(items, rows, strict=True)
    )
    return PageLoad(items, provenance, truncated)


def load_materials(
    database: Database, course: CourseId, course_key: int, filter: MaterialFilter, offset: int = 0
) -> PageLoad[MaterialSummary]:
    clauses = ["content.course_key = ?"]
    values: list[object] = [course_key]
    if filter.availability:
        clauses.append(f"resource.availability IN ({','.join('?' for _ in filter.availability)})")
        values.extend(
            item.value for item in sorted(filter.availability, key=lambda item: item.value)
        )
    if filter.file_formats:
        clauses.append(f"version.file_format IN ({','.join('?' for _ in filter.file_formats)})")
        values.extend(sorted(filter.file_formats))
    version_join = (
        "JOIN resource_version version ON version.resource_key = resource.resource_key"
        if filter.include_historical_versions
        else "LEFT JOIN resource_version version ON "
        "version.version_key = resource.current_version_key"
    )
    values.extend((filter.limit + 1, offset))
    semantic_type_sql = _classification_sql("semantic_type")
    confidence_sql = _classification_sql("confidence")
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT resource.*, object.remote_key, provider.name AS provider,
                    version.version_key, version.version_number, version.file_format,
                    version.byte_size, version.downloaded_at,
                    ({semantic_type_sql}) AS semantic_type,
                    ({confidence_sql}) AS classification_confidence
                FROM resource JOIN content_node content USING(content_key)
                JOIN source_object object ON object.source_object_key = resource.source_object_key
                JOIN source_provider provider USING(provider_key)
                {version_join}
                WHERE {" AND ".join(clauses)}
                ORDER BY resource.display_title, resource.resource_key,
                         version.version_number LIMIT ? OFFSET ?""",
            values,
        ).fetchall()
    finally:
        connection.close()
    truncated = len(rows) > filter.limit
    rows = rows[: filter.limit]
    items = tuple(
        MaterialSummary(
            int(row["resource_key"]),
            AttachmentId(str(row["provider"]), str(row["remote_key"])),
            course,
            int(row["content_key"]),
            str(row["display_title"]),
            Availability(str(row["availability"])),
            None if row["version_key"] is None else int(row["version_key"]),
            None if row["version_number"] is None else int(row["version_number"]),
            None if row["file_format"] is None else str(row["file_format"]),
            None if row["byte_size"] is None else int(row["byte_size"]),
            None if row["downloaded_at"] is None else from_storage_time(str(row["downloaded_at"])),
            None if row["semantic_type"] is None else str(row["semantic_type"]),
            None
            if row["classification_confidence"] is None
            else float(row["classification_confidence"]),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "source_object",
            int(row["source_object_key"]),
            item.resource_id.provider,
            course,
            item.version_key,
        )
        for item, row in zip(items, rows, strict=True)
    )
    return PageLoad(items, provenance, truncated)


def load_library_status(
    database: Database, courses: tuple[tuple[CourseId, int], ...]
) -> tuple[tuple[LibraryStatus, ...], tuple[ProvenanceView, ...]]:
    if not courses:
        return (), ()
    keys = tuple(key for _course, key in courses)
    placeholders = ",".join("?" for _ in keys)
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT course.course_key, course.code, course.title,
                    course_object.source_object_key,
                    provider.name AS provider, course_object.remote_key,
                    COUNT(DISTINCT content.content_key) AS content_nodes,
                    COUNT(DISTINCT resource.resource_key) AS discovered_materials,
                    COUNT(DISTINCT CASE WHEN resource.current_version_key IS NOT NULL
                        THEN resource.resource_key END) AS downloaded_materials,
                    COUNT(DISTINCT CASE WHEN parsed.status IN ('COMPLETE', 'PARTIAL')
                        THEN resource.resource_key END) AS parsed_materials,
                    COUNT(DISTINCT CASE WHEN parsed.status = 'COMPLETE'
                                             AND parsed.coverage = 'COMPLETE'
                        THEN resource.resource_key END) AS completely_parsed_materials,
                    COUNT(DISTINCT CASE WHEN document.entity_kind = 'chunk'
                        THEN document.search_document_key END) AS indexed_chunks,
                    MAX(resource.last_observed_at) AS last_material_observed_at
                FROM course
                JOIN source_object course_object USING(source_object_key)
                JOIN source_provider provider USING(provider_key)
                LEFT JOIN content_node content ON content.course_key = course.course_key
                LEFT JOIN resource ON resource.content_key = content.content_key
                LEFT JOIN parsed_document parsed ON parsed.parse_key = (
                    SELECT candidate.parse_key FROM parsed_document candidate
                    WHERE candidate.version_key = resource.current_version_key
                    ORDER BY candidate.parsed_at DESC, candidate.parse_key DESC LIMIT 1
                )
                LEFT JOIN search_document document
                  ON document.resource_key = resource.resource_key
                 AND document.version_key = resource.current_version_key
                WHERE course.course_key IN ({placeholders})
                GROUP BY course.course_key
                ORDER BY course.code, course.title, course.course_key""",
            keys,
        ).fetchall()
        version_courses = {
            int(row["version_key"]): int(row["course_key"])
            for row in connection.execute(
                f"""SELECT version.version_key, content.course_key
                    FROM resource_version version
                    JOIN resource USING(resource_key)
                    JOIN content_node content USING(content_key)
                    WHERE content.course_key IN ({placeholders})""",
                keys,
            )
        }
        observation_courses = {
            int(row["observation_key"]): int(row["course_key"])
            for row in connection.execute(
                f"""SELECT observation.observation_key, owner.course_key
                    FROM source_observation observation
                    JOIN (
                        SELECT source_object_key, course_key FROM announcement
                        UNION ALL SELECT source_object_key, course_key FROM assessment
                        UNION ALL SELECT source_object_key, course_key FROM schedule_item
                        UNION ALL SELECT source_object_key, course_key FROM due_item
                    ) owner ON owner.source_object_key = observation.source_object_key
                    WHERE owner.course_key IN ({placeholders})""",
                keys,
            )
        }
        job_counts: dict[int, dict[str, int]] = {
            key: {status: 0 for status in ("TOTAL", "SUCCEEDED", "PENDING", "RUNNING", "FAILED")}
            for key in keys
        }
        for job in connection.execute("SELECT status, payload_json FROM local_job"):
            payload = json.loads(str(job["payload_json"]))
            owner: object = payload.get("course_key")
            if owner is None and "version_key" in payload:
                owner = version_courses.get(payload["version_key"])
            if owner is None and "observation_key" in payload:
                owner = observation_courses.get(payload["observation_key"])
            if isinstance(owner, bool) or not isinstance(owner, int) or owner not in job_counts:
                continue
            status = str(job["status"])
            job_counts[owner]["TOTAL"] += 1
            job_counts[owner][status] += 1
    finally:
        connection.close()
    items = tuple(
        LibraryStatus(
            int(row["course_key"]),
            CourseId(str(row["provider"]), str(row["remote_key"])),
            str(row["code"]),
            str(row["title"]),
            int(row["content_nodes"]),
            int(row["discovered_materials"]),
            int(row["downloaded_materials"]),
            int(row["parsed_materials"]),
            int(row["completely_parsed_materials"]),
            int(row["indexed_chunks"]),
            job_counts[int(row["course_key"])]["TOTAL"],
            job_counts[int(row["course_key"])]["SUCCEEDED"],
            job_counts[int(row["course_key"])]["PENDING"],
            job_counts[int(row["course_key"])]["RUNNING"],
            job_counts[int(row["course_key"])]["FAILED"],
            None
            if row["last_material_observed_at"] is None
            else from_storage_time(str(row["last_material_observed_at"])),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "source_object",
            int(row["source_object_key"]),
            item.course.provider,
            item.course,
        )
        for item, row in zip(items, rows, strict=True)
    )
    return items, provenance


def load_recent_material_changes(
    database: Database,
    course: CourseId,
    course_key: int,
    window: TimeWindow,
    limit: int,
    offset: int = 0,
) -> PageLoad[MaterialChangeView]:
    connection = database.connect()
    try:
        rows = connection.execute(
            """WITH history AS (
                    SELECT observation.*,
                        LAG(observation.observation_key) OVER ordering AS previous_observation_key,
                        LAG(observation.metadata_fingerprint) OVER ordering
                            AS previous_metadata_fingerprint,
                        LAG(observation.observation_status) OVER ordering
                            AS previous_observation_status,
                        LAG(observation.availability) OVER ordering AS previous_availability,
                        (
                            SELECT previous.version_key
                            FROM resource_observation previous
                            WHERE previous.resource_key = observation.resource_key
                              AND previous.version_key IS NOT NULL
                              AND (
                                previous.observed_at < observation.observed_at
                                OR (
                                  previous.observed_at = observation.observed_at
                                  AND previous.sync_run_key < observation.sync_run_key
                                )
                                OR (
                                  previous.observed_at = observation.observed_at
                                  AND previous.sync_run_key = observation.sync_run_key
                                  AND previous.observation_key < observation.observation_key
                                )
                              )
                            ORDER BY previous.observed_at DESC, previous.sync_run_key DESC,
                                     previous.observation_key DESC
                            LIMIT 1
                        ) AS previous_version_key
                    FROM resource_observation observation
                    WHERE observation.observation_status <> 'UNKNOWN'
                    WINDOW ordering AS (
                        PARTITION BY observation.resource_key
                        ORDER BY observation.observed_at, observation.sync_run_key,
                                 observation.observation_key
                    )
                )
                SELECT history.*, resource.display_title, resource.source_object_key
                FROM history
                JOIN resource USING(resource_key)
                JOIN content_node content USING(content_key)
                WHERE content.course_key = ?
                  AND history.observed_at >= ? AND history.observed_at < ?
                  AND (
                    history.previous_observation_key IS NULL
                    OR history.metadata_fingerprint <> history.previous_metadata_fingerprint
                    OR history.observation_status <> history.previous_observation_status
                    OR history.availability <> history.previous_availability
                    OR (
                        history.version_key IS NOT NULL
                        AND history.version_key IS NOT history.previous_version_key
                    )
                  )
                ORDER BY history.observed_at DESC, history.observation_key DESC
                LIMIT ? OFFSET ?""",
            (
                course_key,
                to_storage_time(window.since),
                to_storage_time(window.until),
                limit + 1,
                offset,
            ),
        ).fetchall()
    finally:
        connection.close()

    def kinds(row: sqlite3.Row) -> tuple[str, ...]:
        if row["previous_observation_key"] is None:
            return (
                "FIRST_OBSERVED"
                if str(row["observation_status"]) == "OBSERVED"
                else "FIRST_RECORDED",
            )
        values: list[str] = []
        if row["version_key"] is not None and row["version_key"] != row["previous_version_key"]:
            values.append(
                "BINARY_AVAILABLE" if row["previous_version_key"] is None else "BINARY_CHANGED"
            )
        if row["metadata_fingerprint"] != row["previous_metadata_fingerprint"]:
            values.append("METADATA_CHANGED")
        if (
            row["observation_status"] != row["previous_observation_status"]
            or row["availability"] != row["previous_availability"]
        ):
            values.append("AVAILABILITY_CHANGED")
        return tuple(values)

    truncated = len(rows) > limit
    rows = rows[:limit]
    items = tuple(
        MaterialChangeView(
            int(row["resource_key"]),
            int(row["observation_key"]),
            course,
            str(row["display_title"]),
            from_storage_time(str(row["observed_at"])),
            kinds(row),
            None if row["version_key"] is None else int(row["version_key"]),
            None if row["previous_version_key"] is None else int(row["previous_version_key"]),
            None if row["candidate_modified_at"] is None else str(row["candidate_modified_at"]),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "resource_observation",
            item.observation_key,
            course.provider,
            course,
            item.version_key,
        )
        for item in items
    )
    return PageLoad(items, provenance, truncated)


def load_announcements(
    database: Database,
    course: CourseId,
    course_key: int,
    filter: AnnouncementFilter,
    offset: int = 0,
) -> PageLoad[AnnouncementView]:
    clauses = ["announcement.course_key = ?"]
    values: list[object] = [course_key]
    if filter.availability:
        clauses.append(
            f"announcement.availability IN ({','.join('?' for _ in filter.availability)})"
        )
        values.extend(
            item.value for item in sorted(filter.availability, key=lambda item: item.value)
        )
    values.extend((filter.limit + 1, offset))
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT announcement.*, object.remote_key FROM announcement
                JOIN source_object object USING(source_object_key)
                WHERE {" AND ".join(clauses)}
                ORDER BY announcement.announcement_key DESC LIMIT ? OFFSET ?""",
            values,
        ).fetchall()
    finally:
        connection.close()
    truncated = len(rows) > filter.limit
    rows = rows[: filter.limit]
    items = tuple(
        AnnouncementView(
            int(row["announcement_key"]),
            str(row["remote_key"]),
            course,
            str(row["title"]),
            str(row["body"]),
            Availability(str(row["availability"])),
            _json(row["created_time_json"]),
            _json(row["modified_time_json"]),
            _json(row["published_time_json"]),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "source_observation", int(row["current_observation_key"]), course.provider, course
        )
        for row in rows
    )
    return PageLoad(items, provenance, truncated)


def load_assessments(
    database: Database, course: CourseId, course_key: int, filter: AssessmentFilter, offset: int = 0
) -> PageLoad[AssessmentView]:
    clauses = ["assessment.course_key = ?"]
    values: list[object] = [course_key]
    if filter.subtypes:
        clauses.append(f"assessment.subtype IN ({','.join('?' for _ in filter.subtypes)})")
        values.extend(item.value for item in sorted(filter.subtypes, key=lambda item: item.value))
    if filter.availability:
        clauses.append(f"assessment.availability IN ({','.join('?' for _ in filter.availability)})")
        values.extend(
            item.value for item in sorted(filter.availability, key=lambda item: item.value)
        )
    values.extend((filter.limit + 1, offset))
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT assessment.*, object.remote_key FROM assessment
                JOIN source_object object USING(source_object_key)
                WHERE {" AND ".join(clauses)} ORDER BY assessment.assessment_key DESC
                LIMIT ? OFFSET ?""",
            values,
        ).fetchall()
    finally:
        connection.close()
    truncated = len(rows) > filter.limit
    rows = rows[: filter.limit]
    items = tuple(
        AssessmentView(
            int(row["assessment_key"]),
            str(row["remote_key"]),
            course,
            int(row["content_key"]),
            str(row["title"]),
            AssessmentSubtype(str(row["subtype"])),
            str(row["instructions"]),
            Availability(str(row["availability"])),
            _json(row["due_time_json"]),
            _json(row["grading_due_time_json"]),
            _json(row["generic_due_time_json"]),
            _json(row["open_time_json"]),
            _json(row["close_time_json"]),
        )
        for row in rows
    )
    provenance = tuple(
        ProvenanceView(
            "source_observation", int(row["current_observation_key"]), course.provider, course
        )
        for row in rows
    )
    return PageLoad(items, provenance, truncated)


def _json(value: object) -> Mapping[str, object] | None:
    if value is None:
        return None
    parsed = json.loads(str(value))
    return parsed if isinstance(parsed, dict) else None
