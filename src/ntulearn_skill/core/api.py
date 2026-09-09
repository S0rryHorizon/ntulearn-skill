"""Stable local-first core service over the M1-M8 persistence and sync layers."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, TypeAlias, TypeVar, cast

from ntulearn_skill.client import (
    AuthenticationRequired,
    SessionExpired,
    SourceError,
    TimeWindow,
    UnsupportedCapability,
)
from ntulearn_skill.core.identifiers import AttachmentId, CourseId, require_identifier
from ntulearn_skill.core.models import (
    AssessmentSubtype,
    Availability,
    Coverage,
    TemporalPrecision,
    from_storage_time,
    utc_now,
)
from ntulearn_skill.core.results import (
    ConflictView,
    CoverageView,
    ErrorCategory,
    FreshnessView,
    ProvenanceView,
    ResultEnvelope,
    SafeError,
    SafeWarning,
)
from ntulearn_skill.events import (
    CandidateFieldName,
    CanonicalEvent,
    EventReconciler,
    EventResolutionState,
    EventType,
    ManualFieldResolution,
    ManualIdentityResolution,
)
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
    MigrationError,
    ResourceRecord,
    ResourceRepository,
    RuntimePaths,
    StorageError,
)
from ntulearn_skill.sync.engine import CourseSelectionPolicy, SyncEngine
from ntulearn_skill.sync.event_sources import EventSourceScope
from ntulearn_skill.sync.freshness import (
    FreshnessDecision,
    FreshnessRequirement,
    FreshnessStatus,
    evaluate_freshness,
)
from ntulearn_skill.sync.models import SyncRunResult
from ntulearn_skill.sync.observability import SyncRunRecorder
from ntulearn_skill.sync.state import ScopeKey, SyncStateRepository

_DEFAULT_LIMIT = 100
_COVERAGE_RANK = {
    Coverage.COMPLETE: 0,
    Coverage.STALE: 1,
    Coverage.PARTIAL: 2,
    Coverage.UNKNOWN: 3,
    Coverage.FAILED: 4,
}


def _classification_sql(column: str) -> str:
    return f"""COALESCE(
        (SELECT classification.{column}
         FROM material_classification_selection selected
         JOIN material_classification classification USING(classification_key)
         JOIN classification_run run USING(classification_run_key)
         WHERE selected.resource_key = resource.resource_key
           AND (run.version_key IS NULL OR run.version_key IS version.version_key)
         ORDER BY selected.selection_key DESC LIMIT 1),
        (SELECT classification.{column}
         FROM classification_run run
         JOIN material_classification classification USING(classification_run_key)
         WHERE run.resource_key = resource.resource_key
           AND (run.version_key IS NULL OR run.version_key IS version.version_key)
           AND run.status <> 'FAILED'
         ORDER BY run.created_at DESC, run.classification_run_key DESC,
                  classification.rank LIMIT 1)
    )"""


def _bounded_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")


@dataclass(frozen=True, slots=True)
class CourseRef:
    local_key: int | None = None
    remote_id: CourseId | None = None

    def __post_init__(self) -> None:
        if (self.local_key is None) == (self.remote_id is None):
            raise ValueError("course reference requires exactly one local or remote identity")
        if self.local_key is not None and (isinstance(self.local_key, bool) or self.local_key <= 0):
            raise ValueError("course local key must be positive")
        if self.remote_id is not None:
            require_identifier(self.remote_id, CourseId)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    local_key: int | None = None
    remote_id: AttachmentId | None = None

    def __post_init__(self) -> None:
        if (self.local_key is None) == (self.remote_id is None):
            raise ValueError("resource reference requires exactly one local or remote identity")
        if self.local_key is not None and (isinstance(self.local_key, bool) or self.local_key <= 0):
            raise ValueError("resource local key must be positive")
        if self.remote_id is not None:
            require_identifier(self.remote_id, AttachmentId)


@dataclass(frozen=True, slots=True)
class SourceLocatorRef:
    reference: SourceReference

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SourceReference):
            raise TypeError("source locator reference must be typed")


@dataclass(frozen=True, slots=True)
class CourseFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = _DEFAULT_LIMIT

    def __post_init__(self) -> None:
        _bounded_limit(self.limit)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("course availability filters must be typed")


@dataclass(frozen=True, slots=True)
class MaterialFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    file_formats: frozenset[str] = field(default_factory=frozenset)
    include_historical_versions: bool = False
    limit: int = _DEFAULT_LIMIT

    def __post_init__(self) -> None:
        _bounded_limit(self.limit)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("material availability filters must be typed")
        if any(item not in {"pdf", "docx", "pptx", "unknown"} for item in self.file_formats):
            raise ValueError("material file format is invalid")


@dataclass(frozen=True, slots=True)
class AnnouncementFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = _DEFAULT_LIMIT

    def __post_init__(self) -> None:
        _bounded_limit(self.limit)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("announcement availability filters must be typed")


@dataclass(frozen=True, slots=True)
class AssessmentFilter:
    subtypes: frozenset[AssessmentSubtype] = field(default_factory=frozenset)
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = _DEFAULT_LIMIT

    def __post_init__(self) -> None:
        _bounded_limit(self.limit)
        if any(not isinstance(item, AssessmentSubtype) for item in self.subtypes):
            raise TypeError("assessment subtype filters must be typed")
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("assessment availability filters must be typed")


@dataclass(frozen=True, slots=True)
class EventFilter:
    course: CourseRef | None = None
    event_types: frozenset[EventType] = field(default_factory=frozenset)
    window: TimeWindow | None = None
    include_cancelled: bool = True
    limit: int = _DEFAULT_LIMIT

    def __post_init__(self) -> None:
        _bounded_limit(self.limit)
        if self.course is not None and not isinstance(self.course, CourseRef):
            raise TypeError("event course filter must be typed")
        if any(not isinstance(item, EventType) for item in self.event_types):
            raise TypeError("event type filters must be typed")
        if self.window is not None and not isinstance(self.window, TimeWindow):
            raise TypeError("event window must be typed")


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    window: TimeWindow
    fetch_resources: bool = True
    verify_resources: bool = False
    page_size: int = 100
    max_pages_per_scope: int = 100
    max_content_nodes: int = 10_000
    max_jobs: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.window, TimeWindow):
            raise TypeError("sync policy window must be typed")
        if not 1 <= self.page_size <= 1_000:
            raise ValueError("page size must be between 1 and 1000")
        if min(self.max_pages_per_scope, self.max_content_nodes, self.max_jobs) <= 0:
            raise ValueError("sync bounds must be positive")


@dataclass(frozen=True, slots=True)
class CourseSummary:
    local_key: int
    course_id: CourseId
    code: str
    title: str
    term: str | None
    availability: Availability
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class MaterialSummary:
    local_key: int
    resource_id: AttachmentId
    course: CourseId
    content_key: int
    display_title: str
    availability: Availability
    version_key: int | None
    version_number: int | None
    file_format: str | None
    byte_size: int | None
    downloaded_at: datetime | None
    semantic_type: str | None
    classification_confidence: float | None


@dataclass(frozen=True, slots=True)
class AnnouncementView:
    local_key: int
    announcement_id: str
    course: CourseId
    title: str
    body: str
    availability: Availability
    created: Mapping[str, object] | None
    modified: Mapping[str, object] | None
    published: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class AssessmentView:
    local_key: int
    assessment_id: str
    course: CourseId
    content_key: int
    title: str
    subtype: AssessmentSubtype
    instructions: str
    availability: Availability
    due: Mapping[str, object] | None
    grading_due: Mapping[str, object] | None
    generic_due: Mapping[str, object] | None
    open_at: Mapping[str, object] | None
    close_at: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ResourceView:
    local_key: int
    resource_id: AttachmentId
    display_title: str
    availability: Availability
    version_key: int | None
    version_number: int | None
    sha256: str | None
    byte_size: int | None
    file_format: str | None
    declared_mime: str | None
    downloaded_at: datetime | None
    local_path: Path | None = None


ManualResolution: TypeAlias = ManualFieldResolution | ManualIdentityResolution
R = TypeVar("R")


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
        initialize: bool = True,
    ) -> CoreService:
        paths = RuntimePaths.discover(root)
        if initialize:
            paths.ensure()
        database = Database(paths.database)
        if initialize:
            DomainRepository(database).initialize()
        return cls(database, runtime_paths=paths, sync_engine=sync_engine)

    def list_courses(
        self,
        filter: CourseFilter = CourseFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[CourseSummary]:
        operation = "list_courses"
        try:
            providers = self._providers()
            if not providers and self.sync_engine is not None:
                providers = (self.sync_engine.source.provider_name,)
            scopes = tuple(ScopeKey(provider, None, "courses") for provider in providers)
            return self._query(
                operation,
                lambda: self._load_courses(filter),
                scopes,
                freshness,
                bounded_limit=filter.limit,
            )
        except Exception as error:
            return self._failure(operation, error, "courses")

    def list_materials(
        self,
        course: CourseRef,
        filter: MaterialFilter = MaterialFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[MaterialSummary]:
        operation = "list_materials"
        try:
            course_id, course_key = self._course(course)
            scope = ScopeKey(course_id.provider, course_id, "content")
            return self._query(
                operation,
                lambda: self._load_materials(course_id, course_key, filter),
                (scope,),
                freshness,
                bounded_limit=filter.limit,
                extra_coverage=lambda: (self._parse_coverage(course_id, course_key),),
            )
        except Exception as error:
            return self._failure(operation, error, "materials")

    def search(
        self,
        query: SearchQuery,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[object]:
        operation = "search"
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

            def load_search() -> tuple[tuple[object, ...], tuple[ProvenanceView, ...]]:
                result = self.search_service.search(effective_query)
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
                return tuple(result.items), provenance

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
                        self._event_derivation_coverage(course, key)
                        for course, key in self._course_id_rows(effective_query.filters.course)
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
            )
        except Exception as error:
            return self._failure(operation, error, "search")

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
            return replace(
                self.search(
                    replace(query, filters=replace(query.filters, course=course_id)), freshness
                ),
                operation=operation,
            )
        except Exception as error:
            return self._failure(operation, error, "search")

    def get_announcements(
        self,
        course: CourseRef,
        filter: AnnouncementFilter = AnnouncementFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[AnnouncementView]:
        operation = "get_announcements"
        try:
            course_id, course_key = self._course(course)
            return self._query(
                operation,
                lambda: self._load_announcements(course_id, course_key, filter),
                (ScopeKey(course_id.provider, course_id, "announcements"),),
                freshness,
                bounded_limit=filter.limit,
            )
        except Exception as error:
            return self._failure(operation, error, "announcements")

    def get_assessments(
        self,
        course: CourseRef,
        filter: AssessmentFilter = AssessmentFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[AssessmentView]:
        operation = "get_assessments"
        try:
            course_id, course_key = self._course(course)
            return self._query(
                operation,
                lambda: self._load_assessments(course_id, course_key, filter),
                (ScopeKey(course_id.provider, course_id, "assessments"),),
                freshness,
                bounded_limit=filter.limit,
            )
        except Exception as error:
            return self._failure(operation, error, "assessments")

    def get_events(
        self,
        filter: EventFilter = EventFilter(),
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[CanonicalEvent]:
        operation = "get_events"
        try:
            courses = self._event_courses(filter.course)
            scopes = self._event_scopes(
                courses, filter.window, include_inventory=filter.course is None
            )
            loaded_events: list[tuple[CanonicalEvent, ...]] = []

            def load_events() -> tuple[tuple[CanonicalEvent, ...], tuple[ProvenanceView, ...]]:
                items, provenance = self._load_events(self._event_courses(filter.course), filter)
                loaded_events.clear()
                loaded_events.append(items)
                return items, provenance

            return self._query(
                operation,
                load_events,
                scopes,
                freshness,
                bounded_limit=filter.limit,
                extra_coverage=lambda: (
                    tuple(
                        self._parse_coverage(course, key)
                        for course, key in self._event_courses(filter.course)
                    )
                    + tuple(
                        self._event_derivation_coverage(course, key)
                        for course, key in self._event_courses(filter.course)
                    )
                    + self._unbounded_event_coverage(
                        self._event_courses(filter.course), filter.window
                    )
                    + self._temporal_projection_coverage(
                        () if not loaded_events else loaded_events[0], filter.window
                    )
                    + self._new_course_coverage(courses, filter.course)
                ),
                extra_warnings=lambda: (
                    self._event_derivation_warnings(self._event_courses(filter.course))
                    + self._temporal_projection_warnings(
                        () if not loaded_events else loaded_events[0], filter.window
                    )
                ),
            )
        except Exception as error:
            return self._failure(operation, error, "events")

    def get_upcoming_events(
        self,
        window: TimeWindow,
        course: CourseRef | None = None,
        freshness: FreshnessRequirement = FreshnessRequirement.cache_only(),
    ) -> ResultEnvelope[CanonicalEvent]:
        if not isinstance(window, TimeWindow):
            return self._failure("get_upcoming_events", TypeError("window must be typed"), "events")
        return replace(
            self.get_events(EventFilter(course=course, window=window), freshness),
            operation="get_upcoming_events",
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
                return self._not_found(operation, "resource_version")
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
            return self._failure(operation, error, "resource")

    def resolve_source(
        self, locator: SourceLocatorRef, context_window: int = 1
    ) -> ResultEnvelope[object]:
        operation = "resolve_source"
        try:
            if not isinstance(locator, SourceLocatorRef):
                raise TypeError("source locator must be typed")
            if not 0 <= context_window <= 5:
                raise ValueError("context window must be between 0 and 5")
            resolved = self.search_service.resolve_source(
                locator.reference, context_window=context_window
            )
            provenance = ProvenanceView(
                locator.reference.kind.value,
                locator.reference.key,
                resolved.provider,
                version_key=resolved.version_key,
                locator=resolved.locator,
            )
            return ResultEnvelope(
                operation,
                (resolved,),
                provenance=(provenance,),
                completeness=Coverage.COMPLETE,
                local_reads=1,
            )
        except Exception as error:
            return self._failure(operation, error, "source")

    def resolve_event_candidate(self, decision: ManualResolution) -> ResultEnvelope[CanonicalEvent]:
        operation = "resolve_event_candidate"
        try:
            if isinstance(decision, ManualFieldResolution):
                event = self.events.resolve_field(decision)
            elif isinstance(decision, ManualIdentityResolution):
                event = self.events.resolve_event_source(decision)
            else:
                raise TypeError("manual resolution decision must be typed")
            return self._event_envelope(operation, (event,), Coverage.COMPLETE)
        except Exception as error:
            return self._failure(operation, error, "manual_resolution")

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
            return self._failure(operation, error, "sync")

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
            return self._failure(operation, error, "sync")

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
            return self._failure(operation, error, "sync")

    def fetch_resource(self, resource: ResourceRef, verify: bool = False) -> ResultEnvelope[object]:
        operation = "fetch_resource"
        try:
            engine = self._engine(operation)
            resource_id = self._resource_for_sync(resource)
            result = engine.fetch_resource(resource_id, verify=verify)
            if result.observation is None:
                error = self._category_error(
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
            return self._failure(operation, error, "resource")

    def _query(
        self,
        operation: str,
        load: Callable[[], tuple[tuple[R, ...], tuple[ProvenanceView, ...]]],
        scopes: tuple[ScopeKey, ...],
        requirement: FreshnessRequirement,
        *,
        bounded_limit: int,
        extra_coverage: Callable[[], tuple[CoverageView, ...]] | None = None,
        extra_warnings: Callable[[], tuple[SafeWarning, ...]] | None = None,
        context_events: Callable[[], tuple[CanonicalEvent, ...]] | None = None,
        historical: bool = False,
    ) -> ResultEnvelope[R]:
        if not isinstance(requirement, FreshnessRequirement):
            raise TypeError("freshness requirement must be typed")
        items, provenance = load()
        evaluation_time = self._now()
        decisions = tuple(self._freshness(scope, requirement, evaluation_time) for scope in scopes)
        refresh_targets = tuple(
            scope
            for scope, decision in zip(scopes, decisions, strict=True)
            if decision.should_refresh
        )
        refresh_attempted = False
        refresh_errors: list[SafeError] = []
        refresh_warnings: list[SafeWarning] = []
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
                            refresh_errors.extend(self._refresh_result_errors(operation, result))
                    except Exception as error:
                        refresh_errors.append(
                            self._safe_error(operation, error, self._scope_name(scope))
                        )
                items, provenance = load()
                decisions = tuple(
                    self._freshness(scope, requirement, evaluation_time) for scope in scopes
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
        completeness = self._aggregate_coverage(coverage)
        if any(decision.status is FreshnessStatus.UNKNOWN for decision in decisions):
            completeness = self._worse(completeness, Coverage.UNKNOWN)
        elif any(decision.status is FreshnessStatus.STALE for decision in decisions):
            completeness = self._worse(completeness, Coverage.STALE)
        freshness = tuple(
            self._freshness_view(scope, decision)
            for scope, decision in zip(scopes, decisions, strict=True)
        )
        warnings = list(refresh_warnings)
        if extra_warnings is not None:
            warnings.extend(extra_warnings())
        for view in freshness:
            warnings.extend(
                SafeWarning(code, self._warning_message(code), view.scope)
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
        if len(items) >= bounded_limit:
            warnings.append(
                SafeWarning(
                    "result_limit_reached",
                    "The result limit was reached; additional local matches may exist.",
                    operation,
                )
            )
            if completeness is Coverage.COMPLETE:
                completeness = Coverage.PARTIAL
        if (
            any(not decision.satisfied for decision in decisions)
            or completeness is not Coverage.COMPLETE
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
        conflicts = self._conflicts(items + event_context)
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
        )

    def _load_courses(
        self, filter: CourseFilter
    ) -> tuple[tuple[CourseSummary, ...], tuple[ProvenanceView, ...]]:
        clauses: list[str] = []
        values: list[object] = []
        if filter.availability:
            clauses.append(f"course.availability IN ({','.join('?' for _ in filter.availability)})")
            values.extend(
                item.value for item in sorted(filter.availability, key=lambda item: item.value)
            )
        values.append(filter.limit)
        where = "" if not clauses else "WHERE " + " AND ".join(clauses)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""SELECT course.*, object.source_object_key AS provenance_key,
                    object.remote_key, provider.name AS provider
                FROM course JOIN source_object object USING(source_object_key)
                JOIN source_provider provider USING(provider_key)
                {where} ORDER BY course.code, course.title, course.course_key LIMIT ?""",
                values,
            ).fetchall()
        finally:
            connection.close()
        items = tuple(
            CourseSummary(
                int(row["course_key"]),
                CourseId(str(row["provider"]), str(row["remote_key"])),
                str(row["code"]),
                str(row["title"]),
                None if row["term"] is None else str(row["term"]),
                Availability(str(row["availability"])),
                from_storage_time(str(row["first_observed_at"])),
                from_storage_time(str(row["last_observed_at"])),
            )
            for row in rows
        )
        provenance = tuple(
            ProvenanceView(
                "source_object", int(row["provenance_key"]), item.course_id.provider, item.course_id
            )
            for item, row in zip(items, rows, strict=True)
        )
        return items, provenance

    def _load_materials(
        self, course: CourseId, course_key: int, filter: MaterialFilter
    ) -> tuple[tuple[MaterialSummary, ...], tuple[ProvenanceView, ...]]:
        clauses = ["content.course_key = ?"]
        values: list[object] = [course_key]
        if filter.availability:
            clauses.append(
                f"resource.availability IN ({','.join('?' for _ in filter.availability)})"
            )
            values.extend(
                item.value for item in sorted(filter.availability, key=lambda item: item.value)
            )
        if filter.file_formats:
            clauses.append(f"version.file_format IN ({','.join('?' for _ in filter.file_formats)})")
            values.extend(sorted(filter.file_formats))
        version_join = (
            "JOIN resource_version version ON version.resource_key = resource.resource_key"
            if filter.include_historical_versions
            else "LEFT JOIN resource_version version ON "
            "version.version_key = resource.current_version_key"
        )
        values.append(filter.limit)
        semantic_type_sql = _classification_sql("semantic_type")
        confidence_sql = _classification_sql("confidence")
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""SELECT resource.*, object.remote_key, provider.name AS provider,
                    version.version_key, version.version_number, version.file_format,
                    version.byte_size, version.downloaded_at,
                    ({semantic_type_sql}) AS semantic_type,
                    ({confidence_sql}) AS classification_confidence
                FROM resource JOIN content_node content USING(content_key)
                JOIN source_object object ON object.source_object_key = resource.source_object_key
                JOIN source_provider provider USING(provider_key)
                {version_join}
                WHERE {" AND ".join(clauses)}
                ORDER BY resource.display_title, resource.resource_key,
                         version.version_number LIMIT ?""",
                values,
            ).fetchall()
        finally:
            connection.close()
        items = tuple(
            MaterialSummary(
                int(row["resource_key"]),
                AttachmentId(str(row["provider"]), str(row["remote_key"])),
                course,
                int(row["content_key"]),
                str(row["display_title"]),
                Availability(str(row["availability"])),
                None if row["version_key"] is None else int(row["version_key"]),
                None if row["version_number"] is None else int(row["version_number"]),
                None if row["file_format"] is None else str(row["file_format"]),
                None if row["byte_size"] is None else int(row["byte_size"]),
                None
                if row["downloaded_at"] is None
                else from_storage_time(str(row["downloaded_at"])),
                None if row["semantic_type"] is None else str(row["semantic_type"]),
                None
                if row["classification_confidence"] is None
                else float(row["classification_confidence"]),
            )
            for row in rows
        )
        provenance = tuple(
            ProvenanceView(
                "source_object",
                int(row["source_object_key"]),
                item.resource_id.provider,
                course,
                item.version_key,
            )
            for item, row in zip(items, rows, strict=True)
        )
        return items, provenance

    def _load_announcements(
        self, course: CourseId, course_key: int, filter: AnnouncementFilter
    ) -> tuple[tuple[AnnouncementView, ...], tuple[ProvenanceView, ...]]:
        clauses = ["announcement.course_key = ?"]
        values: list[object] = [course_key]
        if filter.availability:
            clauses.append(
                f"announcement.availability IN ({','.join('?' for _ in filter.availability)})"
            )
            values.extend(
                item.value for item in sorted(filter.availability, key=lambda item: item.value)
            )
        values.append(filter.limit)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""SELECT announcement.*, object.remote_key FROM announcement
                JOIN source_object object USING(source_object_key)
                WHERE {" AND ".join(clauses)}
                ORDER BY announcement.announcement_key DESC LIMIT ?""",
                values,
            ).fetchall()
        finally:
            connection.close()
        items = tuple(
            AnnouncementView(
                int(row["announcement_key"]),
                str(row["remote_key"]),
                course,
                str(row["title"]),
                str(row["body"]),
                Availability(str(row["availability"])),
                self._json(row["created_time_json"]),
                self._json(row["modified_time_json"]),
                self._json(row["published_time_json"]),
            )
            for row in rows
        )
        provenance = tuple(
            ProvenanceView(
                "source_observation", int(row["current_observation_key"]), course.provider, course
            )
            for row in rows
        )
        return items, provenance

    def _load_assessments(
        self, course: CourseId, course_key: int, filter: AssessmentFilter
    ) -> tuple[tuple[AssessmentView, ...], tuple[ProvenanceView, ...]]:
        clauses = ["assessment.course_key = ?"]
        values: list[object] = [course_key]
        if filter.subtypes:
            clauses.append(f"assessment.subtype IN ({','.join('?' for _ in filter.subtypes)})")
            values.extend(
                item.value for item in sorted(filter.subtypes, key=lambda item: item.value)
            )
        if filter.availability:
            clauses.append(
                f"assessment.availability IN ({','.join('?' for _ in filter.availability)})"
            )
            values.extend(
                item.value for item in sorted(filter.availability, key=lambda item: item.value)
            )
        values.append(filter.limit)
        connection = self.database.connect()
        try:
            rows = connection.execute(
                f"""SELECT assessment.*, object.remote_key FROM assessment
                JOIN source_object object USING(source_object_key)
                WHERE {" AND ".join(clauses)} ORDER BY assessment.assessment_key DESC LIMIT ?""",
                values,
            ).fetchall()
        finally:
            connection.close()
        items = tuple(
            AssessmentView(
                int(row["assessment_key"]),
                str(row["remote_key"]),
                course,
                int(row["content_key"]),
                str(row["title"]),
                AssessmentSubtype(str(row["subtype"])),
                str(row["instructions"]),
                Availability(str(row["availability"])),
                self._json(row["due_time_json"]),
                self._json(row["grading_due_time_json"]),
                self._json(row["generic_due_time_json"]),
                self._json(row["open_time_json"]),
                self._json(row["close_time_json"]),
            )
            for row in rows
        )
        provenance = tuple(
            ProvenanceView(
                "source_observation", int(row["current_observation_key"]), course.provider, course
            )
            for row in rows
        )
        return items, provenance

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
        self, courses: tuple[tuple[CourseId, int], ...], filter: EventFilter
    ) -> tuple[tuple[CanonicalEvent, ...], tuple[ProvenanceView, ...]]:
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
        events.sort(key=self._event_sort_key)
        events = events[: filter.limit]
        provenance = tuple(
            ProvenanceView(
                source.evidence_kind, source.evidence_key, event.course.provider, event.course
            )
            for event in events
            for source in event.sources
        )
        return tuple(events), provenance

    def _event_scopes(
        self,
        courses: tuple[tuple[CourseId, int], ...],
        window: TimeWindow | None,
        *,
        include_inventory: bool = False,
    ) -> tuple[ScopeKey, ...]:
        scopes: list[ScopeKey] = []
        if include_inventory:
            providers = {course.provider for course, _ in courses}
            if not providers and self.sync_engine is not None:
                providers.add(self.sync_engine.source.provider_name)
            scopes.extend(ScopeKey(provider, None, "courses") for provider in sorted(providers))
        for course, _ in courses:
            scopes.extend(
                (
                    ScopeKey(course.provider, course, "content"),
                    ScopeKey(course.provider, course, EventSourceScope.ANNOUNCEMENTS.value),
                    ScopeKey(course.provider, course, EventSourceScope.ASSESSMENTS.value),
                )
            )
            if window is not None:
                scopes.extend(
                    (
                        ScopeKey(course.provider, course, EventSourceScope.SCHEDULE.value, window),
                        ScopeKey(course.provider, course, EventSourceScope.DUE_ITEMS.value, window),
                    )
                )
        return tuple(scopes)

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
    def _unbounded_event_coverage(
        courses: tuple[tuple[CourseId, int], ...], window: TimeWindow | None
    ) -> tuple[CoverageView, ...]:
        if window is not None:
            return ()
        return tuple(
            CoverageView(
                course.provider,
                data_kind,
                Coverage.UNKNOWN,
                course,
                evidence="unbounded_query_scope",
            )
            for course, _ in courses
            for data_kind in ("schedule", "due_items")
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

    def _event_derivation_coverage(self, course: CourseId, course_key: int) -> CoverageView:
        connection = self.database.connect()
        try:
            resource_rows = connection.execute(
                """SELECT resource.current_version_key AS version_key,
                    (SELECT parsed.parse_key FROM parsed_document parsed
                     WHERE parsed.version_key = resource.current_version_key
                     ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1) AS parse_key
                FROM resource JOIN content_node content USING(content_key)
                WHERE content.course_key = ? AND resource.current_version_key IS NOT NULL""",
                (course_key,),
            ).fetchall()
            observation_rows = connection.execute(
                """SELECT current_observation_key AS observation_key FROM announcement
                    WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM assessment WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM schedule_item WHERE course_key = ?
                UNION ALL SELECT current_observation_key FROM due_item WHERE course_key = ?""",
                (course_key, course_key, course_key, course_key),
            ).fetchall()
            extraction_keys: list[int] = []
            complete = True
            observed_values: list[datetime] = []
            for row in resource_rows:
                parse_key = row["parse_key"]
                extraction = None
                if parse_key is not None:
                    extraction = connection.execute(
                        """SELECT extraction_record_key, status, extracted_at
                        FROM extraction_record
                        WHERE input_kind = 'resource_version' AND version_key = ? AND parse_key = ?
                        ORDER BY extracted_at DESC, extraction_record_key DESC LIMIT 1""",
                        (int(row["version_key"]), int(parse_key)),
                    ).fetchone()
                if extraction is None or str(extraction["status"]) != "COMPLETE":
                    complete = False
                else:
                    extraction_keys.append(int(extraction["extraction_record_key"]))
                    observed_values.append(from_storage_time(str(extraction["extracted_at"])))
            for row in observation_rows:
                extraction = connection.execute(
                    """SELECT extraction.extraction_record_key,
                        extraction.status, extraction.extracted_at
                    FROM source_observation current
                    JOIN source_observation equivalent
                      ON equivalent.source_object_key = current.source_object_key
                     AND equivalent.observation_hash = current.observation_hash
                    JOIN extraction_record extraction
                      ON extraction.source_observation_key = equivalent.observation_key
                     AND extraction.input_kind = 'source_observation'
                    WHERE current.observation_key = ?
                    ORDER BY (extraction.status = 'COMPLETE') DESC,
                        extraction.extracted_at DESC,
                        extraction.extraction_record_key DESC
                    LIMIT 1""",
                    (int(row["observation_key"]),),
                ).fetchone()
                if extraction is None or str(extraction["status"]) != "COMPLETE":
                    complete = False
                else:
                    extraction_keys.append(int(extraction["extraction_record_key"]))
                    observed_values.append(from_storage_time(str(extraction["extracted_at"])))
            if extraction_keys:
                placeholders = ",".join("?" for _ in extraction_keys)
                missing = connection.execute(
                    f"""SELECT COUNT(*) FROM event_candidate candidate
                    LEFT JOIN event_source source USING(candidate_key)
                    WHERE candidate.extraction_record_key IN ({placeholders})
                      AND (source.event_source_key IS NULL
                           OR source.event_key IS NULL
                           OR source.resolution_state = 'UNRESOLVED')""",
                    extraction_keys,
                ).fetchone()
                complete = complete and missing is not None and int(missing[0]) == 0
        finally:
            connection.close()
        return CoverageView(
            course.provider,
            "event_derivation",
            Coverage.COMPLETE if complete else Coverage.PARTIAL,
            course,
            observed_at=max(observed_values, default=None),
            evidence="current_extraction_and_reconciliation",
        )

    def _event_derivation_warnings(
        self, courses: tuple[tuple[CourseId, int], ...]
    ) -> tuple[SafeWarning, ...]:
        if not courses:
            return ()
        connection = self.database.connect()
        try:
            placeholders = ",".join("?" for _ in courses)
            row = connection.execute(
                f"""SELECT 1 FROM event_source
                WHERE course_key IN ({placeholders})
                  AND (event_key IS NULL OR resolution_state = 'UNRESOLVED')
                LIMIT 1""",
                tuple(key for _, key in courses),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return ()
        return (
            SafeWarning(
                "unresolved_event_identity",
                "One or more event candidates retain unresolved source identity.",
                "events",
            ),
        )

    def _freshness(
        self, scope: ScopeKey, requirement: FreshnessRequirement, now: datetime
    ) -> FreshnessDecision:
        return evaluate_freshness(self.states.get(scope), requirement, now=now)

    def _freshness_view(self, scope: ScopeKey, decision: FreshnessDecision) -> FreshnessView:
        return FreshnessView(
            self._scope_name(scope),
            decision.status.value,
            decision.as_of,
            None if decision.age is None else int(decision.age.total_seconds()),
            decision.satisfied,
            tuple(code.value for code in decision.warning_codes),
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
    def _aggregate_coverage(coverage: tuple[CoverageView, ...]) -> Coverage:
        return (
            Coverage.UNKNOWN
            if not coverage
            else max((item.coverage for item in coverage), key=_COVERAGE_RANK.__getitem__)
        )

    @staticmethod
    def _worse(left: Coverage, right: Coverage) -> Coverage:
        return max((left, right), key=_COVERAGE_RANK.__getitem__)

    @staticmethod
    def _json(value: object) -> Mapping[str, object] | None:
        if value is None:
            return None
        parsed = json.loads(str(value))
        return parsed if isinstance(parsed, dict) else None

    @staticmethod
    def _temporal_bounds(value: object) -> tuple[datetime, datetime] | None:
        if not isinstance(value, Mapping):
            return None
        instant = value.get("instant")
        if isinstance(instant, str):
            parsed = datetime.fromisoformat(instant.replace("Z", "+00:00"))
            if parsed.tzinfo is not None and parsed.utcoffset() is not None:
                point = parsed.astimezone(UTC)
                return point, point
        if value.get("precision") == "DATE_ONLY" and isinstance(value.get("date"), str):
            try:
                day = date.fromisoformat(str(value["date"]).strip())
            except ValueError:
                return None
            # Expand conservatively across all plausible civil timezones rather
            # than assigning the floating source date a fabricated timezone.
            return (
                datetime.combine(day - timedelta(days=1), time.min, UTC),
                datetime.combine(day + timedelta(days=2), time.min, UTC),
            )
        return None

    @classmethod
    def _overlaps(cls, event: CanonicalEvent, window: TimeWindow) -> bool:
        if cls._has_unbounded_temporal(event):
            return True
        bounds = [
            cls._temporal_bounds(value)
            for value in (event.start_time, event.end_time, event.due_time)
        ]
        conflict_keys = {
            key
            for conflict in event.conflicts
            if conflict.state.value == "OPEN"
            for key in conflict.alternative_claim_keys
        }
        bounds.extend(
            cls._temporal_bounds(claim.value)
            for claim in event.claims
            if claim.key in conflict_keys
        )
        known = [value for value in bounds if value is not None]
        if not known:
            return True
        start = min(value[0] for value in known)
        end = max(value[1] for value in known)
        return start < window.until and end >= window.since

    @classmethod
    def _has_unbounded_temporal(cls, event: CanonicalEvent) -> bool:
        values: list[object] = [event.start_time, event.end_time, event.due_time]
        conflict_keys = {
            key
            for conflict in event.conflicts
            if conflict.state.value == "OPEN"
            for key in conflict.alternative_claim_keys
        }
        temporal_fields = {
            CandidateFieldName.START_TIME,
            CandidateFieldName.END_TIME,
            CandidateFieldName.DUE_TIME,
            CandidateFieldName.OPEN_AT,
            CandidateFieldName.CLOSE_AT,
            CandidateFieldName.AVAILABLE_FROM,
            CandidateFieldName.AVAILABLE_UNTIL,
            CandidateFieldName.PUBLISHED_AT,
        }
        values.extend(
            claim.value
            for claim in event.claims
            if claim.key in conflict_keys and claim.field_name in temporal_fields
        )
        present = [value for value in values if value is not None]
        return not present or any(cls._temporal_bounds(value) is None for value in present)

    @classmethod
    def _temporal_projection_coverage(
        cls, events: tuple[CanonicalEvent, ...], window: TimeWindow | None
    ) -> tuple[CoverageView, ...]:
        if window is None:
            return ()
        courses = {event.course for event in events if cls._has_unbounded_temporal(event)}
        return tuple(
            CoverageView(
                course.provider,
                "event_time_projection",
                Coverage.PARTIAL,
                course,
                window.since,
                window.until,
                evidence="unsupported_or_missing_temporal_precision",
            )
            for course in sorted(courses, key=lambda item: (item.provider, item.value))
        )

    @classmethod
    def _temporal_projection_warnings(
        cls, events: tuple[CanonicalEvent, ...], window: TimeWindow | None
    ) -> tuple[SafeWarning, ...]:
        if window is None or not any(cls._has_unbounded_temporal(event) for event in events):
            return ()
        return (
            SafeWarning(
                "unbounded_event_time",
                "An event with missing or unsupported time precision may overlap the window.",
                "events",
            ),
        )

    @classmethod
    def _event_sort_key(cls, event: CanonicalEvent) -> tuple[datetime, int]:
        bounds = [
            cls._temporal_bounds(value)
            for value in (event.start_time, event.due_time, event.end_time)
        ]
        known = [value[0] for value in bounds if value is not None]
        return (min(known) if known else datetime.max.replace(tzinfo=UTC), event.key)

    @staticmethod
    def _conflicts(items: tuple[object, ...]) -> tuple[ConflictView, ...]:
        return tuple(
            ConflictView(
                conflict.event_key,
                conflict.field_name.value,
                conflict.reason,
                conflict.alternative_claim_keys,
            )
            for item in items
            if isinstance(item, CanonicalEvent)
            for conflict in item.conflicts
            if conflict.state.value == "OPEN"
        )

    def _event_envelope(
        self, operation: str, events: tuple[CanonicalEvent, ...], completeness: Coverage
    ) -> ResultEnvelope[CanonicalEvent]:
        provenance = tuple(
            ProvenanceView(
                source.evidence_kind, source.evidence_key, event.course.provider, event.course
            )
            for event in events
            for source in event.sources
        )
        conflicts = self._conflicts(events)
        warnings = (
            ()
            if not conflicts
            else (
                SafeWarning(
                    "unresolved_event_conflicts",
                    "One or more event fields require resolution.",
                    "events",
                ),
            )
        )
        return ResultEnvelope(
            operation,
            events,
            provenance=provenance,
            conflicts=conflicts,
            warnings=warnings,
            completeness=completeness,
        )

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
        completeness = self._aggregate_coverage(coverage)
        errors: tuple[SafeError, ...] = ()
        if getattr(result, "error_category", None):
            errors = (self._category_error(operation, str(result.error_category), "sync"),)
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

    def _not_found(self, operation: str, scope: str) -> ResultEnvelope[Any]:
        return ResultEnvelope(
            operation,
            errors=(
                SafeError(
                    ErrorCategory.RESOURCE_UNAVAILABLE,
                    "local_item_not_found",
                    "The requested local item was not found.",
                    operation,
                    scope,
                    False,
                    Coverage.UNKNOWN,
                ),
            ),
            completeness=Coverage.UNKNOWN,
        )

    def _failure(self, operation: str, error: Exception, scope: str) -> ResultEnvelope[Any]:
        return ResultEnvelope(
            operation,
            errors=(self._safe_error(operation, error, scope),),
            completeness=Coverage.FAILED,
        )

    @staticmethod
    def _safe_error(operation: str, error: Exception, scope: str) -> SafeError:
        category = ErrorCategory.STORAGE_FAILURE
        code = "operation_failed"
        retryable = False
        message = "The local operation could not be completed."
        source_category = getattr(error, "category", None)
        if (
            isinstance(error, AuthenticationRequired)
            or source_category == "authentication_required"
        ):
            category, code, retryable, message = (
                ErrorCategory.AUTHENTICATION_REQUIRED,
                "authentication_required",
                True,
                "An authorized read session is required.",
            )
        elif isinstance(error, SessionExpired) or source_category == "session_expired":
            category, code, retryable, message = (
                ErrorCategory.SESSION_EXPIRED,
                "session_expired",
                True,
                "The authorized read session expired.",
            )
        elif (
            isinstance(error, UnsupportedCapability) or source_category == "unsupported_capability"
        ):
            category, code, message = (
                ErrorCategory.CAPABILITY_UNSUPPORTED,
                "capability_unsupported",
                "The source does not support this read capability.",
            )
        elif isinstance(error, SourceError):
            category, code, retryable, message = (
                ErrorCategory.SOURCE_UNAVAILABLE,
                str(source_category or "source_unavailable"),
                True,
                "The source read could not be completed.",
            )
        elif isinstance(error, MigrationError):
            category, code, message = (
                ErrorCategory.MIGRATION_FAILURE,
                "migration_failed",
                "The local database migration could not be completed.",
            )
        elif isinstance(error, StorageError) or isinstance(error, sqlite3.Error):
            category, code, message = (
                ErrorCategory.STORAGE_FAILURE,
                "storage_failed",
                "The local data operation could not be completed.",
            )
        elif isinstance(error, LookupError):
            category, code, message = (
                ErrorCategory.RESOURCE_UNAVAILABLE,
                "local_item_not_found",
                "The requested local item was not found.",
            )
        elif isinstance(error, (TypeError, ValueError)):
            category, code, message = (
                ErrorCategory.INVALID_REQUEST,
                "invalid_request",
                "The request is invalid.",
            )
        elif "sync_engine_unavailable" in str(error):
            category, code, retryable, message = (
                ErrorCategory.CONFIGURATION_REQUIRED,
                "sync_engine_unavailable",
                True,
                "A configured read-only sync engine is required.",
            )
        return SafeError(category, code, message, operation, scope, retryable, Coverage.FAILED)

    @staticmethod
    def _category_error(operation: str, code: str, scope: str) -> SafeError:
        mapping = {
            "authentication_required": (
                ErrorCategory.AUTHENTICATION_REQUIRED,
                True,
                "An authorized read session is required.",
            ),
            "session_expired": (
                ErrorCategory.SESSION_EXPIRED,
                True,
                "The authorized read session expired.",
            ),
            "unsupported_capability": (
                ErrorCategory.CAPABILITY_UNSUPPORTED,
                False,
                "The source does not support this read capability.",
            ),
            "source_unavailable": (
                ErrorCategory.SOURCE_UNAVAILABLE,
                True,
                "The source read could not be completed.",
            ),
        }
        category, retryable, message = mapping.get(
            code,
            (ErrorCategory.SOURCE_UNAVAILABLE, True, "The source read could not be completed."),
        )
        return SafeError(category, code, message, operation, scope, retryable, Coverage.FAILED)

    def _refresh_result_errors(
        self, operation: str, result: SyncRunResult
    ) -> tuple[SafeError, ...]:
        failures = [
            (scope.failure_category, scope.data_kind)
            for scope in result.scopes
            if scope.failure_category is not None
        ]
        if result.error_category is not None:
            failures.append((result.error_category, "refresh"))
        return tuple(
            self._category_error(operation, code, scope)
            for code, scope in dict.fromkeys(failures)
            if code is not None
        )

    @staticmethod
    def _warning_message(code: str) -> str:
        return {
            "no_complete_observation": "No complete observation exists for this exact scope.",
            "max_age_unconfigured": "No maximum age is configured for this scope.",
            "latest_attempt_incomplete": "The latest observation of this scope was incomplete.",
            "current_coverage_unestablished": "Current complete coverage is not established.",
        }.get(code, "Freshness could not be fully established for this scope.")


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
    "CandidateFieldName",
    "AssessmentSubtype",
    "Availability",
    "Coverage",
    "CourseId",
    "AttachmentId",
    "TemporalPrecision",
    "TimeWindow",
]
