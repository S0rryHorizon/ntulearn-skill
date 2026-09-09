from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntulearn_skill.client.contracts import AnnouncementSourceRecord
from ntulearn_skill.core import (
    AnnouncementId,
    Availability,
    CourseId,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    ChangeKind,
    DeterministicEventExtractor,
    EventReconciler,
    EventRepository,
    ObservedSourceRecord,
)
from ntulearn_skill.storage import Database, DomainRepository


@dataclass(frozen=True)
class _Harness:
    database: Database
    course: CourseId
    repository: EventRepository
    extractor: DeterministicEventExtractor
    reconciler: EventReconciler


def _harness(tmp_path: Path) -> _Harness:
    database = Database(tmp_path / "synthetic-events.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() >= 7
    course = CourseId("synthetic", "change-language-course")
    domain.put_course(course, code="PH0000", title="Synthetic Change Language")
    return _Harness(
        database,
        course,
        EventRepository(database),
        DeterministicEventExtractor(database),
        EventReconciler(database, resolver_version="change-language-v1"),
    )


def _source_time(day: int) -> SourceTime:
    instant = datetime(2030, 1, day, 8, tzinfo=UTC)
    return SourceTime(
        instant,
        instant.strftime("%Y-%m-%d %H:%M UTC"),
        "UTC",
        TemporalPrecision.EXACT_TIME,
    )


def _observe(
    harness: _Harness, remote_key: str, wording: str, ordinal: int
) -> ObservedSourceRecord:
    with harness.database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-change-language', '{}', ?, 'RUNNING')""",
            (datetime(2030, 1, ordinal, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        run_key = int(cursor.lastrowid)
    return harness.repository.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", remote_key),
            harness.course,
            "Synthetic change notice",
            wording,
            Availability.ACTIVE,
            published_at=_source_time(ordinal),
        ),
        sync_run_key=run_key,
        observed_at=datetime(2030, 2, ordinal, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("original", "qualified_change"),
    (
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 is not cancelled."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "If Quiz 1 is cancelled, we will notify you."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 may be cancelled."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 can be cancelled."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 is likely to be cancelled."),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 is unlikely to be cancelled."),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "It is possible that Quiz 1 is cancelled.",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 is expected to be cancelled.",
        ),
        ("Quiz 1 due 2030-02-10 10:00 UTC.", "Quiz 1 should be cancelled."),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Students asked whether Quiz 1 has been cancelled.",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 is not, despite the rumours repeated all week, cancelled.",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 is not moved to 2030-02-12 10:00 UTC.",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "If Quiz 1 is moved to 2030-02-12 10:00 UTC, we will notify you.",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 may be moved to 2030-02-12 10:00 UTC.",
        ),
        (
            "Tutorial 1 starts 2030-02-10 10:00 UTC.",
            "Tutorial 1 room is not changed to Synthetic Room B on 2030-02-12.",
        ),
        (
            "Tutorial 1 starts 2030-02-10 10:00 UTC.",
            "If Tutorial 1 room is changed to Synthetic Room B, we will notify you.",
        ),
        (
            "Tutorial 1 starts 2030-02-10 10:00 UTC.",
            "Tutorial 1 room may be changed to Synthetic Room B on 2030-02-12.",
        ),
    ),
    ids=(
        "not-cancelled",
        "if-cancelled",
        "may-be-cancelled",
        "can-be-cancelled",
        "likely-cancelled",
        "unlikely-cancelled",
        "possibly-cancelled",
        "expected-cancelled",
        "should-be-cancelled",
        "asked-whether-cancelled",
        "long-negated-cancellation",
        "not-moved",
        "if-moved",
        "may-be-moved",
        "venue-not-changed",
        "venue-if-changed",
        "venue-may-change",
    ),
)
def test_qualified_change_language_abstains_before_reconciliation(
    tmp_path: Path, original: str, qualified_change: str
) -> None:
    harness = _harness(tmp_path)
    first = _observe(harness, "same-source", original, 1)
    first_extraction = harness.extractor.extract_observation(first.observation.key)
    assert len(first_extraction.candidates) == 1
    qualified = _observe(harness, "same-source", qualified_change, 2)

    qualified_extraction = harness.extractor.extract_observation(qualified.observation.key)
    reconciled = harness.reconciler.reconcile_course(harness.course)

    assert qualified_extraction.candidates == ()
    assert qualified_extraction.extractor_version == "4"
    assert len(reconciled.events) == 1
    event = reconciled.events[0]
    assert event.field(CandidateFieldName.STATUS) is None
    assert all(source.change_kind is ChangeKind.NONE for source in event.sources)
    assert event.source_count == 1


@pytest.mark.parametrize(
    ("original", "affirmative_change", "expected_kind", "field", "expected"),
    (
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 cancelled.",
            ChangeKind.CANCELLATION,
            CandidateFieldName.STATUS,
            "CANCELLED",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 is cancelled.",
            ChangeKind.CANCELLATION,
            CandidateFieldName.STATUS,
            "CANCELLED",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 has been cancelled.",
            ChangeKind.CANCELLATION,
            CandidateFieldName.STATUS,
            "CANCELLED",
        ),
        (
            "Quiz 1 due 2030-02-10 10:00 UTC.",
            "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
            ChangeKind.MOVE,
            CandidateFieldName.DUE_TIME,
            "2030-02-12T10:00:00",
        ),
        (
            "Tutorial 1 starts 2030-02-10 10:00 UTC.",
            "Tutorial 1 room changed to Synthetic Room B.",
            ChangeKind.VENUE_CHANGE,
            CandidateFieldName.LOCATION,
            "Synthetic Room B",
        ),
    ),
    ids=(
        "bare-cancelled",
        "is-cancelled",
        "has-been-cancelled",
        "moved",
        "venue-changed",
    ),
)
def test_affirmative_change_language_survives_extraction_and_reconciliation(
    tmp_path: Path,
    original: str,
    affirmative_change: str,
    expected_kind: ChangeKind,
    field: CandidateFieldName,
    expected: str,
) -> None:
    harness = _harness(tmp_path)
    first = _observe(harness, "same-source", original, 1)
    harness.extractor.extract_observation(first.observation.key)
    changed = _observe(harness, "same-source", affirmative_change, 2)

    change_extraction = harness.extractor.extract_observation(changed.observation.key)
    reconciled = harness.reconciler.reconcile_course(harness.course)

    assert len(change_extraction.candidates) == 1
    assert len(reconciled.events) == 1
    event = reconciled.events[0]
    projection = event.field(field)
    assert projection is not None
    if isinstance(projection.value, dict):
        assert str(projection.value["instant"]).startswith(expected)
    else:
        assert projection.value == expected
    assert any(source.change_kind is expected_kind for source in event.sources)
