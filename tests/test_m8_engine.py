from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest
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
from ntulearn_skill.events import EventReconciler, EventRepository
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
from ntulearn_skill.sync.state import ScopeKey

NOW = datetime(2030, 1, 2, tzinfo=UTC)
WINDOW = TimeWindow(datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 4, 1, tzinfo=UTC))


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
        self.calls: list[ReadPurpose] = []
        self.content_coverage = Coverage.COMPLETE
        self.include_resource = True
        self.announcement_error: Exception | None = None
        self.course_error: Exception | None = None
        self.include_course = True
        self.capability_states = {
            SourceCapability.COURSE_DISCOVERY: CapabilityState.SUPPORTED,
            SourceCapability.CONTENT_TREE: CapabilityState.SUPPORTED,
            SourceCapability.ANNOUNCEMENTS: CapabilityState.SUPPORTED,
            SourceCapability.ASSESSMENT_DETAILS: CapabilityState.SUPPORTED,
            SourceCapability.RESOURCE_METADATA: CapabilityState.SUPPORTED,
            SourceCapability.RESOURCE_STREAM: CapabilityState.SUPPORTED,
        }

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(self.capability_states)

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        self.calls.append(ReadPurpose.DISCOVERY)
        if self.course_error is not None:
            raise self.course_error
        items = (
            (
                CourseSourceRecord(
                    self.course,
                    "PH0000",
                    "Example Physics Course",
                    None,
                    Availability.ACTIVE,
                ),
            )
            if self.include_course
            else ()
        )
        return Page(
            items,
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
            "Invented notes",
            "invented-notes.pdf",
            "application/pdf",
        )
        items = [
            ContentSourceRecord(
                self.assessment_content,
                course,
                None,
                "resource/x-bb-asmt-test-link",
                "Quiz 1",
                0,
                Availability.ACTIVE,
                False,
            ),
            ContentSourceRecord(
                self.document_content,
                course,
                None,
                "document",
                "Invented optics notes",
                1,
                Availability.ACTIVE,
                False,
                resources=(resource,) if self.include_resource else (),
            ),
        ]
        return Page(tuple(items), None, self.content_coverage)

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]:
        self.calls.append(ReadPurpose.ANNOUNCEMENTS)
        if self.announcement_error is not None:
            raise self.announcement_error
        return Page(
            (
                AnnouncementSourceRecord(
                    AnnouncementId("synthetic", "announcement-one"),
                    course,
                    "Quiz 1 reminder",
                    "Quiz 1 is due 2030-02-10 10:00 UTC.",
                    Availability.ACTIVE,
                    published_at=_time(NOW),
                ),
            ),
            None,
            Coverage.COMPLETE,
        )

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
            due_at=_time(datetime(2030, 2, 10, 10, tzinfo=UTC)),
        )

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self.calls.append(ReadPurpose.RESOURCE_METADATA)
        assert resource == self.attachment
        return ResourceMetadataRecord(
            self.attachment,
            self.document_content,
            "Invented notes",
            "invented-notes.pdf",
            "application/pdf",
        )

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self.calls.append(ReadPurpose.RESOURCE_STREAM)
        assert resource == self.attachment
        return EphemeralByteStream((_pdf("Synthetic optics reference for Quiz 1"),))

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]:
        raise AssertionError("not selected")

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]:
        raise AssertionError("not selected")


def _engine(tmp_path: Path) -> tuple[SyncEngine, _Source, _Sessions, Database]:
    paths = RuntimePaths(tmp_path / "private-runtime")
    database = Database(paths.database)
    store = ResourceStore(paths, ResourceRepository(database))
    assert store.initialize() >= 9
    source = _Source()
    sessions = _Sessions()
    engine = SyncEngine(sessions, source, DomainRepository(database), store)
    return engine, source, sessions, database


def test_sync_course_runs_source_to_sqlite_parse_search_and_events(tmp_path: Path) -> None:
    engine, source, _sessions, database = _engine(tmp_path)

    result = engine.sync_course(
        source.course,
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS, EventSourceScope.ASSESSMENTS}),
    )

    assert result.status.value == "SUCCEEDED"
    assert result.counts["resources_fetched"] == 1
    assert result.counts["observations"] == 2
    assert result.counts["jobs_failed"] == 0
    assert ReadPurpose.ASSESSMENTS in source.calls
    assert SearchService(database).search(SearchQuery("Synthetic optics")).items
    assert EventReconciler(database).list_events(source.course)


def test_sync_all_applies_explicit_selection_to_current_accessible_courses(
    tmp_path: Path,
) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)

    result = engine.sync_all(
        CourseSelectionPolicy(frozenset({source.course})),
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        fetch_resources=False,
    )

    assert result.status.value == "SUCCEEDED"
    assert source.calls.count(ReadPurpose.DISCOVERY) == 1
    assert source.calls.count(ReadPurpose.CONTENT) == 1
    assert source.calls.count(ReadPurpose.ANNOUNCEMENTS) == 1
    assert ReadPurpose.ASSESSMENTS not in source.calls


def test_targeted_content_refresh_calls_only_content_source(tmp_path: Path) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)
    engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        fetch_resources=False,
    )
    source.calls.clear()

    result = engine.refresh_scope(ScopeKey("synthetic", source.course, "content"))

    assert result.status.value == "SUCCEEDED"
    assert source.calls == [ReadPurpose.CONTENT]
    assert [scope.data_kind for scope in result.scopes] == ["content"]


def test_targeted_expired_announcement_refresh_fails_and_invalidates(tmp_path: Path) -> None:
    engine, source, sessions, _database = _engine(tmp_path)
    engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        include_content=False,
    )
    source.calls.clear()
    source.announcement_error = SessionExpired()

    result = engine.refresh_scope(ScopeKey("synthetic", source.course, "announcements"))

    assert result.status.value == "FAILED"
    assert result.error_category == "session_expired"
    assert source.calls == [ReadPurpose.ANNOUNCEMENTS]
    assert sessions.invalidated == ["session_expired"]


def test_complete_content_omission_marks_resource_missing_but_partial_preserves(
    tmp_path: Path,
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)
    engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        fetch_resources=False,
    )
    source.include_resource = False
    source.content_coverage = Coverage.PARTIAL
    partial = engine.refresh_scope(ScopeKey("synthetic", source.course, "content"))
    repository = ResourceRepository(database)
    retained = repository.get_resource(source.attachment)
    assert partial.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert retained is not None and retained.availability is Availability.ACTIVE

    source.content_coverage = Coverage.COMPLETE
    complete = engine.refresh_scope(ScopeKey("synthetic", source.course, "content"))
    missing = repository.get_resource(source.attachment)
    assert complete.counts["resources_marked_missing"] == 1
    assert missing is not None and missing.availability is Availability.MISSING


def test_fetch_resource_owns_run_and_runs_bounded_local_pipeline(tmp_path: Path) -> None:
    engine, source, _sessions, database = _engine(tmp_path)
    engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        fetch_resources=False,
    )

    result = engine.fetch_resource(source.attachment)

    assert result.outcome == "NEW_VERSION"
    assert SearchService(database).search(SearchQuery("Synthetic optics")).items
    connection = database.connect()
    try:
        run = connection.execute(
            "SELECT status FROM sync_run WHERE mode = 'resource' ORDER BY sync_run_key DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert run is not None and run["status"] == "SUCCEEDED"


def test_bounded_jobs_make_processing_scope_partial(tmp_path: Path) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)

    result = engine.sync_course(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        max_jobs=1,
    )

    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert result.counts["jobs_remaining"] > 0
    scope = next(item for item in result.scopes if item.data_kind == "local_jobs")
    assert scope.coverage is Coverage.PARTIAL


def test_targeted_refresh_does_not_consume_another_course_job_backlog(
    tmp_path: Path,
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)
    other_course = CourseId("synthetic", "course-two")
    engine.domain.put_course(source.course, code="PH0000", title="Selected course")
    engine.domain.put_course(other_course, code="PH0001", title="Other course")
    seed_run_key = engine.recorder.start(mode="seed", requested_scope={})
    observed = EventRepository(database).observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "other-announcement"),
            other_course,
            "Other course reminder",
            "Other course quiz is due 2030-03-10 10:00 UTC.",
            Availability.ACTIVE,
            published_at=_time(NOW),
        ),
        sync_run_key=seed_run_key,
    )
    unrelated_plan = engine.planner.plan_observation(observed.observation.key)

    result = engine.refresh_scope(ScopeKey("synthetic", source.course, "content"))

    connection = database.connect()
    try:
        statuses = {
            str(row["status"])
            for row in connection.execute(
                "SELECT status FROM local_job WHERE job_key IN (?, ?)",
                unrelated_plan.keys,
            ).fetchall()
        }
    finally:
        connection.close()
    assert result.counts["jobs_claimed"] == 0
    assert statuses == {"PENDING"}


@pytest.mark.parametrize("course_error", [None, AuthenticationRequired()])
def test_first_course_lookup_failure_finishes_without_fabricating_course(
    tmp_path: Path,
    course_error: Exception | None,
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)
    source.include_course = False
    source.course_error = course_error

    result = engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
    )

    assert result.status.value == "FAILED"
    assert result.scopes[0].course_id is None
    assert engine.domain.get_course(source.course) is None
    connection = database.connect()
    try:
        run = connection.execute(
            "SELECT status, ended_at FROM sync_run WHERE sync_run_key = ?", (result.key,)
        ).fetchone()
        resource_count = int(connection.execute("SELECT COUNT(*) FROM resource").fetchone()[0])
    finally:
        connection.close()
    assert run is not None and run["status"] == "FAILED" and run["ended_at"] is not None
    assert resource_count == 0


@pytest.mark.parametrize("state", [CapabilityState.UNKNOWN, CapabilityState.UNSUPPORTED])
def test_unavailable_discovery_capability_does_not_call_source_endpoints(
    tmp_path: Path, state: CapabilityState
) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)
    source.capability_states = {SourceCapability.COURSE_DISCOVERY: state}

    result = engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
    )

    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert result.scopes[0].coverage is Coverage.UNKNOWN
    assert source.calls == []


def test_unknown_content_capability_skips_content_endpoint(tmp_path: Path) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)
    source.capability_states.pop(SourceCapability.CONTENT_TREE)

    result = engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
    )

    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert source.calls == [ReadPurpose.DISCOVERY]
    content_scope = next(scope for scope in result.scopes if scope.data_kind == "content")
    assert content_scope.coverage is Coverage.UNKNOWN


def test_unknown_event_capability_skips_selected_endpoint(tmp_path: Path) -> None:
    engine, source, _sessions, _database = _engine(tmp_path)
    engine.domain.put_course(source.course, code="PH0000", title="Selected course")
    source.capability_states[SourceCapability.ANNOUNCEMENTS] = CapabilityState.UNKNOWN

    result = engine.refresh_scope(ScopeKey("synthetic", source.course, "announcements"))

    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    assert source.calls == []
    assert result.scopes[0].coverage is Coverage.UNKNOWN


def test_resource_recovery_warning_marks_course_run_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)

    def fail_planning(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic planner failure")

    monkeypatch.setattr(engine.planner, "plan_resource", fail_planning)
    result = engine.sync_course(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
    )

    assert result.status.value == "SUCCEEDED_WITH_WARNINGS"
    resources = next(scope for scope in result.scopes if scope.data_kind == "resources")
    assert resources.coverage is Coverage.PARTIAL
    assert resources.failure_category == "receipt_and_jobs_pending_recovery"
    assert len(ResourceRepository(database).list_versions(source.attachment)) == 1
    connection = database.connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM resource_fetch_receipt").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM local_job").fetchone()[0] == 0
    finally:
        connection.close()


def test_resource_recovery_warning_marks_standalone_run_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)
    engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset(),
        fetch_resources=False,
    )

    def fail_planning(*_args: object, **_kwargs: object) -> None:
        raise ValueError("synthetic planner failure")

    monkeypatch.setattr(engine.planner, "plan_resource", fail_planning)
    fetched = engine.fetch_resource(source.attachment)

    assert fetched.warning_codes == ("receipt_and_jobs_pending_recovery",)
    connection = database.connect()
    try:
        run = connection.execute(
            "SELECT status, ended_at FROM sync_run WHERE mode = 'resource' "
            "ORDER BY sync_run_key DESC LIMIT 1"
        ).fetchone()
    finally:
        connection.close()
    assert run is not None
    assert run["status"] == "SUCCEEDED_WITH_WARNINGS"
    assert run["ended_at"] is not None


def test_unexpected_downstream_failure_still_finishes_owned_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine, source, _sessions, database = _engine(tmp_path)

    def fail_planning(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("private details must not escape")

    monkeypatch.setattr(engine.planner, "plan_observation", fail_planning)
    result = engine.quick_sync(
        source.course,
        window=WINDOW,
        event_scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        include_content=False,
    )

    assert result.status.value == "FAILED"
    assert result.error_category == "sync_engine_error"
    connection = database.connect()
    try:
        run = connection.execute(
            "SELECT status, ended_at, error_category FROM sync_run WHERE sync_run_key = ?",
            (result.key,),
        ).fetchone()
    finally:
        connection.close()
    assert run is not None
    assert tuple(run) == ("FAILED", run["ended_at"], "sync_engine_error")
    assert run["ended_at"] is not None
