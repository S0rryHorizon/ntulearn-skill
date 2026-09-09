from __future__ import annotations

from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_reconciliation_adversarial import (
    _announce,
    _assessment_with_due_alternatives,
    _harness,
)

from ntulearn_skill.client import AnnouncementSourceRecord
from ntulearn_skill.core import (
    AnnouncementId,
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    SyncRunStatus,
)
from ntulearn_skill.core.api import (
    CoreService,
    CourseFilter,
    CourseRef,
    FreshnessRequirement,
    ResourceRef,
    SearchEntityKind,
    SearchFilters,
    SearchQuery,
    SyncPolicy,
    TimeWindow,
)
from ntulearn_skill.core.results import ErrorCategory, ResultEnvelope
from ntulearn_skill.events import (
    CandidateFieldName,
    CanonicalEvent,
    Claim,
    ClaimDecisionState,
    ClaimOrigin,
    ConflictState,
    EventConflict,
    EventRepository,
    EventResolutionState,
)
from ntulearn_skill.extractors.classification import (
    ClassificationInput,
    ClassificationRepository,
    ClassificationService,
    RuleBasedMaterialClassifier,
)
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.sync.models import ScopeResult
from ntulearn_skill.sync.observability import SyncRunRecorder
from ntulearn_skill.sync.state import ScopeKey, SyncAttemptOutcome, SyncStateRepository

NOW = datetime(2031, 1, 10, 12, tzinfo=UTC)


def _pdf(label: bytes) -> bytes:
    result = bytearray(b"%PDF-1.4\n% synthetic-only\n")
    object_offset = len(result)
    result.extend(b"1 0 obj\n<< /Type /Catalog /Label (" + label + b") >>\nendobj\n")
    xref_offset = len(result)
    result.extend(b"xref\n0 2\n0000000000 65535 f \n")
    result.extend(f"{object_offset:010d} 00000 n \n".encode("ascii"))
    result.extend(f"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode())
    return bytes(result)


def _database(tmp_path: Path) -> tuple[Database, RuntimePaths, DomainRepository]:
    paths = RuntimePaths(tmp_path / "private-synthetic")
    database = Database(paths.database)
    domain = DomainRepository(database)
    assert domain.initialize() == 9
    return database, paths, domain


def _run(database: Database, provider: str, *, at: datetime = NOW) -> int:
    recorder = SyncRunRecorder(database, provider)
    key = recorder.start(mode="test", requested_scope={})
    recorder.finish(
        key,
        status=SyncRunStatus.SUCCEEDED,
        counts={},
        warnings=(),
    )
    return key


def _record_complete(
    database: Database,
    scope: ScopeKey,
    *,
    at: datetime = NOW,
    max_age: timedelta | None = None,
) -> None:
    recorder = SyncRunRecorder(database, scope.provider)
    run_key = recorder.start(mode="test-scope", requested_scope={})
    recorder.record_scope(
        run_key,
        ScopeResult(
            scope.provider,
            scope.course_id,
            scope.data_kind,
            Coverage.COMPLETE,
            1,
            0,
            True,
            None,
            (),
            at,
        ),
    )
    recorder.finish(
        run_key,
        status=SyncRunStatus.SUCCEEDED,
        counts={},
        warnings=(),
    )
    SyncStateRepository(database).record_attempt(
        scope,
        run_key=run_key,
        attempted_at=at,
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=max_age,
    )


def test_empty_database_is_not_conclusive_and_no_engine_refresh_is_safe(tmp_path: Path) -> None:
    database, paths, _ = _database(tmp_path)
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)

    cached = service.list_courses()
    assert cached.items == ()
    assert cached.completeness is Coverage.UNKNOWN
    assert not cached.conclusive_empty
    assert cached.to_dict()["schema_version"] == "1.0"

    refreshed = service.list_courses(freshness=FreshnessRequirement.require_current())
    assert not refreshed.ok
    assert {error.category for error in refreshed.errors} == {
        ErrorCategory.CONFIGURATION_REQUIRED,
        ErrorCategory.FRESHNESS_UNSATISFIED,
    }
    assert all("private" not in error.message for error in refreshed.errors)


def test_course_provenance_uses_source_object_namespace_and_limit_is_partial(
    tmp_path: Path,
) -> None:
    database, paths, domain = _database(tmp_path)
    stamp = NOW.isoformat(timespec="microseconds")
    with database.transaction() as connection:
        provider_key = connection.execute(
            "INSERT INTO source_provider(name, created_at, updated_at) VALUES (?, ?, ?)",
            ("synthetic", stamp, stamp),
        ).lastrowid
        connection.execute(
            """INSERT INTO source_object(
                provider_key, object_kind, remote_key, first_observed_at, last_observed_at
            ) VALUES (?, 'announcement', 'orphan-synthetic', ?, ?)""",
            (provider_key, stamp, stamp),
        )
    first = CourseId("synthetic", "course-1")
    second = CourseId("synthetic", "course-2")
    one = domain.put_course(first, code="PH0001", title="Synthetic One")
    domain.put_course(second, code="PH0002", title="Synthetic Two")
    _record_complete(database, ScopeKey("synthetic", None, "courses"))

    result = CoreService(database, runtime_paths=paths, now=lambda: NOW).list_courses(
        CourseFilter(limit=1)
    )
    assert result.items[0].local_key == one.key == 1
    assert result.provenance[0].source_key == 2
    assert result.completeness is Coverage.PARTIAL
    assert not result.conclusive_empty
    assert any(warning.code == "result_limit_reached" for warning in result.warnings)


def test_stale_complete_scope_does_not_make_empty_result_conclusive(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    course = CourseId("synthetic", "course-stale")
    domain.put_course(course, code="PH0003", title="Synthetic Stale")
    _record_complete(
        database,
        ScopeKey("synthetic", None, "courses"),
        at=NOW - timedelta(days=2),
        max_age=timedelta(hours=1),
    )
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)

    result = service.list_courses(
        CourseFilter(availability=frozenset({Availability.REMOVED_CONFIRMED}))
    )
    assert result.items == ()
    assert result.completeness is Coverage.STALE
    assert not result.conclusive_empty


def test_search_preserves_announcement_scope_coverage(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    course = CourseId("synthetic", "course-announcement")
    domain.put_course(course, code="PH0004", title="Synthetic Announcements")
    run = _run(database, "synthetic")
    EventRepository(database).observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "announcement-1"),
            course,
            "Synthetic deadline",
            "The synthetic deadline is Friday.",
            Availability.ACTIVE,
        ),
        sync_run_key=run,
        observed_at=NOW,
    )
    _record_complete(database, ScopeKey("synthetic", course, "announcements"))
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)

    result = service.search(
        SearchQuery(
            "deadline",
            SearchFilters(
                course=course,
                entity_kinds=frozenset({SearchEntityKind.ANNOUNCEMENT}),
            ),
        )
    )
    assert len(result.items) == 1
    assert {view.data_kind for view in result.coverage} == {"announcements"}
    assert result.completeness is Coverage.COMPLETE


@pytest.mark.parametrize(
    ("text", "kind"),
    (("Quiz", SearchEntityKind.EVENT), ("2030", SearchEntityKind.CLAIM)),
)
def test_event_and_claim_searches_preserve_conflicts(
    tmp_path: Path, text: str, kind: SearchEntityKind
) -> None:
    harness = _harness(tmp_path)
    _assessment_with_due_alternatives(harness)
    event = harness.reconciler.reconcile_course(harness.first_course).events[0]
    assert event.conflicts
    service = CoreService(harness.database)

    result = service.search(
        SearchQuery(
            text,
            SearchFilters(
                course=harness.first_course,
                entity_kinds=frozenset({kind}),
            ),
        )
    )
    assert result.items
    assert result.conflicts
    assert any(warning.code == "unresolved_event_conflicts" for warning in result.warnings)


def test_upcoming_bounds_keep_normalized_date_and_conflicting_alternative() -> None:
    date_only = {
        "precision": "DATE_ONLY",
        "date": "2031-01-10",
        "source_text": "10 January 2031",
        "source_timezone": None,
    }
    assert CoreService._temporal_bounds(date_only) is not None

    alternative = Claim(
        7,
        1,
        1,
        1,
        ClaimOrigin.SOURCE,
        CandidateFieldName.DUE_TIME,
        {"instant": "2031-01-10T13:00:00+00:00", "precision": "EXACT_TIME"},
        "synthetic alternative",
        None,
        "UTC",
        1.0,
        ClaimDecisionState.CONFLICTING,
        "synthetic conflict",
    )
    conflict = EventConflict(
        1,
        1,
        CandidateFieldName.DUE_TIME,
        ConflictState.OPEN,
        "synthetic conflict",
        (7,),
    )
    event = CanonicalEvent(
        1,
        "a" * 64,
        CourseId("synthetic", "course"),
        None,
        "Synthetic event",
        None,
        None,
        {"instant": "2031-02-10T13:00:00+00:00", "precision": "EXACT_TIME"},
        None,
        None,
        EventResolutionState.CONFLICTING,
        1.0,
        1,
        (),
        (),
        (alternative,),
        (conflict,),
    )
    window = TimeWindow(NOW, NOW + timedelta(hours=2))
    assert CoreService._overlaps(event, window)


def test_unbound_related_change_keeps_upcoming_inconclusive(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "move-first",
        "Quiz 1 deadline moved to 2030-02-12 10:00 UTC.",
        ordinal=1,
        published_day=2,
    )
    assert not harness.reconciler.reconcile_course(harness.first_course).events
    window = TimeWindow(
        datetime(2030, 2, 1, tzinfo=UTC),
        datetime(2030, 3, 1, tzinfo=UTC),
    )
    for scope in (
        ScopeKey("synthetic", harness.first_course, "content"),
        ScopeKey("synthetic", harness.first_course, "announcements"),
        ScopeKey("synthetic", harness.first_course, "assessments"),
        ScopeKey("synthetic", harness.first_course, "schedule", window),
        ScopeKey("synthetic", harness.first_course, "due_items", window),
    ):
        _record_complete(harness.database, scope, at=datetime(2030, 3, 1, tzinfo=UTC))

    result = CoreService(harness.database).get_upcoming_events(
        window,
        CourseRef(remote_id=harness.first_course),
    )
    assert result.items == ()
    assert result.completeness is Coverage.PARTIAL
    assert not result.conclusive_empty
    assert any(warning.code == "unresolved_event_identity" for warning in result.warnings)


def test_week_only_event_is_retained_with_partial_window_coverage(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _announce(
        harness,
        "week-only",
        "Quiz 1 due Week 3.",
        ordinal=1,
        published_day=1,
    )
    reconciled = harness.reconciler.reconcile_course(harness.first_course)
    assert reconciled.events[0].due_time is not None
    assert reconciled.events[0].due_time["precision"] == "WEEK_ONLY"
    window = TimeWindow(
        datetime(2030, 6, 1, tzinfo=UTC),
        datetime(2030, 6, 2, tzinfo=UTC),
    )
    for scope in (
        ScopeKey("synthetic", harness.first_course, "content"),
        ScopeKey("synthetic", harness.first_course, "announcements"),
        ScopeKey("synthetic", harness.first_course, "assessments"),
        ScopeKey("synthetic", harness.first_course, "schedule", window),
        ScopeKey("synthetic", harness.first_course, "due_items", window),
    ):
        _record_complete(harness.database, scope, at=datetime(2030, 6, 1, tzinfo=UTC))

    result = CoreService(harness.database).get_upcoming_events(
        window,
        CourseRef(remote_id=harness.first_course),
    )
    assert result.items == reconciled.events
    assert result.completeness is Coverage.PARTIAL
    assert any(warning.code == "unbounded_event_time" for warning in result.warnings)


class _RefreshEngine:
    def __init__(self, database: Database, now: datetime) -> None:
        self.domain = DomainRepository(database)
        self.source = SimpleNamespace(provider_name="synthetic")
        self.database = database
        self.now = now
        self.calls: list[ScopeKey] = []

    def refresh_scope(self, scope: ScopeKey) -> None:
        self.calls.append(scope)
        _record_complete(self.database, scope, at=self.now)


class _DiscoveringEngine(_RefreshEngine):
    def refresh_scope(self, scope: ScopeKey) -> None:
        self.domain.put_course(
            CourseId("synthetic", "course-discovered"),
            code="PH0099",
            title="Synthetic discovered course",
        )
        super().refresh_scope(scope)


def test_query_uses_one_refresh_cycle_and_one_evaluation_time(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    domain.put_course(CourseId("synthetic", "course-1"), code="PH0001", title="Synthetic")
    engine = _RefreshEngine(database, NOW)
    service = CoreService(
        database,
        runtime_paths=paths,
        sync_engine=engine,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.list_courses(freshness=FreshnessRequirement.require_current())
    assert result.ok
    assert result.completeness is Coverage.COMPLETE
    assert result.refresh_attempted
    assert result.local_reads == 2
    assert len(engine.calls) == 1


def test_global_search_bootstraps_inventory_once_and_keeps_new_scopes_unknown(
    tmp_path: Path,
) -> None:
    database, paths, _ = _database(tmp_path)
    engine = _DiscoveringEngine(database, NOW)
    service = CoreService(
        database,
        runtime_paths=paths,
        sync_engine=engine,  # type: ignore[arg-type]
        now=lambda: NOW,
    )

    result = service.search(
        SearchQuery("missing"),
        FreshnessRequirement.require_current(),
    )
    assert engine.calls == [ScopeKey("synthetic", None, "courses")]
    assert any(
        item.data_kind == "new_course_scopes"
        and item.course == CourseId("synthetic", "course-discovered")
        and item.coverage is Coverage.UNKNOWN
        for item in result.coverage
    )
    assert not result.conclusive_empty


def test_explicit_historical_version_search_never_refreshes_source(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    course = CourseId("synthetic", "course-history")
    domain.put_course(course, code="PH0003", title="Synthetic history")
    engine = _RefreshEngine(database, NOW)
    service = CoreService(
        database,
        runtime_paths=paths,
        sync_engine=engine,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    result = service.search(
        SearchQuery(
            "missing",
            SearchFilters(course=course, version_key=999),
        ),
        FreshnessRequirement.require_current(),
    )
    assert engine.calls == []
    assert not result.refresh_attempted
    assert any(warning.code == "historical_local_only" for warning in result.warnings)


def test_resource_versions_are_owned_and_paths_are_explicit(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    store = ResourceStore(paths, ResourceRepository(database))
    course = CourseId("synthetic", "course")
    content = ContentId("synthetic", "content")
    domain.put_course(course, code="PH0000", title="Synthetic")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Synthetic files",
        position=0,
    )
    run = _run(database, "synthetic")
    first = store.ingest(
        BytesIO(_pdf(b"one")),
        AttachmentId("synthetic", "a-1"),
        content_id=content,
        sync_run_key=run,
        display_title="One",
        original_filename="one.pdf",
    )
    second = store.ingest(
        BytesIO(_pdf(b"two")),
        AttachmentId("synthetic", "a-2"),
        content_id=content,
        sync_run_key=run,
        display_title="Two",
        original_filename="two.pdf",
    )
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)

    wrong = service.get_resource(ResourceRef(local_key=first.resource.key), second.version.key)
    assert wrong.errors[0].category is ErrorCategory.INVALID_REQUEST

    result = service.get_resource(ResourceRef(local_key=first.resource.key))
    encoded = result.to_dict()
    assert "local_path" not in encoded["items"][0]
    assert "blob_relpath" not in str(encoded)
    explicit = service.get_resource(
        ResourceRef(local_key=first.resource.key), include_local_path=True
    )
    assert explicit.items[0].local_path is not None
    assert "local_path" in explicit.to_dict(include_local_paths=True)["items"][0]


def test_material_classification_is_automatic_and_version_scoped(tmp_path: Path) -> None:
    database, paths, domain = _database(tmp_path)
    store = ResourceStore(paths, ResourceRepository(database))
    course = CourseId("synthetic", "course-classification")
    content = ContentId("synthetic", "content-classification")
    domain.put_course(course, code="PH0005", title="Synthetic classification")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Synthetic files",
        position=0,
    )
    run = _run(database, "synthetic")
    first = store.ingest(
        BytesIO(_pdf(b"classification one")),
        AttachmentId("synthetic", "classified-resource"),
        content_id=content,
        sync_run_key=run,
        display_title="Synthetic material",
        original_filename="synthetic.pdf",
    )
    repository = ClassificationRepository(database)
    classified = ClassificationService(repository, RuleBasedMaterialClassifier()).classify(
        ClassificationInput(
            first.resource.key,
            first.version.key,
            "Lecture Slides",
            "synthetic.pdf",
        )
    )
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)
    current = service.list_materials(CourseRef(remote_id=course))
    assert current.items[0].semantic_type == "lecture_slides"

    repository.select(classified.run.candidates[0].key, reason="synthetic selection")
    store.ingest(
        BytesIO(_pdf(b"classification two")),
        AttachmentId("synthetic", "classified-resource"),
        content_id=content,
        sync_run_key=run,
        display_title="Synthetic material",
        original_filename="synthetic.pdf",
    )
    revised = service.list_materials(CourseRef(remote_id=course))
    assert revised.items[0].version_number == 2
    assert revised.items[0].semantic_type is None


def test_result_serialization_fails_closed_and_orders_sets() -> None:
    class Secret:
        def __str__(self) -> str:
            raise AssertionError("unsupported objects must never be stringified")

    ordered = ResultEnvelope("test", items=(frozenset({"b", "a"}),)).to_dict()
    assert ordered["items"] == [["a", "b"]]
    with pytest.raises(TypeError, match="unsupported value"):
        ResultEnvelope("test", items=(Secret(),)).to_dict()


def test_no_engine_sync_returns_typed_configuration_error(tmp_path: Path) -> None:
    database, paths, _ = _database(tmp_path)
    service = CoreService(database, runtime_paths=paths, now=lambda: NOW)
    window = TimeWindow(NOW, NOW + timedelta(days=1))

    result = service.quick_sync(None, SyncPolicy(window, fetch_resources=False))
    assert result.errors[0].category is ErrorCategory.CONFIGURATION_REQUIRED
    assert result.errors[0].code == "sync_engine_unavailable"
    assert result.to_dict()["errors"][0]["message"] == (
        "A configured read-only sync engine is required."
    )
