from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from ntulearn_skill.client import (
    AuthorizedReadSession,
    NtulearnSourceAdapter,
    PageRequest,
    ReadOnlyTransport,
    ReadPurpose,
    ResolvedReadRequest,
    WireResponse,
)
from ntulearn_skill.core import CourseId, Coverage
from ntulearn_skill.events import (
    CandidateFieldName,
    DeterministicEventExtractor,
    EventRepository,
)
from ntulearn_skill.search import (
    SearchEntityKind,
    SearchFilters,
    SearchQuery,
    SearchService,
    SourceReferenceKind,
)
from ntulearn_skill.storage import Database, DomainRepository
from ntulearn_skill.sync import ScopeResult, SyncRunRecorder


def test_adapter_observations_extract_candidates_and_refresh_fts_transactionally(
    tmp_path: Path,
) -> None:
    course_path = "/learn/api/v1/users/me/memberships"
    content_path = "/learn/api/v1/courses/course-alpha/contents/ROOT/children"
    announcement_path = "/learn/api/v1/courses/course-alpha/announcements"
    assessment_path = "/learn/api/v1/courses/course-alpha/contents/assessment-one"
    payloads: dict[str, dict[str, object]] = {
        course_path: {
            "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
            "results": [
                {
                    "isAvailable": True,
                    "course": {
                        "id": "course-alpha",
                        "courseId": "PH0000",
                        "displayName": "Example Physics Course",
                    },
                }
            ],
        },
        content_path: {
            "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
            "results": [
                {
                    "id": "assessment-one",
                    "courseId": "course-alpha",
                    "title": "Synthetic Term Test",
                    "position": 0,
                    "contentHandler": "resource/x-bb-asmt-test-link",
                    "contentDetail": {},
                    "visibility": "VISIBLE",
                }
            ],
        },
        announcement_path: {
            "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
            "results": [
                {
                    "id": "announcement-one",
                    "title": "Assignment reminder",
                    "body": {"rawText": "Assignment 1 is due 2030-02-10 23:59 +08:00."},
                    "createdDate": "2030-01-02T09:00:00+0800",
                    "startDateRestriction": "2030-01-03T09:00:00+08:00:30",
                    "visibility": "VISIBLE",
                }
            ],
        },
        assessment_path: {
            "id": "assessment-one",
            "title": "Synthetic Term Test",
            "contentHandler": "resource/x-bb-asmt-test-link",
            "contentDetail": {
                "resource/x-bb-asmt-test-link": {
                    "test": {
                        "assessment": {
                            "id": "assessment-remote-one",
                            "instructions": {"rawText": "Revise the invented optics unit."},
                        },
                        "gradingColumn": {
                            "id": "grading-one",
                            "dueDate": "2030-03-05T12:00:00+08:00",
                        },
                    }
                }
            },
            "genericReadOnlyData": {"dueDate": "2030-03-06T12:00:00+08:00"},
            "visibility": "VISIBLE",
        },
    }

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        assert forward_credentials
        return WireResponse(200, json_body=payloads[request.target])

    database = Database(tmp_path / "private" / "metadata.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() == 6
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor), source_origin="https://learn.example.invalid"
    )
    session = AuthorizedReadSession("ntulearn", frozenset(ReadPurpose))
    course_source = adapter.list_courses(session, PageRequest()).items[0]
    domain.put_course(
        course_source.remote_id,
        code=course_source.code,
        title=course_source.title,
        availability=course_source.availability,
    )
    content_source = adapter.list_content(
        session, course_source.remote_id, None, PageRequest()
    ).items[0]
    domain.put_content_node(
        content_source.remote_id,
        course_id=course_source.remote_id,
        handler_kind=content_source.handler_kind,
        title=content_source.title,
        position=content_source.position,
        availability=content_source.availability,
    )

    recorder = SyncRunRecorder(database, "ntulearn")
    run_key = recorder.start(mode="event-sources", requested_scope={"synthetic": True})
    repository = EventRepository(database)
    extractor = DeterministicEventExtractor(database)
    announcement = adapter.list_announcements(
        session, course_source.remote_id, PageRequest()
    ).items[0]
    assert announcement.created_at is not None
    assert announcement.created_at.source_timezone == "+0800"
    assert announcement.available_from is not None
    assert announcement.available_from.source_timezone == "+08:00:30"
    assessment = adapter.get_assessment(session, course_source.remote_id, content_source.remote_id)
    announcement_observation = repository.observe_announcement(
        announcement,
        sync_run_key=run_key,
        observed_at=datetime(2030, 1, 4, tzinfo=UTC),
    )
    assessment_observation = repository.observe_assessment(
        assessment,
        sync_run_key=run_key,
        observed_at=datetime(2030, 1, 4, tzinfo=UTC),
    )
    recorder.record_scope(
        run_key,
        ScopeResult(
            "ntulearn",
            course_source.remote_id,
            "announcements",
            Coverage.COMPLETE,
            1,
            1,
            True,
            None,
            (),
            datetime(2030, 1, 4, tzinfo=UTC),
        ),
    )
    recorder.record_scope(
        run_key,
        ScopeResult(
            "ntulearn",
            course_source.remote_id,
            "assessments",
            Coverage.PARTIAL,
            1,
            1,
            False,
            None,
            (),
            datetime(2030, 1, 4, tzinfo=UTC),
        ),
    )

    announcement_result = extractor.extract_observation(announcement_observation.observation.key)
    assessment_result = extractor.extract_observation(assessment_observation.observation.key)

    announcement_due = announcement_result.candidates[0].field(CandidateFieldName.DUE_TIME)
    assert announcement_due is not None
    assert announcement_due.source_observation_key == announcement_observation.observation.key
    assert announcement_due.locator_key is None
    assert {
        candidate.field(CandidateFieldName.DUE_TIME).source_path  # type: ignore[union-attr]
        for candidate in assessment_result.candidates
    } == {"gradingColumn.dueDate", "genericReadOnlyData.dueDate"}

    search = SearchService(database)
    announcement_search = search.search(
        SearchQuery(
            "Assignment reminder",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.ANNOUNCEMENT})),
        )
    )
    announcement_hit = announcement_search.items[0]
    assessment_search = search.search(
        SearchQuery(
            "invented optics",
            SearchFilters(
                course=CourseId("ntulearn", "course-alpha"),
                entity_kinds=frozenset({SearchEntityKind.ASSESSMENT}),
            ),
        )
    )
    assessment_hit = assessment_search.items[0]
    assert announcement_search.completeness is Coverage.COMPLETE
    assert assessment_search.completeness is Coverage.PARTIAL
    assert search.resolve_source(announcement_hit.source).object_kind == "announcement"
    assert search.resolve_source(assessment_hit.source).object_kind == "assessment"
    assert announcement_hit.source.kind is SourceReferenceKind.SOURCE_OBSERVATION

    first_reference = announcement_hit.source
    announcement_payload = payloads[announcement_path]["results"]
    assert isinstance(announcement_payload, list)
    announcement_payload[0] = {
        "id": "announcement-one",
        "title": "Revised assignment reminder",
        "body": {
            "rawText": (
                "<p>Assignment 1 revised due 2030-02-11 23:59 +08:00. "
                '<a href="https://files.example.invalid/brief?token=SYNTHETIC-SECRET">'
                "Open brief</a><img "
                'src="https://files.example.invalid/image?X-Amz-Signature=IMG-SECRET">'
                "</p><p>Read https://files.example.invalid/manual?"
                "X-Amz-Signature=LITERAL-SECRET before class.</p>"
                "<p>Quiz due 2030-02-30.</p>"
            )
        },
        "visibility": "VISIBLE",
    }
    second_run = recorder.start(mode="event-sources", requested_scope={"synthetic": True})
    revised = adapter.list_announcements(session, course_source.remote_id, PageRequest()).items[0]
    revised_observation = repository.observe_announcement(
        revised,
        sync_run_key=second_run,
        observed_at=datetime(2030, 1, 5, tzinfo=UTC),
    )

    assert "SYNTHETIC-SECRET" not in revised_observation.observation.raw_wording
    assert "IMG-SECRET" not in revised_observation.observation.raw_wording
    assert "LITERAL-SECRET" not in revised_observation.observation.raw_wording
    assert "href=" not in revised_observation.observation.raw_wording
    assert "Open brief" in revised_observation.observation.raw_wording
    revised_extraction = extractor.extract_observation(revised_observation.observation.key)
    assert all(
        field.original_text != "2030-02-30"
        for candidate in revised_extraction.candidates
        for field in candidate.fields
    )
    resolved_first = search.resolve_source(first_reference)
    assert resolved_first.reference == first_reference
    assert resolved_first.observation is not None
    assert resolved_first.observation["body"] == ("Assignment 1 is due 2030-02-10 23:59 +08:00.")
    assert not search.search(
        SearchQuery(
            "2030-02-10",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.ANNOUNCEMENT})),
        )
    ).items
    revised_hit = search.search(
        SearchQuery(
            "Revised assignment",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.ANNOUNCEMENT})),
        )
    ).items[0]
    assert revised_hit.source.key == revised_observation.observation.key
    assert revised_hit.source != first_reference
    connection = database.connect()
    try:
        durable_dump = "\n".join(connection.iterdump())
    finally:
        connection.close()
    assert "SYNTHETIC-SECRET" not in durable_dump
    assert "IMG-SECRET" not in durable_dump
    assert "LITERAL-SECRET" not in durable_dump
    assert database.integrity_check()
