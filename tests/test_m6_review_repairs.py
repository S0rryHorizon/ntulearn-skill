from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

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
    Availability,
    CalendarItemId,
    ContentId,
    CourseId,
    GradingColumnId,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    DeterministicEventExtractor,
    EventRepository,
)
from ntulearn_skill.storage import Database, DomainRepository


def _setup(
    database_path: Path,
) -> tuple[
    Database,
    EventRepository,
    DeterministicEventExtractor,
    CourseId,
    CourseId,
    ContentId,
    ContentId,
]:
    database = Database(database_path)
    domain = DomainRepository(database)
    assert domain.initialize() == 9
    course_a = CourseId("synthetic", "course-a")
    course_b = CourseId("synthetic", "course-b")
    content_a = ContentId("synthetic", "content-a")
    content_b = ContentId("synthetic", "content-b")
    domain.put_course(course_a, code="PH0000", title="Invented Course A")
    domain.put_course(course_b, code="CS0000", title="Invented Course B")
    domain.put_content_node(
        content_a,
        course_id=course_a,
        handler_kind="assessment",
        title="Invented assessment A",
        position=0,
    )
    domain.put_content_node(
        content_b,
        course_id=course_b,
        handler_kind="assessment",
        title="Invented assessment B",
        position=0,
    )
    return (
        database,
        EventRepository(database),
        DeterministicEventExtractor(database),
        course_a,
        course_b,
        content_a,
        content_b,
    )


def _sync_run(database: Database) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-review', '{}', ?, 'RUNNING')""",
            (datetime(2030, 1, 1, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _observe_announcement(
    repository: EventRepository,
    database: Database,
    course_id: CourseId,
    remote_value: str,
    body: str,
):
    return repository.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", remote_value),
            course_id,
            "Invented event notice",
            body,
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(database),
        observed_at=datetime(2030, 1, 2, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("wording", "expected"),
    (
        (
            "Test starts 10 February 2030 14:00 +08:00.",
            datetime(2030, 2, 10, 6, tzinfo=UTC),
        ),
        (
            "Test starts February 10, 2030 14:00 -05:30.",
            datetime(2030, 2, 10, 19, 30, tzinfo=UTC),
        ),
    ),
)
def test_named_exact_dates_apply_positive_and_negative_numeric_offsets(
    tmp_path: Path, wording: str, expected: datetime
) -> None:
    database, repository, extractor, course_a, _, _, _ = _setup(tmp_path / "events.sqlite3")
    observation = _observe_announcement(
        repository, database, course_a, f"named-{expected.hour}", wording
    )

    result = extractor.extract_observation(observation.observation.key)

    assert len(result.candidates) == 1
    start = result.candidates[0].field(CandidateFieldName.START_TIME)
    assert start is not None
    assert start.source_timezone in {"+08:00", "-05:30"}
    assert isinstance(start.value, dict)
    instant = start.value["instant"]
    assert isinstance(instant, str)
    assert datetime.fromisoformat(instant.replace("Z", "+00:00")) == expected


def test_named_numeric_offset_matches_sgt_for_the_same_instant(tmp_path: Path) -> None:
    database, repository, extractor, course_a, _, _, _ = _setup(tmp_path / "events.sqlite3")
    numeric = _observe_announcement(
        repository,
        database,
        course_a,
        "numeric-zone",
        "Test starts 10 February 2030 14:00 +08:00.",
    )
    named = _observe_announcement(
        repository,
        database,
        course_a,
        "named-zone",
        "Test starts 10 February 2030 14:00 SGT.",
    )

    numeric_start = (
        extractor.extract_observation(numeric.observation.key)
        .candidates[0]
        .field(CandidateFieldName.START_TIME)
    )
    named_start = (
        extractor.extract_observation(named.observation.key)
        .candidates[0]
        .field(CandidateFieldName.START_TIME)
    )

    assert numeric_start is not None and named_start is not None
    assert isinstance(numeric_start.value, dict) and isinstance(named_start.value, dict)
    assert numeric_start.value["instant"] == named_start.value["instant"]
    assert numeric_start.source_timezone == "+08:00"
    assert named_start.source_timezone == "SGT"


@pytest.mark.parametrize(
    "wording",
    (
        "Quiz 1 opens on 2030-02-01 and starts on 2030-02-10.",
        "Quiz 1 was announced on 2030-02-01 and will take place on 2030-02-10.",
    ),
)
def test_explicit_start_clause_wins_over_earlier_non_event_date(
    tmp_path: Path, wording: str
) -> None:
    database, repository, extractor, course_a, _, _, _ = _setup(tmp_path / "events.sqlite3")
    observation = _observe_announcement(
        repository, database, course_a, f"clause-{len(wording)}", wording
    )

    result = extractor.extract_observation(observation.observation.key)

    assert len(result.candidates) == 1
    start = result.candidates[0].field(CandidateFieldName.START_TIME)
    assert start is not None
    assert start.original_text == "2030-02-10"
    assert isinstance(start.value, dict)
    assert start.value["date"] == "2030-02-10"


@pytest.mark.parametrize(
    "wording",
    (
        "Quiz 1 opens on 2030-02-01.",
        "Quiz 1 was announced on 2030-02-01.",
    ),
)
def test_availability_and_publication_dates_are_not_assigned_as_start_times(
    tmp_path: Path, wording: str
) -> None:
    database, repository, extractor, course_a, _, _, _ = _setup(tmp_path / "events.sqlite3")
    observation = _observe_announcement(
        repository, database, course_a, f"non-start-{len(wording)}", wording
    )

    result = extractor.extract_observation(observation.observation.key)

    assert result.candidates == ()


def _observe_kind(
    repository: EventRepository,
    database: Database,
    kind: str,
    course_id: CourseId,
    content_id: ContentId,
    *,
    observed_at: datetime,
):
    run_key = _sync_run(database)
    if kind == "announcement":
        return repository.observe_announcement(
            AnnouncementSourceRecord(
                AnnouncementId("synthetic", "shared-announcement"),
                course_id,
                f"Announcement for {course_id.value}",
                "Invented body.",
                Availability.ACTIVE,
            ),
            sync_run_key=run_key,
            observed_at=observed_at,
        )
    if kind == "assessment":
        return repository.observe_assessment(
            AssessmentSourceRecord(
                AssessmentId("synthetic", "shared-assessment"),
                course_id,
                content_id,
                None,
                f"Assessment for {course_id.value}",
                AssessmentSubtype.QUIZ,
                "Invented instructions.",
                Availability.ACTIVE,
            ),
            sync_run_key=run_key,
            observed_at=observed_at,
        )
    if kind == "schedule_item":
        return repository.observe_schedule(
            ScheduleSourceRecord(
                CalendarItemId("synthetic", "shared-schedule"),
                course_id,
                f"Schedule for {course_id.value}",
                Availability.ACTIVE,
            ),
            sync_run_key=run_key,
            observed_at=observed_at,
        )
    return repository.observe_due_item(
        DueSourceRecord(
            CalendarItemId("synthetic", "shared-due"),
            course_id,
            f"Due item for {course_id.value}",
            "invented-calendar",
            None,
            None,
            Availability.ACTIVE,
        ),
        sync_run_key=run_key,
        observed_at=observed_at,
    )


@pytest.mark.parametrize(
    ("kind", "data_kind"),
    (
        ("announcement", "announcement"),
        ("assessment", "assessment"),
        ("schedule_item", "schedule"),
        ("due_item", "due_item"),
    ),
)
def test_source_identity_cannot_move_between_courses_and_failed_observation_rolls_back(
    tmp_path: Path, kind: str, data_kind: str
) -> None:
    database, repository, _, course_a, course_b, content_a, content_b = _setup(
        tmp_path / "events.sqlite3"
    )
    first = _observe_kind(
        repository,
        database,
        kind,
        course_a,
        content_a,
        observed_at=datetime(2030, 1, 2, tzinfo=UTC),
    )
    with database.connect() as connection:
        source_before = connection.execute(
            "SELECT last_observed_at FROM source_object WHERE source_object_key = ?",
            (first.observation.source_object_key,),
        ).fetchone()
    assert source_before is not None

    with pytest.raises(ValueError, match="another course"):
        _observe_kind(
            repository,
            database,
            kind,
            course_b,
            content_b,
            observed_at=datetime(2030, 1, 3, tzinfo=UTC),
        )

    with database.connect() as connection:
        current = connection.execute(
            f"SELECT course_key, current_observation_key FROM {kind} WHERE source_object_key = ?",
            (first.observation.source_object_key,),
        ).fetchone()
        observations = connection.execute(
            "SELECT observation_key, data_kind FROM source_observation WHERE source_object_key = ?",
            (first.observation.source_object_key,),
        ).fetchall()
        source_after = connection.execute(
            "SELECT last_observed_at FROM source_object WHERE source_object_key = ?",
            (first.observation.source_object_key,),
        ).fetchone()
        course_a_key = DomainRepository._lookup_domain_key(connection, course_a, "course", "course")
    assert current is not None and source_after is not None
    assert int(current["course_key"]) == course_a_key
    assert int(current["current_observation_key"]) == first.observation.key
    assert [(int(row["observation_key"]), str(row["data_kind"])) for row in observations] == [
        (first.observation.key, data_kind)
    ]
    assert source_after["last_observed_at"] == source_before["last_observed_at"]
    assert repository.get_observation(first.observation.key) == first.observation
    assert database.integrity_check()


def test_calendar_item_shared_namespace_rejects_cross_kind_cross_course_reuse(
    tmp_path: Path,
) -> None:
    database, repository, _, course_a, course_b, _, _ = _setup(tmp_path / "events.sqlite3")
    remote_id = CalendarItemId("synthetic", "shared-calendar-item")
    first = repository.observe_schedule(
        ScheduleSourceRecord(remote_id, course_a, "Invented tutorial", Availability.ACTIVE),
        sync_run_key=_sync_run(database),
        observed_at=datetime(2030, 1, 2, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="another course"):
        repository.observe_due_item(
            DueSourceRecord(
                remote_id,
                course_b,
                "Invented deadline",
                "invented-calendar",
                None,
                None,
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(database),
            observed_at=datetime(2030, 1, 3, tzinfo=UTC),
        )

    with database.connect() as connection:
        observations = connection.execute(
            "SELECT observation_key, data_kind FROM source_observation WHERE source_object_key = ?",
            (first.observation.source_object_key,),
        ).fetchall()
        due_count = connection.execute("SELECT COUNT(*) FROM due_item").fetchone()[0]
    assert [(int(row["observation_key"]), str(row["data_kind"])) for row in observations] == [
        (first.observation.key, "schedule")
    ]
    assert due_count == 0
    assert database.integrity_check()


def test_assessment_content_must_belong_to_requested_course_without_partial_provenance(
    tmp_path: Path,
) -> None:
    database, repository, _, course_a, _, _, content_b = _setup(tmp_path / "events.sqlite3")

    with pytest.raises(ValueError, match="assessment content belongs to another course"):
        repository.observe_assessment(
            AssessmentSourceRecord(
                AssessmentId("synthetic", "wrong-content-owner"),
                course_a,
                content_b,
                GradingColumnId("synthetic", "unused-grading-column"),
                "Invented cross-course assessment",
                AssessmentSubtype.ASSIGNMENT,
                "Invented instructions.",
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(database),
            observed_at=datetime(2030, 1, 2, tzinfo=UTC),
        )

    with database.connect() as connection:
        source_counts = dict(
            connection.execute(
                """SELECT object_kind, COUNT(*) FROM source_object
                WHERE object_kind IN ('assessment', 'grading_column') GROUP BY object_kind"""
            ).fetchall()
        )
        observation_count = connection.execute(
            "SELECT COUNT(*) FROM source_observation"
        ).fetchone()[0]
        assessment_count = connection.execute("SELECT COUNT(*) FROM assessment").fetchone()[0]
    assert source_counts == {}
    assert observation_count == 0
    assert assessment_count == 0
    assert database.integrity_check()
