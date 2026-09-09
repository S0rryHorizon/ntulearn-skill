"""Public synthetic integration coverage for the stable M9 core facade."""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from ntulearn_skill.client import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    DueSourceRecord,
    EphemeralByteStream,
    Page,
    PageRequest,
    ReadPurpose,
    ResourceMetadataRecord,
    ScheduleSourceRecord,
    SessionExpired,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    TimeWindow,
)
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.core.api import (
    CoreService,
    CourseRef,
    EventFilter,
    FreshnessRequirement,
    ResourceRef,
    SourceLocatorRef,
    SyncPolicy,
)
from ntulearn_skill.core.results import ErrorCategory
from ntulearn_skill.search import SearchQuery, SourceReference, SourceReferenceKind
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync import SyncEngine

NOW = datetime(2034, 1, 2, 3, tzinfo=UTC)
WINDOW = TimeWindow(NOW, NOW + timedelta(days=90))


def _pdf(text: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, text)
    document.save()
    return output.getvalue()


def _time(value: datetime) -> SourceTime:
    return SourceTime(value, value.isoformat(), "UTC", TemporalPrecision.EXACT_TIME)


@dataclass
class _Sessions:
    marker: object = field(default_factory=object)
    acquired: list[ReadPurpose] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        self.acquired.append(purpose)
        return AuthorizedReadSession("synthetic", frozenset({purpose}), self.marker)

    def status(self) -> SessionStatus:
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        self.invalidated.append(reason)


class _Source:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.course = CourseId("synthetic", "course-one")
        self.assessment_content = ContentId("synthetic", "assessment-content")
        self.document_content = ContentId("synthetic", "document-content")
        self.attachment = AttachmentId("synthetic", "attachment-one")
        self.payload = _pdf("Synthetic optics reference for Quiz 1")
        self.announcement_error: Exception | None = None
        self.include_event_sources = True
        self.calls: list[ReadPurpose] = []
        self.capability_states = {
            capability: CapabilityState.SUPPORTED for capability in SourceCapability
        }

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(self.capability_states)

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        self.calls.append(ReadPurpose.DISCOVERY)
        return Page(
            (
                CourseSourceRecord(
                    self.course,
                    "PH0000",
                    "Synthetic Physics Course",
                    None,
                    Availability.ACTIVE,
                ),
            ),
            None,
            Coverage.COMPLETE,
        )

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]:
        self.calls.append(ReadPurpose.CONTENT)
        assert course == self.course and parent is None
        resource = ResourceMetadataRecord(
            self.attachment,
            self.document_content,
            "Synthetic optics handout",
            "synthetic-optics.pdf",
            "application/pdf",
        )
        items = []
        if self.include_event_sources:
            items.append(
                ContentSourceRecord(
                    self.assessment_content,
                    course,
                    None,
                    "resource/x-bb-asmt-test-link",
                    "Quiz 1",
                    0,
                    Availability.ACTIVE,
                    False,
                )
            )
        items.append(
            ContentSourceRecord(
                self.document_content,
                course,
                None,
                "document",
                "Synthetic optics notes",
                1,
                Availability.ACTIVE,
                False,
                resources=(resource,),
            )
        )
        return Page(tuple(items), None, Coverage.COMPLETE)

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]:
        self.calls.append(ReadPurpose.ANNOUNCEMENTS)
        if self.announcement_error is not None:
            raise self.announcement_error
        items = (
            (
                AnnouncementSourceRecord(
                    AnnouncementId("synthetic", "announcement-one"),
                    course,
                    "Quiz 1 reminder",
                    "Quiz 1 is due later this month.",
                    Availability.ACTIVE,
                    published_at=_time(NOW),
                ),
            )
            if self.include_event_sources
            else ()
        )
        return Page(items, None, Coverage.COMPLETE)

    def get_assessment(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        content: ContentId,
    ) -> AssessmentSourceRecord:
        self.calls.append(ReadPurpose.ASSESSMENTS)
        assert content == self.assessment_content
        return AssessmentSourceRecord(
            AssessmentId("synthetic", "assessment-one"),
            course,
            content,
            None,
            "Quiz 1",
            AssessmentSubtype.QUIZ,
            "Complete the synthetic assessment.",
            Availability.ACTIVE,
            due_at=_time(NOW + timedelta(days=10)),
        )

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self.calls.append(ReadPurpose.RESOURCE_METADATA)
        assert resource == self.attachment
        return ResourceMetadataRecord(
            resource,
            self.document_content,
            "Synthetic optics handout",
            "synthetic-optics.pdf",
            "application/pdf",
        )

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self.calls.append(ReadPurpose.RESOURCE_STREAM)
        assert resource == self.attachment
        return EphemeralByteStream((self.payload,))

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]:
        self.calls.append(ReadPurpose.SCHEDULE)
        return Page((), None, Coverage.COMPLETE)

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]:
        self.calls.append(ReadPurpose.DUE_ITEMS)
        return Page((), None, Coverage.COMPLETE)


@dataclass(frozen=True)
class _Harness:
    service: CoreService
    engine: SyncEngine
    source: _Source
    sessions: _Sessions
    database: Database
    store: ResourceStore
    course: CourseRef


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-synthetic-runtime")
    database = Database(paths.database)
    domain = DomainRepository(database)
    store = ResourceStore(paths, ResourceRepository(database))
    assert store.initialize() >= 9
    source = _Source()
    course = domain.put_course(
        source.course,
        code="PH0000",
        title="Synthetic Physics Course",
        observed_at=NOW,
    )
    sessions = _Sessions()
    engine = SyncEngine(sessions, source, domain, store)
    service = CoreService(
        database,
        runtime_paths=paths,
        sync_engine=engine,
        now=lambda: NOW,
    )
    return _Harness(
        service,
        engine,
        source,
        sessions,
        database,
        store,
        CourseRef(local_key=course.key),
    )


def _policy(*, fetch_resources: bool = True, max_jobs: int = 64) -> SyncPolicy:
    return SyncPolicy(WINDOW, fetch_resources=fetch_resources, max_jobs=max_jobs)


def _event_derivation(result: object) -> Coverage:
    coverage = getattr(result, "coverage")
    return next(item.coverage for item in coverage if item.data_kind == "event_derivation")


def test_real_sync_then_material_search_event_and_source_resolution(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    synced = harness.service.sync_course(harness.course, _policy())
    materials = harness.service.list_materials(harness.course)
    searched = harness.service.search_course(
        harness.course, SearchQuery("Synthetic optics reference")
    )
    events = harness.service.get_events(EventFilter(course=harness.course, window=WINDOW))

    assert synced.ok
    assert [item.display_title for item in materials.items] == ["Synthetic optics handout"]
    assert searched.items and searched.provenance
    assert events.items and _event_derivation(events) is Coverage.COMPLETE
    provenance = searched.provenance[0]
    resolved = harness.service.resolve_source(
        SourceLocatorRef(
            SourceReference(SourceReferenceKind(provenance.source_kind), provenance.source_key)
        )
    )
    assert resolved.ok and resolved.items
    assert resolved.provenance[0].source_key == provenance.source_key


def test_repeated_equal_and_changed_resource_cycles_keep_event_derivation_complete(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    assert harness.service.sync_course(harness.course, _policy()).ok
    resource = ResourceRepository(harness.database).get_resource(harness.source.attachment)
    assert resource is not None
    reference = ResourceRef(local_key=resource.key)

    repeated = harness.service.fetch_resource(reference)
    equal = harness.service.fetch_resource(reference, verify=True)
    harness.source.payload = _pdf("Synthetic changed optics reference for Quiz 1")
    changed = harness.service.fetch_resource(reference, verify=True)

    assert repeated.items[0].outcome == "UNCHANGED_ASSUMED"
    assert equal.items[0].outcome == "UNCHANGED_HASH_VERIFIED"
    assert changed.items[0].outcome == "BINARY_CHANGED"
    assert changed.items[0].version.version_number == 2

    assert harness.service.sync_course(harness.course, _policy()).ok
    events = harness.service.get_events(EventFilter(course=harness.course, window=WINDOW))
    assert _event_derivation(events) is Coverage.COMPLETE


def test_fetch_postcommit_warning_is_partial_after_the_version_commits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    assert harness.service.sync_course(harness.course, _policy(fetch_resources=False)).ok
    resource = ResourceRepository(harness.database).get_resource(harness.source.attachment)
    assert resource is not None

    def fail_materialize(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic postcommit view failure")

    monkeypatch.setattr(harness.store, "_materialize_version", fail_materialize)
    fetched = harness.service.fetch_resource(ResourceRef(local_key=resource.key), verify=True)

    assert fetched.ok
    assert fetched.completeness is Coverage.PARTIAL
    assert fetched.items[0].version is not None
    assert ResourceRepository(harness.database).get_current_version(harness.source.attachment)
    assert {warning.code for warning in fetched.warnings} >= {"browse_view_requires_repair"}


def test_expired_announcement_refresh_keeps_cached_items_and_returns_typed_error(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    assert harness.service.sync_course(harness.course, _policy()).ok
    harness.source.announcement_error = SessionExpired()
    harness.service.now = lambda: datetime.now(UTC) + timedelta(days=1)

    refreshed = harness.service.get_announcements(
        harness.course, freshness=FreshnessRequirement.require_current()
    )

    assert [item.title for item in refreshed.items] == ["Quiz 1 reminder"]
    assert refreshed.refresh_attempted and refreshed.local_reads == 2
    assert ErrorCategory.SESSION_EXPIRED in {error.category for error in refreshed.errors}
    assert harness.sessions.invalidated == ["session_expired"]


def test_parsed_without_extraction_cannot_make_empty_upcoming_conclusive(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.source.include_event_sources = False

    synced = harness.service.sync_course(harness.course, _policy(max_jobs=1))
    with harness.database.connect() as connection:
        parsed = int(connection.execute("SELECT COUNT(*) FROM parsed_document").fetchone()[0])
        extracted = int(
            connection.execute(
                """SELECT COUNT(*) FROM extraction_record
                WHERE input_kind = 'resource_version'"""
            ).fetchone()[0]
        )
    upcoming = harness.service.get_upcoming_events(WINDOW, harness.course)

    assert synced.completeness is not Coverage.COMPLETE
    assert any(
        item.data_kind == "local_jobs" and item.coverage is Coverage.PARTIAL
        for item in synced.coverage
    )
    assert parsed == 1 and extracted == 0
    assert upcoming.items == ()
    assert _event_derivation(upcoming) is Coverage.PARTIAL
    assert upcoming.completeness is not Coverage.COMPLETE
    assert not upcoming.conclusive_empty
