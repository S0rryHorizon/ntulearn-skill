from __future__ import annotations

import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from PIL import Image
from reportlab.lib.utils import ImageReader
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


def _pdf_multiline(*lines: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    text = document.beginText(72, 720)
    for line in lines:
        text.textLine(line)
    document.drawText(text)
    document.save()
    return output.getvalue()


def _text_rich_pdf_with_embedded_image() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(
        72,
        720,
        "Assignment deadline is 2032-05-01. This synthetic paragraph supplies native text.",
    )
    pixels = Image.new("RGB", (8, 8), color=(31, 63, 95))
    encoded = io.BytesIO()
    pixels.save(encoded, format="PNG")
    encoded.seek(0)
    document.drawImage(ImageReader(encoded), 72, 600, width=80, height=80)
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
    assert all(field.source_path.startswith("text") for field in candidate.fields)
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


def test_multiline_announcement_associates_date_time_and_venue_without_guessing_timezone(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    observed = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "multiline-announcement"),
            harness.course_id,
            "Synthetic Midterm Briefing",
            (
                "Date: Wednesday, 10th March 2032\n"
                "Time: 2.30 to 4.00 pm\n"
                "Venue\n"
                "Invented Hall 7\n"
                "Bring the synthetic reference sheet."
            ),
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database),
    )

    result = harness.extractor.extract_observation(observed.observation.key)

    assert result.extractor_version == "4"
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    start = candidate.field(CandidateFieldName.START_TIME)
    end = candidate.field(CandidateFieldName.END_TIME)
    location = candidate.field(CandidateFieldName.LOCATION)
    assert start is not None and end is not None and location is not None
    assert start.precision is TemporalPrecision.UNKNOWN
    assert end.precision is TemporalPrecision.UNKNOWN
    assert start.source_timezone is None and end.source_timezone is None
    assert start.value["instant"] is None and start.value["local_time"] == "14:30"  # type: ignore[index]
    assert end.value["instant"] is None and end.value["local_time"] == "16:00"  # type: ignore[index]
    assert start.original_text == "2.30 to 4.00 pm"
    assert start.value["date_source_text"] == "Wednesday, 10th March 2032"  # type: ignore[index]
    assert location.value == "Invented Hall 7"
    assert location.original_text == "Venue\nInvented Hall 7"
    assert len({field.source_observation_key for field in candidate.fields}) == 1
    assert all(field.locator_key is None for field in candidate.fields)
    assert len({field.source_path for field in candidate.fields}) >= 3


def test_pdf_list_context_stops_at_next_event_and_keeps_exact_page_locator(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _pdf_multiline(
            "Invented assessment timeline",
            "First",
            "Quiz",
            "Date: Friday, 12th April 2030",
            "Time: 09.30 am to 10.45 am",
            "Second Assignment",
            "Deadline",
            "by Monday, 15th April 2030",
        ),
        "contextual-events-pdf",
        "contextual-events.pdf",
    )
    harness.parser.parse_version(written.version.key)

    result = harness.extractor.extract_resource_version(written.version.key)

    assert len(result.candidates) == 2
    quiz, assignment = result.candidates
    assert quiz.field(CandidateFieldName.TITLE).value == "First Quiz"  # type: ignore[union-attr]
    assert quiz.field(CandidateFieldName.START_TIME).value["local_time"] == "09:30"  # type: ignore[index,union-attr]
    assert quiz.field(CandidateFieldName.END_TIME).value["local_time"] == "10:45"  # type: ignore[index,union-attr]
    assert assignment.field(CandidateFieldName.DUE_TIME).value["date"] == "2030-04-15"  # type: ignore[index,union-attr]
    assert quiz.field(CandidateFieldName.DUE_TIME) is None
    assert assignment.field(CandidateFieldName.START_TIME) is None
    for candidate in result.candidates:
        assert len({field.locator_key for field in candidate.fields}) == 1
        assert all(field.source_observation_key is None for field in candidate.fields)
        with harness.database.connect() as connection:
            locator = connection.execute(
                "SELECT version_key, physical_page_index FROM source_locator WHERE locator_key = ?",
                (candidate.fields[0].locator_key,),
            ).fetchone()
        assert locator["version_key"] == written.version.key
        assert locator["physical_page_index"] == 0


def test_bare_page_number_is_not_promoted_to_event_ordinal(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _pdf_multiline(
            "7",
            "Synthetic Assignment Brief",
            "Deadline",
            "by 18 August 2034",
        ),
        "page-number-before-event",
        "page-number-before-event.pdf",
    )
    harness.parser.parse_version(written.version.key)

    candidate = harness.extractor.extract_resource_version(written.version.key).candidates[0]

    title = candidate.field(CandidateFieldName.TITLE)
    assert title is not None and title.value == "Synthetic Assignment Brief"
    assert title.original_text == "Synthetic Assignment Brief"


def test_contextual_extraction_abstains_on_publication_office_placeholder_and_negation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    bodies = (
        (
            "negative-publication",
            "Synthetic Lecture Notes",
            (
                "Published: Friday, 12th April 2030\n"
                "Instructor office location: Invented Faculty Office\n"
                "Date: __________\n"
                "The schedule will be arranged later."
            ),
        ),
        (
            "negative-negation",
            "Synthetic Quiz Notice",
            "The quiz will not be held on Friday, 19th April 2030.",
        ),
        (
            "negative-missing-year",
            "Synthetic Exam Notice",
            "Date: 21 April\nTime: 2.30 pm\nVenue: Invented Hall 4",
        ),
    )
    for remote_key, title, body in bodies:
        observed = harness.events.observe_announcement(
            AnnouncementSourceRecord(
                AnnouncementId("synthetic", remote_key),
                harness.course_id,
                title,
                body,
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(harness.database),
        )
        assert harness.extractor.extract_observation(observed.observation.key).candidates == ()


def test_context_window_does_not_merge_distinct_events(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    observed = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "distinct-events"),
            harness.course_id,
            "Synthetic course update",
            (
                "Quiz One\n"
                "Date: Monday, 6th May 2030\n"
                "Assignment Two\n"
                "Deadline: Friday, 10th May 2030"
            ),
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database),
    )

    result = harness.extractor.extract_observation(observed.observation.key)

    assert len(result.candidates) == 2
    quiz, assignment = result.candidates
    assert quiz.field(CandidateFieldName.START_TIME).value["date"] == "2030-05-06"  # type: ignore[index,union-attr]
    assert quiz.field(CandidateFieldName.DUE_TIME) is None
    assert assignment.field(CandidateFieldName.DUE_TIME).value["date"] == "2030-05-10"  # type: ignore[index,union-attr]
    assert assignment.field(CandidateFieldName.START_TIME) is None


def test_context_uses_first_event_date_and_ignores_later_labeled_assertions(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    cases = (
        (
            "competing-revision",
            "Synthetic Quiz Notice",
            "Date: 3 June 2033\nRevision session: 8 June 2033\nTime: 10:00 to 11:00 UTC",
            "2033-06-03",
        ),
        (
            "competing-feedback",
            "Synthetic Quiz Update",
            "Date: 4 June 2033\nFeedback deadline: 9 June 2033",
            "2033-06-04",
        ),
        (
            "later-update-word",
            "Synthetic Lecture Notice",
            "Date: 5 June 2033\nUpdated syllabus published separately.",
            "2033-06-05",
        ),
    )
    for remote_key, title, body, expected_date in cases:
        observed = harness.events.observe_announcement(
            AnnouncementSourceRecord(
                AnnouncementId("synthetic", remote_key),
                harness.course_id,
                title,
                body,
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(harness.database),
        )
        candidate = harness.extractor.extract_observation(observed.observation.key).candidates[0]
        start = candidate.field(CandidateFieldName.START_TIME)
        assert start is not None and start.value["date"] == expected_date  # type: ignore[index]
        assert start.precision is TemporalPrecision.DATE_ONLY
        assert candidate.field(CandidateFieldName.END_TIME) is None
        assert candidate.field(CandidateFieldName.DUE_TIME) is None


def test_established_event_assertion_stops_before_independent_sentences(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    cases = (
        (
            "later-revision-session",
            "Synthetic Quiz Notice",
            "Quiz on 6 May 2030. Revision session starts 7 May 2030 10:00 UTC.",
            CandidateFieldName.START_TIME,
            "2030-05-06",
        ),
        (
            "later-doors-open",
            "Synthetic Test Notice",
            "Test on 8 May 2030. Doors open at 08:00 UTC.",
            CandidateFieldName.START_TIME,
            "2030-05-08",
        ),
        (
            "later-reference-deadline",
            "Synthetic Assignment Notice",
            ("Assignment deadline 9 May 2030. Reference materials are not due until 10 May 2030."),
            CandidateFieldName.DUE_TIME,
            "2030-05-09",
        ),
    )
    for remote_key, title, body, field_name, expected_date in cases:
        observed = harness.events.observe_announcement(
            AnnouncementSourceRecord(
                AnnouncementId("synthetic", remote_key),
                harness.course_id,
                title,
                body,
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(harness.database),
        )
        candidate = harness.extractor.extract_observation(observed.observation.key).candidates[0]
        temporal = candidate.field(field_name)
        assert temporal is not None and temporal.value["date"] == expected_date  # type: ignore[index]
        assert temporal.precision is TemporalPrecision.DATE_ONLY
        assert candidate.field(CandidateFieldName.END_TIME) is None


def test_ambiguous_dot_clock_ranges_do_not_create_clock_claims(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    cases = (
        (
            "descending-shared-meridiem",
            "Synthetic Test Notice",
            "Date: 6 June 2033\nTime: 11.00 to 1.00 pm",
        ),
        (
            "unbounded-marks-range",
            "Synthetic Exam Notice",
            "Date: 7 June 2033\nMarks range: 2.30 to 4.00",
        ),
    )
    for remote_key, title, body in cases:
        observed = harness.events.observe_announcement(
            AnnouncementSourceRecord(
                AnnouncementId("synthetic", remote_key),
                harness.course_id,
                title,
                body,
                Availability.ACTIVE,
            ),
            sync_run_key=_sync_run(harness.database),
        )
        candidate = harness.extractor.extract_observation(observed.observation.key).candidates[0]
        start = candidate.field(CandidateFieldName.START_TIME)
        assert start is not None and start.precision is TemporalPrecision.DATE_ONLY
        assert candidate.field(CandidateFieldName.END_TIME) is None


def test_assignment_due_context_rejects_availability_dates_but_accepts_wrapped_date(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    availability = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "upload-availability"),
            harness.course_id,
            "Synthetic Assignment Presentation Files",
            (
                "Submit by the separately stated due dates.\n"
                "The upload portal will be available from 2 July 2033, 3 July 2033, "
                "and 4 July 2033, respectively."
            ),
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database),
    )
    wrapped = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "wrapped-topic-date"),
            harness.course_id,
            "Synthetic Assignment Topic Selection",
            "Choose an invented topic according to\nyour preference, by 23 November\n2034.",
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database),
    )

    assert harness.extractor.extract_observation(availability.observation.key).candidates == ()
    candidate = harness.extractor.extract_observation(wrapped.observation.key).candidates[0]
    due = candidate.field(CandidateFieldName.DUE_TIME)
    assert due is not None and due.value["date"] == "2034-11-23"  # type: ignore[index]
    assert "+" in due.source_path


def test_context_does_not_promote_staff_office_to_event_location(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    observed = harness.events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "staff-office-location"),
            harness.course_id,
            "Synthetic Test Notice",
            "Date: Tuesday, 7th May 2030\nLocation: Instructor Office 4",
            Availability.ACTIVE,
        ),
        sync_run_key=_sync_run(harness.database),
    )

    candidate = harness.extractor.extract_observation(observed.observation.key).candidates[0]

    assert candidate.field(CandidateFieldName.START_TIME) is not None
    assert candidate.field(CandidateFieldName.LOCATION) is None


def test_text_rich_pdf_with_image_reports_native_extraction_limit(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written = _ingest(
        harness,
        _text_rich_pdf_with_embedded_image(),
        "native-plus-image",
        "native-plus-image.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = harness.extractor.extract_resource_version(written.version.key)

    assert parsed.document.coverage.value == "COMPLETE"
    assert len(result.candidates) == 1
    with harness.database.connect() as connection:
        extraction = connection.execute(
            """SELECT status, warning_codes_json FROM extraction_record
            WHERE extraction_record_key = ?""",
            (result.extraction_record_key,),
        ).fetchone()
    assert extraction["status"] == "PARTIAL"
    assert "embedded_visual_content_not_extracted" in json.loads(extraction["warning_codes_json"])


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
