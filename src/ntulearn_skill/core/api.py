"""Stable local-first core service over the M1-M8 persistence and sync layers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from ntulearn_skill.client import TimeWindow
from ntulearn_skill.core import errors as core_errors
from ntulearn_skill.core import event_views, readers, results
from ntulearn_skill.core import pagination as paging
from ntulearn_skill.core.identifiers import AttachmentId, CourseId, require_identifier
from ntulearn_skill.core.models import (
    AssessmentSubtype,
    Availability,
    Coverage,
    TemporalPrecision,
    from_storage_time,
    to_storage_time,
    utc_now,
)
from ntulearn_skill.core.pagination import PageLoad, Pagination, R
from ntulearn_skill.core.queries import (
    DEFAULT_LIMIT,
    AnnouncementFilter,
    AnnouncementView,
    AssessmentFilter,
    AssessmentView,
    CourseFilter,
    CourseRef,
    CourseSummary,
    EventFilter,
    LibraryStatus,
    ManualResolution,
    MaterialChangeView,
    MaterialFilter,
    MaterialSummary,
    ResourceRef,
    ResourceView,
    SourceLocatorRef,
    SyncPolicy,
    bounded_cursor,
    bounded_limit,
)
from ntulearn_skill.core.results import (
    CoverageView,
    ErrorCategory,
    ProvenanceView,
    ResultEnvelope,
    SafeError,
    SafeWarning,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    CanonicalEvent,
    DeterministicEventExtractor,
    EventReconciler,
    EventResolutionState,
    ManualFieldResolution,
    ManualIdentityResolution,
)
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.search import (
    SearchEntityKind,
    SearchFilters,
    SearchQuery,
    SearchResult,
    SearchService,
    SourceReference,
    SourceReferenceKind,
)
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRecord,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync.engine import CourseSelectionPolicy, SyncEngine
from ntulearn_skill.sync.freshness import (
    FreshnessRequirement,
    FreshnessStatus,
    evaluate_freshness,
)
from ntulearn_skill.sync.models import SyncRunResult
from ntulearn_skill.sync.observability import SyncRunRecorder
from ntulearn_skill.sync.state import ScopeKey, SyncStateRepository


class CoreService:
    """Cohesive local query facade with an optional injected read-only sync engine."""

    def __init__(
        self,
        database: Database,
        *,
        runtime_paths: RuntimePaths | None = None,
        sync_engine: SyncEngine | None = None,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        if not isinstance(database, Database):
            raise TypeError("core database must be typed")
        if runtime_paths is not None and runtime_paths.database != database.path:
            raise ValueError("runtime paths and database must identify the same runtime")
        if sync_engine is not None and sync_engine.domain.database.path != database.path:
            raise ValueError("sync engine and core must share one database")
        self.database = database
        self.runtime_paths = runtime_paths
        self.sync_engine = sync_engine
        self.now = now
        self.domain = DomainRepository(database)
        self.resources = ResourceRepository(database)
        self.search_service = SearchService(database)
        self.events = EventReconciler(database)
        self.states = SyncStateRepository(database)

    @classmethod
    def from_runtime(
        cls,
        root: str | Path | None = None,
        *,
        sync_engine: SyncEngine | None = None,
        browser_capture: str | Path | None = None,
        initialize: bool = True,
    ) -> CoreService:
        if sync_engine is not None and browser_capture is not None:
            raise ValueError("runtime accepts only one source configuration")
        paths = RuntimePaths.discover(root)
        if initialize:
            paths.ensure()
        database = Database(paths.database)
        if initialize:
            DomainRepository(database).initialize()
        if browser_capture is not None:
            from ntulearn_skill.client import (
                BrowserCaptureProvider,
                BrowserCaptureSessionProvider,
            )

            source = BrowserCaptureProvider.from_manifest(browser_capture)
            sessions = BrowserCaptureSessionProvider(source.bundle)
            sync_engine = SyncEngine(
                sessions,
                source,
                DomainRepository(database),
                ResourceStore(paths, ResourceRepository(database)),
            )
        return cls(database, runtime_paths=paths, sync_engine=sync_engine)

    def list_courses(
        self,
        filter: CourseFilter = CourseFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[CourseSummary]:
        operation = "list_courses"
        try:
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                filter.cursor,
                {"availability": sorted(item.value for item in filter.availability)},
                filter.limit,
            )
            providers = self._providers()
            if not providers and self.sync_engine is not None:
                providers = (self.sync_engine.source.provider_name,)
            scopes = tuple(ScopeKey(provider, None, "courses") for provider in providers)
            return self._query(
                operation,
                lambda: self._load_courses(filter, pagination.offset),
                scopes,
                freshness,
                bounded_limit=filter.limit,
                pagination=pagination,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "courses")

    def list_materials(
        self,
        course: CourseRef,
        filter: MaterialFilter = MaterialFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[MaterialSummary]:
        operation = "list_materials"
        try:
            course_id, course_key = self._course(course)
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                filter.cursor,
                {
                    "availability": sorted(item.value for item in filter.availability),
                    "file_formats": sorted(filter.file_formats),
                    "include_historical_versions": filter.include_historical_versions,
                    "course": [course_id.provider, course_id.value],
                },
                filter.limit,
            )
            scope = ScopeKey(course_id.provider, course_id, "content")
            return self._query(
                operation,
                lambda: readers.load_materials(
                    self.database, course_id, course_key, filter, pagination.offset
                ),
                (scope,),
                freshness,
                bounded_limit=filter.limit,
                pagination=pagination,
                extra_coverage=lambda: (self._parse_coverage(course_id, course_key),),
            )
        except Exception as error:
            return core_errors.failure(operation, error, "materials")

    def get_library_status(
        self,
        course: CourseRef | None = None,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[LibraryStatus]:
        """Report bounded local discovery/download/parse/index progress by course."""

        operation = "get_library_status"
        try:
            providers = self._providers()
            if not providers and self.sync_engine is not None:
                providers = (self.sync_engine.source.provider_name,)
            if course is None:
                selected = self._course_id_rows(None)
                initial_course_ids = frozenset(item for item, _key in selected)
                scopes = tuple(
                    ScopeKey(provider, None, "courses") for provider in providers
                ) + tuple(ScopeKey(item.provider, item, "content") for item, _key in selected)

                def current_courses() -> tuple[tuple[CourseId, int], ...]:
                    return self._course_id_rows(None)

                def coverage() -> tuple[CoverageView, ...]:
                    return tuple(
                        view
                        for item, key in current_courses()
                        for view in (
                            (self._parse_coverage(item, key),)
                            if item in initial_course_ids
                            else (
                                CoverageView(
                                    item.provider,
                                    "content",
                                    Coverage.UNKNOWN,
                                    item,
                                    evidence="discovered_during_query_freshness_unchecked",
                                ),
                                self._parse_coverage(item, key),
                            )
                        )
                    )

            else:
                selected = (self._course(course),)
                scopes = (ScopeKey(selected[0][0].provider, selected[0][0], "content"),)

                def current_courses() -> tuple[tuple[CourseId, int], ...]:
                    return selected

                def coverage() -> tuple[CoverageView, ...]:
                    return tuple(self._parse_coverage(item, key) for item, key in current_courses())

            return self._query(
                operation,
                lambda: readers.load_library_status(self.database, current_courses()),
                scopes,
                freshness,
                bounded_limit=DEFAULT_LIMIT,
                extra_coverage=coverage,
                extra_warnings=lambda: (
                    SafeWarning(
                        "content_page_body_coverage_unavailable",
                        "Library progress covers content metadata and file-backed text; "
                        "non-file content page bodies are not established as indexed.",
                        "library_status",
                    ),
                ),
            )
        except Exception as error:
            return core_errors.failure(operation, error, "library_status")

    def get_recent_material_changes(
        self,
        course: CourseRef,
        window: TimeWindow,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> ResultEnvelope[MaterialChangeView]:
        """Return changes proven by resource observations inside a bounded window."""

        operation = "get_recent_material_changes"
        try:
            if not isinstance(window, TimeWindow):
                raise TypeError("material change window must be typed")
            bounded_limit(limit)
            bounded_cursor(cursor)
            course_id, course_key = self._course(course)
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                cursor,
                {
                    "course": [course_id.provider, course_id.value],
                    "window": [to_storage_time(window.since), to_storage_time(window.until)],
                },
                limit,
            )
            scope = ScopeKey(course_id.provider, course_id, "content")

            def observation_coverage() -> tuple[CoverageView, ...]:
                content = self._coverage(scope)
                return (
                    CoverageView(
                        course_id.provider,
                        "resource_observations",
                        content.coverage,
                        course_id,
                        window.since,
                        window.until,
                        content.observed_at,
                        "resource_observation_history",
                    ),
                )

            return self._query(
                operation,
                lambda: readers.load_recent_material_changes(
                    self.database, course_id, course_key, window, limit, pagination.offset
                ),
                (scope,),
                freshness,
                bounded_limit=limit,
                pagination=pagination,
                extra_coverage=observation_coverage,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "material_changes")

    def search(
        self,
        query: SearchQuery,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
        *,
        _operation: str = "search",
    ) -> ResultEnvelope[object]:
        operation = _operation
        try:
            if not isinstance(query, SearchQuery):
                raise TypeError("search query must be typed")
            effective_query = query
            if (
                freshness.mode.value == "REQUIRE_CURRENT"
                and query.filters.version_key is None
                and query.filters.include_historical_versions
            ):
                effective_query = replace(
                    query,
                    filters=replace(query.filters, include_historical_versions=False),
                )
            search_filters = effective_query.filters
            if effective_query.cursor is None:
                # Establish the deterministic local index before capturing the
                # page revision.  Subsequent continuation calls are read-only.
                with self.database.transaction() as connection:
                    self.search_service.index.ensure_current(connection)
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                effective_query.cursor,
                {
                    "text": effective_query.text,
                    "match_mode": effective_query.match_mode,
                    "ranking_profile": self.search_service.ranking_profile,
                    "course": None
                    if search_filters.course is None
                    else [search_filters.course.provider, search_filters.course.value],
                    "entity_kinds": sorted(item.value for item in search_filters.entity_kinds),
                    "semantic_types": sorted(item.value for item in search_filters.semantic_types),
                    "file_formats": sorted(search_filters.file_formats),
                    "availabilities": sorted(item.value for item in search_filters.availabilities),
                    "text_origins": sorted(item.value for item in search_filters.text_origins),
                    "version_key": search_filters.version_key,
                    "minimum_classification_confidence": (
                        search_filters.minimum_classification_confidence
                    ),
                    "include_historical_versions": search_filters.include_historical_versions,
                    "neighbor_count": effective_query.neighbor_count,
                },
                effective_query.limit,
            )
            courses = self._course_id_rows(effective_query.filters.course)
            scopes = self._search_scopes(
                courses,
                effective_query,
                include_inventory=effective_query.filters.course is None,
                fallback_provider=(
                    None if self.sync_engine is None else self.sync_engine.source.provider_name
                ),
            )
            loaded: list[SearchResult] = []
            context_events: list[CanonicalEvent] = []
            unresolved_matches: list[bool] = []

            def load_search() -> PageLoad[object]:
                result = self.search_service.search(
                    replace(effective_query, cursor=None), offset=pagination.offset
                )
                loaded.clear()
                loaded.append(result)
                events, unresolved = self._search_event_context(result)
                context_events.clear()
                context_events.extend(events)
                unresolved_matches.clear()
                unresolved_matches.append(unresolved)
                provenance = tuple(
                    ProvenanceView(
                        hit.source.kind.value,
                        hit.source.key,
                        hit.course.provider,
                        hit.course,
                        hit.version_key,
                        hit.locator,
                    )
                    for hit in result.items
                )
                return PageLoad(tuple(result.items), provenance, result.truncated)

            def search_coverage() -> tuple[CoverageView, ...]:
                result = loaded[0]
                values = tuple(
                    CoverageView(
                        item.course.provider,
                        item.data_kind,
                        item.coverage,
                        item.course,
                        observed_at=item.observed_at,
                        evidence=item.evidence,
                    )
                    for item in result.coverage
                )
                kinds = effective_query.filters.entity_kinds
                if kinds.intersection({SearchEntityKind.EVENT, SearchEntityKind.CLAIM}):
                    values += tuple(
                        CoverageView(
                            course.provider,
                            kind,
                            Coverage.UNKNOWN,
                            course,
                            evidence="unbounded_search_scope",
                        )
                        for course, _ in courses
                        for kind in ("schedule", "due_items")
                    )
                    values += tuple(
                        event_views.event_derivation_coverage(self.database, course, key)
                        for (course, key) in self._course_id_rows(effective_query.filters.course)
                    )
                values += self._new_course_coverage(
                    courses,
                    None
                    if effective_query.filters.course is None
                    else CourseRef(remote_id=effective_query.filters.course),
                )
                return values

            def search_warnings() -> tuple[SafeWarning, ...]:
                result = loaded[0]
                warnings = tuple(
                    SafeWarning(code, "Local search coverage is incomplete.", "search")
                    for code in result.warnings
                )
                if unresolved_matches and unresolved_matches[0]:
                    warnings += (
                        SafeWarning(
                            "unresolved_event_identity",
                            "A matched event claim has unresolved source identity.",
                            "search",
                        ),
                    )
                return warnings

            return self._query(
                operation,
                load_search,
                scopes,
                freshness,
                bounded_limit=query.limit,
                extra_coverage=search_coverage,
                extra_warnings=search_warnings,
                context_events=lambda: tuple(context_events),
                historical=effective_query.filters.version_key is not None,
                pagination=pagination,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "search")

    def search_course(
        self,
        course: CourseRef,
        query: SearchQuery,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[object]:
        operation = "search_course"
        try:
            course_id, _ = self._course(course)
            if query.filters.course is not None and query.filters.course != course_id:
                raise ValueError("search query course does not match the requested course")
            return self.search(
                replace(query, filters=replace(query.filters, course=course_id)),
                freshness,
                _operation=operation,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "search")

    def get_announcements(
        self,
        course: CourseRef,
        filter: AnnouncementFilter = AnnouncementFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[AnnouncementView]:
        operation = "get_announcements"
        try:
            course_id, course_key = self._course(course)
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                filter.cursor,
                {
                    "availability": sorted(item.value for item in filter.availability),
                    "course": [course_id.provider, course_id.value],
                },
                filter.limit,
            )
            return self._query(
                operation,
                lambda: readers.load_announcements(
                    self.database, course_id, course_key, filter, pagination.offset
                ),
                (ScopeKey(course_id.provider, course_id, "announcements"),),
                freshness,
                bounded_limit=filter.limit,
                pagination=pagination,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "announcements")

    def get_assessments(
        self,
        course: CourseRef,
        filter: AssessmentFilter = AssessmentFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[AssessmentView]:
        operation = "get_assessments"
        try:
            course_id, course_key = self._course(course)
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                filter.cursor,
                {
                    "subtypes": sorted(item.value for item in filter.subtypes),
                    "availability": sorted(item.value for item in filter.availability),
                    "course": [course_id.provider, course_id.value],
                },
                filter.limit,
            )
            return self._query(
                operation,
                lambda: readers.load_assessments(
                    self.database, course_id, course_key, filter, pagination.offset
                ),
                (ScopeKey(course_id.provider, course_id, "assessments"),),
                freshness,
                bounded_limit=filter.limit,
                pagination=pagination,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "assessments")

    def get_events(
        self,
        filter: EventFilter = EventFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
        *,
        _operation: str = "get_events",
    ) -> ResultEnvelope[CanonicalEvent]:
        operation = _operation
        try:
            pagination = paging.prepare_pagination(
                self.database,
                operation,
                filter.cursor,
                {
                    "course": None
                    if filter.course is None
                    else {
                        "local_key": filter.course.local_key,
                        "remote_id": None
                        if filter.course.remote_id is None
                        else [filter.course.remote_id.provider, filter.course.remote_id.value],
                    },
                    "event_types": sorted(item.value for item in filter.event_types),
                    "window": None
                    if filter.window is None
                    else [
                        to_storage_time(filter.window.since),
                        to_storage_time(filter.window.until),
                    ],
                    "include_cancelled": filter.include_cancelled,
                },
                filter.limit,
            )
            courses = self._event_courses(filter.course)
            scopes = event_views.event_scopes(
                None
                if filter.course is not None or courses or self.sync_engine is None
                else self.sync_engine.source.provider_name,
                courses,
                filter.window,
                include_inventory=filter.course is None,
            )
            loaded_events: list[tuple[CanonicalEvent, ...]] = []

            def load_events() -> PageLoad[CanonicalEvent]:
                page = self._load_events(
                    self._event_courses(filter.course), filter, pagination.offset
                )
                loaded_events.clear()
                loaded_events.append(page.items)
                return page

            return self._query(
                operation,
                load_events,
                scopes,
                freshness,
                bounded_limit=filter.limit,
                pagination=pagination,
                extra_coverage=lambda: (
                    tuple(
                        self._parse_coverage(course, key)
                        for (course, key) in self._event_courses(filter.course)
                    )
                    + tuple(
                        event_views.event_derivation_coverage(self.database, course, key)
                        for (course, key) in self._event_courses(filter.course)
                    )
                    + event_views.unbounded_event_coverage(
                        self._event_courses(filter.course), filter.window
                    )
                    + event_views.temporal_projection_coverage(
                        () if not loaded_events else loaded_events[0], filter.window
                    )
                    + self._new_course_coverage(courses, filter.course)
                ),
                extra_warnings=lambda: (
                    event_views.event_derivation_warnings(
                        self.database, self._event_courses(filter.course)
                    )
                    + event_views.temporal_projection_warnings(
                        () if not loaded_events else loaded_events[0], filter.window
                    )
                ),
            )
        except Exception as error:
            return core_errors.failure(operation, error, "events")

    def get_upcoming_events(
        self,
        window: TimeWindow,
        course: CourseRef | None = None,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
        *,
        limit: int = DEFAULT_LIMIT,
        cursor: str | None = None,
    ) -> ResultEnvelope[CanonicalEvent]:
        if not isinstance(window, TimeWindow):
            return core_errors.failure(
                "get_upcoming_events", TypeError("window must be typed"), "events"
            )
        return self.get_events(
            EventFilter(course=course, window=window, limit=limit, cursor=cursor),
            freshness,
            _operation="get_upcoming_events",
        )

    def get_resource(
        self,
        resource: ResourceRef,
        version: int | None = None,
        include_local_path: bool = False,
    ) -> ResultEnvelope[ResourceView]:
        operation = "get_resource"
        try:
            resource_id, record = self._resource(resource)
            selected_key = record.current_version_key if version is None else version
            if selected_key is not None and (isinstance(selected_key, bool) or selected_key <= 0):
                raise ValueError("resource version key must be positive")
            selected = self.resources.get_version(selected_key)
            if selected is not None and selected.resource_key != record.key:
                raise ValueError("resource version does not belong to the requested resource")
            if selected_key is not None and selected is None:
                return core_errors.not_found(operation, "resource_version")
            local_path = None
            if include_local_path and selected is not None:
                if self.runtime_paths is None:
                    raise ValueError("runtime paths are required to return a local path")
                local_path = self.runtime_paths.root / selected.browse_relpath
            item = ResourceView(
                record.key,
                resource_id,
                record.display_title,
                record.availability,
                None if selected is None else selected.key,
                None if selected is None else selected.version_number,
                None if selected is None else selected.sha256,
                None if selected is None else selected.byte_size,
                None if selected is None else selected.file_format,
                None if selected is None else selected.declared_mime,
                None if selected is None else selected.downloaded_at,
                None if selected is None else self._latest_parse_key(selected.key),
                local_path,
            )
            return ResultEnvelope(
                operation,
                (item,),
                provenance=(
                    ProvenanceView(
                        "source_object",
                        self._resource_source_key(record.key),
                        resource_id.provider,
                        version_key=selected_key,
                    ),
                ),
                completeness=Coverage.COMPLETE,
                as_of=record.last_observed_at,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "resource")

    def prepare_visual_evidence(self, parse_key: int, *, dpi: int = 150) -> ResultEnvelope[object]:
        """Render only parser-flagged PDF pages into the private derived cache."""

        operation = "prepare_visual_evidence"
        try:
            parse_coverage = self._visual_parse_coverage(parse_key)
            service = self._visual_evidence_service()
            items = service.prepare(parse_key, dpi=dpi)
            warnings = tuple(
                item
                for item in (
                    SafeWarning(
                        "visual_evidence_needs_review",
                        "Flagged rendered pages require bounded host inspection.",
                        f"parse:{parse_key}",
                    )
                    if items
                    else None,
                    SafeWarning(
                        "visual_source_parse_partial",
                        "The source parse remains partial after selective visual preparation.",
                        f"parse:{parse_key}",
                    )
                    if parse_coverage is not Coverage.COMPLETE
                    else None,
                )
                if item is not None
            )
            return ResultEnvelope(
                operation,
                cast(tuple[object, ...], items),
                warnings=warnings,
                completeness=(
                    Coverage.PARTIAL
                    if items or parse_coverage is not Coverage.COMPLETE
                    else Coverage.COMPLETE
                ),
            )
        except Exception as error:
            return core_errors.failure(operation, error, "visual_evidence")

    def import_visual_evidence(self, bundle_path: str | Path) -> ResultEnvelope[object]:
        """Import a private host result bundle without weakening its review state."""

        operation = "import_visual_evidence"
        try:
            items = self._visual_evidence_service().import_bundle(bundle_path)
            projection_warning: tuple[SafeWarning, ...] = ()
            try:
                for parse_key in sorted({item.parse_key for item in items}):
                    version_key, course = self._visual_projection_source(parse_key)
                    DeterministicEventExtractor(self.database).extract_resource_version(
                        version_key, parse_key=parse_key
                    )
                    self.events.reconcile_course(course)
                SearchIndex(self.database).rebuild()
            except Exception:
                projection_warning = (
                    SafeWarning(
                        "visual_projection_pending",
                        "Visual evidence was imported; rerun import to retry its projection.",
                        f"parse:{items[0].parse_key}",
                    ),
                )
            statuses = {item.review_status.value for item in items}
            warnings = (
                tuple(
                    SafeWarning(
                        "visual_evidence_partial"
                        if status == "PARTIAL"
                        else "visual_evidence_needs_review",
                        "Imported visual evidence remains incomplete or requires review.",
                        f"parse:{items[0].parse_key}",
                    )
                    for status in sorted(statuses)
                )
                + projection_warning
            )
            return ResultEnvelope(
                operation,
                cast(tuple[object, ...], items),
                warnings=warnings,
                completeness=Coverage.PARTIAL,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "visual_evidence")

    def resolve_source(
        self,
        locator: SourceLocatorRef,
        context_window: int = 1,
        *,
        include_visual_history: bool = False,
    ) -> ResultEnvelope[object]:
        operation = "resolve_source"
        try:
            if not isinstance(locator, SourceLocatorRef):
                raise TypeError("source locator must be typed")
            if not 0 <= context_window <= 5:
                raise ValueError("context window must be between 0 and 5")
            if not isinstance(include_visual_history, bool):
                raise TypeError("visual history selection must be boolean")
            resolved = self.search_service.resolve_source(
                locator.reference,
                context_window=context_window,
                include_visual_history=include_visual_history,
            )
            provenance = ProvenanceView(
                locator.reference.kind.value,
                locator.reference.key,
                resolved.provider,
                version_key=resolved.version_key,
                locator=resolved.locator,
            )
            current_visual_statuses = {
                item.review_status for item in resolved.visual_evidence if item.is_current
            }
            warnings = tuple(
                SafeWarning(
                    "visual_evidence_partial"
                    if status.value == "PARTIAL"
                    else "visual_evidence_needs_review",
                    "Visual source evidence remains incomplete or requires review.",
                    f"source_locator:{locator.reference.key}",
                )
                for status in sorted(current_visual_statuses, key=lambda item: item.value)
            )
            return ResultEnvelope(
                operation,
                (resolved,),
                provenance=(provenance,),
                warnings=warnings,
                completeness=Coverage.PARTIAL if current_visual_statuses else Coverage.COMPLETE,
                local_reads=1,
            )
        except Exception as error:
            return core_errors.failure(operation, error, "source")

    def resolve_event_candidate(self, decision: ManualResolution) -> ResultEnvelope[CanonicalEvent]:
        operation = "resolve_event_candidate"
        try:
            if isinstance(decision, ManualFieldResolution):
                event = self.events.resolve_field(decision)
            elif isinstance(decision, ManualIdentityResolution):
                event = self.events.resolve_event_source(decision)
            else:
                raise TypeError("manual resolution decision must be typed")
            return event_views.event_envelope(
                operation, self._with_event_reviews((event,)), Coverage.COMPLETE
            )
        except Exception as error:
            return core_errors.failure(operation, error, "manual_resolution")

    def quick_sync(self, course: CourseRef | None, policy: SyncPolicy) -> ResultEnvelope[object]:
        operation = "quick_sync"
        try:
            engine = self._engine(operation)
            if course is None:
                result = engine.sync_all(
                    CourseSelectionPolicy(),
                    window=policy.window,
                    fetch_resources=policy.fetch_resources,
                    verify_resources=policy.verify_resources,
                    page_size=policy.page_size,
                    max_pages_per_scope=policy.max_pages_per_scope,
                    max_content_nodes=policy.max_content_nodes,
                    max_jobs_per_course=policy.max_jobs,
                )
            else:
                course_id = self._course_for_sync(course)
                result = engine.quick_sync(
                    course_id,
                    window=policy.window,
                    fetch_resources=policy.fetch_resources,
                    verify_resources=policy.verify_resources,
                    page_size=policy.page_size,
                    max_pages_per_scope=policy.max_pages_per_scope,
                    max_content_nodes=policy.max_content_nodes,
                    max_jobs=policy.max_jobs,
                )
            return self._sync_envelope(operation, result, window=policy.window)
        except Exception as error:
            return core_errors.failure(operation, error, "sync")

    def sync_course(self, course: CourseRef, policy: SyncPolicy) -> ResultEnvelope[object]:
        operation = "sync_course"
        try:
            engine = self._engine(operation)
            course_id = self._course_for_sync(course)
            result = engine.sync_course(
                course_id,
                window=policy.window,
                fetch_resources=policy.fetch_resources,
                verify_resources=policy.verify_resources,
                page_size=policy.page_size,
                max_pages_per_scope=policy.max_pages_per_scope,
                max_content_nodes=policy.max_content_nodes,
                max_jobs=policy.max_jobs,
            )
            return self._sync_envelope(operation, result, window=policy.window)
        except Exception as error:
            return core_errors.failure(operation, error, "sync")

    def sync_all(
        self,
        policy: SyncPolicy,
        selection: CourseSelectionPolicy = CourseSelectionPolicy(),
    ) -> ResultEnvelope[object]:
        operation = "sync_all"
        try:
            engine = self._engine(operation)
            result = engine.sync_all(
                selection,
                window=policy.window,
                fetch_resources=policy.fetch_resources,
                verify_resources=policy.verify_resources,
                page_size=policy.page_size,
                max_pages_per_scope=policy.max_pages_per_scope,
                max_content_nodes=policy.max_content_nodes,
                max_jobs_per_course=policy.max_jobs,
            )
            return self._sync_envelope(operation, result, window=policy.window)
        except Exception as error:
            return core_errors.failure(operation, error, "sync")

    def fetch_resource(self, resource: ResourceRef, verify: bool = False) -> ResultEnvelope[object]:
        operation = "fetch_resource"
        try:
            engine = self._engine(operation)
            resource_id = self._resource_for_sync(resource)
            result = engine.fetch_resource(resource_id, verify=verify)
            if result.observation is None:
                error = core_errors.category_error(
                    operation, result.error_category or "source_unavailable", "resource"
                )
                return ResultEnvelope(
                    operation, (result,), errors=(error,), completeness=Coverage.FAILED
                )
            run = SyncRunRecorder(self.database, resource_id.provider).get(
                result.observation.sync_run_key
            )
            envelope = self._sync_envelope(operation, run, window=self._unused_window())
            warnings = envelope.warnings + tuple(
                SafeWarning(code, "Resource processing completed with a warning.", "resource")
                for code in result.warning_codes
            )
            return replace(envelope, items=(result,), warnings=warnings)
        except Exception as error:
            return core_errors.failure(operation, error, "resource")

    def _query(
        self,
        operation: str,
        load: Callable[[], tuple[tuple[R, ...], tuple[ProvenanceView, ...]] | PageLoad[R]],
        scopes: tuple[ScopeKey, ...],
        requirement: FreshnessRequirement,
        *,
        bounded_limit: int,
        extra_coverage: Callable[[], tuple[CoverageView, ...]] | None = None,
        extra_warnings: Callable[[], tuple[SafeWarning, ...]] | None = None,
        context_events: Callable[[], tuple[CanonicalEvent, ...]] | None = None,
        historical: bool = False,
        pagination: Pagination | None = None,
    ) -> ResultEnvelope[R]:
        if not isinstance(requirement, FreshnessRequirement):
            raise TypeError("freshness requirement must be typed")
        loaded = load()
        if isinstance(loaded, PageLoad):
            items, provenance = loaded.items, loaded.provenance
            truncated = loaded.truncated
        else:
            items, provenance = loaded
            truncated = False
        evaluation_time = self._now()
        decisions = tuple(
            evaluate_freshness(self.states.get(scope), requirement, now=evaluation_time)
            for scope in scopes
        )
        refresh_targets = tuple(
            scope
            for scope, decision in zip(scopes, decisions, strict=True)
            if decision.should_refresh
        )
        refresh_attempted = False
        refresh_errors: list[SafeError] = []
        refresh_warnings: list[SafeWarning] = []
        if pagination is not None and pagination.offset and refresh_targets:
            raise ValueError(
                "a continuation page cannot refresh source data; restart from the first page"
            )
        if (
            not scopes
            and self.sync_engine is None
            and requirement.mode.value not in {"CACHE_ONLY", "ALLOW_STALE"}
        ):
            refresh_errors.append(
                SafeError(
                    ErrorCategory.CONFIGURATION_REQUIRED,
                    "sync_engine_unavailable",
                    "A configured read-only sync engine is required for refresh.",
                    operation,
                    "freshness",
                    True,
                    Coverage.UNKNOWN,
                )
            )
        if refresh_targets and not historical:
            if self.sync_engine is None:
                refresh_errors.append(
                    SafeError(
                        ErrorCategory.CONFIGURATION_REQUIRED,
                        "sync_engine_unavailable",
                        "A configured read-only sync engine is required for refresh.",
                        operation,
                        "freshness",
                        True,
                        Coverage.UNKNOWN,
                    )
                )
            else:
                refresh_attempted = True
                for scope in refresh_targets:
                    try:
                        result = self.sync_engine.refresh_scope(scope)
                        if isinstance(result, SyncRunResult):
                            refresh_errors.extend(
                                core_errors.refresh_result_errors(operation, result)
                            )
                    except Exception as error:
                        refresh_errors.append(
                            core_errors.safe_error(operation, error, self._scope_name(scope))
                        )
                if pagination is not None:
                    pagination = replace(
                        pagination, revision=paging.database_revision(self.database)
                    )
                loaded = load()
                if isinstance(loaded, PageLoad):
                    items, provenance = loaded.items, loaded.provenance
                    truncated = loaded.truncated
                else:
                    items, provenance = loaded
                    truncated = False
                decisions = tuple(
                    evaluate_freshness(self.states.get(scope), requirement, now=evaluation_time)
                    for scope in scopes
                )
        elif refresh_targets and historical:
            refresh_warnings.append(
                SafeWarning(
                    "historical_local_only",
                    "Historical retrieval uses verified local evidence and does not "
                    "access the source.",
                    "historical",
                )
            )

        coverage = tuple(self._coverage(scope) for scope in scopes)
        if extra_coverage is not None:
            coverage += extra_coverage()
        source_completeness = results.aggregate_coverage(coverage)
        completeness = source_completeness
        if any(decision.status is FreshnessStatus.UNKNOWN for decision in decisions):
            completeness = results.worse_coverage(completeness, Coverage.UNKNOWN)
        elif any(decision.status is FreshnessStatus.STALE for decision in decisions):
            completeness = results.worse_coverage(completeness, Coverage.STALE)
        freshness = tuple(
            results.freshness_view(self._scope_name(scope), decision)
            for (scope, decision) in zip(scopes, decisions, strict=True)
        )
        warnings = list(refresh_warnings)
        if extra_warnings is not None:
            warnings.extend(extra_warnings())
        for view in freshness:
            warnings.extend(
                SafeWarning(code, core_errors.warning_message(code), view.scope)
                for code in view.warning_codes
            )
        warnings.extend(
            SafeWarning(
                f"coverage_{view.coverage.value.lower()}",
                "Available local evidence does not establish complete coverage.",
                view.data_kind,
            )
            for view in coverage
            if view.coverage is not Coverage.COMPLETE
        )
        next_cursor = None
        if truncated:
            warnings.append(
                SafeWarning(
                    "result_limit_reached",
                    "The result limit was reached; additional local matches may exist.",
                    operation,
                )
            )
            if pagination is None:
                raise ValueError("truncated query is missing pagination metadata")
            next_cursor = paging.encode_cursor(
                pagination.operation,
                pagination.binding,
                pagination.offset + bounded_limit,
                bounded_limit,
                pagination.revision,
            )
            # ``completeness`` retains its pre-pagination compatibility meaning.
            # New callers should inspect ``source_completeness`` and ``truncated``
            # independently.
            if completeness is Coverage.COMPLETE:
                completeness = Coverage.PARTIAL
        if (
            any(not decision.satisfied for decision in decisions)
            or source_completeness is not Coverage.COMPLETE
        ) and requirement.mode.value not in {
            "CACHE_ONLY",
            "ALLOW_STALE",
        }:
            refresh_errors.append(
                SafeError(
                    ErrorCategory.FRESHNESS_UNSATISFIED,
                    "freshness_unsatisfied",
                    "The requested freshness could not be established for every exact scope.",
                    operation,
                    "freshness",
                    True,
                    completeness,
                )
            )
        as_of_values = [view.observed_at for view in coverage if view.observed_at is not None]
        event_context = () if context_events is None else context_events()
        conflicts = event_views.event_conflicts(items + event_context)
        if conflicts:
            warnings.append(
                SafeWarning(
                    "unresolved_event_conflicts",
                    "One or more event fields require resolution.",
                    "events",
                )
            )
        elif any(
            isinstance(item, CanonicalEvent)
            and item.resolution_state is not EventResolutionState.RESOLVED
            for item in items + event_context
        ):
            warnings.append(
                SafeWarning(
                    "unresolved_event_identity",
                    "One or more events retain unresolved source identity.",
                    "events",
                )
            )
        if (
            pagination is not None
            and paging.database_revision(self.database) != pagination.revision
        ):
            raise ValueError("local data changed while reading a page; restart from the first page")
        return ResultEnvelope(
            operation,
            items,
            provenance,
            coverage,
            freshness,
            conflicts=conflicts,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=tuple(refresh_errors),
            completeness=completeness,
            as_of=min(as_of_values) if as_of_values else None,
            refresh_attempted=refresh_attempted,
            local_reads=2 if refresh_attempted else 1,
            next_cursor=next_cursor,
            truncated=truncated,
            source_completeness=source_completeness,
            freshness_satisfied=(
                None if not decisions else all(decision.satisfied for decision in decisions)
            ),
        )

    def _load_courses(self, filter: CourseFilter, offset: int = 0) -> PageLoad[CourseSummary]:
        return readers.load_courses(self.database, filter, offset)

    def _search_event_context(
        self, result: SearchResult
    ) -> tuple[tuple[CanonicalEvent, ...], bool]:
        event_keys = {
            hit.entity_key for hit in result.items if hit.entity_kind is SearchEntityKind.EVENT
        }
        claim_keys = tuple(
            hit.entity_key for hit in result.items if hit.entity_kind is SearchEntityKind.CLAIM
        )
        unresolved = False
        if claim_keys:
            connection = self.database.connect()
            try:
                rows = connection.execute(
                    f"""SELECT event_key FROM claim
                    WHERE claim_key IN ({",".join("?" for _ in claim_keys)})""",
                    claim_keys,
                ).fetchall()
            finally:
                connection.close()
            unresolved = any(row["event_key"] is None for row in rows)
            event_keys.update(int(row["event_key"]) for row in rows if row["event_key"] is not None)
        events = tuple(
            event for key in sorted(event_keys) if (event := self.events.get_event(key)) is not None
        )
        return events, unresolved

    def _load_events(
        self,
        courses: tuple[tuple[CourseId, int], ...],
        filter: EventFilter,
        offset: int = 0,
    ) -> PageLoad[CanonicalEvent]:
        events = [event for course, _ in courses for event in self.events.list_events(course)]
        if filter.event_types:
            events = [event for event in events if event.event_type in filter.event_types]
        if not filter.include_cancelled:
            events = [
                event
                for event in events
                if event.status is None or event.status.value != "CANCELLED"
            ]
        if filter.window is not None:
            events = [event for event in events if self._overlaps(event, filter.window)]
        events.sort(key=event_views.event_sort_key)
        selected = events[offset : offset + filter.limit + 1]
        truncated = len(selected) > filter.limit
        events = list(self._with_event_reviews(tuple(selected[: filter.limit])))
        provenance = tuple(
            ProvenanceView(
                source.evidence_kind, source.evidence_key, event.course.provider, event.course
            )
            for event in events
            for source in event.sources
        )
        return PageLoad(tuple(events), provenance, truncated)

    def _with_event_reviews(self, events: tuple[CanonicalEvent, ...]) -> tuple[CanonicalEvent, ...]:
        return event_views.with_event_reviews(self.database, events)

    def _new_course_coverage(
        self,
        original: tuple[tuple[CourseId, int], ...],
        reference: CourseRef | None,
    ) -> tuple[CoverageView, ...]:
        if reference is not None:
            return ()
        original_ids = {course for course, _ in original}
        return tuple(
            CoverageView(
                course.provider,
                "new_course_scopes",
                Coverage.UNKNOWN,
                course,
                evidence="discovered_during_query_refresh",
            )
            for course, _ in self._course_id_rows(None)
            if course not in original_ids
        )

    @staticmethod
    def _search_scopes(
        courses: tuple[tuple[CourseId, int], ...],
        query: SearchQuery,
        *,
        include_inventory: bool = False,
        fallback_provider: str | None = None,
    ) -> tuple[ScopeKey, ...]:
        kinds = query.filters.entity_kinds
        scopes: list[ScopeKey] = []
        providers: set[str] = set()
        for course, _ in courses:
            providers.add(course.provider)
            if kinds.intersection(
                {
                    SearchEntityKind.CONTENT,
                    SearchEntityKind.MATERIAL,
                    SearchEntityKind.CHUNK,
                    SearchEntityKind.EVENT,
                    SearchEntityKind.CLAIM,
                }
            ):
                scopes.append(ScopeKey(course.provider, course, "content"))
            if kinds.intersection(
                {SearchEntityKind.ANNOUNCEMENT, SearchEntityKind.EVENT, SearchEntityKind.CLAIM}
            ):
                scopes.append(ScopeKey(course.provider, course, "announcements"))
            if kinds.intersection(
                {SearchEntityKind.ASSESSMENT, SearchEntityKind.EVENT, SearchEntityKind.CLAIM}
            ):
                scopes.append(ScopeKey(course.provider, course, "assessments"))
        if include_inventory or SearchEntityKind.COURSE in kinds:
            if not providers and fallback_provider is not None:
                providers.add(fallback_provider)
            scopes.extend(ScopeKey(provider, None, "courses") for provider in sorted(providers))
        return tuple(dict.fromkeys(scopes))

    def _event_courses(self, reference: CourseRef | None) -> tuple[tuple[CourseId, int], ...]:
        if reference is not None:
            course, key = self._course(reference)
            return ((course, key),)
        return self._course_id_rows(None)

    def _course_id_rows(self, course: CourseId | None) -> tuple[tuple[CourseId, int], ...]:
        connection = self.database.connect()
        try:
            parameters: tuple[object, ...] = ()
            where = ""
            if course is not None:
                where = "WHERE provider.name = ? AND object.remote_key = ?"
                parameters = (course.provider, course.value)
            rows = connection.execute(
                f"""SELECT course.course_key, provider.name, object.remote_key FROM course
                JOIN source_object object USING(source_object_key)
                JOIN source_provider provider USING(provider_key) {where}
                ORDER BY course.course_key""",
                parameters,
            ).fetchall()
            return tuple(
                (CourseId(str(row["name"]), str(row["remote_key"])), int(row["course_key"]))
                for row in rows
            )
        finally:
            connection.close()

    def _course(self, reference: CourseRef) -> tuple[CourseId, int]:
        if not isinstance(reference, CourseRef):
            raise TypeError("course reference must be typed")
        connection = self.database.connect()
        try:
            if reference.local_key is not None:
                row = connection.execute(
                    """SELECT course.course_key, provider.name, object.remote_key FROM course
                    JOIN source_object object USING(source_object_key)
                    JOIN source_provider provider USING(provider_key)
                    WHERE course.course_key = ?""",
                    (reference.local_key,),
                ).fetchone()
            else:
                assert reference.remote_id is not None
                row = connection.execute(
                    """SELECT course.course_key, provider.name, object.remote_key FROM course
                    JOIN source_object object USING(source_object_key)
                    JOIN source_provider provider USING(provider_key)
                    WHERE provider.name = ? AND object.remote_key = ?""",
                    (reference.remote_id.provider, reference.remote_id.value),
                ).fetchone()
            if row is None:
                raise LookupError("local course was not found")
            return CourseId(str(row["name"]), str(row["remote_key"])), int(row["course_key"])
        finally:
            connection.close()

    def _resource(self, reference: ResourceRef) -> tuple[AttachmentId, ResourceRecord]:
        if not isinstance(reference, ResourceRef):
            raise TypeError("resource reference must be typed")
        if reference.remote_id is not None:
            record = self.resources.get_resource(reference.remote_id)
        else:
            connection = self.database.connect()
            try:
                row = connection.execute(
                    """SELECT provider.name, object.remote_key FROM resource
                    JOIN source_object object USING(source_object_key)
                    JOIN source_provider provider USING(provider_key)
                    WHERE resource.resource_key = ?""",
                    (reference.local_key,),
                ).fetchone()
            finally:
                connection.close()
            record = (
                None
                if row is None
                else self.resources.get_resource(
                    AttachmentId(str(row["name"]), str(row["remote_key"]))
                )
            )
        if record is None:
            raise LookupError("local resource was not found")
        return record.remote_id, record

    def _resource_source_key(self, resource_key: int) -> int:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT source_object_key FROM resource WHERE resource_key = ?", (resource_key,)
            ).fetchone()
            if row is None:
                raise LookupError("local resource was not found")
            return int(row["source_object_key"])
        finally:
            connection.close()

    def _latest_parse_key(self, version_key: int) -> int | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """SELECT parse_key FROM parsed_document
                WHERE version_key = ? AND status <> 'FAILED'
                ORDER BY parse_key DESC LIMIT 1""",
                (version_key,),
            ).fetchone()
            return None if row is None else int(row["parse_key"])
        finally:
            connection.close()

    def _visual_evidence_service(self) -> Any:
        if self.runtime_paths is None:
            raise ValueError("runtime paths are required for visual evidence")
        from ntulearn_skill.parsers import ParserRegistry
        from ntulearn_skill.parsers.repository import ParseRepository
        from ntulearn_skill.parsers.service import ParseService
        from ntulearn_skill.parsers.visual import VisualEvidenceService

        repository = ParseRepository(self.database)
        parser = ParseService(
            self.runtime_paths,
            self.resources,
            repository,
            ParserRegistry(),
        )
        return VisualEvidenceService(self.runtime_paths, parser, repository)

    def _visual_parse_coverage(self, parse_key: int) -> Coverage:
        if isinstance(parse_key, bool) or not isinstance(parse_key, int) or parse_key <= 0:
            raise ValueError("parse key must be positive")
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT coverage FROM parsed_document WHERE parse_key = ?", (parse_key,)
            ).fetchone()
            if row is None:
                raise ValueError("parsed document was not found")
            return Coverage(str(row["coverage"]))
        finally:
            connection.close()

    def _visual_projection_source(self, parse_key: int) -> tuple[int, CourseId]:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT version.version_key, provider.name AS provider,
                       course_object.remote_key AS course_remote
                FROM parsed_document parsed
                JOIN resource_version version ON version.version_key = parsed.version_key
                JOIN resource ON resource.resource_key = version.resource_key
                JOIN content_node content ON content.content_key = resource.content_key
                JOIN course ON course.course_key = content.course_key
                JOIN source_object course_object
                  ON course_object.source_object_key = course.source_object_key
                JOIN source_provider provider
                  ON provider.provider_key = course_object.provider_key
                WHERE parsed.parse_key = ?
                """,
                (parse_key,),
            ).fetchone()
            if row is None:
                raise ValueError("visual evidence source course is unavailable")
            return int(row["version_key"]), CourseId(
                str(row["provider"]), str(row["course_remote"])
            )
        finally:
            connection.close()

    def _course_for_sync(self, reference: CourseRef) -> CourseId:
        if not isinstance(reference, CourseRef):
            raise TypeError("course reference must be typed")
        if reference.remote_id is not None:
            return reference.remote_id
        return self._course(reference)[0]

    def _resource_for_sync(self, reference: ResourceRef) -> AttachmentId:
        if not isinstance(reference, ResourceRef):
            raise TypeError("resource reference must be typed")
        if reference.remote_id is not None:
            return cast(AttachmentId, require_identifier(reference.remote_id, AttachmentId))
        return self._resource(reference)[0]

    def _providers(self) -> tuple[str, ...]:
        connection = self.database.connect()
        try:
            return tuple(
                str(row[0])
                for row in connection.execute("SELECT name FROM source_provider ORDER BY name")
            )
        finally:
            connection.close()

    def _coverage(self, scope: ScopeKey) -> CoverageView:
        state = self.states.get(scope)
        return CoverageView(
            scope.provider,
            scope.data_kind,
            Coverage.UNKNOWN if state is None else state.latest_coverage,
            scope.course_id,
            None if scope.time_window is None else scope.time_window.since,
            None if scope.time_window is None else scope.time_window.until,
            None if state is None else state.last_attempt_at,
        )

    def _parse_coverage(self, course: CourseId, course_key: int) -> CoverageView:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """SELECT COUNT(*) AS total,
                    SUM(CASE WHEN resource.current_version_key IS NOT NULL
                        THEN 1 ELSE 0 END) AS versions,
                    SUM(CASE WHEN (
                        SELECT parsed.coverage FROM parsed_document parsed
                        WHERE parsed.version_key = resource.current_version_key
                        ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
                    ) = 'COMPLETE' THEN 1 ELSE 0 END) AS complete,
                    MAX((
                        SELECT parsed.parsed_at FROM parsed_document parsed
                        WHERE parsed.version_key = resource.current_version_key
                        ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
                    )) AS observed_at
                FROM resource JOIN content_node content USING(content_key)
                WHERE content.course_key = ?""",
                (course_key,),
            ).fetchone()
        finally:
            connection.close()
        assert row is not None
        total, versions, complete = (
            int(row["total"]),
            int(row["versions"] or 0),
            int(row["complete"] or 0),
        )
        coverage = Coverage.COMPLETE if total == versions == complete else Coverage.PARTIAL
        observed = (
            None if row["observed_at"] is None else from_storage_time(str(row["observed_at"]))
        )
        return CoverageView(
            course.provider,
            "parsed_documents",
            coverage,
            course,
            observed_at=observed,
            evidence="local_parse_state",
        )

    def _now(self) -> datetime:
        value = self.now()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("core clock must return a timezone-aware timestamp")
        return value.astimezone(UTC)

    @staticmethod
    def _unused_window() -> TimeWindow:
        start = datetime(2000, 1, 1, tzinfo=UTC)
        return TimeWindow(start, start + timedelta(seconds=1))

    @staticmethod
    def _scope_name(scope: ScopeKey) -> str:
        course = "global" if scope.course_id is None else f"course:{scope.course_id.value}"
        return f"{scope.provider}/{course}/{scope.data_kind}/{scope.window_key}"

    @staticmethod
    def _temporal_bounds(value: object) -> tuple[datetime, datetime] | None:
        return event_views.temporal_bounds(value)

    @staticmethod
    def _overlaps(event: CanonicalEvent, window: TimeWindow) -> bool:
        return event_views.overlaps(event, window)

    def _sync_envelope(
        self, operation: str, result: SyncRunResult, *, window: TimeWindow
    ) -> ResultEnvelope[object]:
        scopes = getattr(result, "scopes", ())
        coverage = tuple(
            CoverageView(
                item.provider,
                item.data_kind,
                item.coverage,
                item.course_id,
                window.since if item.data_kind in {"schedule", "due_items"} else None,
                window.until if item.data_kind in {"schedule", "due_items"} else None,
                item.observed_at,
                "sync_run",
            )
            for item in scopes
        )
        completeness = results.aggregate_coverage(coverage)
        errors: tuple[SafeError, ...] = ()
        if getattr(result, "error_category", None):
            errors = (core_errors.category_error(operation, str(result.error_category), "sync"),)
        warnings = tuple(
            SafeWarning(
                warning.value,
                "Synchronization completed with incomplete scope coverage.",
                item.data_kind,
            )
            for item in scopes
            for warning in item.warnings
        )
        return ResultEnvelope(
            operation,
            (result,),
            coverage=coverage,
            warnings=warnings,
            errors=errors,
            completeness=completeness,
            as_of=getattr(result, "ended_at", None),
            local_reads=0,
        )

    def _engine(self, operation: str) -> SyncEngine:
        if self.sync_engine is None:
            raise RuntimeError(f"{operation}:sync_engine_unavailable")
        return self.sync_engine


__all__ = [
    "AnnouncementFilter",
    "AnnouncementView",
    "AssessmentFilter",
    "AssessmentView",
    "CoreService",
    "CourseFilter",
    "CourseRef",
    "CourseSummary",
    "EventFilter",
    "LibraryStatus",
    "MaterialChangeView",
    "MaterialFilter",
    "MaterialSummary",
    "ResourceRef",
    "ResourceView",
    "SourceLocatorRef",
    "SyncPolicy",
    "FreshnessRequirement",
    "SearchEntityKind",
    "SearchFilters",
    "SearchQuery",
    "SourceReference",
    "SourceReferenceKind",
    "ManualFieldResolution",
    "ManualIdentityResolution",
    "ManualResolution",
    "CandidateFieldName",
    "AssessmentSubtype",
    "Availability",
    "Coverage",
    "CourseId",
    "AttachmentId",
    "TemporalPrecision",
    "TimeWindow",
]
