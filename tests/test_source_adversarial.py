from __future__ import annotations

import traceback
from collections.abc import Iterator

import pytest

from ntulearn_skill.client import (
    AuthenticationRequired,
    AuthorizedReadSession,
    EphemeralByteStream,
    NtulearnSourceAdapter,
    PageRequest,
    ReadOnlyTransport,
    ReadOperation,
    ReadPolicyViolation,
    ReadPurpose,
    ResolvedReadRequest,
    SafeReadRequest,
    SessionExpired,
    SourceAccessDenied,
    SourceProtocolError,
    SourceUnavailable,
    WireResponse,
)
from ntulearn_skill.core import AttachmentId, ContentId, CourseId, Coverage

SOURCE_ORIGIN = "https://source.example.invalid"


def test_stream_protocol_error_chain_is_redacted_and_closed() -> None:
    canary = "SYNTHETIC-ITERATOR-PROTOCOL-CANARY"
    closed: list[bool] = []

    def chunks() -> Iterator[bytes]:
        try:
            raise RuntimeError(canary)
        except RuntimeError as error:
            raise SourceProtocolError() from error
        yield b"unreachable"

    stream = EphemeralByteStream(chunks(), close=lambda: closed.append(True))
    with pytest.raises(SourceProtocolError) as caught:
        stream.read()
    assert canary not in "".join(traceback.format_exception(caught.value))
    assert closed == [True]


DISCOVERY_PATH = "/learn/api/v1/users/me/memberships"
DISCOVERY_QUERY = (
    ("organization", "false"),
    ("includeCount", "true"),
    ("expand", "course.effectiveAvailability"),
    ("sort", "lastAccessDate(desc:nullslast)"),
    ("limit", "100"),
    ("offset", "0"),
)
CONTENT_QUERY = (
    ("@view", "Summary"),
    ("expand", "assignedGroups,selfEnrollmentGroups.group,gradebookCategory"),
    ("includeInActivityTracking", "true"),
    ("limit", "100"),
    ("offset", "0"),
)
STREAM_PATH = "/bbcswebdav/pid-content-a-dt-content-rid-file/xid-file"
STREAM_QUERY = (
    ("isInlineRender", "true"),
    ("xythos-download", "true"),
    ("render", "inline"),
)


def _session(*purposes: ReadPurpose, provider: str = "ntulearn") -> AuthorizedReadSession:
    return AuthorizedReadSession(provider, frozenset(purposes))


def _course_payload(*, next_page: object = None, count: object = 1) -> dict[str, object]:
    return {
        "results": [
            {
                "isAvailable": True,
                "course": {
                    "id": "course-a",
                    "courseId": "PH0000",
                    "displayName": "Example Physics Course",
                },
            }
        ],
        "paging": {"offset": 0, "limit": 100, "count": count, "nextPage": next_page},
    }


def _content_payload(*, course_id: str = "course-a") -> dict[str, object]:
    return {
        "results": [
            {
                "id": "content-a",
                "courseId": course_id,
                "title": "Invented module",
                "position": 0,
                "contentHandler": "folder",
                "contentDetail": {"isFolder": True},
                "hasChildren": True,
            }
        ],
        "paging": {"offset": 0, "limit": 100, "count": 1, "nextPage": None},
    }


def _content_with_attachment_payload() -> dict[str, object]:
    payload = _content_payload()
    results = payload["results"]
    assert isinstance(results, list)
    content = results[0]
    assert isinstance(content, dict)
    content["attachments"] = [
        {
            "id": "file",
            "fileName": "invented.pdf",
            "displayName": "Invented file",
            "mimeType": "application/pdf",
        }
    ]
    return payload


def _discover_attachment(
    adapter: NtulearnSourceAdapter, session: AuthorizedReadSession
) -> AttachmentId:
    course = adapter.list_courses(session, PageRequest()).items[0].remote_id
    content = adapter.list_content(session, course, None, PageRequest()).items[0]
    return content.resources[0].remote_id


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/learn/api/v1/courses/course-a/contents/content%2Fchild/children",
        "/learn/api/v1/courses/course-a/contents/../children",
        "/learn/api/v1/courses/course-a/contents/%2e%2e/children",
        "/learn/api/v1/courses/course-a//contents/content-a/children",
        "/learn/api/v1/courses/course-a/contents/content-a\\children",
    ),
)
def test_unsafe_operation_path_is_rejected_before_executor(
    unsafe_path: str,
) -> None:
    calls: list[ResolvedReadRequest] = []

    def executor(request, session, forward_credentials):
        calls.append(request)
        return WireResponse(200, json_body={})

    transport = ReadOnlyTransport(executor)
    request = SafeReadRequest(ReadOperation.LIST_CONTENT_CHILDREN, unsafe_path, CONTENT_QUERY)

    with pytest.raises(ReadPolicyViolation):
        transport.send(request, _session(ReadPurpose.CONTENT))

    assert calls == []


@pytest.mark.parametrize(
    "session",
    (
        _session(ReadPurpose.CONTENT),
        _session(ReadPurpose.DISCOVERY, provider="other-provider"),
    ),
)
def test_wrong_session_purpose_or_provider_is_rejected_before_executor(
    session: AuthorizedReadSession,
) -> None:
    calls = 0

    def executor(request, authorized_session, forward_credentials):
        nonlocal calls
        calls += 1
        return WireResponse(200, json_body=_course_payload())

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
    )

    with pytest.raises(AuthenticationRequired):
        adapter.list_courses(session, PageRequest())

    assert calls == 0


def test_discovered_course_authorization_cannot_be_reused_by_another_session() -> None:
    calls: list[ReadOperation] = []

    def executor(request, session, forward_credentials):
        calls.append(request.operation)
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        return WireResponse(200, json_body=_content_payload())

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
    )
    first_session = _session(ReadPurpose.DISCOVERY, ReadPurpose.CONTENT)
    second_session = _session(ReadPurpose.DISCOVERY, ReadPurpose.CONTENT)
    course = adapter.list_courses(first_session, PageRequest()).items[0].remote_id

    with pytest.raises((AuthenticationRequired, SourceAccessDenied)):
        adapter.list_content(second_session, course, None, PageRequest())

    assert calls == [ReadOperation.DISCOVER_COURSES]


def test_content_page_rejects_record_from_a_different_course() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )
    course = CourseId("ntulearn", "course-a")

    with pytest.raises(SourceProtocolError):
        adapter.translate_content_payload(
            _content_payload(course_id="course-b"),
            course=course,
            parent=None,
            page=PageRequest(),
            expected_path="/learn/api/v1/courses/course-a/contents/ROOT/children",
        )


@pytest.mark.parametrize(
    "payload",
    (
        {"results": (), "paging": {"offset": 0, "limit": 100, "count": 0}},
        {"results": [], "paging": []},
        {"results": [], "paging": {"offset": False, "limit": 100, "count": 0}},
        {"results": [], "paging": {"offset": 0, "limit": "100", "count": 0}},
    ),
)
def test_malformed_pagination_shape_is_rejected(payload: dict[str, object]) -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )

    with pytest.raises(SourceProtocolError):
        adapter.translate_courses_payload(payload, page=PageRequest())


def test_pagination_rejects_non_source_origin() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )
    payload = _course_payload(
        next_page=("https://other.example.invalid/learn/api/v1/users/me/memberships?offset=1"),
        count=2,
    )

    with pytest.raises(SourceProtocolError):
        adapter.translate_courses_payload(payload, page=PageRequest())


def test_pagination_count_cannot_claim_more_items_without_a_next_page() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )

    with pytest.raises(SourceProtocolError):
        adapter.translate_courses_payload(
            _course_payload(next_page=None, count=2),
            page=PageRequest(),
        )


def test_pagination_rejects_cursor_that_does_not_advance() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )
    payload = _course_payload(next_page=f"{DISCOVERY_PATH}?offset=0", count=2)

    with pytest.raises(SourceProtocolError):
        adapter.translate_courses_payload(payload, page=PageRequest())


def test_pagination_rejects_next_page_that_skips_an_item() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )
    payload = _course_payload(next_page=f"{DISCOVERY_PATH}?offset=2", count=3)

    with pytest.raises(SourceProtocolError):
        adapter.translate_courses_payload(payload, page=PageRequest())


def test_empty_intermediate_page_with_forward_offset_is_explicitly_partial() -> None:
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(lambda *_: WireResponse(200)),
        source_origin=SOURCE_ORIGIN,
    )
    payload = {
        "results": [],
        "paging": {
            "offset": 0,
            "limit": 100,
            "count": 3,
            "nextPage": f"{DISCOVERY_PATH}?offset=2",
        },
    }

    page = adapter.translate_courses_payload(payload, page=PageRequest())

    assert page.items == ()
    assert page.next_cursor == "courses:2"
    assert page.coverage_for_page is Coverage.PARTIAL


def test_executor_exception_is_redacted() -> None:
    canary = "PRIVATE-EXECUTOR-CANARY"

    def executor(request, session, forward_credentials):
        raise RuntimeError(f"{canary}: https://source.example.invalid/private?token=invented")

    transport = ReadOnlyTransport(executor)
    request = SafeReadRequest(ReadOperation.DISCOVER_COURSES, DISCOVERY_PATH, DISCOVERY_QUERY)

    with pytest.raises(SourceUnavailable) as caught:
        transport.send(request, _session(ReadPurpose.DISCOVERY))

    assert str(caught.value) == "the source read could not be completed"
    assert canary not in str(caught.value)
    assert "token" not in str(caught.value)


def test_typed_executor_exception_discards_private_exception_context() -> None:
    canary = "PRIVATE-CHAIN-CANARY"

    def executor(request, session, forward_credentials):
        try:
            raise RuntimeError(f"{canary}: https://source.example.invalid/private?token=invented")
        except RuntimeError as error:
            raise SessionExpired() from error

    transport = ReadOnlyTransport(executor)
    request = SafeReadRequest(ReadOperation.DISCOVER_COURSES, DISCOVERY_PATH, DISCOVERY_QUERY)

    with pytest.raises(SessionExpired) as caught:
        transport.send(request, _session(ReadPurpose.DISCOVERY))

    rendered = "".join(traceback.format_exception(caught.value))
    assert str(caught.value) == "the authorized read session expired"
    assert canary not in rendered
    assert "token" not in rendered


def test_iterator_and_close_exceptions_are_redacted_and_stream_closes() -> None:
    iterator_canary = "PRIVATE-ITERATOR-CANARY"
    close_canary = "PRIVATE-CLOSE-CANARY"
    close_calls = 0

    def chunks() -> Iterator[bytes]:
        yield b"invented-prefix"
        raise RuntimeError(iterator_canary)

    def close() -> None:
        nonlocal close_calls
        close_calls += 1
        raise RuntimeError(close_canary)

    stream = EphemeralByteStream(chunks(), close=close)

    with pytest.raises(SourceUnavailable) as caught:
        stream.read()

    assert str(caught.value) == "the source read could not be completed"
    assert iterator_canary not in str(caught.value)
    assert close_canary not in str(caught.value)
    assert close_calls == 1
    assert stream.read() == b""


def test_response_is_closed_when_json_translation_fails() -> None:
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    def executor(request, session, forward_credentials):
        return WireResponse(200, json_body={"invented": "unsupported shape"}, close=close)

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
    )

    with pytest.raises(SourceProtocolError):
        adapter.list_courses(_session(ReadPurpose.DISCOVERY), PageRequest())

    assert close_calls == 1


def test_invalid_wire_response_status_is_closed() -> None:
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    transport = ReadOnlyTransport(lambda *_: WireResponse(0, close=close))
    request = SafeReadRequest(ReadOperation.DISCOVER_COURSES, DISCOVERY_PATH, DISCOVERY_QUERY)

    with pytest.raises(SourceProtocolError):
        transport.send(request, _session(ReadPurpose.DISCOVERY))

    assert close_calls == 1


def test_stream_constructor_failure_is_redacted_and_closes_response() -> None:
    canary = "PRIVATE-STREAM-CONSTRUCTOR-CANARY"
    close_calls = 0

    class ExplodingChunks:
        def __iter__(self) -> Iterator[bytes]:
            raise RuntimeError(canary)

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    def executor(request, session, forward_credentials):
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.LIST_CONTENT_CHILDREN:
            return WireResponse(200, json_body=_content_with_attachment_payload())
        return WireResponse(200, body_chunks=ExplodingChunks(), close=close)

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
        resource_request_factory=lambda _: SafeReadRequest(
            ReadOperation.OPEN_RESOURCE_STREAM, STREAM_PATH, STREAM_QUERY
        ),
    )
    session = _session(ReadPurpose.DISCOVERY, ReadPurpose.CONTENT, ReadPurpose.RESOURCE_STREAM)
    resource = _discover_attachment(adapter, session)

    with pytest.raises(SourceUnavailable) as caught:
        adapter.open_resource_stream(session, resource)

    rendered = "".join(traceback.format_exception(caught.value))
    assert canary not in rendered
    assert close_calls == 1


@pytest.mark.parametrize(
    "unsafe_path",
    (
        "/bbcswebdav/pid-content-a-dt-content-rid-file/xid-file-other",
        "/bbcswebdav/pid-content-other-dt-content-rid-file/xid-file",
    ),
)
def test_resource_route_must_exactly_match_discovered_attachment_and_content(
    unsafe_path: str,
) -> None:
    operations: list[ReadOperation] = []

    def executor(request, session, forward_credentials):
        operations.append(request.operation)
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        return WireResponse(200, json_body=_content_with_attachment_payload())

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
        resource_request_factory=lambda _: SafeReadRequest(
            ReadOperation.OPEN_RESOURCE_STREAM, unsafe_path, STREAM_QUERY
        ),
    )
    session = _session(ReadPurpose.DISCOVERY, ReadPurpose.CONTENT, ReadPurpose.RESOURCE_STREAM)
    resource = _discover_attachment(adapter, session)

    with pytest.raises(SourceProtocolError):
        adapter.open_resource_stream(session, resource)

    assert operations == [
        ReadOperation.DISCOVER_COURSES,
        ReadOperation.LIST_CONTENT_CHILDREN,
    ]


@pytest.mark.parametrize(
    "location",
    (
        "https://other.example.invalid/file?signature=invented",
        "https://download.example.invalid:444/file?signature=invented",
        "https://user:secret@download.example.invalid/file",
    ),
)
def test_unapproved_redirect_never_receives_forwarded_credentials(location: str) -> None:
    calls: list[tuple[str, bool]] = []
    closed = 0

    def close() -> None:
        nonlocal closed
        closed += 1

    def executor(request, session, forward_credentials):
        calls.append((request.target, forward_credentials))
        return WireResponse(302, headers={"Location": location}, close=close)

    transport = ReadOnlyTransport(
        executor,
        redirect_hosts=frozenset({"download.example.invalid"}),
    )
    request = SafeReadRequest(ReadOperation.OPEN_RESOURCE_STREAM, STREAM_PATH, STREAM_QUERY)

    with pytest.raises(ReadPolicyViolation):
        transport.send(request, _session(ReadPurpose.RESOURCE_STREAM))

    assert calls == [(STREAM_PATH, True)]
    assert closed == 1


def test_approved_redirect_is_followed_without_forwarding_credentials() -> None:
    calls: list[tuple[str, bool, bool]] = []
    location = "https://download.example.invalid/file?signature=invented"

    def executor(request, session, forward_credentials):
        calls.append((request.target, forward_credentials, request.is_ephemeral_redirect))
        if len(calls) == 1:
            return WireResponse(302, headers={"Location": location})
        return WireResponse(200, body_chunks=(b"invented",))

    transport = ReadOnlyTransport(
        executor,
        redirect_hosts=frozenset({"download.example.invalid"}),
    )
    request = SafeReadRequest(ReadOperation.OPEN_RESOURCE_STREAM, STREAM_PATH, STREAM_QUERY)

    response = transport.send(request, _session(ReadPurpose.RESOURCE_STREAM))

    assert response.status_code == 200
    assert calls == [
        (STREAM_PATH, True, False),
        (location, False, True),
    ]


def test_foreign_provider_content_identifier_is_rejected_before_executor() -> None:
    calls = 0

    def executor(request, session, forward_credentials):
        nonlocal calls
        calls += 1
        return WireResponse(200, json_body=_content_payload())

    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=SOURCE_ORIGIN,
    )
    with pytest.raises(TypeError):
        adapter.translate_content_payload(
            _content_payload(),
            course=CourseId("ntulearn", "course-a"),
            parent=ContentId("other-provider", "content-a"),
            page=PageRequest(),
            expected_path="/learn/api/v1/courses/course-a/contents/content-a/children",
        )

    assert calls == 0
