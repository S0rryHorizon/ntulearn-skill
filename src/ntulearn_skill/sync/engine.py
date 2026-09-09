"""Small orchestration layer for M8 read-only synchronization modes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import MappingProxyType

from ntulearn_skill.client import (
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    PageRequest,
    ResourceMetadataRecord,
    SessionProvider,
    SourceCapability,
    SourceProvider,
    TimeWindow,
)
from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId, Coverage
from ntulearn_skill.core.models import FetchDecision, SyncRunStatus, utc_now
from ntulearn_skill.events import DeterministicEventExtractor, EventReconciler, EventRepository
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.parsers import (
    DocxParser,
    ParseRepository,
    ParserRegistry,
    ParseService,
    PdfParser,
)
from ntulearn_skill.storage import DomainRepository, ResourceStore, StorageError
from ntulearn_skill.sync.discovery import DiscoverySync, ScopedDiscoveryResult
from ntulearn_skill.sync.event_sources import EventSourceScope, EventSourceSync
from ntulearn_skill.sync.jobs import (
    LocalJobPlanner,
    LocalJobQueue,
    LocalJobRunner,
    LocalJobRunResult,
)
from ntulearn_skill.sync.lifecycle import ResourceInventoryReconciler
from ntulearn_skill.sync.models import ScopeResult, SyncRunResult, SyncWarning
from ntulearn_skill.sync.observability import SyncRunRecorder
from ntulearn_skill.sync.resources import ResourceFetchResult, ResourceFetchService
from ntulearn_skill.sync.state import ScopeKey, SyncAttemptOutcome, SyncStateRepository


@dataclass(frozen=True, slots=True)
class CourseSelectionPolicy:
    included_course_ids: frozenset[CourseId] | None = None
    include_unavailable: bool = False

    def __post_init__(self) -> None:
        if self.included_course_ids is not None:
            values = frozenset(self.included_course_ids)
            if any(type(value) is not CourseId for value in values):
                raise TypeError("included course ids must be typed")
            object.__setattr__(self, "included_course_ids", values)
        if not isinstance(self.include_unavailable, bool):
            raise TypeError("include_unavailable must be boolean")

    def accepts(self, course: CourseSourceRecord) -> bool:
        return bool(
            (self.included_course_ids is None or course.remote_id in self.included_course_ids)
            and (self.include_unavailable or course.availability is Availability.ACTIVE)
        )


@dataclass(slots=True)
class _RunProgress:
    scopes: list[ScopeResult] = field(default_factory=list)
    observations: int = 0
    resources_discovered: int = 0
    resources_fetched: int = 0
    resources_failed: int = 0
    jobs_claimed: int = 0
    jobs_succeeded: int = 0
    jobs_failed: int = 0
    jobs_remaining: int = 0
    jobs_blocked: int = 0
    job_keys: set[int] = field(default_factory=set)
    resources_marked_missing: int = 0
    uncertainty_observations: int = 0
    removal_candidates: int = 0
    inventories: list[
        tuple[
            CourseId,
            ScopeResult,
            tuple[ContentId, ...],
            tuple[AttachmentId, ...],
        ]
    ] = field(default_factory=list)


class SyncEngine:
    """Compose M8 services while retaining their narrow contracts."""

    def __init__(
        self,
        sessions: SessionProvider,
        source: SourceProvider,
        domain: DomainRepository,
        store: ResourceStore,
        *,
        parser_registry: ParserRegistry | None = None,
        verification_interval: timedelta = timedelta(days=7),
    ) -> None:
        if domain.database.path != store.repository.database.path:
            raise ValueError("domain and resource repositories must share one database")
        database = domain.database
        parser = ParseService(
            store.paths,
            store.repository,
            ParseRepository(database),
            parser_registry or ParserRegistry((PdfParser(), DocxParser())),
        )
        extractor = DeterministicEventExtractor(database)
        reconciler = EventReconciler(database)
        queue = LocalJobQueue(database)
        planner = LocalJobPlanner(queue, parser, extractor, reconciler)
        self.sessions = sessions
        self.source = source
        self.domain = domain
        self.store = store
        self.discovery = DiscoverySync(sessions=sessions, source=source, repository=domain)
        self.events = EventSourceSync(
            sessions=sessions,
            source=source,
            repository=EventRepository(database),
        )
        self.planner = planner
        self.runner = LocalJobRunner(queue, parser, SearchIndex(database), extractor, reconciler)
        self.resources = ResourceFetchService(
            sessions,
            source,
            store,
            planner,
            verification_interval=verification_interval,
        )
        self.state = SyncStateRepository(database)
        self.lifecycle = ResourceInventoryReconciler(database)
        self.recorder = SyncRunRecorder(database, source.provider_name)

    def quick_sync(
        self,
        course: CourseId,
        *,
        window: TimeWindow,
        event_scopes: frozenset[EventSourceScope] | None = None,
        assessment_content_ids: Iterable[ContentId] = (),
        assessment_inventory_coverage: Coverage = Coverage.UNKNOWN,
        include_content: bool = True,
        fetch_resources: bool = False,
        verify_resources: bool = False,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_content_nodes: int = 10_000,
        max_jobs: int = 32,
    ) -> SyncRunResult:
        return self._one_course_run(
            "quick",
            course,
            window=window,
            event_scopes=event_scopes,
            assessment_content_ids=assessment_content_ids,
            assessment_inventory_coverage=assessment_inventory_coverage,
            include_availability=True,
            include_content=include_content,
            fetch_resources=fetch_resources,
            verify_resources=verify_resources,
            page_size=page_size,
            max_pages_per_scope=max_pages_per_scope,
            max_content_nodes=max_content_nodes,
            max_jobs=max_jobs,
        )

    def sync_course(
        self,
        course: CourseId,
        *,
        window: TimeWindow,
        event_scopes: frozenset[EventSourceScope] | None = None,
        assessment_content_ids: Iterable[ContentId] = (),
        assessment_inventory_coverage: Coverage = Coverage.UNKNOWN,
        fetch_resources: bool = True,
        verify_resources: bool = False,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_content_nodes: int = 10_000,
        max_jobs: int = 64,
    ) -> SyncRunResult:
        return self._one_course_run(
            "course",
            course,
            window=window,
            event_scopes=event_scopes,
            assessment_content_ids=assessment_content_ids,
            assessment_inventory_coverage=assessment_inventory_coverage,
            include_availability=True,
            include_content=True,
            fetch_resources=fetch_resources,
            verify_resources=verify_resources,
            page_size=page_size,
            max_pages_per_scope=max_pages_per_scope,
            max_content_nodes=max_content_nodes,
            max_jobs=max_jobs,
        )

    def sync_all(
        self,
        policy: CourseSelectionPolicy,
        *,
        window: TimeWindow,
        event_scopes: frozenset[EventSourceScope] | None = None,
        fetch_resources: bool = True,
        verify_resources: bool = False,
        page_size: int = 100,
        max_pages_per_scope: int = 100,
        max_content_nodes: int = 10_000,
        max_jobs_per_course: int = 64,
    ) -> SyncRunResult:
        if not isinstance(policy, CourseSelectionPolicy):
            raise TypeError("sync-all policy must be typed")
        if policy.included_course_ids is not None and any(
            item.provider != self.source.provider_name for item in policy.included_course_ids
        ):
            raise ValueError("selected courses must belong to the source provider")
        self._validate_run_inputs(
            window,
            event_scopes,
            (),
            Coverage.UNKNOWN,
            page_size,
            max_pages_per_scope,
            max_content_nodes,
            max_jobs_per_course,
        )
        run_key = self.recorder.start(
            mode="all",
            requested_scope={
                "fetch_resources": fetch_resources,
                "verify_resources": verify_resources,
                "page_size": page_size,
                "max_pages_per_scope": max_pages_per_scope,
                "max_content_nodes": max_content_nodes,
                "max_jobs_per_course": max_jobs_per_course,
            },
        )
        progress = _RunProgress()
        try:
            courses, course_scope = self.discovery.discover_courses(
                run_key,
                page_size=page_size,
                max_pages_per_scope=max_pages_per_scope,
            )
            progress.scopes.append(course_scope)
            for course_record in courses:
                if not policy.accepts(course_record):
                    continue
                discovery = self.discovery.sync_course(
                    run_key,
                    course_record.remote_id,
                    include_availability=False,
                    include_content=True,
                    page_size=page_size,
                    max_pages_per_scope=max_pages_per_scope,
                    max_content_nodes=max_content_nodes,
                )
                progress.scopes.extend(discovery.scopes)
                self._sync_discovered_course(
                    run_key,
                    course_record.remote_id,
                    window,
                    discovery,
                    progress,
                    event_scopes=event_scopes,
                    supplied_assessment_ids=(),
                    supplied_assessment_coverage=Coverage.UNKNOWN,
                    fetch_resources=fetch_resources,
                    verify_resources=verify_resources,
                    max_pages_per_scope=max_pages_per_scope,
                    page_size=page_size,
                    max_jobs=max_jobs_per_course,
                )
            return self._finish(run_key, progress, window)
        except Exception:
            return self._finish_failed(run_key, progress)

    def fetch_resource(
        self,
        resource: ResourceMetadataRecord | AttachmentId,
        *,
        verify: bool = False,
        max_jobs: int = 16,
    ) -> ResourceFetchResult:
        self._validate_bounds(max_jobs)
        if type(resource) not in {ResourceMetadataRecord, AttachmentId}:
            raise TypeError("resource must use a typed source identity")
        remote_id = resource.remote_id if isinstance(resource, ResourceMetadataRecord) else resource
        if remote_id.provider != self.source.provider_name:
            raise ValueError("resource must belong to the source provider")
        run_key = self.recorder.start(
            mode="resource",
            requested_scope={"verify": verify, "max_jobs": max_jobs},
        )
        progress = _RunProgress()
        try:
            result = self.resources.fetch(resource, sync_run_key=run_key, verify=verify)
            failed = result.fetch_decision is FetchDecision.FAILED
            progress.resources_fetched = 0 if failed else 1
            progress.resources_failed = 1 if failed else 0
            if result.job_plan is not None:
                progress.job_keys.update(result.job_plan.keys)
            recovery_warning = result.warning_codes[0] if result.warning_codes else None
            progress.scopes.append(
                ScopeResult(
                    self.source.provider_name,
                    None,
                    "resource",
                    (
                        Coverage.FAILED
                        if failed
                        else Coverage.PARTIAL
                        if recovery_warning is not None
                        else Coverage.COMPLETE
                    ),
                    0,
                    0 if failed else 1,
                    not failed and recovery_warning is None,
                    result.error_category if failed else recovery_warning,
                    (
                        (SyncWarning.SOURCE_FAILURE,)
                        if failed or recovery_warning is not None
                        else ()
                    ),
                    utc_now(),
                )
            )
            self._run_jobs(progress, None, max_jobs=max_jobs)
            self._finish(run_key, progress, None)
            return result
        except Exception:
            self._finish_failed(run_key, progress)
            raise StorageError("resource synchronization failed") from None

    def refresh_scope(self, scope: ScopeKey, *, max_jobs: int = 32) -> SyncRunResult:
        if not isinstance(scope, ScopeKey) or scope.provider != self.source.provider_name:
            raise ValueError("refresh scope must belong to the source provider")
        self._validate_bounds(max_jobs)
        if scope.course_id is None:
            if scope.data_kind != "courses" or scope.time_window is not None:
                raise ValueError("provider refresh supports only the course inventory")
            return self._refresh_courses(max_jobs=max_jobs)
        if scope.data_kind == "content":
            if scope.time_window is not None:
                raise ValueError("content refresh does not accept a time window")
            return self._one_course_run(
                "refresh",
                scope.course_id,
                window=self._unused_window(),
                event_scopes=frozenset(),
                assessment_content_ids=(),
                assessment_inventory_coverage=Coverage.UNKNOWN,
                include_availability=False,
                include_content=True,
                fetch_resources=False,
                verify_resources=False,
                page_size=100,
                max_pages_per_scope=100,
                max_content_nodes=10_000,
                max_jobs=max_jobs,
            )
        if scope.data_kind == "course_availability":
            if scope.time_window is not None:
                raise ValueError("course availability does not accept a time window")
            return self._one_course_run(
                "refresh",
                scope.course_id,
                window=self._unused_window(),
                event_scopes=frozenset(),
                assessment_content_ids=(),
                assessment_inventory_coverage=Coverage.UNKNOWN,
                include_availability=True,
                include_content=False,
                fetch_resources=False,
                verify_resources=False,
                page_size=100,
                max_pages_per_scope=100,
                max_content_nodes=10_000,
                max_jobs=max_jobs,
            )
        try:
            event_scope = EventSourceScope(scope.data_kind)
        except ValueError:
            raise ValueError("refresh scope data kind is unsupported") from None
        if event_scope in {EventSourceScope.SCHEDULE, EventSourceScope.DUE_ITEMS}:
            if scope.time_window is None:
                raise ValueError("calendar refresh requires an exact time window")
            window = scope.time_window
        else:
            if scope.time_window is not None:
                raise ValueError("this event refresh does not accept a time window")
            window = self._unused_window()
        assessment_ids = (
            self._known_assessment_ids(scope.course_id)
            if event_scope is EventSourceScope.ASSESSMENTS
            else ()
        )
        return self._one_course_run(
            "refresh",
            scope.course_id,
            window=window,
            event_scopes=frozenset({event_scope}),
            assessment_content_ids=assessment_ids,
            assessment_inventory_coverage=Coverage.UNKNOWN,
            include_availability=False,
            include_content=False,
            fetch_resources=False,
            verify_resources=False,
            page_size=100,
            max_pages_per_scope=100,
            max_content_nodes=10_000,
            max_jobs=max_jobs,
        )

    def _one_course_run(
        self,
        mode: str,
        course: CourseId,
        *,
        window: TimeWindow,
        event_scopes: frozenset[EventSourceScope] | None,
        assessment_content_ids: Iterable[ContentId],
        assessment_inventory_coverage: Coverage,
        include_availability: bool,
        include_content: bool,
        fetch_resources: bool,
        verify_resources: bool,
        page_size: int,
        max_pages_per_scope: int,
        max_content_nodes: int,
        max_jobs: int,
    ) -> SyncRunResult:
        self._validate_course(course)
        assessment_ids = tuple(assessment_content_ids)
        self._validate_run_inputs(
            window,
            event_scopes,
            assessment_ids,
            assessment_inventory_coverage,
            page_size,
            max_pages_per_scope,
            max_content_nodes,
            max_jobs,
        )
        if not include_availability and self.domain.get_course(course) is None:
            raise ValueError("course must be observed before a scoped refresh")
        run_key = self.recorder.start(
            mode=mode,
            requested_scope={
                "include_availability": include_availability,
                "include_content": include_content,
                "fetch_resources": fetch_resources,
                "verify_resources": verify_resources,
                "page_size": page_size,
                "max_pages_per_scope": max_pages_per_scope,
                "max_content_nodes": max_content_nodes,
                "max_jobs": max_jobs,
            },
        )
        progress = _RunProgress()
        try:
            discovery = self.discovery.sync_course(
                run_key,
                course,
                include_availability=include_availability,
                include_content=include_content,
                page_size=page_size,
                max_pages_per_scope=max_pages_per_scope,
                max_content_nodes=max_content_nodes,
            )
            progress.scopes.extend(discovery.scopes)
            if not include_availability or discovery.course is not None:
                self._sync_discovered_course(
                    run_key,
                    course,
                    window,
                    discovery,
                    progress,
                    event_scopes=event_scopes,
                    supplied_assessment_ids=assessment_ids,
                    supplied_assessment_coverage=assessment_inventory_coverage,
                    fetch_resources=fetch_resources,
                    verify_resources=verify_resources,
                    max_pages_per_scope=max_pages_per_scope,
                    page_size=page_size,
                    max_jobs=max_jobs,
                )
            return self._finish(run_key, progress, window)
        except Exception:
            return self._finish_failed(run_key, progress)

    def _sync_discovered_course(
        self,
        run_key: int,
        course: CourseId,
        window: TimeWindow,
        discovery: ScopedDiscoveryResult,
        progress: _RunProgress,
        *,
        event_scopes: frozenset[EventSourceScope] | None,
        supplied_assessment_ids: Iterable[ContentId],
        supplied_assessment_coverage: Coverage,
        fetch_resources: bool,
        verify_resources: bool,
        max_pages_per_scope: int,
        page_size: int,
        max_jobs: int,
    ) -> None:
        course_job_keys: set[int] = set()
        progress.resources_discovered += len(discovery.resources)
        content_scope = next(
            (scope for scope in discovery.scopes if scope.data_kind == "content"), None
        )
        if content_scope is not None:
            progress.inventories.append(
                (
                    course,
                    content_scope,
                    tuple(item.remote_id for item in discovery.content),
                    tuple(item.remote_id for item in discovery.resources),
                )
            )
        assessment_coverage = (
            supplied_assessment_coverage if content_scope is None else content_scope.coverage
        )
        event_result = self.events.run(
            run_key=run_key,
            course=course,
            window=window,
            assessment_content_ids=self._assessment_ids(discovery.content, supplied_assessment_ids),
            assessment_inventory_coverage=assessment_coverage,
            scopes=event_scopes,
            page_size=page_size,
            max_pages_per_scope=max_pages_per_scope,
        )
        progress.scopes.extend(event_result.scopes)
        progress.observations += len(event_result.observation_keys)
        for observation_key in event_result.observation_keys:
            keys = self.planner.plan_observation(observation_key).keys
            progress.job_keys.update(keys)
            course_job_keys.update(keys)
        if fetch_resources:
            self._fetch_discovered(
                run_key,
                course,
                discovery,
                progress,
                course_job_keys,
                verify=verify_resources,
            )
        self._run_jobs(progress, course, max_jobs=max_jobs, job_keys=course_job_keys)

    @staticmethod
    def _assessment_ids(
        content: tuple[ContentSourceRecord, ...], supplied: Iterable[ContentId]
    ) -> tuple[ContentId, ...]:
        result: list[ContentId] = []
        seen: set[ContentId] = set()
        for item in content:
            handler = item.handler_kind.casefold()
            if "asmt" in handler or "assessment" in handler:
                result.append(item.remote_id)
                seen.add(item.remote_id)
        for content_id in supplied:
            if content_id not in seen:
                result.append(content_id)
                seen.add(content_id)
        return tuple(result)

    def _fetch_discovered(
        self,
        run_key: int,
        course: CourseId,
        discovery: ScopedDiscoveryResult,
        progress: _RunProgress,
        job_keys: set[int],
        *,
        verify: bool,
    ) -> None:
        successful = 0
        failures = 0
        recovery_warnings: list[str] = []
        for resource in discovery.resources:
            result = self.resources.fetch(resource, sync_run_key=run_key, verify=verify)
            if result.fetch_decision is FetchDecision.FAILED:
                failures += 1
            else:
                successful += 1
            for warning in result.warning_codes:
                if warning not in recovery_warnings:
                    recovery_warnings.append(warning)
            if result.job_plan is not None:
                progress.job_keys.update(result.job_plan.keys)
                job_keys.update(result.job_plan.keys)
        progress.resources_fetched += successful
        progress.resources_failed += failures
        content_scope = next(
            (scope for scope in discovery.scopes if scope.data_kind == "content"), None
        )
        coverage = Coverage.COMPLETE if content_scope is None else content_scope.coverage
        if failures or recovery_warnings:
            coverage = (
                Coverage.PARTIAL if successful or content_scope is not None else Coverage.FAILED
            )
        progress.scopes.append(
            ScopeResult(
                self.source.provider_name,
                course,
                "resources",
                coverage,
                0,
                successful,
                bool(content_scope is not None and content_scope.pagination_complete),
                "resource_fetch_failed"
                if failures
                else recovery_warnings[0]
                if recovery_warnings
                else None,
                (SyncWarning.SOURCE_FAILURE,) if failures or recovery_warnings else (),
                utc_now(),
            )
        )

    def _add_jobs(
        self, progress: _RunProgress, course: CourseId | None, result: LocalJobRunResult
    ) -> None:
        progress.jobs_claimed += result.claimed
        progress.jobs_succeeded += result.succeeded
        progress.jobs_failed += result.failed
        progress.jobs_remaining += result.remaining
        progress.jobs_blocked += result.blocked
        if result.failed or result.remaining or result.blocked:
            failure = bool(result.failed)
            progress.scopes.append(
                ScopeResult(
                    self.source.provider_name,
                    course,
                    "local_jobs",
                    Coverage.PARTIAL,
                    0,
                    result.succeeded,
                    False,
                    "local_job_failure" if failure else "local_jobs_pending",
                    ((SyncWarning.SOURCE_FAILURE,) if failure else (SyncWarning.PAGE_CAP_REACHED,)),
                    utc_now(),
                )
            )

    def _run_jobs(
        self,
        progress: _RunProgress,
        course: CourseId | None,
        *,
        max_jobs: int,
        job_keys: Iterable[int] | None = None,
    ) -> None:
        self._add_jobs(
            progress,
            course,
            self.runner.run(
                max_jobs=max_jobs,
                job_keys=progress.job_keys if job_keys is None else job_keys,
            ),
        )

    def _refresh_courses(self, *, max_jobs: int) -> SyncRunResult:
        run_key = self.recorder.start(
            mode="refresh",
            requested_scope={"course_inventory": True, "max_jobs": max_jobs},
        )
        progress = _RunProgress()
        try:
            _, scope = self.discovery.discover_courses(run_key)
            progress.scopes.append(scope)
            return self._finish(run_key, progress, None)
        except Exception:
            return self._finish_failed(run_key, progress)

    def _known_assessment_ids(self, course: CourseId) -> tuple[ContentId, ...]:
        connection = self.domain.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT provider.name AS provider, object.remote_key
                FROM assessment
                JOIN content_node ON content_node.content_key = assessment.content_key
                JOIN course ON course.course_key = assessment.course_key
                JOIN source_object course_object
                  ON course_object.source_object_key = course.source_object_key
                JOIN source_object object
                  ON object.source_object_key = content_node.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE provider.name = ? AND course_object.remote_key = ?
                  AND course_object.provider_key = object.provider_key
                ORDER BY assessment.assessment_key
                """,
                (course.provider, course.value),
            ).fetchall()
            return tuple(ContentId(str(row["provider"]), str(row["remote_key"])) for row in rows)
        except sqlite3.Error:
            raise StorageError("known assessment lookup failed") from None
        finally:
            connection.close()

    @staticmethod
    def _unused_window() -> TimeWindow:
        start = datetime(1970, 1, 1, tzinfo=UTC)
        return TimeWindow(start, start + timedelta(seconds=1))

    def _finish(
        self, run_key: int, progress: _RunProgress, window: TimeWindow | None
    ) -> SyncRunResult:
        capabilities = self._capabilities()
        for scope in progress.scopes:
            self.recorder.record_scope(run_key, scope)
            if scope.data_kind == "local_jobs" or (
                scope.data_kind == "course_availability" and scope.course_id is None
            ):
                continue
            scope_window = (
                window
                if scope.data_kind
                in {EventSourceScope.SCHEDULE.value, EventSourceScope.DUE_ITEMS.value}
                else None
            )
            self.state.record_attempt(
                ScopeKey(
                    self.source.provider_name,
                    scope.course_id,
                    scope.data_kind,
                    scope_window,
                ),
                run_key=run_key,
                attempted_at=scope.observed_at,
                outcome=self._outcome(scope),
                coverage=scope.coverage,
                warning_codes=scope.warnings,
                capabilities=capabilities,
            )
        for course, scope_result, content_ids, resource_ids in progress.inventories:
            lifecycle = self.lifecycle.reconcile(
                run_key,
                course,
                scope_result,
                resource_ids,
                scope=ScopeKey(self.source.provider_name, course, "content"),
                seen_content_ids=content_ids,
            )
            progress.resources_marked_missing += lifecycle.resources_marked_missing
            progress.uncertainty_observations += lifecycle.uncertainty_observations
            progress.removal_candidates += lifecycle.removal_candidates
        warnings = tuple(
            dict.fromkeys(warning for scope in progress.scopes for warning in scope.warnings)
        )
        remote = [scope for scope in progress.scopes if scope.data_kind != "local_jobs"]
        if remote and all(scope.coverage is Coverage.FAILED for scope in remote):
            status = SyncRunStatus.FAILED
            error_category = next(
                (scope.failure_category for scope in remote if scope.failure_category),
                "source_unavailable",
            )
        elif any(scope.coverage is not Coverage.COMPLETE for scope in progress.scopes):
            status = SyncRunStatus.SUCCEEDED_WITH_WARNINGS
            error_category = None
        else:
            status = SyncRunStatus.SUCCEEDED
            error_category = None
        return self.recorder.finish(
            run_key,
            status=status,
            counts=self._counts(progress),
            warnings=warnings,
            error_category=error_category,
        )

    def _finish_failed(self, run_key: int, progress: _RunProgress) -> SyncRunResult:
        return self.recorder.finish(
            run_key,
            status=SyncRunStatus.FAILED,
            counts=self._counts(progress),
            warnings=(SyncWarning.SOURCE_FAILURE,),
            error_category="sync_engine_error",
        )

    @staticmethod
    def _counts(progress: _RunProgress) -> dict[str, int]:
        return {
            "observations": progress.observations,
            "resources_discovered": progress.resources_discovered,
            "resources_fetched": progress.resources_fetched,
            "resources_failed": progress.resources_failed,
            "jobs_claimed": progress.jobs_claimed,
            "jobs_succeeded": progress.jobs_succeeded,
            "jobs_failed": progress.jobs_failed,
            "jobs_remaining": progress.jobs_remaining,
            "jobs_blocked": progress.jobs_blocked,
            "resources_marked_missing": progress.resources_marked_missing,
            "uncertainty_observations": progress.uncertainty_observations,
            "removal_candidates": progress.removal_candidates,
        }

    def _capabilities(self) -> Mapping[SourceCapability, CapabilityState]:
        try:
            return MappingProxyType(dict(self.source.capabilities().states))
        except Exception:
            return MappingProxyType({})

    @staticmethod
    def _outcome(scope: ScopeResult) -> SyncAttemptOutcome:
        if scope.coverage is Coverage.FAILED:
            return SyncAttemptOutcome.FAILED
        return SyncAttemptOutcome.SUCCEEDED

    def _validate_course(self, course: CourseId) -> None:
        if type(course) is not CourseId or course.provider != self.source.provider_name:
            raise ValueError("course must belong to the source provider")

    def _validate_run_inputs(
        self,
        window: TimeWindow,
        event_scopes: frozenset[EventSourceScope] | None,
        assessment_ids: tuple[ContentId, ...],
        assessment_coverage: Coverage,
        page_size: int,
        max_pages: int,
        max_nodes: int,
        max_jobs: int,
    ) -> None:
        if not isinstance(window, TimeWindow):
            raise TypeError("window must be a TimeWindow")
        PageRequest(page_size=page_size)
        if max_pages <= 0 or max_nodes <= 0:
            raise ValueError("sync bounds must be positive")
        self._validate_bounds(max_jobs)
        if event_scopes is not None and any(
            not isinstance(scope, EventSourceScope) for scope in event_scopes
        ):
            raise ValueError("event scopes must be typed")
        if any(
            type(content_id) is not ContentId or content_id.provider != self.source.provider_name
            for content_id in assessment_ids
        ):
            raise ValueError("assessment content must belong to the source provider")
        if assessment_coverage not in {
            Coverage.COMPLETE,
            Coverage.PARTIAL,
            Coverage.UNKNOWN,
        }:
            raise ValueError("assessment inventory coverage is invalid")

    @staticmethod
    def _validate_bounds(max_jobs: int) -> None:
        if isinstance(max_jobs, bool) or not isinstance(max_jobs, int) or max_jobs <= 0:
            raise ValueError("maximum jobs must be positive")
