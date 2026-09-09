from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest

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
    SourceUnavailable,
    TimeWindow,
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
    Coverage,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events import CandidateFieldName, DeterministicEventExtractor, EventRepository
from ntulearn_skill.storage import Database, DomainRepository
from ntulearn_skill.sync.event_sources import EventSourceScope, EventSourceSync
from ntulearn_skill.sync.observability import SyncRunRecorder

NOW = datetime(2030, 1, 2, tzinfo=UTC)
WINDOW = TimeWindow(datetime(2030, 1, 1, tzinfo=UTC), datetime(2030, 4, 1, tzinfo=UTC))


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
        self.states = {capability: CapabilityState.SUPPORTED for capability in SourceCapability}
        self.pages: dict[
            tuple[EventSourceScope, str | None],
            Page[object] | Exception,
        ] = {}
        self.assessments: dict[ContentId, AssessmentSourceRecord | Exception] = {}
        self.calls: list[tuple[EventSourceScope, object]] = []

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(self.states)

    def _page(self, scope: EventSourceScope, page: PageRequest) -> Page[object]:
        self.calls.append((scope, page.cursor))
        result = self.pages[(scope, page.cursor)]
        if isinstance(result, Exception):
            raise result
        return result

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]:
        return cast(
            Page[AnnouncementSourceRecord],
            self._page(EventSourceScope.ANNOUNCEMENTS, page),
        )

    def get_assessment(
        self, session: AuthorizedReadSession, course: CourseId, content: ContentId
    ) -> AssessmentSourceRecord:
        self.calls.append((EventSourceScope.ASSESSMENTS, content))
        result = self.assessments[content]
        if isinstance(result, Exception):
            raise result
        return result

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]:
        return cast(Page[ScheduleSourceRecord], self._page(EventSourceScope.SCHEDULE, page))

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]:
        return cast(Page[DueSourceRecord], self._page(EventSourceScope.DUE_ITEMS, page))

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        raise AssertionError("not used")

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]:
        raise AssertionError("not used")

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        raise AssertionError("not used")

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        raise AssertionError("not used")


@dataclass(frozen=True)
class _Harness:
    domain: DomainRepository
    events: EventRepository
    course: CourseId
    assessment_content: ContentId
    run_key: int


def _harness(tmp_path: Path) -> _Harness:
    database = Database(tmp_path / "private" / "metadata.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() >= 7
    course = CourseId("synthetic", "course-ph0000")
    content = ContentId("synthetic", "assessment-content-one")
    domain.put_course(course, code="PH0000", title="Example Physics Course", observed_at=NOW)
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="assessment",
        title="Quiz 1",
        position=0,
        observed_at=NOW,
    )
    run_key = SyncRunRecorder(database, "synthetic").start(
        mode="event-sources", requested_scope={"synthetic": True}
    )
    return _Harness(domain, EventRepository(database), course, content, run_key)


def _announcement(course: CourseId, value: str = "announcement-one") -> AnnouncementSourceRecord:
    return AnnouncementSourceRecord(
        AnnouncementId("synthetic", value),
        course,
        "Quiz 1 reminder",
        "Quiz 1 is due 2030-02-10 10:00 UTC.",
        Availability.ACTIVE,
        published_at=_time(datetime(2030, 1, 2, tzinfo=UTC)),
    )


def _assessment(course: CourseId, content: ContentId) -> AssessmentSourceRecord:
    return AssessmentSourceRecord(
        AssessmentId("synthetic", "assessment-one"),
        course,
        content,
        None,
        "Quiz 1",
        AssessmentSubtype.QUIZ,
        "Complete the synthetic quiz.",
        Availability.ACTIVE,
        due_at=_time(datetime(2030, 2, 10, 10, tzinfo=UTC)),
    )


def _schedule(course: CourseId) -> ScheduleSourceRecord:
    return ScheduleSourceRecord(
        CalendarItemId("synthetic", "schedule-one"),
        course,
        "Revision session",
        Availability.ACTIVE,
        start_at=_time(datetime(2030, 2, 8, 10, tzinfo=UTC)),
        location="Example Room",
    )


def _due(course: CourseId) -> DueSourceRecord:
    return DueSourceRecord(
        CalendarItemId("synthetic", "due-one"),
        course,
        "Quiz 1",
        "calendar-one",
        "assessment-one",
        "assessment",
        Availability.ACTIVE,
        source_start_at=_time(datetime(2030, 2, 1, tzinfo=UTC)),
        due_at=_time(datetime(2030, 2, 10, 10, tzinfo=UTC)),
    )


def test_ingests_all_scopes_through_repository_and_extractor(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    sessions = _Sessions()
    source = _Source()
    source.pages = {
        (EventSourceScope.ANNOUNCEMENTS, None): Page(
            (_announcement(harness.course),), "announcement-2", Coverage.COMPLETE
        ),
        (EventSourceScope.ANNOUNCEMENTS, "announcement-2"): Page((), None, Coverage.COMPLETE),
        (EventSourceScope.SCHEDULE, None): Page(
            (_schedule(harness.course),), None, Coverage.UNKNOWN
        ),
        (EventSourceScope.DUE_ITEMS, None): Page((_due(harness.course),), None, Coverage.COMPLETE),
    }
    source.assessments[harness.assessment_content] = _assessment(
        harness.course, harness.assessment_content
    )

    result = EventSourceSync(sessions=sessions, source=source, repository=harness.events).run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        assessment_content_ids=(harness.assessment_content,),
        assessment_inventory_coverage=Coverage.COMPLETE,
    )

    assert [scope.coverage for scope in result.scopes] == [
        Coverage.COMPLETE,
        Coverage.COMPLETE,
        Coverage.UNKNOWN,
        Coverage.UNKNOWN,
    ]
    assert result.counts == {
        "announcements_observed": 1,
        "assessments_observed": 1,
        "schedule_observed": 1,
        "due_items_observed": 1,
        "observations": 4,
    }
    assert len(result.observation_keys) == len(set(result.observation_keys)) == 4
    assert sessions.acquired == [
        ReadPurpose.ANNOUNCEMENTS,
        ReadPurpose.ASSESSMENTS,
        ReadPurpose.SCHEDULE,
        ReadPurpose.DUE_ITEMS,
    ]
    extractor = DeterministicEventExtractor(harness.domain.database)
    extracted = [extractor.extract_observation(key) for key in result.observation_keys]
    fields = {
        field.name
        for item in extracted
        for candidate in item.candidates
        for field in candidate.fields
    }
    assert CandidateFieldName.DUE_TIME in fields
    assert CandidateFieldName.START_TIME in fields


def test_mid_page_expiry_preserves_observation_and_invalidates_session(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    sessions = _Sessions()
    source = _Source()
    source.pages = {
        (EventSourceScope.ANNOUNCEMENTS, None): Page(
            (_announcement(harness.course),), "next", Coverage.COMPLETE
        ),
        (EventSourceScope.ANNOUNCEMENTS, "next"): SessionExpired(),
    }

    result = EventSourceSync(sessions=sessions, source=source, repository=harness.events).run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
    )

    scope = result.scopes[0]
    assert scope.coverage is Coverage.PARTIAL
    assert scope.pages_seen == scope.items_seen == 1
    assert scope.failure_category == "session_expired"
    assert sessions.invalidated == ["session_expired"]
    assert harness.events.get_observation(result.observation_keys[0]) is not None


def test_repeated_same_run_and_payload_reuses_observation(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    source = _Source()
    source.pages[(EventSourceScope.ANNOUNCEMENTS, None)] = Page(
        (_announcement(harness.course),), None, Coverage.COMPLETE
    )
    sync = EventSourceSync(sessions=_Sessions(), source=source, repository=harness.events)
    first = sync.run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
    )
    second = sync.run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
    )

    assert first.observation_keys == second.observation_keys
    connection = harness.domain.database.connect()
    try:
        count = connection.execute("SELECT COUNT(*) FROM source_observation").fetchone()[0]
    finally:
        connection.close()
    assert count == 1


@pytest.mark.parametrize("termination", ["cycle", "empty", "cap"])
def test_pagination_cycle_empty_continuation_and_cap_are_partial(
    tmp_path: Path, termination: str
) -> None:
    harness = _harness(tmp_path)
    source = _Source()
    next_cursor = "repeat"
    source.pages[(EventSourceScope.ANNOUNCEMENTS, None)] = Page(
        (_announcement(harness.course),), next_cursor, Coverage.COMPLETE
    )
    if termination == "cycle":
        source.pages[(EventSourceScope.ANNOUNCEMENTS, next_cursor)] = Page(
            (_announcement(harness.course, "announcement-two"),),
            next_cursor,
            Coverage.COMPLETE,
        )
    elif termination == "empty":
        source.pages[(EventSourceScope.ANNOUNCEMENTS, next_cursor)] = Page(
            (), "third", Coverage.COMPLETE
        )

    result = EventSourceSync(sessions=_Sessions(), source=source, repository=harness.events).run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
        max_pages_per_scope=1 if termination == "cap" else 10,
    )

    assert result.scopes[0].coverage is Coverage.PARTIAL
    assert result.scopes[0].items_seen == (2 if termination == "cycle" else 1)
    if termination == "cycle":
        assert result.scopes[0].failure_category == "pagination_cycle"
    elif termination == "empty":
        assert result.scopes[0].failure_category == "source_protocol_error"
    else:
        assert result.scopes[0].failure_category == "pagination_limit_reached"


def test_unknown_and_unsupported_scopes_are_explicit_and_do_not_read(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    source = _Source()
    source.states[SourceCapability.SCHEDULE_ITEMS] = CapabilityState.UNKNOWN
    source.states[SourceCapability.DUE_ITEMS] = CapabilityState.UNSUPPORTED

    result = EventSourceSync(sessions=_Sessions(), source=source, repository=harness.events).run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.SCHEDULE, EventSourceScope.DUE_ITEMS}),
    )

    assert len(result.scopes) == 2
    assert all(scope.coverage is Coverage.UNKNOWN for scope in result.scopes)
    assert all(scope.failure_category == "unsupported_capability" for scope in result.scopes)
    assert source.calls == []


def test_assessment_inventory_uncertainty_and_course_ownership_are_enforced(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    source = _Source()
    source.assessments[harness.assessment_content] = _assessment(
        harness.course, harness.assessment_content
    )
    sync = EventSourceSync(sessions=_Sessions(), source=source, repository=harness.events)

    result = sync.run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        assessment_content_ids=(harness.assessment_content,),
        scopes=frozenset({EventSourceScope.ASSESSMENTS}),
    )
    assert result.scopes[0].coverage is Coverage.UNKNOWN

    other_course = CourseId("synthetic", "course-other")
    foreign_content = ContentId("synthetic", "foreign-assessment")
    harness.domain.put_course(other_course, code="PH0001", title="Other Example", observed_at=NOW)
    harness.domain.put_content_node(
        foreign_content,
        course_id=other_course,
        handler_kind="assessment",
        title="Other Quiz",
        position=0,
        observed_at=NOW,
    )
    previous_calls = list(source.calls)
    with pytest.raises(ValueError, match="does not belong"):
        sync.run(
            run_key=harness.run_key,
            course=harness.course,
            window=WINDOW,
            assessment_content_ids=(foreign_content,),
            scopes=frozenset({EventSourceScope.ASSESSMENTS}),
        )
    assert source.calls == previous_calls


def test_unexpected_source_error_is_safe_and_failed(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    source = _Source()
    source.pages[(EventSourceScope.ANNOUNCEMENTS, None)] = RuntimeError(
        "private invented diagnostic"
    )

    result = EventSourceSync(sessions=_Sessions(), source=source, repository=harness.events).run(
        run_key=harness.run_key,
        course=harness.course,
        window=WINDOW,
        scopes=frozenset({EventSourceScope.ANNOUNCEMENTS}),
    )

    scope = result.scopes[0]
    assert scope.coverage is Coverage.FAILED
    assert scope.failure_category == SourceUnavailable.category
    assert "diagnostic" not in repr(result)
