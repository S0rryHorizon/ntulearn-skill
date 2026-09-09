from __future__ import annotations

import pytest

from ntulearn_skill.client import (
    AuthorizedReadSession,
    CapabilityState,
    NtulearnSourceAdapter,
    PageRequest,
    ReadOnlyTransport,
    ReadOperation,
    ReadPolicyViolation,
    ReadPurpose,
    ResolvedReadRequest,
    SafeReadRequest,
    SourceAccessDenied,
    SourceCapability,
    UnsupportedCapability,
    WireResponse,
)
from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId

ORIGIN = "https://learn.example.invalid"


def _session(marker: object | None = None) -> AuthorizedReadSession:
    return AuthorizedReadSession(
        "ntulearn",
        frozenset(ReadPurpose),
        object() if marker is None else marker,
    )


def _course_page(
    *, offset: int, result: dict[str, object], next_offset: int | None, count: int
) -> dict[str, object]:
    next_page = (
        ""
        if next_offset is None
        else f"/learn/api/v1/users/me/memberships?limit=1&offset={next_offset}"
    )
    return {
        "paging": {
            "previousPage": "",
            "nextPage": next_page,
            "limit": 1,
            "count": count,
            "offset": offset,
        },
        "results": [result],
        "permissions": {},
    }


def test_adapter_translates_two_membership_shapes_and_scoped_pagination() -> None:
    first = {
        "isAvailable": True,
        "course": {
            "id": "course-alpha",
            "courseId": "PH0000",
            "displayName": "Example Physics Course",
            "term": {"name": "Synthetic Term A"},
        },
    }
    second = {
        "isAvailable": False,
        "course": {
            "id": "course-beta",
            "displayId": "CS0000",
            "name": "Example Computing Course",
            "term": "Synthetic Term B",
        },
    }
    responses = [
        WireResponse(200, json_body=_course_page(offset=0, result=first, next_offset=1, count=2)),
        WireResponse(
            200, json_body=_course_page(offset=1, result=second, next_offset=None, count=2)
        ),
    ]
    requests: list[ResolvedReadRequest] = []

    def executor(request, session, forward_credentials):
        requests.append(request)
        return responses.pop(0)

    adapter = NtulearnSourceAdapter(ReadOnlyTransport(executor), source_origin=ORIGIN)
    session = _session()

    page_one = adapter.list_courses(session, PageRequest(page_size=1))
    page_two = adapter.list_courses(session, PageRequest(cursor=page_one.next_cursor, page_size=1))

    assert page_one.items[0].remote_id == CourseId("ntulearn", "course-alpha")
    assert page_one.items[0].term == "Synthetic Term A"
    assert page_two.items[0].code == "CS0000"
    assert page_two.items[0].availability is Availability.UNAVAILABLE
    assert page_two.next_cursor is None
    assert [dict(request.query)["offset"] for request in requests] == ["0", "1"]


def test_content_translation_discovers_generic_container_and_resources() -> None:
    marker = object()
    course_payload = _course_page(
        offset=0,
        result={
            "isAvailable": True,
            "course": {
                "id": "course-alpha",
                "courseId": "PH0000",
                "displayName": "Example Physics Course",
            },
        },
        next_offset=None,
        count=1,
    )
    root_payload = {
        "paging": {"nextPage": "", "limit": 100, "count": 2, "offset": 0},
        "results": [
            {
                "id": "container-lab",
                "courseId": "course-alpha",
                "title": "Laboratory sequence",
                "position": 0,
                "contentHandler": "resource/x-example-folder",
                "contentDetail": {"resource/x-example-folder": {"isFolder": True}},
                "visibility": "VISIBLE",
                "renderType": "LINK",
            },
            {
                "id": "document-notes",
                "courseId": "course-alpha",
                "title": "Reference notes",
                "position": 1,
                "contentHandler": {"id": "resource/x-example-document"},
                "contentDetail": {
                    "attachments": [
                        {
                            "id": "attachment-notes",
                            "fileName": "invented-notes.pdf",
                            "displayName": "Invented notes",
                            "mimeType": "application/pdf",
                            "modifiedDate": 1_800_000_000_000,
                        }
                    ]
                },
                "visibility": "HIDDEN",
            },
        ],
    }
    child_payload = {
        "paging": {"nextPage": None, "limit": 100, "count": 1, "offset": 0},
        "results": [
            {
                "id": "custom-tool",
                "courseId": "course-alpha",
                "title": "External practice",
                "position": 0,
                "contentHandler": "resource/x-example-tool",
                "contentDetail": {},
                "visibility": "VISIBLE",
            }
        ],
    }
    payloads = [course_payload, root_payload, child_payload]

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200, json_body=payloads.pop(0))),
        source_origin=ORIGIN,
    )
    session = _session(marker)
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    root = adapter.list_content(session, course, None, PageRequest())
    container = root.items[0]
    nested = adapter.list_content(session, course, container.remote_id, PageRequest())

    assert container.is_container
    assert container.title == "Laboratory sequence"
    assert root.items[1].availability is Availability.UNAVAILABLE
    resource = root.items[1].resources[0]
    assert resource.remote_id == AttachmentId("ntulearn", "attachment-notes")
    assert adapter.get_resource_metadata(session, resource.remote_id) == resource
    assert nested.items[0].parent_id == ContentId("ntulearn", "container-lab")
    assert not nested.items[0].is_container

    with pytest.raises(SourceAccessDenied):
        adapter.list_content(
            session,
            course,
            ContentId("ntulearn", "not-discovered"),
            PageRequest(),
        )


def test_unknown_optimizations_and_resource_stream_remain_capability_gated() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)), source_origin=ORIGIN
    )
    capabilities = adapter.capabilities()

    assert capabilities.state(SourceCapability.REMOTE_DELTA) is CapabilityState.UNKNOWN
    assert capabilities.state(SourceCapability.AUTOMATIC_SESSION_RENEWAL) is CapabilityState.UNKNOWN
    assert capabilities.state(SourceCapability.RESOURCE_STREAM) is CapabilityState.UNSUPPORTED

    with pytest.raises(UnsupportedCapability):
        adapter.open_resource_stream(_session(), AttachmentId("ntulearn", "attachment-invented"))


def test_transport_refuses_get_shaped_state_change_and_redacts_logs() -> None:
    executed = 0
    events: list[tuple[str, dict[str, object]]] = []

    def executor(request, session, forward_credentials):
        nonlocal executed
        executed += 1
        return WireResponse(200, json_body={})

    transport = ReadOnlyTransport(
        executor,
        logger=lambda event, fields: events.append((event, dict(fields))),
    )
    state_change = SafeReadRequest(
        ReadOperation.GET_CONTENT_DETAIL,
        "/learn/api/v1/courses/course-alpha/contents/content-alpha/markRead",
    )

    with pytest.raises(ReadPolicyViolation):
        transport.send(state_change, _session())
    assert executed == 0

    request = SafeReadRequest(
        ReadOperation.DISCOVER_COURSES,
        "/learn/api/v1/users/me/memberships",
        (
            ("organization", "false"),
            ("includeCount", "true"),
            ("expand", "course.effectiveAvailability"),
            ("sort", "lastAccessDate(desc:nullslast)"),
            ("limit", "10"),
            ("offset", "0"),
        ),
    )
    transport.send(request, _session())
    assert events == [
        (
            "source_read",
            {"operation": "discover_courses", "status_code": 200, "redirected": False},
        )
    ]
    assert "course-alpha" not in repr(request)
