from __future__ import annotations

import io
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from ntulearn_skill.client import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    AuthenticationRequired,
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    DueSourceRecord,
    EphemeralByteStream,
    NtulearnSourceAdapter,
    Page,
    PageRequest,
    ReadOnlyTransport,
    ReadOperation,
    ReadPurpose,
    ResolvedReadRequest,
    ResourceMetadataRecord,
    ScheduleSourceRecord,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    TimeWindow,
    WireResponse,
)
from ntulearn_skill.core import (
    AnnouncementId,
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    FetchDecision,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events import EventReconciler
from ntulearn_skill.search import SearchQuery, SearchService
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync.engine import CourseSelectionPolicy, SyncEngine
from ntulearn_skill.sync.event_sources import EventSourceScope
from ntulearn_skill.sync.freshness import (
    FreshnessRequirement,
    FreshnessStatus,
    retrieve_with_freshness,
)
from ntulearn_skill.sync.state import ScopeKey

WINDOW = TimeWindow(
    datetime(2030, 1, 1, tzinfo=UTC),
    datetime(2030, 4, 1, tzinfo=UTC),
)


def _pdf(text: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, text)
    document.save()
    return output.getvalue()


def _source_time(value: datetime) -> SourceTime:
    return SourceTime(value, value.isoformat(), "UTC", TemporalPrecision.EXACT_TIME)


@dataclass
class _Sessions:
    marker: object = field(default_factory=object)
    purposes: list[ReadPurpose] = field(default_factory=list)
    invalidations: list[str] = field(default_factory=list)

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        self.purposes.append(purpose)
        return AuthorizedReadSession("synthetic", frozenset({purpose}), self.marker)

    def status(self) -> SessionStatus:
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        self.invalidations.append(reason)


class _Source:
    provider_name = "synthetic"

    def __init__(self) -> None:
        self.course = CourseId("synthetic", "course-ph0000")
        self.other_course = CourseId("synthetic", "course-ph0001")
        self.content_id = ContentId("synthetic", "content-notes")
        self.attachment_id = AttachmentId("synthetic", "attachment-notes")
        self.content_coverage = Coverage.COMPLETE
        self.include_resource = True
        self.payload = _pdf("PH0000 baseline optics notes")
        self.announcement_error: Exception | None = None
        self.announcement_enabled = False
        self.calls: Counter[str] = Counter()

    @property
    def metadata(self) -> ResourceMetadataRecord:
        return ResourceMetadataRecord(
            self.attachment_id,
            self.content_id,
            "Invented notes",
            "invented-notes.pdf",
            "application/pdf",
        )

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            {capability: CapabilityState.SUPPORTED for capability in SourceCapability}
        )

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        self.calls["courses"] += 1
        return Page(
            (
                CourseSourceRecord(
                    self.course,
                    "PH0000",
                    "Synthetic Integration Course",
                    "AY2030",
                    Availability.ACTIVE,
                ),
                CourseSourceRecord(
                    self.other_course,
                    "PH0001",
                    "Synthetic Excluded Course",
                    "AY2030",
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
        self.calls["content"] += 1
        assert course == self.course and parent is None
        items: tuple[ContentSourceRecord, ...] = ()
        if self.include_resource:
            items = (
                ContentSourceRecord(
                    self.content_id,
                    course,
                    None,
                    "document",
                    "Invented lecture notes",
                    0,
                    Availability.ACTIVE,
                    False,
                    resources=(self.metadata,),
                ),
            )
        return Page(items, None, self.content_coverage)

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self.calls["resource_metadata"] += 1
        assert resource == self.attachment_id
        return self.metadata

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self.calls["resource_stream"] += 1
        assert resource == self.attachment_id
        return EphemeralByteStream((self.payload,))

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]:
        self.calls["announcements"] += 1
        if self.announcement_error is not None:
            raise self.announcement_error
        items: tuple[AnnouncementSourceRecord, ...] = ()
        if self.announcement_enabled:
            items = (
                AnnouncementSourceRecord(
                    AnnouncementId("synthetic", "announcement-quiz"),
                    course,
                    "Invented quiz reminder",
                    "The invented quiz is due 2030-02-10 10:00 UTC.",
                    Availability.ACTIVE,
                    published_at=_source_time(datetime(2030, 1, 2, tzinfo=UTC)),
                ),
            )
        return Page(items, None, Coverage.COMPLETE)

    def get_assessment(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        content: ContentId,
    ) -> AssessmentSourceRecord:
        self.calls["assessments"] += 1
        raise AssertionError("the synthetic content is not an assessment")

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]:
        self.calls["schedule"] += 1
        return Page((), None, Coverage.COMPLETE)

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]:
        self.calls["due_items"] += 1
        return Page((), None, Coverage.COMPLETE)


@dataclass(frozen=True)
class _Harness:
    engine: SyncEngine
    source: _Source
    sessions: _Sessions
    database: Database
    resources: ResourceRepository
    domain: DomainRepository


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-runtime")
    database = Database(paths.database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() >= 9
    source = _Source()
    sessions = _Sessions()
    domain = DomainRepository(database)
    engine = SyncEngine(
        sessions,
        source,
        domain,
        store,
        verification_interval=timedelta(days=30),
    )
    return _Harness(engine, source, sessions, database, resources, domain)


def _sync_resources(harness: _Harness, *, verify: bool = False):
    return harness.engine.sync_course(
        harness.source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        verify_resources=verify,
    )


def _fetch_decisions(database: Database) -> list[str]:
    connection = database.connect()
    try:
        rows = connection.execute(
            """SELECT fetch_decision FROM resource_fetch_receipt
            ORDER BY fetch_receipt_key"""
        ).fetchall()
    finally:
        connection.close()
    return [str(row["fetch_decision"]) for row in rows]


def test_repeat_sync_skips_bytes_then_forced_verify_reuses_version(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    first = _sync_resources(harness)
    repeated = _sync_resources(harness)
    verified = _sync_resources(harness, verify=True)

    assert first.counts["resources_fetched"] == 1
    assert repeated.counts["resources_fetched"] == 1
    assert verified.counts["resources_fetched"] == 1
    assert harness.source.calls["resource_stream"] == 2
    assert len(harness.resources.list_versions(harness.source.attachment_id)) == 1
    assert _fetch_decisions(harness.database) == [
        FetchDecision.FETCHED.value,
        FetchDecision.NOT_NEEDED.value,
        FetchDecision.REUSED_VERIFIED.value,
    ]


def test_forced_verify_detects_changed_bytes_with_unchanged_metadata(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.source.payload = _pdf("PH0000 baseline optics notes")
    _sync_resources(harness)
    first_version = harness.resources.get_current_version(harness.source.attachment_id)
    assert first_version is not None

    harness.source.payload = _pdf("PH0000 revised diffraction notes")
    changed = _sync_resources(harness, verify=True)
    versions = harness.resources.list_versions(harness.source.attachment_id)

    assert changed.counts["resources_fetched"] == 1
    assert len(versions) == 2
    assert versions[0].sha256 == first_version.sha256
    assert versions[1].sha256 != first_version.sha256
    assert SearchService(harness.database).search(SearchQuery("diffraction")).items
    assert SearchService(harness.database).search(SearchQuery("baseline optics")).items


def test_complete_omission_partial_omission_and_reappearance_keep_history(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _sync_resources(harness)
    original = harness.resources.get_current_version(harness.source.attachment_id)
    assert original is not None

    harness.source.include_resource = False
    omitted = _sync_resources(harness)
    missing = harness.resources.get_resource(harness.source.attachment_id)
    assert omitted.counts["resources_marked_missing"] == 1
    assert missing is not None and missing.availability is Availability.MISSING

    harness.source.content_coverage = Coverage.PARTIAL
    uncertain = _sync_resources(harness)
    preserved = harness.resources.get_resource(harness.source.attachment_id)
    assert uncertain.counts["uncertainty_observations"] == 1
    assert preserved is not None and preserved.availability is Availability.MISSING

    harness.source.content_coverage = Coverage.COMPLETE
    harness.source.include_resource = True
    reappeared = _sync_resources(harness)
    active = harness.resources.get_resource(harness.source.attachment_id)
    assert reappeared.counts["resources_marked_missing"] == 0
    assert active is not None and active.availability is Availability.ACTIVE
    assert harness.resources.get_current_version(harness.source.attachment_id) == original
    assert len(harness.resources.list_versions(harness.source.attachment_id)) == 1


def test_announcement_and_resource_create_events_when_calendar_is_empty(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.source.announcement_enabled = True
    harness.source.payload = _pdf(
        "Assignment 2 is due 2030-03-01 09:00 UTC. This is synthetic PH0000 material."
    )

    result = harness.engine.sync_course(harness.source.course, window=WINDOW)
    events = EventReconciler(harness.database).list_events(harness.source.course)

    assert result.counts["jobs_failed"] == 0
    assert harness.source.calls["schedule"] == 1
    assert harness.source.calls["due_items"] == 1
    assert len(events) >= 2
    assert any("quiz" in event.title.casefold() for event in events)
    assert any("assignment" in event.title.casefold() for event in events)


def test_auth_failure_retains_prior_event_and_makes_scope_unsatisfied(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.source.announcement_enabled = True
    announcement_scope = ScopeKey(
        "synthetic", harness.source.course, EventSourceScope.ANNOUNCEMENTS.value
    )
    harness.engine.quick_sync(
        harness.source.course,
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        include_content=False,
    )
    before = EventReconciler(harness.database).list_events(harness.source.course)
    prior_state = harness.engine.state.get(announcement_scope)
    assert before and prior_state is not None and prior_state.last_complete_at is not None

    harness.source.announcement_error = AuthenticationRequired()
    partial = harness.engine.quick_sync(
        harness.source.course,
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        include_content=False,
    )
    after = EventReconciler(harness.database).list_events(harness.source.course)
    state = harness.engine.state.get(announcement_scope)

    assert partial.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert after == before
    assert state is not None
    assert state.last_complete_at == prior_state.last_complete_at
    assert state.latest_coverage is Coverage.FAILED
    result = retrieve_with_freshness(
        announcement_scope,
        FreshnessRequirement.require_current(),
        load_local=lambda: after,
        load_state=harness.engine.state.get,
        refresh=None,
        now=datetime.now(UTC) + timedelta(seconds=1),
    )
    assert result.decision.status in {FreshnessStatus.STALE, FreshnessStatus.UNKNOWN}
    assert not result.decision.satisfied


def test_exact_refresh_calls_only_requested_source_endpoint(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.domain.put_course(
        harness.source.course,
        code="PH0000",
        title="Synthetic Integration Course",
    )
    harness.source.announcement_enabled = True
    scope = ScopeKey("synthetic", harness.source.course, "announcements")

    result = harness.engine.refresh_scope(scope)

    assert result.status.value == "SUCCEEDED"
    assert harness.source.calls == Counter({"announcements": 1})


def test_cache_and_historical_reads_are_local_and_stale_refresh_runs_once(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    harness.domain.put_course(
        harness.source.course,
        code="PH0000",
        title="Synthetic Integration Course",
    )
    scope = ScopeKey("synthetic", harness.source.course, "announcements")
    harness.engine.refresh_scope(scope)
    harness.source.calls.clear()
    local_reads = 0

    def load_local() -> tuple[object, ...]:
        nonlocal local_reads
        local_reads += 1
        return tuple(EventReconciler(harness.database).list_events(harness.source.course))

    def refresh(value: ScopeKey) -> None:
        harness.engine.refresh_scope(value)

    cache = retrieve_with_freshness(
        scope,
        FreshnessRequirement.cache_only(),
        load_local=load_local,
        load_state=harness.engine.state.get,
        refresh=refresh,
        now=datetime(2040, 1, 1, tzinfo=UTC),
    )
    historical = retrieve_with_freshness(
        scope,
        FreshnessRequirement.with_max_age(timedelta(seconds=1)),
        load_local=load_local,
        load_state=harness.engine.state.get,
        refresh=refresh,
        now=datetime(2040, 1, 1, tzinfo=UTC),
        historical=True,
    )
    refreshed = retrieve_with_freshness(
        scope,
        FreshnessRequirement.with_max_age(timedelta(seconds=1)),
        load_local=load_local,
        load_state=harness.engine.state.get,
        refresh=refresh,
        now=datetime(2040, 1, 1, tzinfo=UTC),
    )

    assert not cache.refresh_attempted
    assert not historical.refresh_attempted
    assert refreshed.refresh_attempted
    assert harness.source.calls == Counter({"announcements": 1})
    assert local_reads == 4


def test_sync_all_honors_selected_course_inclusion(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    selected = CourseSelectionPolicy(frozenset({harness.source.course}))

    result = harness.engine.sync_all(
        selected,
        window=WINDOW,
        event_scopes=frozenset(),
        fetch_resources=False,
    )

    assert result.status.value == "SUCCEEDED"
    assert harness.source.calls["courses"] == 1
    assert harness.source.calls["content"] == 1
    assert harness.domain.get_course(harness.source.course) is not None
    assert harness.domain.get_course(harness.source.other_course) is not None
    assert harness.domain.get_content_node(harness.source.content_id) is not None


def test_real_adapter_authorizes_course_before_followup_source_read(tmp_path: Path) -> None:
    course_id = CourseId("ntulearn", "course-alpha")
    marker = object()
    operations: list[ReadOperation] = []
    payloads: dict[str, dict[str, object]] = {
        "/learn/api/v1/users/me/memberships": {
            "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
            "results": [
                {
                    "isAvailable": True,
                    "course": {
                        "id": "course-alpha",
                        "courseId": "PH0000",
                        "displayName": "Synthetic Adapter Course",
                    },
                }
            ],
        },
        "/learn/api/v1/courses/course-alpha/announcements": {
            "paging": {"nextPage": "", "limit": 100, "count": 1, "offset": 0},
            "results": [
                {
                    "id": "announcement-one",
                    "title": "Synthetic adapter reminder",
                    "body": {"rawText": "Assignment 1 is due 2030-02-10 23:59 +08:00."},
                    "createdDate": "2030-01-02T09:00:00+0800",
                    "visibility": "VISIBLE",
                }
            ],
        },
    }

    def executor(
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        assert forward_credentials
        operations.append(request.operation)
        return WireResponse(200, json_body=payloads[request.target])

    @dataclass
    class AdapterSessions:
        def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
            return AuthorizedReadSession("ntulearn", frozenset({purpose}), marker)

        def status(self) -> SessionStatus:
            return SessionStatus.READY

        def invalidate(self, reason: str) -> None:
            raise AssertionError(f"unexpected invalidation: {reason}")

    paths = RuntimePaths(tmp_path / "private-adapter-runtime")
    database = Database(paths.database)
    store = ResourceStore(paths, ResourceRepository(database))
    assert store.initialize() >= 9
    adapter = NtulearnSourceAdapter(
        ReadOnlyTransport(executor),
        source_origin="https://learn.example.invalid",
    )
    engine = SyncEngine(AdapterSessions(), adapter, DomainRepository(database), store)

    result = engine.quick_sync(
        course_id,
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        include_content=False,
    )

    assert result.status.value == "SUCCEEDED"
    assert operations == [
        ReadOperation.DISCOVER_COURSES,
        ReadOperation.LIST_ANNOUNCEMENTS,
    ]
    assert EventReconciler(database).list_events(course_id)


def test_bounded_local_processing_reports_partial_while_jobs_remain(tmp_path: Path) -> None:
    harness = _harness(tmp_path)

    result = harness.engine.sync_course(
        harness.source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        max_jobs=1,
    )
    connection = harness.database.connect()
    try:
        pending = int(
            connection.execute(
                "SELECT COUNT(*) FROM local_job WHERE status = 'PENDING'"
            ).fetchone()[0]
        )
    finally:
        connection.close()

    assert pending > 0
    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    processing_scope = next(scope for scope in result.scopes if scope.data_kind == "local_jobs")
    assert processing_scope.coverage is Coverage.PARTIAL
