from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from reportlab.pdfgen import canvas

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
    AttachmentId,
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
    DeterministicEventExtractor,
    EventRepository,
)
from ntulearn_skill.parsers import (
    DocxParser,
    ParserOptions,
    ParserRegistry,
    PdfParser,
)
from ntulearn_skill.parsers.repository import ParseRepository
from ntulearn_skill.parsers.service import ParseService
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)


@dataclass(frozen=True)
class _Harness:
    paths: RuntimePaths
    database: Database
    store: ResourceStore
    parser: ParseService
    events: EventRepository
    extractor: DeterministicEventExtractor
    course_id: CourseId
    content_id: ContentId


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-synthetic-runtime")
    database = Database(paths.database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() >= 6
    course_id = CourseId("synthetic", "event-candidate-course")
    content_id = ContentId("synthetic", "event-candidate-content")
    domain = DomainRepository(database)
    domain.put_course(course_id, code="PH0000", title="Invented Event Course")
    domain.put_content_node(
        content_id,
        course_id=course_id,
        handler_kind="document",
        title="Invented event materials",
        position=0,
    )
    parser = ParseService(
        paths,
        resources,
        ParseRepository(database),
        ParserRegistry((PdfParser(), DocxParser())),
    )
    return _Harness(
        paths,
        database,
        store,
        parser,
        EventRepository(database),
        DeterministicEventExtractor(database),
        course_id,
        content_id,
    )


def _sync_run(database: Database) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic', '{}', ?, 'RUNNING')""",
            (datetime(2027, 1, 1, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _exact(day: int, hour: int, source_text: str) -> SourceTime:
    return SourceTime(
        datetime(2027, 2, day, hour, tzinfo=UTC),
        source_text,
        "UTC",
        TemporalPrecision.EXACT_TIME,
    )


def _date_only(source_text: str) -> SourceTime:
    return SourceTime(None, source_text, None, TemporalPrecision.DATE_ONLY)


def _pdf_pages(*pages: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    for text in pages:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return output.getvalue()


def _docx_table(*rows: tuple[str, str]) -> bytes:
    document = Document()
    table = document.add_table(rows=len(rows), cols=2)
    for row, values in zip(table.rows, rows, strict=True):
        for cell, value in zip(row.cells, values, strict=True):
            cell.text = value
    output = io.BytesIO()
    document.save(output)
    return output.getvalue()


def _ingest(harness: _Harness, payload: bytes, key: str, filename: str):
    return harness.store.ingest(
        io.BytesIO(payload),
        AttachmentId("synthetic", key),
        content_id=harness.content_id,
        sync_run_key=_sync_run(harness.database),
        display_title=f"Invented {key}",
        original_filename=filename,
    )


def _assert_single_evidence_path(candidate) -> None:
    for field in candidate.fields:
        assert bool(field.source_observation_key) != bool(field.locator_key)
        assert field.source_path


def test_conflicting_assessment_due_fields_remain_separate_and_keep_exact_source_paths(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    observation = harness.events.observe_assessment(
        AssessmentSourceRecord(
            AssessmentId("synthetic", "assessment-1"),
            harness.course_id,
            harness.content_id,
            GradingColumnId("synthetic", "grade-column-1"),
            "Assignment 1",
            AssessmentSubtype.ASSIGNMENT,
            "Use only the invented fixture.",
            Availability.ACTIVE,
            available_from=_exact(1, 1, "available 1 February 01:00 UTC"),
            available_until=_exact(20, 1, "available until 20 February 01:00 UTC"),
            open_at=_exact(2, 2, "opens 2 February 02:00 UTC"),
            close_at=_exact(11, 3, "closes 11 February 03:00 UTC"),
            due_at=_exact(10, 10, "direct due 10 February 10:00 UTC"),
            grading_due_at=_exact(12, 12, "grading due 12 February 12:00 UTC"),
            generic_due_at=_exact(13, 13, "generic due 13 February 13:00 UTC"),
        ),
        sync_run_key=_sync_run(harness.database),
    )

    result = harness.extractor.extract_observation(observation.observation.key)
    repeated = harness.extractor.extract_observation(observation.observation.key)

    assert len(result.candidates) == 3
    assert repeated.cache_hit
    assert repeated.extraction_record_key == result.extraction_record_key
    due_fields = [candidate.field(CandidateFieldName.DUE_TIME) for candidate in result.candidates]
    assert [field.original_text for field in due_fields if field is not None] == [
        "direct due 10 February 10:00 UTC",
        "grading due 12 February 12:00 UTC",
        "generic due 13 February 13:00 UTC",
    ]
    assert [field.source_path for field in due_fields if field is not None] == [
        "assessment.dueDate",
        "gradingColumn.dueDate",
        "genericReadOnlyData.dueDate",
    ]
    for candidate in result.candidates:
        _assert_single_evidence_path(candidate)
        assert {field.name for field in candidate.fields}.issuperset(
            {
                CandidateFieldName.DUE_TIME,
                CandidateFieldName.OPEN_AT,
                CandidateFieldName.CLOSE_AT,
                CandidateFieldName.AVAILABLE_FROM,
                CandidateFieldName.AVAILABLE_UNTIL,
            }
        )
        assert all(
            field.source_observation_key == observation.observation.key
            for field in candidate.fields
        )
        assert {
            field.source_path
            for field in candidate.fields
            if field.name is not CandidateFieldName.DUE_TIME
        } == {
            "title",
            "subtype",
            "available_from",
            "available_until",
            "open_at",
            "close_at",
        }


def test_schedule_and_due_endpoint_times_do_not_collapse_into_one_semantic_field(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    schedule = harness.events.observe_schedule(
        ScheduleSourceRecord(
            CalendarItemId("synthetic", "schedule-1"),
            harness.course_id,
            "Tutorial 4",
            Availability.ACTIVE,
            start_at=_exact(4, 2, "starts 4 February 02:00 UTC"),
            end_at=_exact(4, 3, "ends 4 February 03:00 UTC"),
            location="Synthetic Room 1",
        ),
        sync_run_key=_sync_run(harness.database),
    )
    due_item = harness.events.observe_due_item(
        DueSourceRecord(
            CalendarItemId("synthetic", "due-1"),
            harness.course_id,
            "Assignment 2",
            "synthetic-calendar",
            "synthetic-source-id",
            "assessment",
            Availability.ACTIVE,
            source_start_at=_exact(5, 1, "opaque startDate"),
            source_end_at=_exact(6, 1, "opaque endDate"),
            due_at=_date_only("2027-02-09"),
        ),
        sync_run_key=_sync_run(harness.database),
    )

    schedule_candidate = harness.extractor.extract_observation(schedule.observation.key).candidates[
        0
    ]
    due_candidate = harness.extractor.extract_observation(due_item.observation.key).candidates[0]

    assert schedule_candidate.field(CandidateFieldName.START_TIME) is not None
    assert schedule_candidate.field(CandidateFieldName.END_TIME) is not None
    assert schedule_candidate.field(CandidateFieldName.LOCATION) is not None
    assert schedule_candidate.field(CandidateFieldName.DUE_TIME) is None
    due = due_candidate.field(CandidateFieldName.DUE_TIME)
    assert due is not None
    assert due.precision is TemporalPrecision.DATE_ONLY
    assert due_candidate.field(CandidateFieldName.START_TIME) is None
    assert due_candidate.field(CandidateFieldName.END_TIME) is None
    assert due.source_path == "due_at"
    _assert_single_evidence_path(schedule_candidate)
    _assert_single_evidence_path(due_candidate)


def test_announcement_clause_binding_keeps_open_date_out_of_due_field(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    observation = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "announcement-1"),
            harness.course_id,
            "Assignment 3 dates",
            "Assignment opens 2027-02-01 and is due 2027-02-10.",
            Availability.ACTIVE,
            published_at=_exact(1, 0, "published 1 February 00:00 UTC"),
        ),
        sync_run_key=_sync_run(harness.database),
    )

    result = harness.extractor.extract_observation(observation.observation.key)

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    due = candidate.field(CandidateFieldName.DUE_TIME)
    published = candidate.field(CandidateFieldName.PUBLISHED_AT)
    assert due is not None and due.original_text == "2027-02-10"
    assert published is not None
    assert published.source_path == "published_at"
    assert not any(
        field.original_text == "2027-02-01"
        and field.name in {CandidateFieldName.DUE_TIME, CandidateFieldName.START_TIME}
        for field in candidate.fields
    )
    _assert_single_evidence_path(candidate)


def test_pdf_extraction_finds_multiple_mentions_without_calendar_and_keeps_physical_pages(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _pdf_pages(
            "Invented heading without an event",
            "Tutorial takes place Week 4. Assignment deadline is 2027-03-10.",
            "Test starts 10 March 2027 09:00 SGT to 10 March 2027 10:30 SGT.",
        ),
        "events-pdf",
        "events.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = harness.extractor.extract_resource_version(written.version.key)

    assert parsed.chunks
    assert len(result.candidates) == 3
    assert [candidate.ordinal for candidate in result.candidates] == [0, 1, 2]
    event_type_fields = [
        candidate.field(CandidateFieldName.EVENT_TYPE) for candidate in result.candidates
    ]
    assert all(field is not None for field in event_type_fields)
    assert [field.value for field in event_type_fields if field is not None] == [
        "tutorial",
        "assignment_due",
        "test",
    ]
    tutorial_start = result.candidates[0].field(CandidateFieldName.START_TIME)
    assignment_due = result.candidates[1].field(CandidateFieldName.DUE_TIME)
    test_start = result.candidates[2].field(CandidateFieldName.START_TIME)
    test_end = result.candidates[2].field(CandidateFieldName.END_TIME)
    assert tutorial_start is not None and tutorial_start.precision is TemporalPrecision.WEEK_ONLY
    assert assignment_due is not None and assignment_due.precision is TemporalPrecision.DATE_ONLY
    assert test_start is not None and test_start.precision is TemporalPrecision.EXACT_TIME
    assert test_end is not None and test_end.precision is TemporalPrecision.EXACT_TIME
    with harness.database.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM schedule_item").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM due_item").fetchone()[0] == 0
        locator_rows = [
            connection.execute(
                """SELECT version_key, format, physical_page_index, structured_json
                FROM source_locator WHERE locator_key = ?""",
                (candidate.fields[0].locator_key,),
            ).fetchone()
            for candidate in result.candidates
        ]
    assert [row["physical_page_index"] for row in locator_rows] == [1, 1, 2]
    assert all(row["version_key"] == written.version.key for row in locator_rows)
    assert all(row["format"] == "pdf" for row in locator_rows)
    for candidate in result.candidates:
        _assert_single_evidence_path(candidate)
        assert {field.source_path for field in candidate.fields} == {"text"}
        assert len({field.locator_key for field in candidate.fields}) == 1


def test_docx_table_candidate_resolves_to_structured_table_locator(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _docx_table(
            ("Assessment", "Timing"),
            ("Quiz 2", "2027-03-01"),
        ),
        "events-docx",
        "events.docx",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = harness.extractor.extract_resource_version(written.version.key)

    assert len(parsed.chunks) == 1
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    event_type = candidate.field(CandidateFieldName.EVENT_TYPE)
    start = candidate.field(CandidateFieldName.START_TIME)
    assert event_type is not None and event_type.value == "quiz"
    assert start is not None and start.precision is TemporalPrecision.DATE_ONLY
    _assert_single_evidence_path(candidate)
    assert {field.source_path for field in candidate.fields} == {"text"}
    with harness.database.connect() as connection:
        locator = connection.execute(
            """SELECT version_key, format, docx_element_index, docx_table_index,
                      structured_json
            FROM source_locator WHERE locator_key = ?""",
            (candidate.fields[0].locator_key,),
        ).fetchone()
    assert locator["version_key"] == written.version.key
    assert locator["format"] == "docx"
    assert locator["docx_element_index"] == 0
    assert locator["docx_table_index"] == 0
    assert '"table_index":0' in locator["structured_json"]


def test_extraction_is_idempotent_per_parse_but_new_parse_input_is_reprocessed(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _pdf_pages("Exam is 2027-03-15."),
        "parse-identity",
        "parse-identity.pdf",
    )
    first_parse = harness.parser.parse_version(written.version.key)
    first = harness.extractor.extract_resource_version(written.version.key)
    repeated = harness.extractor.extract_resource_version(written.version.key)
    second_parse = harness.parser.parse_version(
        written.version.key,
        options=ParserOptions(low_text_character_threshold=0),
    )
    second = harness.extractor.extract_resource_version(written.version.key)
    second_repeated = harness.extractor.extract_resource_version(written.version.key)

    assert repeated.cache_hit
    assert repeated.extraction_record_key == first.extraction_record_key
    assert second_parse.document.key != first_parse.document.key
    assert not second.cache_hit
    assert second.extraction_record_key != first.extraction_record_key
    assert second_repeated.cache_hit
    assert second_repeated.extraction_record_key == second.extraction_record_key
    with harness.database.connect() as connection:
        inputs = connection.execute(
            """SELECT extraction_record_key, parse_key FROM extraction_record
            WHERE version_key = ? ORDER BY extraction_record_key""",
            (written.version.key,),
        ).fetchall()
    assert [(row["extraction_record_key"], row["parse_key"]) for row in inputs] == [
        (first.extraction_record_key, first_parse.document.key),
        (second.extraction_record_key, second_parse.document.key),
    ]
