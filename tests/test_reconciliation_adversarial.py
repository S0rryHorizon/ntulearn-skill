from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntulearn_skill.client.contracts import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    ScheduleSourceRecord,
)
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    Availability,
    CalendarItemId,
    ContentId,
    CourseId,
    GradingColumnId,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    ChangeKind,
    ClaimDecisionState,
    ConflictState,
    DeterministicEventExtractor,
    EventReconciler,
    EventRepository,
    EventResolutionState,
    EventSourceResolutionState,
    ManualFieldResolution,
    ReconciliationDecisionResult,
)
from ntulearn_skill.storage import Database, DomainRepository, RuntimePaths


@dataclass(frozen=True)
class _Harness:
    database: Database
    repository: EventRepository
    extractor: DeterministicEventExtractor
    reconciler: EventReconciler
    first_course: CourseId
    second_course: CourseId
    first_content: ContentId
    second_content: ContentId


def _harness(tmp_path: Path, *, resolver_version: str = "test-v1") -> _Harness:
    paths = RuntimePaths(tmp_path / "private-synthetic-runtime")
    database = Database(paths.database)
    domain = DomainRepository(database)
    assert domain.initialize() >= 7
    first_course = CourseId("synthetic", "ph0000-course-a")
    second_course = CourseId("synthetic", "ph0000-course-b")
    first_content = ContentId("synthetic", "ph0000-content-a")
    second_content = ContentId("synthetic", "ph0000-content-b")
    for course, content, suffix in (
        (first_course, first_content, "A"),
        (second_course, second_content, "B"),
    ):
        domain.put_course(course, code=f"PH0000-{suffix}", title=f"Synthetic Course {suffix}")
        domain.put_content_node(
            content,
            course_id=course,
            handler_kind="assessment",
            title=f"Synthetic assessment container {suffix}",
            position=0,
        )
    return _Harness(
        database,
        EventRepository(database),
        DeterministicEventExtractor(database),
        EventReconciler(database, resolver_version=resolver_version),
        first_course,
        second_course,
        first_content,
        second_content,
    )


def _sync_run(database: Database, ordinal: int) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-reconciliation', '{}', ?, 'RUNNING')""",
            (datetime(2030, 1, ordinal, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _exact(day: int, hour: int = 10) -> SourceTime:
    instant = datetime(2030, 2, day, hour, tzinfo=UTC)
    return SourceTime(
        instant,
        instant.strftime("%Y-%m-%d %H:%M UTC"),
        "UTC",
        TemporalPrecision.EXACT_TIME,
    )


def _announce(
    harness: _Harness,
    remote_key: str,
    wording: str,
    *,
    course: CourseId | None = None,
    ordinal: int,
    published_day: int | None = None,
) -> int:
    course = course or harness.first_course
    observation = harness.repository.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", remote_key),
            course,
            "Synthetic PH0000 notice",
            wording,
            Availability.ACTIVE,
            published_at=None if published_day is None else _exact(published_day, 8),
        ),
        sync_run_key=_sync_run(harness.database, ordinal),
        observed_at=datetime(2030, 3, ordinal, 12, tzinfo=UTC),
    )
    result = harness.extractor.extract_observation(observation.observation.key)
    assert result.candidates, wording
    return observation.observation.key


def _schedule(
    harness: _Harness,
    remote_key: str,
    title: str,
    *,
    start_day: int,
    location: str | None,
    ordinal: int,
) -> int:
    observation = harness.repository.observe_schedule(
        ScheduleSourceRecord(
            CalendarItemId("synthetic", remote_key),
            harness.first_course,
            title,
            Availability.ACTIVE,
            start_at=_exact(start_day),
            end_at=_exact(start_day, 11),
            location=location,
        ),
        sync_run_key=_sync_run(harness.database, ordinal),
        observed_at=datetime(2030, 3, ordinal, 12, tzinfo=UTC),
    )
    result = harness.extractor.extract_observation(observation.observation.key)
    assert len(result.candidates) == 1
    return observation.observation.key


def _assessment_with_due_alternatives(harness: _Harness, *, ordinal: int = 1) -> None:
    observation = harness.repository.observe_assessment(
        AssessmentSourceRecord(
            AssessmentId("synthetic", "ph0000-assessment-1"),
            harness.first_course,
            harness.first_content,
            GradingColumnId("synthetic", "ph0000-grade-column-1"),
            "Quiz 1",
            AssessmentSubtype.QUIZ,
            "Synthetic instructions only.",
            Availability.ACTIVE,
            due_at=_exact(10),
            grading_due_at=_exact(12),
        ),
        sync_run_key=_sync_run(harness.database, ordinal),
        observed_at=datetime(2030, 3, ordinal, 12, tzinfo=UTC),
    )
    result = harness.extractor.extract_observation(observation.observation.key)
    assert len(result.candidates) == 2


def _due_claims(harness: _Harness, event_key: int):
    event = harness.reconciler.get_event(event_key)
    assert event is not None
    return tuple(claim for claim in event.claims if claim.field_name is CandidateFieldName.DUE_TIME)


def test_quiz_one_aliases_merge_when_course_time_and_context_agree(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    for ordinal, (remote_key, wording) in enumerate(
        (
            ("alias-numeric", "Quiz 1 due 2030-02-10 10:00 UTC."),
            ("alias-hash", "Quiz #1 deadline 2030-02-10 10:00 UTC."),
            ("alias-word", "First Quiz due 2030-02-10 10:00 UTC."),
        ),
        start=1,
    ):
        _announce(harness, remote_key, wording, ordinal=ordinal)

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    event = result.events[0]
    assert event.course == harness.first_course
    assert event.source_count == 3
    assert event.resolution_state is EventResolutionState.RESOLVED
    assert len(event.sources) == 3
    assert len(_due_claims(harness, event.key)) == 3


@pytest.mark.parametrize(
    ("left", "right"),
    (
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 due 2030-04-10 10:00 UTC."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 2 due 2030-02-10 10:00 UTC."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 2030-02-10 10:00 UTC."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Test 1 due 2030-02-10 10:00 UTC."),
    ),
    ids=("title-alone", "numbered-event", "due-versus-start", "incompatible-type"),
)
def test_hard_identity_negatives_are_not_forced_to_merge(
    tmp_path: Path, left: str, right: str
) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "negative-left", left, ordinal=1)
    _announce(harness, "negative-right", right, ordinal=2)

    result = harness.reconciler.reconcile_course(harness.first_course)

    sources = tuple(
        source
        for source in harness.reconciler.list_event_sources()
        if source.course == harness.first_course
    )
    assert len(sources) == 2
    assert not (sources[0].event_key is not None and sources[0].event_key == sources[1].event_key)
    if len(result.events) == 1:
        assert len(result.unresolved_sources) == 1
        unresolved = next(
            decision
            for decision in result.decisions
            if decision.result is ReconciliationDecisionResult.UNRESOLVED
        )
        assert unresolved.considered_event_keys == (result.events[0].key,)
    else:
        assert len(result.events) == 2


def test_identical_candidates_in_different_courses_never_share_an_event(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    wording = "Quiz 1 due 2030-02-10 10:00 UTC."
    _announce(harness, "cross-course-a", wording, ordinal=1)
    _announce(
        harness,
        "cross-course-b",
        wording,
        course=harness.second_course,
        ordinal=2,
    )

    first = harness.reconciler.reconcile_course(harness.first_course)
    second = harness.reconciler.reconcile_course(harness.second_course)

    assert len(first.events) == len(second.events) == 1
    assert first.events[0].course == harness.first_course
    assert second.events[0].course == harness.second_course
    assert first.events[0].key != second.events[0].key
    assert first.events[0].stable_id != second.events[0].stable_id


def test_equally_plausible_multiple_matches_leave_candidate_unresolved(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "ambiguous-early", "Quiz 1 due 2030-02-10 10:00 UTC.", ordinal=1)
    _announce(harness, "ambiguous-late", "Quiz 1 due 2030-02-14 10:00 UTC.", ordinal=2)
    _announce(harness, "ambiguous-middle", "Quiz due 2030-02-12 10:00 UTC.", ordinal=3)

    result = harness.reconciler.reconcile_course(harness.first_course)

    unresolved = [
        source
        for source in result.unresolved_sources
        if source.resolution_state is EventSourceResolutionState.UNRESOLVED
    ]
    assert len(result.events) == 2
    assert len(unresolved) == 1
    assert unresolved[0].event_key is None


def test_conflicting_due_claims_are_retained_and_canonical_field_is_uncertain(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _assessment_with_due_alternatives(harness)

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    event = result.events[0]
    due_claims = _due_claims(harness, event.key)
    conflicts = event.conflicts
    due = event.field(CandidateFieldName.DUE_TIME)
    assert {claim.decision_state for claim in due_claims} == {ClaimDecisionState.CONFLICTING}
    assert len(conflicts) == 1
    assert conflicts[0].field_name is CandidateFieldName.DUE_TIME
    assert conflicts[0].state is ConflictState.OPEN
    assert event.resolution_state is EventResolutionState.CONFLICTING
    assert due is None or due.uncertain


def test_explicit_later_move_supersedes_only_time_and_preserves_venue(tmp_path: Path) -> None:
    harness = _harness(tmp_path / "known-source-time")
    _announce(
        harness,
        "movable-announcement",
        ("Quiz 1 due 2030-02-10 10:00 UTC. Quiz 1 room changed to Synthetic Room A."),
        ordinal=1,
        published_day=1,
    )
    _announce(
        harness,
        "movable-announcement",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=2,
        published_day=2,
    )

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    event = result.events[0]
    due_claims = tuple(
        claim for claim in event.claims if claim.field_name is CandidateFieldName.DUE_TIME
    )
    due = event.field(CandidateFieldName.DUE_TIME)
    location = event.field(CandidateFieldName.LOCATION)
    assert {claim.decision_state for claim in due_claims} == {
        ClaimDecisionState.ACCEPTED,
        ClaimDecisionState.SUPERSEDED,
    }
    assert due is not None and due.value["instant"].startswith("2030-02-12T10:00:00")
    assert location is not None and location.value == "Synthetic Room A"
    assert any(claim.superseded_by for claim in due_claims)

    unknown = _harness(tmp_path / "unknown-source-time")
    _schedule(
        unknown,
        "movable-calendar-item",
        "Quiz 1",
        start_day=10,
        location="Synthetic Room A",
        ordinal=1,
    )
    _schedule(
        unknown,
        "movable-calendar-item",
        "Quiz 1 moved",
        start_day=12,
        location=None,
        ordinal=2,
    )

    uncertain = unknown.reconciler.reconcile_course(unknown.first_course).events[0]
    unknown_start_claims = tuple(
        claim for claim in uncertain.claims if claim.field_name is CandidateFieldName.START_TIME
    )
    assert {claim.decision_state for claim in unknown_start_claims} == {
        ClaimDecisionState.CONFLICTING
    }
    assert not any(claim.supersedes or claim.superseded_by for claim in unknown_start_claims)
    assert uncertain.field(CandidateFieldName.START_TIME) is None
    preserved = uncertain.field(CandidateFieldName.LOCATION)
    assert preserved is not None and preserved.value == "Synthetic Room A"


def test_late_observation_of_older_vague_wording_does_not_replace_exact_claim(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "precision-order",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
        published_day=2,
    )
    _announce(
        harness,
        "precision-order",
        "Quiz 1 due 2030-02-10.",
        ordinal=2,
        published_day=1,
    )

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    event = result.events[0]
    selected = event.field(CandidateFieldName.DUE_TIME)
    assert selected is not None
    assert selected.value["precision"] == TemporalPrecision.EXACT_TIME.value
    accepted = [
        claim
        for claim in _due_claims(harness, event.key)
        if claim.decision_state is ClaimDecisionState.ACCEPTED
    ]
    assert len(accepted) == 1
    assert accepted[0].value["precision"] == TemporalPrecision.EXACT_TIME.value


def test_cancellation_and_venue_only_sources_update_their_target_fields(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "cancellation-source", "Quiz 3 2030-02-20 10:00 UTC.", ordinal=1)
    _announce(harness, "cancellation-source", "Quiz 3 is cancelled.", ordinal=2)
    _announce(harness, "venue-source", "Tutorial 1 2030-02-21 10:00 UTC.", ordinal=3)
    _announce(
        harness,
        "venue-source",
        "Tutorial 1 room changed to Synthetic Room B.",
        ordinal=4,
    )

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 2
    cancelled = next(event for event in result.events if event.title == "Quiz 3")
    relocated = next(event for event in result.events if event.title == "Tutorial 1")
    status = cancelled.field(CandidateFieldName.STATUS)
    venue = relocated.field(CandidateFieldName.LOCATION)
    assert status is not None and status.value == "CANCELLED"
    assert venue is not None and venue.value == "Synthetic Room B"
    assert any(source.change_kind is ChangeKind.CANCELLATION for source in cancelled.sources)
    assert any(source.change_kind is ChangeKind.VENUE_CHANGE for source in relocated.sources)


def test_manual_choice_persists_across_new_resolver_and_invalid_claim_rolls_back(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _assessment_with_due_alternatives(harness)
    _announce(harness, "other-event", "Quiz 2 due 2030-02-20 10:00 UTC.", ordinal=2)
    first = harness.reconciler.reconcile_course(harness.first_course)
    conflicted = next(event for event in first.events if len(_due_claims(harness, event.key)) == 2)
    other = next(event for event in first.events if event.key != conflicted.key)
    selected_claim = _due_claims(harness, conflicted.key)[0]
    foreign_claim = _due_claims(harness, other.key)[0]

    resolved = harness.reconciler.resolve_field(
        ManualFieldResolution(
            conflicted.key,
            CandidateFieldName.DUE_TIME,
            "Select the synthetic direct due field.",
            selected_claim_key=selected_claim.key,
        )
    )
    chosen = resolved.field(CandidateFieldName.DUE_TIME)
    assert chosen is not None and chosen.selected_claim_key == selected_claim.key
    with harness.database.connect() as connection:
        before_invalid = tuple(
            tuple(row)
            for table in (
                "claim",
                "event_field_projection",
                "event_conflict",
                "manual_field_resolution",
            )
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )

    with pytest.raises(ValueError, match="claim"):
        harness.reconciler.resolve_field(
            ManualFieldResolution(
                conflicted.key,
                CandidateFieldName.DUE_TIME,
                "This claim belongs to another synthetic event.",
                selected_claim_key=foreign_claim.key,
            )
        )

    with harness.database.connect() as connection:
        after_invalid = tuple(
            tuple(row)
            for table in (
                "claim",
                "event_field_projection",
                "event_conflict",
                "manual_field_resolution",
            )
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    assert after_invalid == before_invalid

    rerun = EventReconciler(harness.database, resolver_version="test-v2").reconcile_course(
        harness.first_course
    )
    persisted = next(event for event in rerun.events if event.key == conflicted.key)
    projection = persisted.field(CandidateFieldName.DUE_TIME)
    assert projection is not None
    assert projection.selected_claim_key == selected_claim.key
    assert not projection.uncertain


def test_reconciliation_is_idempotent_and_keeps_m6_evidence_immutable(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    observation_key = _announce(
        harness,
        "idempotent",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
    )
    with harness.database.connect() as connection:
        before = tuple(
            tuple(row)
            for table in (
                "source_observation",
                "extraction_record",
                "event_candidate",
                "event_candidate_field",
            )
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )

    first = harness.reconciler.reconcile_course(harness.first_course)
    repeated = harness.reconciler.reconcile_course(harness.first_course)

    assert not first.cache_hit
    assert repeated.cache_hit
    assert repeated.run_key == first.run_key
    assert repeated.events == first.events
    with harness.database.connect() as connection:
        after = tuple(
            tuple(row)
            for table in (
                "source_observation",
                "extraction_record",
                "event_candidate",
                "event_candidate_field",
            )
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )
    assert after == before
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with harness.database.transaction() as connection:
            connection.execute(
                "UPDATE source_observation SET raw_wording = ? WHERE observation_key = ?",
                ("tampered synthetic wording", observation_key),
            )
    assert harness.database.integrity_check()


def test_unknown_structured_time_is_reflexive_without_fabricating_an_instant(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    observation = harness.repository.observe_assessment(
        AssessmentSourceRecord(
            AssessmentId("synthetic", "unknown-time-assessment"),
            harness.first_course,
            harness.first_content,
            None,
            "Synthetic presentation",
            AssessmentSubtype.PRESENTATION,
            "Synthetic evidence retains wording without a parseable date.",
            Availability.ACTIVE,
            due_at=SourceTime(
                None,
                "Time remains to be announced",
                None,
                TemporalPrecision.UNKNOWN,
            ),
        ),
        sync_run_key=_sync_run(harness.database, 1),
        observed_at=datetime(2030, 3, 1, 12, tzinfo=UTC),
    )
    extracted = harness.extractor.extract_observation(observation.observation.key)

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert extracted.candidates
    assert len(result.events) == 1
    event = result.events[0]
    due = event.field(CandidateFieldName.DUE_TIME)
    assert due is not None
    selected = next(claim for claim in event.claims if claim.key == due.selected_claim_key)
    assert selected.decision_state is ClaimDecisionState.ACCEPTED
    assert selected.precision is TemporalPrecision.UNKNOWN
    assert due.value["instant"] is None
    assert due.value.get("source_timezone") is None


def test_identical_unknown_times_do_not_merge_unrelated_assessments(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    other_content = ContentId("synthetic", "unrelated-assessment-content")
    DomainRepository(harness.database).put_content_node(
        other_content,
        course_id=harness.first_course,
        handler_kind="assessment",
        title="Synthetic unrelated assessment container",
        position=1,
    )
    unknown_time = SourceTime(
        None,
        "Time remains to be announced",
        None,
        TemporalPrecision.UNKNOWN,
    )
    for ordinal, (remote_key, content) in enumerate(
        (
            ("unknown-time-one", harness.first_content),
            ("unknown-time-two", other_content),
        ),
        start=1,
    ):
        observation = harness.repository.observe_assessment(
            AssessmentSourceRecord(
                AssessmentId("synthetic", remote_key),
                harness.first_course,
                content,
                None,
                "Synthetic presentation",
                AssessmentSubtype.PRESENTATION,
                "Synthetic evidence has no shared context.",
                Availability.ACTIVE,
                due_at=unknown_time,
            ),
            sync_run_key=_sync_run(harness.database, ordinal),
            observed_at=datetime(2030, 3, ordinal, 12, tzinfo=UTC),
        )
        harness.extractor.extract_observation(observation.observation.key)

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    assert result.events[0].source_count == 1
    assert len(result.unresolved_sources) == 1
