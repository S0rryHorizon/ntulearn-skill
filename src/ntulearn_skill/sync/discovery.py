"""Bounded, coverage-aware course and generic content-tree synchronization."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass, field

from ntulearn_skill.client import (
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    Page,
    PageRequest,
    ReadPurpose,
    ResourceMetadataRecord,
    SessionExpired,
    SessionProvider,
    SourceCapability,
    SourceError,
    SourceProtocolError,
    SourceProvider,
    SourceUnavailable,
    safe_source_error_category,
    source_observed_at,
)
from ntulearn_skill.core import (
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    SyncRunStatus,
)
from ntulearn_skill.core.models import to_storage_time
from ntulearn_skill.storage import DomainRepository, ResourceRepository
from ntulearn_skill.sync.models import ScopeResult, SyncRunResult, SyncWarning
from ntulearn_skill.sync.observability import SyncRunRecorder


@dataclass(frozen=True, slots=True)
class ScopedDiscoveryResult:
    """Observed records and exact coverage for one caller-owned sync run."""

    course: CourseSourceRecord | None
    content: tuple[ContentSourceRecord, ...]
    resources: tuple[ResourceMetadataRecord, ...]
    scopes: tuple[ScopeResult, ...]


@dataclass(slots=True)
class _Progress:
    pages_seen: int = 0
    items_seen: int = 0
    resources_seen: int = 0
    pagination_complete: bool = False
    coverage: Coverage = Coverage.COMPLETE
    failure_category: str | None = None
    warnings: list[SyncWarning] = field(default_factory=list)

    def warn(self, warning: SyncWarning) -> None:
        if warning not in self.warnings:
            self.warnings.append(warning)


class DiscoverySync:
    """Persist discovered courses/content while retaining partial progress and safe run evidence."""

    def __init__(
        self,
        *,
        sessions: SessionProvider,
        source: SourceProvider,
        repository: DomainRepository,
    ) -> None:
        self.sessions = sessions
        self.source = source
        self.repository = repository
        self.resources = ResourceRepository(repository.database)
        self.recorder = SyncRunRecorder(repository.database, source.provider_name)

    def run(
        self,
        *,
        include_content: bool = True,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_content_nodes: int = 10_000,
    ) -> SyncRunResult:
        if max_pages_per_scope <= 0 or max_content_nodes <= 0:
            raise ValueError("sync bounds must be positive")
        PageRequest(page_size=page_size)
        run_key = self.recorder.start(
            mode="discovery",
            requested_scope={
                "include_content": include_content,
                "page_size": page_size,
                "max_pages_per_scope": max_pages_per_scope,
                "max_content_nodes": max_content_nodes,
            },
        )
        counts = {
            "courses_observed": 0,
            "content_observed": 0,
            "resources_discovered": 0,
            "scopes_complete": 0,
            "scopes_partial": 0,
            "scopes_failed": 0,
        }
        scopes: list[ScopeResult] = []
        courses: list[CourseSourceRecord] = []

        if not self._supported(SourceCapability.COURSE_DISCOVERY):
            course_scope = self._unsupported_scope("courses", None)
            courses = []
        else:
            try:
                discovery_session = self._acquire(ReadPurpose.DISCOVERY)
                courses, course_scope = self._courses(
                    discovery_session, page_size, max_pages_per_scope
                )
            except SourceError as error:
                self._invalidate_if_expired(error)
                course_scope = self._failed_scope(
                    "courses", None, safe_source_error_category(error.category)
                )
        scopes.append(course_scope)
        self.recorder.record_scope(run_key, course_scope)
        counts["courses_observed"] = course_scope.items_seen

        if include_content and courses:
            if not self._supported(SourceCapability.CONTENT_TREE):
                for course in courses:
                    scope = self._unsupported_scope("content", course.remote_id)
                    scopes.append(scope)
                    self.recorder.record_scope(run_key, scope)
            else:
                try:
                    content_session = self._acquire(ReadPurpose.CONTENT)
                except SourceError as error:
                    self._invalidate_if_expired(error)
                    for course in courses:
                        scope = self._failed_scope(
                            "content",
                            course.remote_id,
                            safe_source_error_category(error.category),
                        )
                        scopes.append(scope)
                        self.recorder.record_scope(run_key, scope)
                else:
                    for course in courses:
                        scope, resources_seen = self._content(
                            run_key,
                            content_session,
                            course.remote_id,
                            page_size,
                            max_pages_per_scope,
                            max_content_nodes,
                        )
                        scopes.append(scope)
                        self.recorder.record_scope(run_key, scope)
                        counts["content_observed"] += scope.items_seen
                        counts["resources_discovered"] += resources_seen

        for scope in scopes:
            if scope.coverage is Coverage.COMPLETE:
                counts["scopes_complete"] += 1
            elif scope.coverage is Coverage.FAILED:
                counts["scopes_failed"] += 1
            else:
                counts["scopes_partial"] += 1
        warnings = tuple(dict.fromkeys(warning for scope in scopes for warning in scope.warnings))
        if course_scope.coverage is Coverage.FAILED:
            status = SyncRunStatus.FAILED
            error_category = course_scope.failure_category
        elif any(scope.coverage is not Coverage.COMPLETE for scope in scopes):
            status = SyncRunStatus.SUCCEEDED_WITH_WARNINGS
            error_category = None
        else:
            status = SyncRunStatus.SUCCEEDED
            error_category = None
        return self.recorder.finish(
            run_key,
            status=status,
            counts=counts,
            warnings=warnings,
            error_category=error_category,
        )

    def sync_course(
        self,
        run_key: int,
        course: CourseId,
        *,
        include_availability: bool = True,
        include_content: bool = True,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_content_nodes: int = 10_000,
    ) -> ScopedDiscoveryResult:
        """Observe only one requested course inside a run owned by a higher-level engine."""

        if isinstance(run_key, bool) or not isinstance(run_key, int) or run_key <= 0:
            raise ValueError("run_key must be a positive integer")
        if type(course) is not CourseId or course.provider != self.source.provider_name:
            raise ValueError("course must belong to the source provider")
        if max_pages_per_scope <= 0 or max_content_nodes <= 0:
            raise ValueError("sync bounds must be positive")
        PageRequest(page_size=page_size)

        scopes: list[ScopeResult] = []
        observed_course: CourseSourceRecord | None = None
        if include_availability:
            if not self._supported(SourceCapability.COURSE_DISCOVERY):
                persisted = course if self.repository.get_course(course) is not None else None
                course_scope = self._unsupported_scope("course_availability", persisted)
            else:
                observed_course, course_scope = self._target_course(
                    course, page_size, max_pages_per_scope
                )
            scopes.append(course_scope)
            if observed_course is None:
                return ScopedDiscoveryResult(None, (), (), tuple(scopes))
        elif self.repository.get_course(course) is None:
            raise ValueError("course must already exist when availability refresh is skipped")

        content: list[ContentSourceRecord] = []
        resources: list[ResourceMetadataRecord] = []
        if include_content:
            if not self._supported(SourceCapability.CONTENT_TREE):
                content_scope = self._unsupported_scope("content", course)
            else:
                try:
                    session = self._acquire(ReadPurpose.CONTENT)
                except SourceError as error:
                    self._invalidate_if_expired(error)
                    content_scope = self._failed_scope(
                        "content", course, safe_source_error_category(error.category)
                    )
                else:
                    content_scope, _ = self._content(
                        run_key,
                        session,
                        course,
                        page_size,
                        max_pages_per_scope,
                        max_content_nodes,
                        observed_content=content,
                        observed_resources=resources,
                    )
            scopes.append(content_scope)
        return ScopedDiscoveryResult(
            observed_course, tuple(content), tuple(resources), tuple(scopes)
        )

    def discover_courses(
        self,
        run_key: int,
        *,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
    ) -> tuple[tuple[CourseSourceRecord, ...], ScopeResult]:
        """Observe the accessible course inventory inside a caller-owned run."""

        if isinstance(run_key, bool) or not isinstance(run_key, int) or run_key <= 0:
            raise ValueError("run_key must be a positive integer")
        if max_pages_per_scope <= 0:
            raise ValueError("sync bounds must be positive")
        PageRequest(page_size=page_size)
        if not self._supported(SourceCapability.COURSE_DISCOVERY):
            courses: list[CourseSourceRecord] = []
            scope = self._unsupported_scope("courses", None)
        else:
            try:
                session = self._acquire(ReadPurpose.DISCOVERY)
            except SourceError as error:
                self._invalidate_if_expired(error)
                courses = []
                scope = self._failed_scope(
                    "courses", None, safe_source_error_category(error.category)
                )
            else:
                courses, scope = self._courses(session, page_size, max_pages_per_scope)
        return tuple(courses), scope

    def _target_course(
        self, course: CourseId, page_size: int, max_pages: int
    ) -> tuple[CourseSourceRecord | None, ScopeResult]:
        progress = _Progress()
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        result: CourseSourceRecord | None = None
        try:
            session = self._acquire(ReadPurpose.DISCOVERY)
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
                page = self.source.list_courses(
                    session, PageRequest(cursor=cursor, page_size=page_size)
                )
                self._validate_page(page)
                progress.pages_seen += 1
                self._merge_page_coverage(progress, page.coverage_for_page)
                for item in page.items:
                    self._validate_course(item)
                    if item.remote_id != course:
                        continue
                    self.repository.put_course(
                        item.remote_id,
                        code=item.code,
                        title=item.title,
                        term=item.term,
                        availability=item.availability,
                        observed_at=source_observed_at(self.source),
                    )
                    progress.items_seen = 1
                    progress.pagination_complete = True
                    result = item
                    break
                if result is not None:
                    break
                cursor = page.next_cursor
                if cursor is None:
                    progress.pagination_complete = True
                    break
        except SourceError as error:
            self._source_failure(progress, error)
            self._invalidate_if_expired(error)
        except Exception:
            self._source_failure(progress, SourceUnavailable())
        if result is None and progress.coverage is Coverage.COMPLETE:
            progress.coverage = Coverage.FAILED
            progress.pagination_complete = False
            progress.failure_category = "course_not_observed"
            progress.warn(SyncWarning.SOURCE_FAILURE)
        persisted = course if self.repository.get_course(course) is not None else None
        return result, self._scope("course_availability", persisted, progress)

    def _courses(
        self, session: AuthorizedReadSession, page_size: int, max_pages: int
    ) -> tuple[list[CourseSourceRecord], ScopeResult]:
        progress = _Progress()
        courses: list[CourseSourceRecord] = []
        seen_ids: set[CourseId] = set()
        cursor: str | None = None
        seen_cursors: set[str | None] = set()
        try:
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
                page = self.source.list_courses(
                    session, PageRequest(cursor=cursor, page_size=page_size)
                )
                self._validate_page(page)
                progress.pages_seen += 1
                self._merge_page_coverage(progress, page.coverage_for_page)
                for item in page.items:
                    self._validate_course(item)
                    if item.remote_id in seen_ids:
                        self._partial(progress, "source_protocol_error", SyncWarning.SOURCE_FAILURE)
                        continue
                    seen_ids.add(item.remote_id)
                    self.repository.put_course(
                        item.remote_id,
                        code=item.code,
                        title=item.title,
                        term=item.term,
                        availability=item.availability,
                        observed_at=source_observed_at(self.source),
                    )
                    courses.append(item)
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
        return courses, self._scope("courses", None, progress)

    def _content(
        self,
        run_key: int,
        session: AuthorizedReadSession,
        course: CourseId,
        page_size: int,
        max_pages: int,
        max_nodes: int,
        *,
        observed_content: list[ContentSourceRecord] | None = None,
        observed_resources: list[ResourceMetadataRecord] | None = None,
    ) -> tuple[ScopeResult, int]:
        progress = _Progress()
        parents: deque[ContentId | None] = deque([None])
        queued: set[ContentId] = set()
        visited_parents: set[ContentId | None] = set()
        seen_nodes: set[ContentId] = set()
        try:
            while parents:
                parent = parents.popleft()
                if parent in visited_parents:
                    self._partial(progress, "pagination_cycle", SyncWarning.PAGINATION_CYCLE)
                    continue
                visited_parents.add(parent)
                cursor: str | None = None
                seen_cursors: set[str | None] = set()
                while True:
                    if progress.pages_seen >= max_pages:
                        self._partial(
                            progress, "pagination_limit_reached", SyncWarning.PAGE_CAP_REACHED
                        )
                        parents.clear()
                        break
                    if cursor in seen_cursors:
                        self._partial(progress, "pagination_cycle", SyncWarning.PAGINATION_CYCLE)
                        break
                    seen_cursors.add(cursor)
                    page = self.source.list_content(
                        session,
                        course,
                        parent,
                        PageRequest(cursor=cursor, page_size=page_size),
                    )
                    self._validate_page(page)
                    progress.pages_seen += 1
                    self._merge_page_coverage(progress, page.coverage_for_page)
                    stop_parent = False
                    for item in page.items:
                        self._validate_content(item, course, parent)
                        if item.remote_id in seen_nodes:
                            self._partial(
                                progress,
                                "source_protocol_error",
                                SyncWarning.DUPLICATE_CONTENT_NODE,
                            )
                            continue
                        if progress.items_seen >= max_nodes:
                            self._partial(
                                progress,
                                "content_node_limit_reached",
                                SyncWarning.CONTENT_NODE_CAP_REACHED,
                            )
                            parents.clear()
                            stop_parent = True
                            break
                        seen_nodes.add(item.remote_id)
                        self.repository.put_content_node(
                            item.remote_id,
                            course_id=course,
                            parent_id=parent,
                            handler_kind=item.handler_kind,
                            title=item.title,
                            position=item.position,
                            availability=item.availability,
                            sanitized_metadata=item.sanitized_metadata,
                            observed_at=source_observed_at(self.source),
                        )
                        if observed_content is not None:
                            observed_content.append(item)
                        progress.items_seen += 1
                        for resource in item.resources:
                            self.resources.observe(
                                resource.remote_id,
                                content_id=item.remote_id,
                                sync_run_key=run_key,
                                display_title=resource.display_title,
                                original_filename=resource.original_filename,
                                availability=item.availability,
                                sanitized_metadata={
                                    "content_type": resource.declared_mime,
                                    "display_name": resource.display_title,
                                },
                                candidate_modified_at=None
                                if resource.candidate_modified_at is None
                                else to_storage_time(resource.candidate_modified_at),
                                observed_at=source_observed_at(self.source),
                            )
                            if observed_resources is not None:
                                observed_resources.append(resource)
                            progress.resources_seen += 1
                        if item.is_container and item.remote_id not in queued:
                            parents.append(item.remote_id)
                            queued.add(item.remote_id)
                    if stop_parent:
                        break
                    cursor = page.next_cursor
                    if cursor is None:
                        break
                if progress.failure_category in {
                    "pagination_limit_reached",
                    "content_node_limit_reached",
                }:
                    break
            if progress.failure_category is None:
                progress.pagination_complete = True
        except SourceError as error:
            self._source_failure(progress, error)
            self._invalidate_if_expired(error)
        except Exception:
            self._source_failure(progress, SourceUnavailable())
        return self._scope("content", course, progress), progress.resources_seen

    def _validate_course(self, item: CourseSourceRecord) -> None:
        if (
            type(item) is not CourseSourceRecord
            or type(item.remote_id) is not CourseId
            or item.remote_id.provider != self.source.provider_name
            or not isinstance(item.code, str)
            or not item.code.strip()
            or not isinstance(item.title, str)
            or not item.title.strip()
            or (item.term is not None and not isinstance(item.term, str))
            or not isinstance(item.availability, Availability)
        ):
            raise SourceProtocolError()

    @staticmethod
    def _validate_content(
        item: ContentSourceRecord, course: CourseId, parent: ContentId | None
    ) -> None:
        if (
            type(item) is not ContentSourceRecord
            or type(item.remote_id) is not ContentId
            or item.remote_id.provider != course.provider
            or item.course_id != course
            or item.parent_id != parent
            or not isinstance(item.handler_kind, str)
            or not item.handler_kind.strip()
            or not isinstance(item.title, str)
            or not item.title.strip()
            or isinstance(item.position, bool)
            or not isinstance(item.position, int)
            or item.position < 0
            or not isinstance(item.availability, Availability)
            or not isinstance(item.is_container, bool)
            or not isinstance(item.sanitized_metadata, Mapping)
            or any(
                type(resource) is not ResourceMetadataRecord
                or type(resource.remote_id) is not AttachmentId
                or resource.remote_id.provider != course.provider
                or resource.content_id != item.remote_id
                or not isinstance(resource.display_title, str)
                or not resource.display_title.strip()
                or not isinstance(resource.original_filename, str)
                or not resource.original_filename
                or (
                    resource.declared_mime is not None
                    and not isinstance(resource.declared_mime, str)
                )
                for resource in item.resources
            )
        ):
            raise SourceProtocolError()

    @staticmethod
    def _validate_page(page: object) -> None:
        if not isinstance(page, Page):
            raise SourceProtocolError()

    @staticmethod
    def _merge_page_coverage(progress: _Progress, coverage: Coverage) -> None:
        if coverage is Coverage.PARTIAL:
            progress.coverage = Coverage.PARTIAL
            progress.warn(SyncWarning.PAGE_REPORTED_PARTIAL)
        elif coverage is Coverage.UNKNOWN and progress.coverage is Coverage.COMPLETE:
            progress.coverage = Coverage.UNKNOWN
            progress.warn(SyncWarning.PAGE_COVERAGE_UNKNOWN)
        elif coverage in {Coverage.STALE, Coverage.FAILED} or not isinstance(coverage, Coverage):
            raise SourceProtocolError()

    @staticmethod
    def _partial(progress: _Progress, category: str, warning: SyncWarning) -> None:
        progress.coverage = Coverage.PARTIAL
        progress.failure_category = category
        progress.pagination_complete = False
        progress.warn(warning)

    @staticmethod
    def _source_failure(progress: _Progress, error: SourceError) -> None:
        progress.coverage = Coverage.PARTIAL if progress.pages_seen else Coverage.FAILED
        progress.pagination_complete = False
        progress.failure_category = safe_source_error_category(error.category)
        progress.warn(SyncWarning.SOURCE_FAILURE)

    def _scope(self, data_kind: str, course: CourseId | None, progress: _Progress) -> ScopeResult:
        return ScopeResult(
            provider=self.source.provider_name,
            course_id=course,
            data_kind=data_kind,
            coverage=progress.coverage,
            pages_seen=progress.pages_seen,
            items_seen=progress.items_seen,
            pagination_complete=progress.pagination_complete,
            failure_category=progress.failure_category,
            warnings=tuple(progress.warnings),
            observed_at=source_observed_at(self.source),
        )

    def _failed_scope(self, data_kind: str, course: CourseId | None, category: str) -> ScopeResult:
        return ScopeResult(
            provider=self.source.provider_name,
            course_id=course,
            data_kind=data_kind,
            coverage=Coverage.FAILED,
            pages_seen=0,
            items_seen=0,
            pagination_complete=False,
            failure_category=category,
            warnings=(SyncWarning.SOURCE_FAILURE,),
            observed_at=source_observed_at(self.source),
        )

    def _unsupported_scope(self, data_kind: str, course: CourseId | None) -> ScopeResult:
        return ScopeResult(
            provider=self.source.provider_name,
            course_id=course,
            data_kind=data_kind,
            coverage=Coverage.UNKNOWN,
            pages_seen=0,
            items_seen=0,
            pagination_complete=False,
            failure_category="unsupported_capability",
            warnings=(SyncWarning.PAGE_COVERAGE_UNKNOWN,),
            observed_at=source_observed_at(self.source),
        )

    def _supported(self, capability: SourceCapability) -> bool:
        try:
            return self.source.capabilities().state(capability) is CapabilityState.SUPPORTED
        except Exception:
            return False

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
