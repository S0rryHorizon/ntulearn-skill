from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from ntulearn_skill.client import (
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    NtulearnSourceAdapter,
    Page,
    PageRequest,
    ReadOnlyTransport,
    ReadPurpose,
    ResolvedReadRequest,
    ResourceMetadataRecord,
    SessionExpired,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    WireResponse,
)
from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId, Coverage
from ntulearn_skill.storage import Database, DomainRepository, ResourceRepository
from ntulearn_skill.sync import DiscoverySync, SyncWarning


@dataclass
class FakeSessions:
    provider_name: str = "synthetic"
    marker: object = field(default_factory=object)
    invalidated: list[str] = field(default_factory=list)

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        return AuthorizedReadSession(self.provider_name, frozenset(ReadPurpose), self.marker)

    def status(self) -> SessionStatus:
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        self.invalidated.append(reason)


class FakeSource:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.course_pages: dict[str | None, Page[CourseSourceRecord] | Exception] = {}
        self.content_pages: dict[
            tuple[CourseId, ContentId | None, str | None], Page[ContentSourceRecord] | Exception
        ] = {}

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            {
                SourceCapability.COURSE_DISCOVERY: CapabilityState.SUPPORTED,
                SourceCapability.CONTENT_TREE: CapabilityState.SUPPORTED,
            }
        )

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        result = self.course_pages[page.cursor]
        if isinstance(result, Exception):
            raise result
        return result

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]:
        result = self.content_pages[(course, parent, page.cursor)]
        if isinstance(result, Exception):
            raise result
        return result

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        raise AssertionError("not used by discovery sync")

    def open_resource_stream(self, session, resource):
        raise AssertionError("not used by discovery sync")


def _repository(tmp_path: Path) -> DomainRepository:
    repository = DomainRepository(Database(tmp_path / "private" / "db" / "metadata.sqlite3"))
    assert repository.initialize() == 5
    return repository


def _course(value: str, code: str) -> CourseSourceRecord:
    return CourseSourceRecord(
        CourseId("synthetic", value), code, f"Example {code} Course", None, Availability.ACTIVE
    )


def _content(
    course: CourseId,
    value: str,
    *,
    parent: ContentId | None,
    title: str,
    position: int,
    container: bool = False,
    resources: tuple[ResourceMetadataRecord, ...] = (),
) -> ContentSourceRecord:
    return ContentSourceRecord(
        ContentId("synthetic", value),
        course,
        parent,
        "folder" if container else "document",
        title,
        position,
        Availability.ACTIVE,
        container,
        {"content_type": "folder" if container else "document"},
        resources,
    )


def test_sync_persists_multi_page_courses_and_generic_nested_tree(tmp_path: Path) -> None:
    source = FakeSource()
    sessions = FakeSessions()
    repository = _repository(tmp_path)
    physics = _course("course-physics", "PH0000")
    computing = _course("course-computing", "CS0000")
    source.course_pages = {
        None: Page((physics,), "course-page-2", Coverage.COMPLETE),
        "course-page-2": Page((computing,), None, Coverage.COMPLETE),
    }
    lab = _content(
        physics.remote_id,
        "lab-container",
        parent=None,
        title="Laboratory sequence",
        position=0,
        container=True,
    )
    attachment = ResourceMetadataRecord(
        AttachmentId("synthetic", "attachment-guide"),
        ContentId("synthetic", "guide-document"),
        "Invented guide",
        "invented-guide.pdf",
        "application/pdf",
    )
    guide = _content(
        physics.remote_id,
        "guide-document",
        parent=lab.remote_id,
        title="Guide",
        position=0,
        resources=(attachment,),
    )
    tool = _content(
        computing.remote_id,
        "practice-tool",
        parent=None,
        title="Practice tool",
        position=0,
    )
    source.content_pages = {
        (physics.remote_id, None, None): Page((lab,), None, Coverage.COMPLETE),
        (physics.remote_id, lab.remote_id, None): Page((guide,), None, Coverage.COMPLETE),
        (computing.remote_id, None, None): Page((tool,), None, Coverage.COMPLETE),
    }

    result = DiscoverySync(sessions=sessions, source=source, repository=repository).run(page_size=1)

    assert result.status.value == "SUCCEEDED"
    assert result.counts == {
        "content_observed": 3,
        "courses_observed": 2,
        "resources_discovered": 1,
        "scopes_complete": 3,
        "scopes_failed": 0,
        "scopes_partial": 0,
    }
    stored_guide = repository.get_content_node(guide.remote_id)
    stored_lab = repository.get_content_node(lab.remote_id)
    assert stored_guide is not None and stored_lab is not None
    assert stored_guide.parent_key == stored_lab.key
    stored_resource = ResourceRepository(repository.database).get_resource(attachment.remote_id)
    assert stored_resource is not None
    assert stored_resource.current_version_key is None
    assert all(scope.pagination_complete for scope in result.scopes)


def test_transport_adapter_sync_sqlite_bridge_persists_tree_and_resource(tmp_path: Path) -> None:
    course_path = "/learn/api/v1/users/me/memberships"
    root_path = "/learn/api/v1/courses/course-alpha/contents/ROOT/children"
    child_path = "/learn/api/v1/courses/course-alpha/contents/container-lab/children"
    payloads: dict[tuple[str, str], dict[str, object]] = {
        (course_path, "0"): {
            "paging": {"nextPage": "", "limit": 1, "count": 1, "offset": 0},
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
        (root_path, "0"): {
            "paging": {
                "nextPage": f"{root_path}?limit=1&offset=1",
                "limit": 1,
                "count": 2,
                "offset": 0,
            },
            "results": [
                {
                    "id": "container-lab",
                    "courseId": "course-alpha",
                    "title": "Laboratory sequence",
                    "position": 0,
                    "contentHandler": "resource/x-example-folder",
                    "contentDetail": {"isFolder": True},
                    "visibility": "VISIBLE",
                }
            ],
        },
        (root_path, "1"): {
            "paging": {"nextPage": "", "limit": 1, "count": 2, "offset": 1},
            "results": [
                {
                    "id": "document-notes",
                    "courseId": "course-alpha",
                    "title": "Reference notes",
                    "position": 1,
                    "contentHandler": "resource/x-example-document",
                    "contentDetail": {
                        "attachments": [
                            {
                                "id": "attachment-notes",
                                "fileName": "invented-notes.pdf",
                                "displayName": "Invented notes",
                                "mimeType": "application/pdf",
                            }
                        ]
                    },
                    "visibility": "VISIBLE",
                }
            ],
        },
        (child_path, "0"): {
            "paging": {"nextPage": None, "limit": 1, "count": 1, "offset": 0},
            "results": [
                {
                    "id": "nested-tool",
                    "courseId": "course-alpha",
                    "title": "External practice",
                    "position": 0,
                    "contentHandler": "resource/x-example-tool",
                    "contentDetail": {},
                    "visibility": "VISIBLE",
                }
            ],
        },
    }
    requests: list[tuple[str, str]] = []

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        offset = dict(request.query)["offset"]
        requests.append((request.target, offset))
        return WireResponse(200, json_body=payloads[(request.target, offset)])

    repository = _repository(tmp_path)
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor), source_origin="https://learn.example.invalid"
    )

    result = DiscoverySync(
        sessions=FakeSessions(provider_name="ntulearn"),
        source=adapter,
        repository=repository,
    ).run(page_size=1)

    assert result.status.value == "SUCCEEDED"
    assert result.counts == {
        "content_observed": 3,
        "courses_observed": 1,
        "resources_discovered": 1,
        "scopes_complete": 2,
        "scopes_failed": 0,
        "scopes_partial": 0,
    }
    assert requests == [
        (course_path, "0"),
        (root_path, "0"),
        (root_path, "1"),
        (child_path, "0"),
    ]
    course = repository.get_course(CourseId("ntulearn", "course-alpha"))
    container = repository.get_content_node(ContentId("ntulearn", "container-lab"))
    nested = repository.get_content_node(ContentId("ntulearn", "nested-tool"))
    resource = ResourceRepository(repository.database).get_resource(
        AttachmentId("ntulearn", "attachment-notes")
    )
    assert course is not None and course.code == "PH0000"
    assert container is not None and container.sanitized_metadata == {
        "content_type": "resource/x-example-folder",
        "display_style": "",
    }
    assert nested is not None and nested.parent_key == container.key
    assert resource is not None and resource.current_version_key is None


def test_mid_pagination_session_expiry_preserves_items_and_marks_partial(tmp_path: Path) -> None:
    source = FakeSource()
    sessions = FakeSessions()
    repository = _repository(tmp_path)
    course = _course("course-alpha", "PH0000")
    source.course_pages = {None: Page((course,), None, Coverage.COMPLETE)}
    first = _content(
        course.remote_id,
        "first-item",
        parent=None,
        title="First item",
        position=0,
    )
    source.content_pages = {
        (course.remote_id, None, None): Page((first,), "content-page-2", Coverage.COMPLETE),
        (course.remote_id, None, "content-page-2"): SessionExpired(),
    }

    result = DiscoverySync(sessions=sessions, source=source, repository=repository).run()

    content_scope = next(scope for scope in result.scopes if scope.data_kind == "content")
    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert content_scope.coverage is Coverage.PARTIAL
    assert content_scope.pages_seen == 1
    assert content_scope.items_seen == 1
    assert content_scope.failure_category == "session_expired"
    assert repository.get_content_node(first.remote_id) is not None
    assert sessions.invalidated == ["session_expired"]


def test_pagination_cycle_and_page_cap_are_bounded_and_observable(tmp_path: Path) -> None:
    cycle_source = FakeSource()
    first = _course("course-alpha", "PH0000")
    cycle_source.course_pages = {
        None: Page((first,), "repeat", Coverage.COMPLETE),
        "repeat": Page((), "repeat", Coverage.COMPLETE),
    }
    cycle_result = DiscoverySync(
        sessions=FakeSessions(), source=cycle_source, repository=_repository(tmp_path / "cycle")
    ).run(include_content=False)
    cycle_scope = cycle_result.scopes[0]
    assert cycle_scope.coverage is Coverage.PARTIAL
    assert cycle_scope.pages_seen == 2
    assert SyncWarning.PAGINATION_CYCLE in cycle_scope.warnings

    cap_source = FakeSource()
    cap_source.course_pages = {None: Page((first,), "more", Coverage.COMPLETE)}
    cap_result = DiscoverySync(
        sessions=FakeSessions(), source=cap_source, repository=_repository(tmp_path / "cap")
    ).run(include_content=False, max_pages_per_scope=1)
    cap_scope = cap_result.scopes[0]
    assert cap_scope.coverage is Coverage.PARTIAL
    assert cap_scope.pages_seen == 1
    assert SyncWarning.PAGE_CAP_REACHED in cap_scope.warnings


def test_unexpected_provider_failure_finishes_run_with_safe_category(tmp_path: Path) -> None:
    source = FakeSource()
    source.course_pages = {None: RuntimeError("invented private diagnostic")}
    repository = _repository(tmp_path)

    result = DiscoverySync(sessions=FakeSessions(), source=source, repository=repository).run(
        include_content=False
    )

    assert result.status.value == "FAILED"
    assert result.error_category == "source_unavailable"
    assert result.scopes[0].coverage is Coverage.FAILED
    assert "diagnostic" not in repr(result)

    connection = repository.database.connect()
    try:
        row = connection.execute(
            "SELECT status, ended_at FROM sync_run WHERE sync_run_key = ?", (result.key,)
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row["status"] == "FAILED"
    assert row["ended_at"] is not None
