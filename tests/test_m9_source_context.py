"""Synthetic regressions for authorization-bound raw API resource context."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from reportlab.pdfgen import canvas

from ntulearn_skill.client import (
    AuthorizedReadSession,
    NtulearnSourceAdapter,
    ReadOnlyTransport,
    ReadOperation,
    ReadPurpose,
    ResolvedReadRequest,
    SafeReadRequest,
    SessionStatus,
    TimeWindow,
    WireResponse,
)
from ntulearn_skill.core import AttachmentId, CourseId
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync import SyncEngine

ORIGIN = "https://learn.example.invalid"
COURSE = CourseId("ntulearn", "course-a")
ATTACHMENT = AttachmentId("ntulearn", "file-a")
CONTENT_PATH = "/learn/api/v1/courses/course-a/contents/ROOT/children"
STREAM_PATH = "/bbcswebdav/pid-content-a-dt-content-rid-file-a/xid-file-a"
STREAM_QUERY = (
    ("isInlineRender", "true"),
    ("xythos-download", "true"),
    ("render", "inline"),
)


def _pdf_bytes() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, "Synthetic source context")
    document.save()
    return output.getvalue()


PDF_BYTES = _pdf_bytes()


@dataclass
class _Sessions:
    marker: object = field(default_factory=object)

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        return AuthorizedReadSession("ntulearn", frozenset({purpose}), self.marker)

    def status(self) -> SessionStatus:
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        del reason


def _course_payload() -> dict[str, object]:
    return {
        "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
        "results": [
            {
                "isAvailable": True,
                "course": {
                    "id": COURSE.value,
                    "courseId": "PH0000",
                    "displayName": "Synthetic course",
                },
            }
        ],
    }


def _content_payload() -> dict[str, object]:
    return {
        "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
        "results": [
            {
                "id": "content-a",
                "title": "Synthetic item",
                "position": 0,
                "contentHandler": {"id": "resource/x-bb-file"},
                "contentDetail": {
                    "resource/x-bb-file": {
                        "attachments": [
                            {
                                "id": ATTACHMENT.value,
                                "fileName": "synthetic.pdf",
                                "mimeType": "application/pdf",
                            }
                        ]
                    }
                },
            }
        ],
    }


def _target_page_with_next() -> dict[str, object]:
    payload = _content_payload()
    payload["paging"] = {
        "nextPage": f"{CONTENT_PATH}?limit=100&offset=1",
        "limit": 100,
        "count": 2,
        "offset": 0,
    }
    return payload


def _request_factory(resource: AttachmentId) -> SafeReadRequest:
    assert resource == ATTACHMENT
    return SafeReadRequest(ReadOperation.OPEN_RESOURCE_STREAM, STREAM_PATH, STREAM_QUERY)


def _adapter(executor) -> NtulearnSourceAdapter:
    return NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin=ORIGIN,
        resource_request_factory=_request_factory,
    )


def _engine(paths: RuntimePaths, sessions: _Sessions, adapter: NtulearnSourceAdapter) -> SyncEngine:
    database = Database(paths.database)
    store = ResourceStore(paths, ResourceRepository(database))
    store.initialize()
    return SyncEngine(sessions, adapter, DomainRepository(database), store)


def _seed(paths: RuntimePaths) -> None:
    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        del session, forward_credentials
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.LIST_CONTENT_CHILDREN:
            return WireResponse(200, json_body=_content_payload())
        return WireResponse(200, body_chunks=(PDF_BYTES,))

    engine = _engine(paths, _Sessions(), _adapter(executor))
    result = engine.sync_course(
        COURSE,
        window=TimeWindow(
            datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 2, 1, tzinfo=UTC)
        ),
        event_scopes=frozenset(),
    )
    assert result.status.value == "SUCCEEDED"


def test_standalone_fetch_rebuilds_adapter_context_after_restart(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "private-runtime")
    _seed(paths)
    operations: list[ReadOperation] = []

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        del session, forward_credentials
        operations.append(request.operation)
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.LIST_CONTENT_CHILDREN:
            return WireResponse(200, json_body=_content_payload())
        return WireResponse(200, body_chunks=(PDF_BYTES,))

    restarted = _engine(paths, _Sessions(), _adapter(executor))
    result = restarted.fetch_resource(ATTACHMENT, verify=True)

    assert result.outcome == "UNCHANGED_HASH_VERIFIED"
    assert operations == [
        ReadOperation.DISCOVER_COURSES,
        ReadOperation.LIST_CONTENT_CHILDREN,
        ReadOperation.OPEN_RESOURCE_STREAM,
    ]


def test_bounded_context_rediscovery_failure_retains_cached_bytes(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "private-runtime")
    _seed(paths)
    repository = ResourceRepository(Database(paths.database))
    before_versions = repository.list_versions(ATTACHMENT)
    assert len(before_versions) == 1
    before_blob = (paths.root / before_versions[0].blob_relpath).read_bytes()
    content_calls = 0
    stream_calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal content_calls, stream_calls
        del session, forward_credentials
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.OPEN_RESOURCE_STREAM:
            stream_calls += 1
            return WireResponse(200, body_chunks=(b"unexpected replacement",))
        content_calls += 1
        offset = int(dict(request.query)["offset"])
        next_offset = offset + 1
        return WireResponse(
            200,
            json_body={
                "paging": {
                    "nextPage": f"{CONTENT_PATH}?limit=100&offset={next_offset}",
                    "limit": 100,
                    "count": 20_000,
                    "offset": offset,
                },
                "results": [
                    {
                        "id": f"other-content-{offset}",
                        "title": "Synthetic unrelated item",
                        "position": offset,
                        "contentHandler": {"id": "resource/x-bb-document"},
                        "contentDetail": {},
                    }
                ],
            },
        )

    restarted = _engine(paths, _Sessions(), _adapter(executor))
    result = restarted.fetch_resource(ATTACHMENT, verify=True)

    after_versions = repository.list_versions(ATTACHMENT)
    assert result.outcome == "FAILED"
    assert result.error_category == "pagination_limit_reached"
    assert content_calls == 100
    assert stream_calls == 0
    assert after_versions == before_versions
    assert (paths.root / after_versions[0].blob_relpath).read_bytes() == before_blob


def test_target_observed_before_later_source_failure_does_not_open_stream(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "private-runtime")
    _seed(paths)
    repository = ResourceRepository(Database(paths.database))
    before_versions = repository.list_versions(ATTACHMENT)
    before_blob = (paths.root / before_versions[0].blob_relpath).read_bytes()
    content_calls = 0
    stream_calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal content_calls, stream_calls
        del session, forward_credentials
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.OPEN_RESOURCE_STREAM:
            stream_calls += 1
            return WireResponse(200, body_chunks=(b"unexpected replacement",))
        content_calls += 1
        if content_calls == 1:
            return WireResponse(200, json_body=_target_page_with_next())
        return WireResponse(503)

    restarted = _engine(paths, _Sessions(), _adapter(executor))
    result = restarted.fetch_resource(ATTACHMENT, verify=True)

    after_versions = repository.list_versions(ATTACHMENT)
    assert result.outcome == "FAILED"
    assert result.error_category == "source_unavailable"
    assert content_calls == 2
    assert stream_calls == 0
    assert after_versions == before_versions
    assert (paths.root / after_versions[0].blob_relpath).read_bytes() == before_blob


def test_target_observed_with_partial_coverage_does_not_open_stream(tmp_path: Path) -> None:
    paths = RuntimePaths(tmp_path / "private-runtime")
    _seed(paths)
    repository = ResourceRepository(Database(paths.database))
    before_versions = repository.list_versions(ATTACHMENT)
    before_blob = (paths.root / before_versions[0].blob_relpath).read_bytes()
    content_calls = 0
    stream_calls = 0

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        nonlocal content_calls, stream_calls
        del session, forward_credentials
        if request.operation is ReadOperation.DISCOVER_COURSES:
            return WireResponse(200, json_body=_course_payload())
        if request.operation is ReadOperation.OPEN_RESOURCE_STREAM:
            stream_calls += 1
            return WireResponse(200, body_chunks=(b"unexpected replacement",))
        content_calls += 1
        if content_calls == 1:
            return WireResponse(200, json_body=_target_page_with_next())
        if content_calls == 2:
            return WireResponse(
                200,
                json_body={
                    "paging": {
                        "nextPage": f"{CONTENT_PATH}?limit=100&offset=2",
                        "limit": 100,
                        "count": 2,
                        "offset": 1,
                    },
                    "results": [],
                },
            )
        return WireResponse(
            200,
            json_body={
                "paging": {"nextPage": "", "limit": 100, "count": 2, "offset": 2},
                "results": [],
            },
        )

    restarted = _engine(paths, _Sessions(), _adapter(executor))
    result = restarted.fetch_resource(ATTACHMENT, verify=True)

    after_versions = repository.list_versions(ATTACHMENT)
    assert result.outcome == "FAILED"
    assert result.error_category == "source_unavailable"
    assert content_calls == 3
    assert stream_calls == 0
    assert after_versions == before_versions
    assert (paths.root / after_versions[0].blob_relpath).read_bytes() == before_blob
