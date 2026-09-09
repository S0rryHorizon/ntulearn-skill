"""Synthetic end-to-end regressions for bounded M7 manual decisions."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntulearn_skill.client.contracts import AnnouncementSourceRecord, AssessmentSourceRecord
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    Availability,
    ContentId,
    CourseId,
    GradingColumnId,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events._manual import resolve_event_source, resolve_field
from ntulearn_skill.events.extraction import DeterministicEventExtractor
from ntulearn_skill.events.models import (
    CandidateFieldName,
    ClaimDecisionState,
    ClaimOrigin,
    EventSourceResolutionState,
    ManualFieldResolution,
    ManualIdentityResolution,
)
from ntulearn_skill.events.reconciliation import EventReconciler
from ntulearn_skill.events.repository import EventRepository
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


def _harness(tmp_path: Path) -> _Harness:
    database = Database(RuntimePaths(tmp_path / "private-synthetic-runtime").database)
    domain = DomainRepository(database)
    assert domain.initialize() >= 7
    first_course = CourseId("synthetic", "manual-course-a")
    second_course = CourseId("synthetic", "manual-course-b")
    first_content = ContentId("synthetic", "manual-content-a")
    for course, suffix in ((first_course, "A"), (second_course, "B")):
        domain.put_course(course, code=f"PH0000-{suffix}", title=f"Synthetic Course {suffix}")
    domain.put_content_node(
        first_content,
        course_id=first_course,
        handler_kind="assessment",
        title="Synthetic assessment container",
        position=0,
    )
    return _Harness(
        database,
        EventRepository(database),
        DeterministicEventExtractor(database),
        EventReconciler(database, resolver_version="manual-v1"),
        first_course,
        second_course,
        first_content,
    )


def _sync_run(database: Database, ordinal: int) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-manual', '{}', ?, 'RUNNING')""",
            (datetime(2032, 1, ordinal, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _exact(day: int) -> SourceTime:
    instant = datetime(2032, 2, day, 10, tzinfo=UTC)
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
    ordinal: int,
    *,
    course: CourseId | None = None,
) -> None:
    observation = harness.repository.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", remote_key),
            course or harness.first_course,
            "Synthetic manual-resolution notice",
            wording,
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database, ordinal),
        observed_at=datetime(2032, 3, ordinal, 12, tzinfo=UTC),
    )
    assert harness.extractor.extract_observation(observation.observation.key).candidates


def _conflicted_assessment(harness: _Harness) -> None:
    observation = harness.repository.observe_assessment(
        AssessmentSourceRecord(
            AssessmentId("synthetic", "manual-assessment"),
            harness.first_course,
            harness.first_content,
            GradingColumnId("synthetic", "manual-grade-column"),
            "Quiz 1",
            AssessmentSubtype.QUIZ,
            "Synthetic instructions.",
            Availability.ACTIVE,
            due_at=_exact(10),
            grading_due_at=_exact(12),
        ),
        sync_run_key=_sync_run(harness.database, 1),
        observed_at=datetime(2032, 3, 1, 12, tzinfo=UTC),
    )
    assert len(harness.extractor.extract_observation(observation.observation.key).candidates) == 2


def _snapshot(database: Database, tables: tuple[str, ...]) -> tuple[tuple[object, ...], ...]:
    with database.connect() as connection:
        return tuple(
            tuple(row)
            for table in tables
            for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        )


def test_field_selection_is_append_only_atomic_and_survives_new_ruleset(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _conflicted_assessment(harness)
    _announce(harness, "other-event", "Quiz 2 due 2032-02-20 10:00 UTC.", 2)
    events = harness.reconciler.reconcile_course(harness.first_course).events
    conflicted = next(
        event
        for event in events
        if len([claim for claim in event.claims if claim.field_name is CandidateFieldName.DUE_TIME])
        == 2
    )
    other = next(event for event in events if event.key != conflicted.key)
    due_claims = [
        claim for claim in conflicted.claims if claim.field_name is CandidateFieldName.DUE_TIME
    ]
    selected = due_claims[0]
    foreign = next(
        claim for claim in other.claims if claim.field_name is CandidateFieldName.DUE_TIME
    )

    first = resolve_field(
        harness.reconciler,
        ManualFieldResolution(
            conflicted.key,
            CandidateFieldName.DUE_TIME,
            "Keep the first synthetic due date.",
            selected_claim_key=selected.key,
        ),
    )
    projection = first.field(CandidateFieldName.DUE_TIME)
    assert projection is not None and projection.selected_claim_key == selected.key
    before = _snapshot(
        harness.database,
        ("claim", "event_field_projection", "event_conflict", "manual_field_resolution"),
    )
    with pytest.raises(ValueError, match="event field"):
        resolve_field(
            harness.reconciler,
            ManualFieldResolution(
                conflicted.key,
                CandidateFieldName.DUE_TIME,
                "Reject cross-event evidence.",
                selected_claim_key=foreign.key,
            ),
        )
    assert (
        _snapshot(
            harness.database,
            ("claim", "event_field_projection", "event_conflict", "manual_field_resolution"),
        )
        == before
    )

    repeated = resolve_field(
        harness.reconciler,
        ManualFieldResolution(
            conflicted.key,
            CandidateFieldName.DUE_TIME,
            "Append a revised synthetic decision.",
            selected_claim_key=selected.key,
        ),
    )
    assert repeated.field(CandidateFieldName.DUE_TIME) == projection
    with harness.database.connect() as connection:
        history = connection.execute(
            """SELECT reason, active FROM manual_field_resolution
            WHERE event_key = ? AND field_name = 'due_time'
            ORDER BY manual_resolution_key""",
            (conflicted.key,),
        ).fetchall()
    assert [(row["reason"], row["active"]) for row in history] == [
        ("Keep the first synthetic due date.", 0),
        ("Append a revised synthetic decision.", 1),
    ]

    rerun = EventReconciler(harness.database, resolver_version="manual-v2").reconcile_course(
        harness.first_course
    )
    persisted = next(event for event in rerun.events if event.key == conflicted.key)
    assert persisted.field(CandidateFieldName.DUE_TIME) == projection


def test_local_claim_is_normalized_source_free_and_invalid_value_rolls_back(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "local-value", "Quiz 1 due 2032-02-10 10:00 UTC.", 1)
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    resolved = resolve_field(
        harness.reconciler,
        ManualFieldResolution(
            event.key,
            CandidateFieldName.DUE_TIME,
            "Enter a synthetic corrected time.",
            value={
                "instant": "2032-02-15T18:00:00+08:00",
                "precision": "EXACT_TIME",
                "source_text": "15 February 2032, 18:00 SGT",
                "source_timezone": "SGT",
            },
            original_text="15 February 2032, 18:00 SGT",
        ),
    )
    projection = resolved.field(CandidateFieldName.DUE_TIME)
    assert projection is not None
    assert isinstance(projection.value, dict)
    assert projection.value["instant"] == "2032-02-15T10:00:00.000000+00:00"
    local = next(claim for claim in resolved.claims if claim.origin is ClaimOrigin.LOCAL_DECISION)
    assert local.event_source_key is None and local.candidate_field_key is None
    assert local.precision is TemporalPrecision.EXACT_TIME
    assert local.source_timezone == "SGT"
    assert local.decision_state is ClaimDecisionState.ACCEPTED

    before = _snapshot(harness.database, ("claim", "manual_field_resolution"))
    with pytest.raises(ValueError, match="exact time"):
        resolve_field(
            harness.reconciler,
            ManualFieldResolution(
                event.key,
                CandidateFieldName.DUE_TIME,
                "Reject a naive timestamp.",
                value={
                    "instant": "2032-02-16T10:00:00",
                    "precision": "EXACT_TIME",
                    "source_text": "synthetic naive time",
                    "source_timezone": "UTC",
                },
            ),
        )
    assert _snapshot(harness.database, ("claim", "manual_field_resolution")) == before


def test_identity_resolution_binds_unresolved_evidence_and_blocks_unsafe_moves(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "early", "Quiz 1 due 2032-02-10 10:00 UTC.", 1)
    _announce(harness, "late", "Quiz 1 due 2032-02-14 10:00 UTC.", 2)
    _announce(harness, "ambiguous", "Quiz due 2032-02-12 10:00 UTC.", 3)
    first = harness.reconciler.reconcile_course(harness.first_course)
    unresolved = next(
        source
        for source in first.unresolved_sources
        if source.resolution_state is EventSourceResolutionState.UNRESOLVED
    )
    target, other = first.events

    bound = resolve_event_source(
        harness.reconciler,
        ManualIdentityResolution(
            unresolved.key,
            target.key,
            "Attach the synthetic ambiguous notice to the first event.",
        ),
    )
    assert any(source.key == unresolved.key for source in bound.sources)
    moved_claims = [claim for claim in bound.claims if claim.event_source_key == unresolved.key]
    assert moved_claims and all(claim.event_key == target.key for claim in moved_claims)
    with harness.database.connect() as connection:
        active = connection.execute(
            """SELECT event_key, reason FROM manual_identity_resolution
            WHERE event_source_key = ? AND active = 1""",
            (unresolved.key,),
        ).fetchone()
        possible = connection.execute(
            "SELECT 1 FROM event_source_possible_match WHERE event_source_key = ?",
            (unresolved.key,),
        ).fetchone()
    assert active is not None and active["event_key"] == target.key
    assert active["reason"] == "Attach the synthetic ambiguous notice to the first event."
    assert possible is None

    before = _snapshot(
        harness.database,
        ("event_source", "claim", "manual_identity_resolution"),
    )
    with pytest.raises(ValueError, match="cannot be moved"):
        resolve_event_source(
            harness.reconciler,
            ManualIdentityResolution(
                unresolved.key,
                other.key,
                "Attempt an unsafe reassignment.",
            ),
        )
    assert (
        _snapshot(
            harness.database,
            ("event_source", "claim", "manual_identity_resolution"),
        )
        == before
    )

    rerun = EventReconciler(harness.database, resolver_version="manual-v2").reconcile_course(
        harness.first_course
    )
    persisted = next(event for event in rerun.events if event.key == target.key)
    assert any(source.key == unresolved.key for source in persisted.sources)


def test_identity_resolution_rejects_cross_course_without_partial_writes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(harness, "course-a", "Quiz 1 due 2032-02-10 10:00 UTC.", 1)
    _announce(
        harness,
        "course-b",
        "Quiz 1 due 2032-02-10 10:00 UTC.",
        2,
        course=harness.second_course,
    )
    first = harness.reconciler.reconcile_course(harness.first_course).events[0]
    second = harness.reconciler.reconcile_course(harness.second_course).events[0]
    before = _snapshot(
        harness.database,
        ("event_source", "claim", "manual_identity_resolution"),
    )
    with pytest.raises(ValueError, match="same course"):
        resolve_event_source(
            harness.reconciler,
            ManualIdentityResolution(
                first.sources[0].key,
                second.key,
                "Reject synthetic cross-course ownership.",
            ),
        )
    assert (
        _snapshot(
            harness.database,
            ("event_source", "claim", "manual_identity_resolution"),
        )
        == before
    )
