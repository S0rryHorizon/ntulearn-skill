from __future__ import annotations

import json
import traceback
from datetime import UTC, datetime

import pytest

from ntulearn_skill.client.contracts import (
    AuthenticationRequired,
    AuthorizedReadSession,
    CapabilityState,
    PageRequest,
    ReadPolicyViolation,
    ReadPurpose,
    SourceAccessDenied,
    SourceCapability,
    SourceProtocolError,
    SourceUnavailable,
    TimeWindow,
    UnsupportedCapability,
)
from ntulearn_skill.client.ntulearn import NtulearnSourceAdapter
from ntulearn_skill.client.transport import (
    ReadOnlyTransport,
    ReadOperation,
    ResolvedReadRequest,
    SafeReadRequest,
    WireResponse,
)
from ntulearn_skill.core import (
    AssessmentSubtype,
    CalendarItemId,
    CourseId,
    Coverage,
    TemporalPrecision,
)

ORIGIN = "https://learn.example.invalid"
COURSE_PATH = "/learn/api/v1/users/me/memberships"
CONTENT_PATH = "/learn/api/v1/courses/course-ph0000/contents/ROOT/children"
ANNOUNCEMENT_PATH = "/learn/api/v1/courses/course-ph0000/announcements"
ASSESSMENT_PATH = "/learn/api/v1/courses/course-ph0000/contents/content-test-1"
DUE_PATH = "/learn/api/v1/courses/course-ph0000/calendars/dueDateCalendarItems"


def _session(*purposes: ReadPurpose, marker: object | None = None) -> AuthorizedReadSession:
    return AuthorizedReadSession(
        "ntulearn",
        frozenset(purposes or tuple(ReadPurpose)),
        object() if marker is None else marker,
    )


def _window(*, until_day: int = 8) -> TimeWindow:
    return TimeWindow(
        datetime(2027, 2, 1, tzinfo=UTC),
        datetime(2027, 2, until_day, tzinfo=UTC),
    )


def _page(
    results: list[object],
    *,
    offset: int = 0,
    count: int | None = None,
    next_page: str | None = None,
    limit: int = 100,
) -> dict[str, object]:
    return {
        "results": results,
        "paging": {
            "offset": offset,
            "limit": limit,
            "count": len(results) if count is None else count,
            "nextPage": next_page,
        },
    }


def _course_page() -> dict[str, object]:
    return _page(
        [
            {
                "isAvailable": True,
                "course": {
                    "id": "course-ph0000",
                    "courseId": "PH0000",
                    "displayName": "Invented Source Testing",
                },
            }
        ]
    )


def _content_page() -> dict[str, object]:
    return _page(
        [
            {
                "id": "content-test-1",
                "courseId": "course-ph0000",
                "title": "Synthetic Test 1",
                "position": 0,
                "contentHandler": "resource/x-invented-test",
                "contentDetail": {},
                "visibility": "VISIBLE",
            }
        ]
    )


def _simple_assessment() -> dict[str, object]:
    return {
        "id": "content-test-1",
        "title": "Synthetic Test 1",
        "contentHandler": "resource/x-invented-test",
        "contentDetail": {
            "resource/x-invented-test": {"assessment": {"id": "assessment-test-1", "type": "test"}}
        },
        "visibility": "VISIBLE",
    }


def test_m6_adapter_dispatches_only_fixed_read_operations_and_closes_responses() -> None:
    requests: list[ResolvedReadRequest] = []
    closed: list[ReadOperation] = []

    payloads = {
        ReadOperation.DISCOVER_COURSES: _course_page(),
        ReadOperation.LIST_ANNOUNCEMENTS: _page([]),
        ReadOperation.LIST_CONTENT_CHILDREN: _content_page(),
        ReadOperation.GET_CONTENT_DETAIL: _simple_assessment(),
        ReadOperation.LIST_DUE_ITEMS: _page([]),
    }

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        requests.append(request)

        def close() -> None:
            closed.append(request.operation)

        return WireResponse(
            200,
            json_body=payloads[request.operation],
            close=close,
        )

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    adapter.list_announcements(session, course, PageRequest())
    content = adapter.list_content(session, course, None, PageRequest()).items[0].remote_id
    adapter.get_assessment(session, course, content)
    adapter.list_due_items(session, course, _window(), PageRequest())

    assert [(request.operation, request.target, request.query) for request in requests] == [
        (
            ReadOperation.DISCOVER_COURSES,
            COURSE_PATH,
            (
                ("organization", "false"),
                ("includeCount", "true"),
                ("expand", "course.effectiveAvailability"),
                ("sort", "lastAccessDate(desc:nullslast)"),
                ("limit", "100"),
                ("offset", "0"),
            ),
        ),
        (
            ReadOperation.LIST_ANNOUNCEMENTS,
            ANNOUNCEMENT_PATH,
            (("sort", "startDateRestriction(desc)"), ("limit", "100"), ("offset", "0")),
        ),
        (
            ReadOperation.LIST_CONTENT_CHILDREN,
            CONTENT_PATH,
            (
                ("@view", "Summary"),
                ("expand", "assignedGroups,selfEnrollmentGroups.group,gradebookCategory"),
                ("includeInActivityTracking", "true"),
                ("limit", "100"),
                ("offset", "0"),
            ),
        ),
        (
            ReadOperation.GET_CONTENT_DETAIL,
            ASSESSMENT_PATH,
            (
                (
                    "expand",
                    "assignedGroups,selfEnrollmentGroups.group,alignedGoals,gradebookCategory",
                ),
                ("includeInActivityTracking", "false"),
            ),
        ),
        (
            ReadOperation.LIST_DUE_ITEMS,
            DUE_PATH,
            (
                ("date", "2027-02-01T00:00:00+00:00"),
                ("date_compare", "greaterOrEqual"),
                ("includeCount", "true"),
                ("limit", "100"),
                ("offset", "0"),
            ),
        ),
    ]
    assert closed == [request.operation for request in requests]


@pytest.mark.parametrize(
    ("operation", "path", "query"),
    (
        (
            ReadOperation.LIST_ANNOUNCEMENTS,
            DUE_PATH,
            (("sort", "startDateRestriction(desc)"), ("limit", "100"), ("offset", "0")),
        ),
        (
            ReadOperation.LIST_DUE_ITEMS,
            ANNOUNCEMENT_PATH,
            (
                ("date", "2027-02-01T00:00:00+00:00"),
                ("date_compare", "greaterOrEqual"),
                ("includeCount", "true"),
                ("limit", "100"),
                ("offset", "0"),
            ),
        ),
        (
            ReadOperation.GET_CONTENT_DETAIL,
            f"{ASSESSMENT_PATH}/attempts",
            (
                (
                    "expand",
                    "assignedGroups,selfEnrollmentGroups.group,alignedGoals,gradebookCategory",
                ),
                ("includeInActivityTracking", "false"),
            ),
        ),
    ),
)
def test_m6_transport_rejects_operation_path_confusion_before_executor(
    operation: ReadOperation, path: str, query: tuple[tuple[str, str], ...]
) -> None:
    calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal calls
        calls += 1
        return WireResponse(200, json_body={})

    transport = ReadOnlyTransport(executor)

    with pytest.raises(ReadPolicyViolation):
        transport.send(SafeReadRequest(operation, path, query), _session())

    assert calls == 0


def test_announcement_rich_text_and_distinct_source_times_are_preserved() -> None:
    timestamps = {
        "createdDate": "2027-01-01T01:00:00+08:00",
        "modifiedDate": "2027-01-02T02:00:00+08:00",
        "publishedDate": "2027-01-03T03:00:00+08:00",
        "startDateRestriction": "2027-01-04T04:00:00+08:00",
        "endDateRestriction": "2027-01-05T05:00:00+08:00",
    }
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(
            200,
            json_body=_page(
                [
                    {
                        "id": "announcement-invented",
                        "title": "Synthetic announcement",
                        "body": {
                            "rawText": "<p>Invented <strong>rich text</strong>.</p>",
                            "displayText": "Invented rich text.",
                        },
                        "visibility": "VISIBLE",
                        **timestamps,
                    }
                ]
            ),
        ),
    ]
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: responses.pop(0)), source_origin=ORIGIN
    )
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id

    record = adapter.list_announcements(session, course, PageRequest()).items[0]

    assert record.body == "<p>Invented <strong>rich text</strong>.</p>"
    observed = {
        "createdDate": record.created_at,
        "modifiedDate": record.modified_at,
        "publishedDate": record.published_at,
        "startDateRestriction": record.available_from,
        "endDateRestriction": record.available_until,
    }
    assert all(value is not None for value in observed.values())
    assert {name: value.source_text for name, value in observed.items() if value} == timestamps
    assert all(
        value.precision is TemporalPrecision.EXACT_TIME for value in observed.values() if value
    )
    assert all(value.source_timezone == "+08:00" for value in observed.values() if value)
    assert len({value.instant for value in observed.values() if value}) == 5


def test_assessment_nested_test_shape_keeps_direct_grading_and_generic_due_evidence() -> None:
    direct_due = None
    grading_due = "2027-03-10T17:00:00+08:00"
    generic_due = "2027-03-10T10:00:00Z"
    assessment_payload = {
        "id": "content-test-1",
        "title": "Synthetic Test 1",
        "contentHandler": {"id": "resource/x-invented-test"},
        "contentDetail": {
            "resource/x-invented-test": {
                "test": {
                    "assessment": {
                        "id": "assessment-test-1",
                        "assessmentType": "test",
                        "instructions": {"rawText": "<p>Answer two invented questions.</p>"},
                        "openDate": "2027-03-10T15:00:00+08:00",
                        "closeDate": "2027-03-10T17:00:00+08:00",
                    },
                    "gradingColumn": {
                        "id": "grading-test-1",
                        "dueDate": grading_due,
                    },
                    "deploymentSettings": {"mode": "invented"},
                }
            }
        },
        "genericReadOnlyData": {"dueDate": generic_due},
        "createdDate": "2027-02-01T00:00:00Z",
        "modifiedDate": "2027-02-02T00:00:00Z",
        "startDate": "2027-03-01T00:00:00Z",
        "endDate": "2027-03-11T00:00:00Z",
        "visibility": "VISIBLE",
    }
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(200, json_body=_content_page()),
        WireResponse(200, json_body=assessment_payload),
    ]
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: responses.pop(0)), source_origin=ORIGIN
    )
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    content = adapter.list_content(session, course, None, PageRequest()).items[0].remote_id

    record = adapter.get_assessment(session, course, content)

    assert record.subtype is AssessmentSubtype.TEST
    assert record.instructions == "<p>Answer two invented questions.</p>"
    assert record.due_at is direct_due
    assert record.grading_due_at is not None
    assert record.grading_due_at.source_text == grading_due
    assert record.generic_due_at is not None
    assert record.generic_due_at.source_text == generic_due
    assert record.grading_due_at.instant != record.generic_due_at.instant


def test_due_items_without_remote_id_use_distinct_typed_composite_identities() -> None:
    due_payload = _page(
        [
            {
                "calendarId": "calendar-shared",
                "itemSourceId": "source-assignment-1",
                "itemSourceType": "ASSIGNMENT",
                "title": "Synthetic assignment",
                "startDate": "2027-02-10T10:00:00+08:00",
                "endDate": "2027-02-10T11:00:00+08:00",
                "visibility": "VISIBLE",
            },
            {
                "calendarId": "calendar-shared",
                "itemSourceId": "source-test-1",
                "itemSourceType": "TEST",
                "title": "Synthetic test",
                "startDate": "2027-02-11T10:00:00+08:00",
                "endDate": "2027-02-11T11:00:00+08:00",
                "visibility": "VISIBLE",
            },
        ]
    )
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(200, json_body=due_payload),
    ]
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: responses.pop(0)), source_origin=ORIGIN
    )
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id

    records = adapter.list_due_items(session, course, _window(), PageRequest()).items

    assert len(records) == 2
    assert all(type(record.remote_id) is CalendarItemId for record in records)
    assert records[0].remote_id != records[1].remote_id
    assert json.loads(records[0].remote_id.value) == [
        "calendar-shared",
        "ASSIGNMENT",
        "source-assignment-1",
    ]
    assert json.loads(records[1].remote_id.value) == [
        "calendar-shared",
        "TEST",
        "source-test-1",
    ]
    assert all(record.source_start_at is not None for record in records)
    assert all(record.source_end_at is not None for record in records)
    assert all(record.due_at is None for record in records)


@pytest.mark.parametrize("missing", ("itemSourceId", "itemSourceType"))
def test_due_identity_rejects_missing_composite_component(missing: str) -> None:
    item = {
        "calendarId": "calendar-invented",
        "itemSourceId": "source-invented",
        "itemSourceType": "TEST",
        "title": "Synthetic test",
    }
    del item[missing]
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)), source_origin=ORIGIN
    )

    with pytest.raises(SourceProtocolError):
        adapter.translate_due_items_payload(
            _page([item]),
            course=CourseId("ntulearn", "course-ph0000"),
            window=_window(),
            page=PageRequest(),
            expected_path=DUE_PATH,
        )


def test_due_paging_reconstructs_the_original_window_and_reports_partial_gap() -> None:
    due_requests: list[ResolvedReadRequest] = []
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(
            200,
            json_body=_page(
                [],
                count=2,
                next_page=(
                    f"{DUE_PATH}?date=2040-01-01T00:00:00%2B00:00"
                    "&date_compare=greaterOrEqual&includeCount=true&limit=100&offset=1"
                ),
            ),
        ),
        WireResponse(200, json_body=_page([], offset=1, count=1)),
    ]

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        if request.operation is ReadOperation.LIST_DUE_ITEMS:
            due_requests.append(request)
        return responses.pop(0)

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    window = _window()

    first = adapter.list_due_items(session, course, window, PageRequest())
    second = adapter.list_due_items(session, course, window, PageRequest(cursor=first.next_cursor))

    assert first.coverage_for_page is Coverage.PARTIAL
    assert second.coverage_for_page is Coverage.UNKNOWN
    assert [dict(request.query)["date"] for request in due_requests] == [
        "2027-02-01T00:00:00+00:00",
        "2027-02-01T00:00:00+00:00",
    ]
    assert [dict(request.query)["offset"] for request in due_requests] == ["0", "1"]


def test_due_cursor_is_bound_to_the_complete_time_window() -> None:
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(
            200,
            json_body=_page(
                [
                    {
                        "calendarId": "calendar-invented",
                        "itemSourceId": "source-invented",
                        "itemSourceType": "TEST",
                        "title": "Synthetic test",
                    }
                ],
                count=2,
                next_page=f"{DUE_PATH}?offset=1",
            ),
        ),
    ]
    calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal calls
        calls += 1
        return responses.pop(0)

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    first = adapter.list_due_items(session, course, _window(until_day=8), PageRequest())

    with pytest.raises(SourceProtocolError):
        adapter.list_due_items(
            session,
            course,
            _window(until_day=9),
            PageRequest(cursor=first.next_cursor),
        )

    assert calls == 2


def test_due_paging_rejects_cycle_and_different_course_path() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)), source_origin=ORIGIN
    )
    course = CourseId("ntulearn", "course-ph0000")
    window = _window()

    with pytest.raises(SourceProtocolError):
        adapter.translate_due_items_payload(
            _page([], offset=1, count=3, next_page=f"{DUE_PATH}?offset=1"),
            course=course,
            window=window,
            page=PageRequest(
                cursor=(
                    f"due:{course.value}:{window.since.isoformat()}:{window.until.isoformat()}:1"
                )
            ),
            expected_path=DUE_PATH,
        )

    with pytest.raises(SourceProtocolError):
        adapter.translate_due_items_payload(
            _page(
                [
                    {
                        "calendarId": "calendar-invented",
                        "itemSourceId": "source-invented",
                        "itemSourceType": "TEST",
                        "title": "Synthetic test",
                    }
                ],
                count=2,
                next_page=(
                    "/learn/api/v1/courses/course-other/calendars/dueDateCalendarItems?offset=1"
                ),
            ),
            course=course,
            window=window,
            page=PageRequest(),
            expected_path=DUE_PATH,
        )


def test_purpose_course_and_rotated_session_scopes_fail_closed() -> None:
    requests: list[ReadOperation] = []
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(200, json_body=_content_page()),
        WireResponse(200, json_body=_course_page()),
    ]

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        requests.append(request.operation)
        return responses.pop(0)

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)
    first_session = _session()
    course = adapter.list_courses(first_session, PageRequest()).items[0].remote_id
    content = adapter.list_content(first_session, course, None, PageRequest()).items[0].remote_id

    same_marker_wrong_purpose = _session(ReadPurpose.CONTENT, marker=first_session.marker)
    with pytest.raises(AuthenticationRequired):
        adapter.list_announcements(same_marker_wrong_purpose, course, PageRequest())
    with pytest.raises(SourceAccessDenied):
        adapter.list_due_items(
            first_session,
            CourseId("ntulearn", "course-other"),
            _window(),
            PageRequest(),
        )

    second_session = _session()
    rediscovered_course = adapter.list_courses(second_session, PageRequest()).items[0].remote_id
    with pytest.raises(SourceAccessDenied):
        adapter.get_assessment(second_session, rediscovered_course, content)

    assert requests == [
        ReadOperation.DISCOVER_COURSES,
        ReadOperation.LIST_CONTENT_CHILDREN,
        ReadOperation.DISCOVER_COURSES,
    ]


def test_schedule_remains_explicitly_unknown_and_never_dispatches() -> None:
    calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal calls
        calls += 1
        return WireResponse(200, json_body={})

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)

    with pytest.raises(UnsupportedCapability) as caught:
        adapter.list_schedule_items(
            _session(ReadPurpose.SCHEDULE),
            CourseId("ntulearn", "course-ph0000"),
            _window(),
            PageRequest(),
        )

    assert caught.value.capability is SourceCapability.SCHEDULE_ITEMS
    assert caught.value.state is CapabilityState.UNKNOWN
    assert calls == 0


def test_malformed_m6_response_is_closed_and_private_values_are_redacted() -> None:
    canary = "PRIVATE-M6-CANARY"
    closed: list[bool] = []
    responses = [
        WireResponse(200, json_body=_course_page()),
        WireResponse(
            200,
            json_body=_page([{"id": "announcement-invented", "body": [canary]}]),
            close=lambda: closed.append(True),
        ),
    ]
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: responses.pop(0)), source_origin=ORIGIN
    )
    session = _session()
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id

    with pytest.raises(SourceProtocolError) as caught:
        adapter.list_announcements(session, course, PageRequest())

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "the source response did not match a supported shape"
    assert canary not in rendered
    assert closed == [True]


def test_m6_transport_failure_closes_response_and_exposes_only_safe_error() -> None:
    canary = "PRIVATE-M6-ERROR-CANARY"
    closed: list[bool] = []
    transport = ReadOnlyTransport(
        lambda *_: WireResponse(
            500,
            json_body={"private": canary},
            close=lambda: closed.append(True),
        )
    )
    request = SafeReadRequest(
        ReadOperation.LIST_ANNOUNCEMENTS,
        ANNOUNCEMENT_PATH,
        (("sort", "startDateRestriction(desc)"), ("limit", "100"), ("offset", "0")),
    )

    with pytest.raises(SourceUnavailable) as caught:
        transport.send(request, _session(ReadPurpose.ANNOUNCEMENTS))

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "the source read could not be completed"
    assert canary not in rendered
    assert closed == [True]
