"""Event review, coverage, and temporal projection helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

from ntulearn_skill.client import TimeWindow
from ntulearn_skill.core.identifiers import CourseId
from ntulearn_skill.core.models import (
    Coverage,
    from_storage_time,
)
from ntulearn_skill.core.results import (
    ConflictView,
    CoverageView,
    ProvenanceView,
    ResultEnvelope,
    SafeWarning,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    CanonicalEvent,
    EventConfirmationBasis,
    EventConfirmationBasisKind,
    EventResolutionState,
    EventReview,
    EventType,
    EventWordingConflict,
)
from ntulearn_skill.events._activity import active_event_source_sql
from ntulearn_skill.storage import Database
from ntulearn_skill.sync.event_sources import EventSourceScope
from ntulearn_skill.sync.state import ScopeKey


def with_event_reviews(
    database: Database, events: tuple[CanonicalEvent, ...]
) -> tuple[CanonicalEvent, ...]:
    if not events:
        return ()
    keys = tuple(event.key for event in events)
    connection = database.connect()
    try:
        rows = connection.execute(
            f"""SELECT manual_resolution_key, event_key, field_name,
                           selected_claim_key, local_claim_key, reason
                    FROM manual_field_resolution
                    WHERE active = 1
                      AND event_key IN ({",".join("?" for _ in keys)})
                    ORDER BY manual_resolution_key""",
            keys,
        ).fetchall()
    finally:
        connection.close()
    manual = {
        (int(row["event_key"]), CandidateFieldName(str(row["field_name"]))): row for row in rows
    }
    reviewed: list[CanonicalEvent] = []
    for event in events:
        projections = {item.field_name: item for item in event.projections}
        claims = {item.key: item for item in event.claims}
        required = {CandidateFieldName.TITLE, CandidateFieldName.EVENT_TYPE}
        if event.event_type in {EventType.ASSIGNMENT_DUE, EventType.SUBMISSION}:
            required.add(CandidateFieldName.DUE_TIME)
        elif CandidateFieldName.START_TIME in projections or event.due_time is None:
            required.add(CandidateFieldName.START_TIME)
        else:
            required.add(CandidateFieldName.DUE_TIME)
        canonical_fields = {
            CandidateFieldName.TITLE,
            CandidateFieldName.EVENT_TYPE,
            CandidateFieldName.START_TIME,
            CandidateFieldName.END_TIME,
            CandidateFieldName.DUE_TIME,
            CandidateFieldName.LOCATION,
            CandidateFieldName.STATUS,
        }
        missing = tuple(sorted(canonical_fields - projections.keys(), key=lambda item: item.value))
        missing_required = tuple(sorted(required - projections.keys(), key=lambda item: item.value))
        review_fields = set(missing_required)
        review_fields.update(item.field_name for item in event.projections if item.uncertain)
        wording_conflicts: list[EventWordingConflict] = []
        for conflict in event.conflicts:
            if conflict.state.value != "OPEN":
                continue
            review_fields.add(conflict.field_name)
            projection = projections.get(conflict.field_name)
            selected_claim = (
                None if projection is None else claims.get(projection.selected_claim_key)
            )
            alternatives = tuple(
                dict.fromkeys(
                    claim.original_text
                    for key in conflict.alternative_claim_keys
                    if (claim := claims.get(key)) is not None
                )
            )
            wording_conflicts.append(
                EventWordingConflict(
                    conflict.field_name,
                    None if projection is None else projection.selected_claim_key,
                    None if selected_claim is None else selected_claim.original_text,
                    alternatives,
                )
            )
        bases: list[EventConfirmationBasis] = []
        for projection in sorted(event.projections, key=lambda item: item.field_name.value):
            selected_claim = claims.get(projection.selected_claim_key)
            resolution = manual.get((event.key, projection.field_name))
            if (
                resolution is not None
                and int(resolution["selected_claim_key"] or resolution["local_claim_key"])
                == projection.selected_claim_key
            ):
                bases.append(
                    EventConfirmationBasis(
                        projection.field_name,
                        projection.selected_claim_key,
                        EventConfirmationBasisKind.MANUAL_RESOLUTION,
                        str(resolution["reason"]),
                        True,
                        int(resolution["manual_resolution_key"]),
                    )
                )
            else:
                bases.append(
                    EventConfirmationBasis(
                        projection.field_name,
                        projection.selected_claim_key,
                        EventConfirmationBasisKind.DETERMINISTIC_RULE,
                        "deterministic projection"
                        if selected_claim is None
                        else selected_claim.decision_reason,
                        False,
                    )
                )
        review = EventReview(
            identity_requires_review=event.resolution_state is not EventResolutionState.RESOLVED,
            fields_requiring_review=tuple(sorted(review_fields, key=lambda item: item.value)),
            missing_fields=missing,
            wording_conflicts=tuple(wording_conflicts),
            confirmation_basis=tuple(bases),
            required_fields=tuple(sorted(required, key=lambda item: item.value)),
            missing_required_fields=missing_required,
        )
        reviewed.append(replace(event, review=review))
    return tuple(reviewed)


def event_derivation_coverage(
    database: Database, course: CourseId, course_key: int
) -> CoverageView:
    connection = database.connect()
    try:
        resource_rows = connection.execute(
            """SELECT resource.current_version_key AS version_key,
                    (SELECT parsed.parse_key FROM parsed_document parsed
                     WHERE parsed.version_key = resource.current_version_key
                     ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1) AS parse_key
                FROM resource JOIN content_node content USING(content_key)
                WHERE content.course_key = ? AND resource.current_version_key IS NOT NULL""",
            (course_key,),
        ).fetchall()
        observation_rows = connection.execute(
            """SELECT current_observation_key AS observation_key FROM announcement
                    WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM assessment WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM schedule_item WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM due_item WHERE course_key = ?""",
            (course_key, course_key, course_key, course_key),
        ).fetchall()
        extraction_keys: list[int] = []
        complete = True
        observed_values: list[datetime] = []
        for row in resource_rows:
            parse_key = row["parse_key"]
            extraction = None
            if parse_key is not None:
                extraction = connection.execute(
                    """SELECT extraction_record_key, status, extracted_at
                        FROM extraction_record
                        WHERE input_kind = 'resource_version' AND version_key = ? AND parse_key = ?
                        ORDER BY extracted_at DESC, extraction_record_key DESC LIMIT 1""",
                    (int(row["version_key"]), int(parse_key)),
                ).fetchone()
            if extraction is None or str(extraction["status"]) != "COMPLETE":
                complete = False
            else:
                extraction_keys.append(int(extraction["extraction_record_key"]))
                observed_values.append(from_storage_time(str(extraction["extracted_at"])))
        for row in observation_rows:
            extraction = connection.execute(
                """SELECT extraction.extraction_record_key,
                        extraction.status, extraction.extracted_at
                    FROM source_observation current
                    JOIN source_observation equivalent
                      ON equivalent.source_object_key = current.source_object_key
                     AND equivalent.observation_hash = current.observation_hash
                    JOIN extraction_record extraction
                      ON extraction.source_observation_key = equivalent.observation_key
                     AND extraction.input_kind = 'source_observation'
                    WHERE current.observation_key = ?
                      AND extraction.status IN ('COMPLETE', 'PARTIAL')
                    ORDER BY extraction.extracted_at DESC,
                        extraction.extraction_record_key DESC
                    LIMIT 1""",
                (int(row["observation_key"]),),
            ).fetchone()
            if extraction is None or str(extraction["status"]) != "COMPLETE":
                complete = False
            else:
                extraction_keys.append(int(extraction["extraction_record_key"]))
                observed_values.append(from_storage_time(str(extraction["extracted_at"])))
        if extraction_keys:
            placeholders = ",".join("?" for _ in extraction_keys)
            missing = connection.execute(
                f"""SELECT COUNT(*) FROM event_candidate candidate
                    LEFT JOIN event_source source USING(candidate_key)
                    WHERE candidate.extraction_record_key IN ({placeholders})
                      AND (source.event_source_key IS NULL
                           OR source.event_key IS NULL
                           OR source.resolution_state = 'UNRESOLVED')""",
                extraction_keys,
            ).fetchone()
            complete = complete and missing is not None and int(missing[0]) == 0
    finally:
        connection.close()
    return CoverageView(
        course.provider,
        "event_derivation",
        Coverage.COMPLETE if complete else Coverage.PARTIAL,
        course,
        observed_at=max(observed_values, default=None),
        evidence="current_extraction_and_reconciliation",
    )


def event_derivation_warnings(
    database: Database, courses: tuple[tuple[CourseId, int], ...]
) -> tuple[SafeWarning, ...]:
    if not courses:
        return ()
    connection = database.connect()
    try:
        placeholders = ",".join("?" for _ in courses)
        active_source = active_event_source_sql("event_source")
        row = connection.execute(
            f"""SELECT 1 FROM event_source
                WHERE course_key IN ({placeholders})
                  AND (event_key IS NULL OR resolution_state = 'UNRESOLVED')
                  AND {active_source}
                LIMIT 1""",
            tuple(key for _, key in courses),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return ()
    return (
        SafeWarning(
            "unresolved_event_identity",
            "One or more event candidates retain unresolved source identity.",
            "events",
        ),
    )


def event_scopes(
    fallback_provider: str | None,
    courses: tuple[tuple[CourseId, int], ...],
    window: TimeWindow | None,
    *,
    include_inventory: bool = False,
) -> tuple[ScopeKey, ...]:
    scopes: list[ScopeKey] = []
    if include_inventory:
        providers = {course.provider for course, _ in courses}
        if not providers and fallback_provider is not None:
            providers.add(fallback_provider)
        scopes.extend(ScopeKey(provider, None, "courses") for provider in sorted(providers))
    for course, _ in courses:
        scopes.extend(
            (
                ScopeKey(course.provider, course, "content"),
                ScopeKey(course.provider, course, EventSourceScope.ANNOUNCEMENTS.value),
                ScopeKey(course.provider, course, EventSourceScope.ASSESSMENTS.value),
            )
        )
        if window is not None:
            scopes.extend(
                (
                    ScopeKey(course.provider, course, EventSourceScope.SCHEDULE.value, window),
                    ScopeKey(course.provider, course, EventSourceScope.DUE_ITEMS.value, window),
                )
            )
    return tuple(scopes)


def unbounded_event_coverage(
    courses: tuple[tuple[CourseId, int], ...], window: TimeWindow | None
) -> tuple[CoverageView, ...]:
    if window is not None:
        return ()
    return tuple(
        CoverageView(
            course.provider,
            data_kind,
            Coverage.UNKNOWN,
            course,
            evidence="unbounded_query_scope",
        )
        for course, _ in courses
        for data_kind in ("schedule", "due_items")
    )


def temporal_bounds(value: object) -> tuple[datetime, datetime] | None:
    if not isinstance(value, Mapping):
        return None
    instant = value.get("instant")
    if isinstance(instant, str):
        parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
        if parsed.tzinfo is not None and parsed.utcoffset() is not None:
            point = parsed.astimezone(UTC)
            return point, point
    if value.get("precision") == "DATE_ONLY" and isinstance(value.get("date"), str):
        try:
            day = date.fromisoformat(str(value["date"]).strip())
        except ValueError:
            return None
        # Expand conservatively across all plausible civil timezones rather
        # than assigning the floating source date a fabricated timezone.
        return (
            datetime.combine(day - timedelta(days=1), time.min, UTC),
            datetime.combine(day + timedelta(days=2), time.min, UTC),
        )
    return None


def event_conflicts(items: tuple[object, ...]) -> tuple[ConflictView, ...]:
    return tuple(
        ConflictView(
            conflict.event_key,
            conflict.field_name.value,
            conflict.reason,
            conflict.alternative_claim_keys,
        )
        for item in items
        if isinstance(item, CanonicalEvent)
        for conflict in item.conflicts
        if conflict.state.value == "OPEN"
    )


def overlaps(event: CanonicalEvent, window: TimeWindow) -> bool:
    if _has_unbounded_temporal(event):
        return True
    bounds = [
        temporal_bounds(value) for value in (event.start_time, event.end_time, event.due_time)
    ]
    conflict_keys = {
        key
        for conflict in event.conflicts
        if conflict.state.value == "OPEN"
        for key in conflict.alternative_claim_keys
    }
    bounds.extend(
        temporal_bounds(claim.value) for claim in event.claims if claim.key in conflict_keys
    )
    known = [value for value in bounds if value is not None]
    if not known:
        return True
    start = min(value[0] for value in known)
    end = max(value[1] for value in known)
    return start < window.until and end >= window.since


def _has_unbounded_temporal(event: CanonicalEvent) -> bool:
    values: list[object] = [event.start_time, event.end_time, event.due_time]
    conflict_keys = {
        key
        for conflict in event.conflicts
        if conflict.state.value == "OPEN"
        for key in conflict.alternative_claim_keys
    }
    temporal_fields = {
        CandidateFieldName.START_TIME,
        CandidateFieldName.END_TIME,
        CandidateFieldName.DUE_TIME,
        CandidateFieldName.OPEN_AT,
        CandidateFieldName.CLOSE_AT,
        CandidateFieldName.AVAILABLE_FROM,
        CandidateFieldName.AVAILABLE_UNTIL,
        CandidateFieldName.PUBLISHED_AT,
    }
    values.extend(
        claim.value
        for claim in event.claims
        if claim.key in conflict_keys and claim.field_name in temporal_fields
    )
    present = [value for value in values if value is not None]
    return not present or any(temporal_bounds(value) is None for value in present)


def temporal_projection_coverage(
    events: tuple[CanonicalEvent, ...], window: TimeWindow | None
) -> tuple[CoverageView, ...]:
    if window is None:
        return ()
    courses = {event.course for event in events if _has_unbounded_temporal(event)}
    return tuple(
        CoverageView(
            course.provider,
            "event_time_projection",
            Coverage.PARTIAL,
            course,
            window.since,
            window.until,
            evidence="unsupported_or_missing_temporal_precision",
        )
        for course in sorted(courses, key=lambda item: (item.provider, item.value))
    )


def temporal_projection_warnings(
    events: tuple[CanonicalEvent, ...], window: TimeWindow | None
) -> tuple[SafeWarning, ...]:
    if window is None or not any(_has_unbounded_temporal(event) for event in events):
        return ()
    return (
        SafeWarning(
            "unbounded_event_time",
            "An event with missing or unsupported time precision may overlap the window.",
            "events",
        ),
    )


def event_sort_key(event: CanonicalEvent) -> tuple[datetime, int]:
    bounds = [
        temporal_bounds(value) for value in (event.start_time, event.due_time, event.end_time)
    ]
    known = [value[0] for value in bounds if value is not None]
    return (min(known) if known else datetime.max.replace(tzinfo=UTC), event.key)


def event_envelope(
    operation: str, events: tuple[CanonicalEvent, ...], completeness: Coverage
) -> ResultEnvelope[CanonicalEvent]:
    provenance = tuple(
        ProvenanceView(
            source.evidence_kind, source.evidence_key, event.course.provider, event.course
        )
        for event in events
        for source in event.sources
    )
    conflicts = event_conflicts(events)
    warnings = (
        ()
        if not conflicts
        else (
            SafeWarning(
                "unresolved_event_conflicts",
                "One or more event fields require resolution.",
                "events",
            ),
        )
    )
    return ResultEnvelope(
        operation,
        events,
        provenance=provenance,
        conflicts=conflicts,
        warnings=warnings,
        completeness=completeness,
    )
