"""Bounded ingestion of structured event sources for one observed course."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType

from ntulearn_skill.client import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    AuthorizedReadSession,
    CapabilityState,
    DueSourceRecord,
    Page,
    PageRequest,
    ReadPurpose,
    ScheduleSourceRecord,
    SessionExpired,
    SessionProvider,
    SourceCapability,
    SourceError,
    SourceProtocolError,
    SourceProvider,
    SourceUnavailable,
    TimeWindow,
    safe_source_error_category,
)
from ntulearn_skill.core import ContentId, CourseId, Coverage
from ntulearn_skill.core.models import utc_now
from ntulearn_skill.events import EventRepository, EventStorageError
from ntulearn_skill.sync.models import ScopeResult, SyncWarning


class EventSourceScope(StrEnum):
    ANNOUNCEMENTS = "announcements"
    ASSESSMENTS = "assessments"
    SCHEDULE = "schedule"
    DUE_ITEMS = "due_items"


@dataclass(frozen=True, slots=True)
class EventSourceSyncResult:
    scopes: tuple[ScopeResult, ...]
    observation_keys: tuple[int, ...]
    counts: Mapping[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "scopes", tuple(self.scopes))
        object.__setattr__(self, "observation_keys", tuple(self.observation_keys))
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))


@dataclass(slots=True)
class _Progress:
    pages_seen: int = 0
    items_seen: int = 0
    pagination_complete: bool = False
    coverage: Coverage = Coverage.COMPLETE
    failure_category: str | None = None
    warnings: list[SyncWarning] = field(default_factory=list)

    def warn(self, warning: SyncWarning) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)


class EventSourceSync:
    """Read and retain selected event-source observations without planning a full sync run."""

    _CAPABILITIES = {
        EventSourceScope.ANNOUNCEMENTS: SourceCapability.ANNOUNCEMENTS,
        EventSourceScope.ASSESSMENTS: SourceCapability.ASSESSMENT_DETAILS,
        EventSourceScope.SCHEDULE: SourceCapability.SCHEDULE_ITEMS,
        EventSourceScope.DUE_ITEMS: SourceCapability.DUE_ITEMS,
    }

    _PURPOSES = {
        EventSourceScope.ANNOUNCEMENTS: ReadPurpose.ANNOUNCEMENTS,
        EventSourceScope.ASSESSMENTS: ReadPurpose.ASSESSMENTS,
        EventSourceScope.SCHEDULE: ReadPurpose.SCHEDULE,
        EventSourceScope.DUE_ITEMS: ReadPurpose.DUE_ITEMS,
    }

    def __init__(
        self,
        *,
        sessions: SessionProvider,
        source: SourceProvider,
        repository: EventRepository,
    ) -> None:
        self.sessions = sessions
        self.source = source
        self.repository = repository

    def run(
        self,
        *,
        run_key: int,
        course: CourseId,
        window: TimeWindow,
        assessment_content_ids: Iterable[ContentId] = (),
        assessment_inventory_coverage: Coverage = Coverage.UNKNOWN,
        scopes: frozenset[EventSourceScope] | None = None,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_assessments: int = 10_000,
    ) -> EventSourceSyncResult:
        if isinstance(run_key, bool) or not isinstance(run_key, int) or run_key <= 0:
            raise ValueError("run_key must be a positive integer")
        if type(course) is not CourseId or course.provider != self.source.provider_name:
            raise ValueError("course must belong to the source provider")
        if not isinstance(window, TimeWindow):
            raise TypeError("window must be a TimeWindow")
        PageRequest(page_size=page_size)
        if max_pages_per_scope <= 0 or max_assessments <= 0:
            raise ValueError("event source sync bounds must be positive")
        if assessment_inventory_coverage not in {
            Coverage.COMPLETE,
            Coverage.PARTIAL,
            Coverage.UNKNOWN,
        }:
            raise ValueError("assessment inventory coverage must be complete, partial, or unknown")
        selected = frozenset(EventSourceScope) if scopes is None else frozenset(scopes)
        if any(not isinstance(scope, EventSourceScope) for scope in selected):
            raise ValueError("event source scopes must be typed")
        content_ids = self._assessment_ids(assessment_content_ids, course)
        if EventSourceScope.ASSESSMENTS in selected:
            self._validate_assessment_ownership(content_ids, course)

        results: list[ScopeResult] = []
        observation_keys: list[int] = []
        for scope in EventSourceScope:
            if scope not in selected:
                continue
            if not self._supported(scope):
                results.append(self._unsupported_scope(scope, course))
                continue
            if scope is EventSourceScope.ASSESSMENTS:
                result, keys = self._assessments(
                    run_key,
                    course,
                    content_ids,
                    assessment_inventory_coverage,
                    max_assessments,
                )
            else:
                result, keys = self._paged(
                    scope,
                    run_key,
                    course,
                    window,
                    page_size,
                    max_pages_per_scope,
                )
            results.append(result)
            observation_keys.extend(keys)

        counts = {f"{scope.value}_observed": 0 for scope in EventSourceScope}
        for result in results:
            counts[f"{result.data_kind}_observed"] = result.items_seen
        counts["observations"] = len(observation_keys)
        return EventSourceSyncResult(tuple(results), tuple(observation_keys), counts)

    def _paged(
        self,
        scope: EventSourceScope,
        run_key: int,
        course: CourseId,
        window: TimeWindow,
        page_size: int,
        max_pages: int,
    ) -> tuple[ScopeResult, list[int]]:
        progress = _Progress()
        keys: list[int] = []
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        try:
            session = self._acquire(self._PURPOSES[scope])
            while True:
                if progress.pages_seen >= max_pages:
                    self._partial(
                        progress, "pagination_limit_reached", SyncWarning.PAGE_CAP_REACHED
                    )
                    break
                if cursor in seen_cursors:
                    self._partial(progress, "pagination_cycle", SyncWarning.PAGINATION_CYCLE)
                    break
                seen_cursors.add(cursor)
                page = self._read_page(scope, session, course, window, cursor, page_size)
                if not isinstance(page, Page):
                    raise SourceProtocolError()
                progress.pages_seen += 1
                self._merge_coverage(progress, page.coverage_for_page)
                if not page.items and page.next_cursor is not None:
                    self._partial(progress, "source_protocol_error", SyncWarning.SOURCE_FAILURE)
                    break
                for item in page.items:
                    self._validate_record(scope, item, course)
                    keys.append(self._observe(scope, item, run_key))
                    progress.items_seen += 1
                cursor = page.next_cursor
                if cursor is None:
                    progress.pagination_complete = True
                    break
        except SourceError as error:
            self._source_failure(progress, error)
            self._invalidate_if_expired(error)
        except Exception:
            self._source_failure(progress, SourceUnavailable())
        if scope is EventSourceScope.DUE_ITEMS and progress.coverage is Coverage.COMPLETE:
            progress.coverage = Coverage.UNKNOWN
            progress.warn(SyncWarning.PAGE_COVERAGE_UNKNOWN)
        return self._scope(scope, course, progress), keys

    def _assessments(
        self,
        run_key: int,
        course: CourseId,
        content_ids: tuple[ContentId, ...],
        inventory_coverage: Coverage,
        max_assessments: int,
    ) -> tuple[ScopeResult, list[int]]:
        progress = _Progress(coverage=inventory_coverage)
        keys: list[int] = []
        if inventory_coverage is Coverage.PARTIAL:
            progress.warn(SyncWarning.PAGE_REPORTED_PARTIAL)
        elif inventory_coverage is Coverage.UNKNOWN:
            progress.warn(SyncWarning.PAGE_COVERAGE_UNKNOWN)
        try:
            session = self._acquire(ReadPurpose.ASSESSMENTS)
            for content_id in content_ids:
                if progress.items_seen >= max_assessments:
                    self._partial(
                        progress, "assessment_limit_reached", SyncWarning.PAGE_CAP_REACHED
                    )
                    break
                item = self.source.get_assessment(session, course, content_id)
                self._validate_assessment(item, course, content_id)
                observed = self.repository.observe_assessment(item, sync_run_key=run_key)
                keys.append(observed.observation.key)
                progress.items_seen += 1
            else:
                progress.pagination_complete = True
        except SourceError as error:
            self._source_failure(progress, error)
            self._invalidate_if_expired(error)
        except Exception:
            self._source_failure(progress, SourceUnavailable())
        return self._scope(EventSourceScope.ASSESSMENTS, course, progress), keys

    def _read_page(
        self,
        scope: EventSourceScope,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        cursor: str | None,
        page_size: int,
    ) -> Page[AnnouncementSourceRecord] | Page[ScheduleSourceRecord] | Page[DueSourceRecord]:
        request = PageRequest(cursor=cursor, page_size=page_size)
        if scope is EventSourceScope.ANNOUNCEMENTS:
            return self.source.list_announcements(session, course, request)
        if scope is EventSourceScope.SCHEDULE:
            return self.source.list_schedule_items(session, course, window, request)
        if scope is EventSourceScope.DUE_ITEMS:
            return self.source.list_due_items(session, course, window, request)
        raise AssertionError("assessment is not paged")

    def _observe(self, scope: EventSourceScope, item: object, run_key: int) -> int:
        if scope is EventSourceScope.ANNOUNCEMENTS:
            assert isinstance(item, AnnouncementSourceRecord)
            observed = self.repository.observe_announcement(item, sync_run_key=run_key)
        elif scope is EventSourceScope.SCHEDULE:
            assert isinstance(item, ScheduleSourceRecord)
            observed = self.repository.observe_schedule(item, sync_run_key=run_key)
        elif scope is EventSourceScope.DUE_ITEMS:
            assert isinstance(item, DueSourceRecord)
            observed = self.repository.observe_due_item(item, sync_run_key=run_key)
        else:
            raise AssertionError("assessment observation uses its dedicated path")
        return observed.observation.key

    def _supported(self, scope: EventSourceScope) -> bool:
        try:
            return (
                self.source.capabilities().state(self._CAPABILITIES[scope])
                is CapabilityState.SUPPORTED
            )
        except Exception:
            return False

    def _unsupported_scope(self, scope: EventSourceScope, course: CourseId) -> ScopeResult:
        return ScopeResult(
            self.source.provider_name,
            course,
            scope.value,
            Coverage.UNKNOWN,
            0,
            0,
            False,
            "unsupported_capability",
            (SyncWarning.PAGE_COVERAGE_UNKNOWN,),
            utc_now(),
        )

    def _scope(self, scope: EventSourceScope, course: CourseId, progress: _Progress) -> ScopeResult:
        return ScopeResult(
            self.source.provider_name,
            course,
            scope.value,
            progress.coverage,
            progress.pages_seen,
            progress.items_seen,
            progress.pagination_complete,
            progress.failure_category,
            tuple(progress.warnings),
            utc_now(),
        )

    def _acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        try:
            session = self.sessions.acquire(purpose)
        except SourceError:
            raise
        except Exception:
            raise SourceUnavailable() from None
        if not isinstance(session, AuthorizedReadSession):
            raise SourceUnavailable()
        return session

    def _invalidate_if_expired(self, error: SourceError) -> None:
        if not isinstance(error, SessionExpired):
            return
        try:
            self.sessions.invalidate(SessionExpired.category)
        except Exception:
            pass

    @staticmethod
    def _assessment_ids(values: Iterable[ContentId], course: CourseId) -> tuple[ContentId, ...]:
        result: list[ContentId] = []
        seen: set[ContentId] = set()
        try:
            for value in values:
                if type(value) is not ContentId or value.provider != course.provider:
                    raise ValueError("assessment content must belong to the course provider")
                if value not in seen:
                    seen.add(value)
                    result.append(value)
        except TypeError:
            raise TypeError("assessment_content_ids must be iterable") from None
        return tuple(result)

    def _validate_assessment_ownership(
        self, content_ids: tuple[ContentId, ...], course: CourseId
    ) -> None:
        if not content_ids:
            return
        connection = self.repository.database.connect()
        try:
            for content_id in content_ids:
                row = connection.execute(
                    """
                    SELECT 1
                    FROM content_node cn
                    JOIN course c ON c.course_key = cn.course_key
                    JOIN source_object content_so
                      ON content_so.source_object_key = cn.source_object_key
                    JOIN source_object course_so
                      ON course_so.source_object_key = c.source_object_key
                    JOIN source_provider p ON p.provider_key = content_so.provider_key
                    WHERE p.name = ?
                      AND content_so.object_kind = 'content'
                      AND content_so.remote_key = ?
                      AND course_so.object_kind = 'course'
                      AND course_so.remote_key = ?
                      AND course_so.provider_key = content_so.provider_key
                    """,
                    (course.provider, content_id.value, course.value),
                ).fetchone()
                if row is None:
                    raise ValueError("assessment content does not belong to the course")
        except sqlite3.Error:
            raise EventStorageError("assessment ownership validation failed") from None
        finally:
            connection.close()

    @staticmethod
    def _validate_record(scope: EventSourceScope, item: object, course: CourseId) -> None:
        expected: type[object]
        if scope is EventSourceScope.ANNOUNCEMENTS:
            expected = AnnouncementSourceRecord
        elif scope is EventSourceScope.SCHEDULE:
            expected = ScheduleSourceRecord
        elif scope is EventSourceScope.DUE_ITEMS:
            expected = DueSourceRecord
        else:
            raise AssertionError("assessment validation uses its dedicated path")
        if type(item) is not expected or getattr(item, "course_id", None) != course:
            raise SourceProtocolError()

    @staticmethod
    def _validate_assessment(item: object, course: CourseId, requested_content: ContentId) -> None:
        if (
            type(item) is not AssessmentSourceRecord
            or item.course_id != course
            or item.content_id != requested_content
        ):
            raise SourceProtocolError()

    @staticmethod
    def _merge_coverage(progress: _Progress, coverage: Coverage) -> None:
        if coverage is Coverage.PARTIAL:
            progress.coverage = Coverage.PARTIAL
            progress.warn(SyncWarning.PAGE_REPORTED_PARTIAL)
        elif coverage is Coverage.UNKNOWN and progress.coverage is Coverage.COMPLETE:
            progress.coverage = Coverage.UNKNOWN
            progress.warn(SyncWarning.PAGE_COVERAGE_UNKNOWN)
        elif coverage is not Coverage.COMPLETE:
            raise SourceProtocolError()

    @staticmethod
    def _partial(progress: _Progress, category: str, warning: SyncWarning) -> None:
        progress.coverage = Coverage.PARTIAL
        progress.pagination_complete = False
        progress.failure_category = category
        progress.warn(warning)

    @staticmethod
    def _source_failure(progress: _Progress, error: SourceError) -> None:
        progress.coverage = (
            Coverage.PARTIAL if progress.pages_seen or progress.items_seen else Coverage.FAILED
        )
        progress.pagination_complete = False
        progress.failure_category = safe_source_error_category(error.category)
        progress.warn(SyncWarning.SOURCE_FAILURE)
