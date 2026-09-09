"""Synthetic M8 regressions for conservative resource lifecycle reconciliation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ntulearn_skill.client import (
    AuthorizedReadSession,
    CapabilityState,
    ReadPurpose,
    ResourceMetadataRecord,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    SourceUnavailable,
    TimeWindow,
)
from ntulearn_skill.core import (
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    ObservationStatus,
)
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
    StorageError,
)
from ntulearn_skill.sync.lifecycle import LifecycleWarning, ResourceInventoryReconciler
from ntulearn_skill.sync.models import ScopeResult, SyncWarning
from ntulearn_skill.sync.resources import ResourceFetchService
from ntulearn_skill.sync.state import ScopeKey


def _time(day: int) -> datetime:
    return datetime(2030, 1, day, tzinfo=UTC)


@dataclass(frozen=True)
class _Harness:
    database: Database
    domain: DomainRepository
    resources: ResourceRepository
    reconciler: ResourceInventoryReconciler
    course: CourseId
    other_course: CourseId
    content_a: ContentId
    content_b: ContentId
    other_content: ContentId
    resource_a: AttachmentId
    resource_b: AttachmentId
    other_resource: AttachmentId


def _add_run(database: Database, key: int, at: datetime) -> None:
    with database.transaction() as connection:
        connection.execute(
            """INSERT INTO sync_run(
                sync_run_key, mode, requested_scope_json, started_at, ended_at, status
            ) VALUES (?, 'synthetic', '{}', ?, ?, 'SUCCEEDED')""",
            (key, at.isoformat(), at.isoformat()),
        )


@pytest.fixture
def harness(tmp_path: Path) -> _Harness:
    database = Database(tmp_path / "private-synthetic" / "metadata.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() >= 8
    resources = ResourceRepository(database)
    course = CourseId("synthetic", "lifecycle-course")
    other_course = CourseId("synthetic", "other-course")
    content_a = ContentId("synthetic", "content-a")
    content_b = ContentId("synthetic", "content-b")
    other_content = ContentId("synthetic", "other-content")
    resource_a = AttachmentId("synthetic", "attachment-a")
    resource_b = AttachmentId("synthetic", "attachment-b")
    other_resource = AttachmentId("synthetic", "other-attachment")
    _add_run(database, 1, _time(1))
    domain.put_course(course, code="PH0000", title="Example Physics Course", observed_at=_time(1))
    domain.put_course(
        other_course, code="CS0000", title="Example Computing Course", observed_at=_time(1)
    )
    domain.put_content_node(
        content_a,
        course_id=course,
        handler_kind="folder",
        title="Unit A",
        position=0,
        observed_at=_time(1),
    )
    domain.put_content_node(
        content_b,
        course_id=course,
        handler_kind="folder",
        title="Unit B",
        position=1,
        observed_at=_time(1),
    )
    domain.put_content_node(
        other_content,
        course_id=other_course,
        handler_kind="folder",
        title="Other Unit",
        position=0,
        observed_at=_time(1),
    )
    for ordinal, (resource, content) in enumerate(
        (
            (resource_a, content_a),
            (resource_b, content_b),
            (other_resource, other_content),
        ),
        start=1,
    ):
        digest = str(ordinal) * 64
        resources.record_verified(
            resource,
            content_id=content,
            sync_run_key=1,
            display_title=f"Synthetic Resource {ordinal}",
            original_filename=f"synthetic-{ordinal}.bin",
            sha256=digest,
            byte_size=ordinal,
            file_format="unknown",
            declared_mime=None,
            blob_relpath=f"objects/{digest}",
            safe_display_filename=f"synthetic-{ordinal}.bin",
            observed_at=_time(1),
            downloaded_at=_time(1),
        )
    resource_b_version = resources.get_current_version(resource_b)
    assert resource_b_version is not None
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO parsed_document(
                version_key, resource_sha256, parser_name, parser_version, engine_version,
                settings_hash, settings_json, status, coverage, parsed_at
            ) VALUES (?, ?, 'synthetic', '1', '1', ?, '{}', 'COMPLETE', 'COMPLETE', ?)
            """,
            (resource_b_version.key, resource_b_version.sha256, "a" * 64, _time(1).isoformat()),
        )
        assert cursor.lastrowid is not None
        connection.execute(
            """INSERT INTO document_chunk(
                parse_key, ordinal, kind, native_text, locator_json
            ) VALUES (?, 0, 'other', 'synthetic chunk', '{}')""",
            (int(cursor.lastrowid),),
        )
    return _Harness(
        database,
        domain,
        resources,
        ResourceInventoryReconciler(database),
        course,
        other_course,
        content_a,
        content_b,
        other_content,
        resource_a,
        resource_b,
        other_resource,
    )


def _scope(
    harness: _Harness,
    run_key: int,
    *,
    coverage: Coverage = Coverage.COMPLETE,
    pagination_complete: bool = True,
    items_seen: int = 1,
    warnings: tuple[SyncWarning, ...] = (),
    windowed: bool = False,
) -> tuple[ScopeKey, ScopeResult]:
    observed_at = _time(run_key)
    scope = ScopeKey(
        "synthetic",
        harness.course,
        "content",
        TimeWindow(_time(1), _time(2)) if windowed else None,
    )
    return scope, ScopeResult(
        provider="synthetic",
        course_id=harness.course,
        data_kind="content",
        coverage=coverage,
        pages_seen=1,
        items_seen=items_seen,
        pagination_complete=pagination_complete,
        failure_category=None if coverage is Coverage.COMPLETE else "synthetic_incomplete",
        warnings=warnings,
        observed_at=observed_at,
    )


def _reconcile(
    harness: _Harness,
    run_key: int,
    *,
    seen_resources: tuple[AttachmentId, ...] | None = None,
    seen_content: tuple[ContentId, ...] | None = None,
    coverage: Coverage = Coverage.COMPLETE,
    pagination_complete: bool = True,
    warnings: tuple[SyncWarning, ...] = (),
    windowed: bool = False,
):
    content = (harness.content_a,) if seen_content is None else seen_content
    scope, result = _scope(
        harness,
        run_key,
        coverage=coverage,
        pagination_complete=pagination_complete,
        items_seen=len(content),
        warnings=warnings,
        windowed=windowed,
    )
    return harness.reconciler.reconcile(
        run_key,
        harness.course,
        result,
        (harness.resource_a,) if seen_resources is None else seen_resources,
        scope=scope,
        seen_content_ids=content,
    )


def _resource_observations(database: Database, resource_key: int) -> list[sqlite3.Row]:
    connection = database.connect()
    try:
        return connection.execute(
            "SELECT * FROM resource_observation WHERE resource_key = ? ORDER BY observation_key",
            (resource_key,),
        ).fetchall()
    finally:
        connection.close()


def test_complete_full_inventory_marks_only_omitted_resource_and_retains_history(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 2, _time(2))
    omitted_before = harness.resources.get_resource(harness.resource_b)
    assert omitted_before is not None
    version_before = harness.resources.get_current_version(harness.resource_b)

    result = _reconcile(harness, 2)

    omitted = harness.resources.get_resource(harness.resource_b)
    seen = harness.resources.get_resource(harness.resource_a)
    assert omitted is not None and omitted.availability is Availability.MISSING
    assert omitted.last_observed_at == omitted_before.last_observed_at == _time(1)
    assert seen is not None and seen.availability is Availability.ACTIVE
    assert harness.resources.get_current_version(harness.resource_b) == version_before
    assert harness.resources.list_versions(harness.resource_b) == (version_before,)
    assert version_before is not None and version_before.browse_relpath
    observations = _resource_observations(harness.database, omitted.key)
    assert [row["observation_status"] for row in observations] == ["OBSERVED", "NOT_OBSERVED"]
    assert observations[-1]["availability"] == "MISSING"
    assert observations[-1]["version_key"] is None
    connection = harness.database.connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM document_chunk").fetchone()[0] == 1
    finally:
        connection.close()
    content = harness.domain.get_content_node(harness.content_b)
    assert content is not None and content.availability is Availability.ACTIVE
    assert result.resources_marked_missing == 1
    assert result.uncertainty_observations == result.removal_candidates == 0


@pytest.mark.parametrize(
    "coverage,pagination_complete,warnings",
    [
        (Coverage.PARTIAL, False, (SyncWarning.PAGE_REPORTED_PARTIAL,)),
        (Coverage.UNKNOWN, False, (SyncWarning.PAGE_COVERAGE_UNKNOWN,)),
        (Coverage.STALE, True, ()),
        (Coverage.FAILED, False, (SyncWarning.SOURCE_FAILURE,)),
    ],
)
def test_incomplete_inventory_preserves_availability_and_appends_uncertainty(
    harness: _Harness,
    coverage: Coverage,
    pagination_complete: bool,
    warnings: tuple[SyncWarning, ...],
) -> None:
    _add_run(harness.database, 2, _time(2))
    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None
    previous = _resource_observations(harness.database, resource.key)[-1]

    result = _reconcile(
        harness,
        2,
        coverage=coverage,
        pagination_complete=pagination_complete,
        warnings=warnings,
    )

    retained = harness.resources.get_resource(harness.resource_b)
    observations = _resource_observations(harness.database, resource.key)
    assert retained is not None and retained.availability is Availability.ACTIVE
    assert retained.last_observed_at == _time(1)
    assert observations[-1]["observation_status"] == "UNKNOWN"
    assert observations[-1]["availability"] == "ACTIVE"
    assert observations[-1]["metadata_fingerprint"] == previous["metadata_fingerprint"]
    assert observations[-1]["original_filename"] == previous["original_filename"]
    assert result.resources_marked_missing == 0
    assert result.uncertainty_observations == 1
    assert result.warning_codes == (LifecycleWarning.INCOMPLETE_INVENTORY_PRESERVED,)


def test_windowed_complete_scope_cannot_mark_an_omission(harness: _Harness) -> None:
    _add_run(harness.database, 2, _time(2))

    result = _reconcile(harness, 2, windowed=True)

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.ACTIVE
    assert result.resources_marked_missing == 0
    assert result.uncertainty_observations == 1
    assert LifecycleWarning.INCOMPLETE_INVENTORY_PRESERVED in result.warning_codes


def test_unavailable_parent_prevents_resource_missing_transition(harness: _Harness) -> None:
    _add_run(harness.database, 2, _time(2))
    harness.domain.put_content_node(
        harness.content_b,
        course_id=harness.course,
        handler_kind="folder",
        title="Unit B",
        position=1,
        availability=Availability.UNAVAILABLE,
        observed_at=_time(2),
    )

    result = _reconcile(
        harness,
        2,
        seen_content=(harness.content_a, harness.content_b),
    )

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.ACTIVE
    assert result.uncertainty_observations == 1
    assert LifecycleWarning.UNAVAILABLE_PARENT_PRESERVED in result.warning_codes


def test_repeated_complete_omission_warns_but_never_confirms_removal(
    harness: _Harness,
) -> None:
    for run_key in (2, 3):
        _add_run(harness.database, run_key, _time(run_key))
        result = _reconcile(harness, run_key)

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.MISSING
    assert resource.last_observed_at == _time(1)
    assert result.resources_marked_missing == 0
    assert result.removal_candidates == 1
    assert result.warning_codes == (LifecycleWarning.REMOVAL_CANDIDATE,)
    assert all(
        row["availability"] != "REMOVED_CONFIRMED"
        for row in _resource_observations(harness.database, resource.key)
    )

    count = len(_resource_observations(harness.database, resource.key))
    repeated_same_run = _reconcile(harness, 3)
    assert len(_resource_observations(harness.database, resource.key)) == count
    assert repeated_same_run.resources_marked_missing == 0
    assert repeated_same_run.removal_candidates == 0


def test_newer_reappearance_restores_active_without_losing_omission_history(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 2, _time(2))
    _reconcile(harness, 2)
    _add_run(harness.database, 3, _time(3))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=3,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(3),
    )

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.ACTIVE
    assert resource.last_observed_at == _time(3)
    assert [
        row["observation_status"] for row in _resource_observations(harness.database, resource.key)
    ] == ["OBSERVED", "NOT_OBSERVED", "OBSERVED"]
    assert len(harness.resources.list_versions(harness.resource_b)) == 1


def test_late_observation_cannot_reverse_newer_omission_but_newer_read_can(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 3, _time(3))
    _reconcile(harness, 3)
    missing = harness.resources.get_resource(harness.resource_b)
    assert missing is not None and missing.availability is Availability.MISSING
    assert missing.last_observed_at == _time(1)

    _add_run(harness.database, 2, _time(2))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=2,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(2),
    )
    late = harness.resources.get_resource(harness.resource_b)
    assert late is not None and late.availability is Availability.MISSING
    assert late.last_observed_at == _time(2)

    _add_run(harness.database, 4, _time(4))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=4,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(4),
    )
    current = harness.resources.get_resource(harness.resource_b)
    assert current is not None and current.availability is Availability.ACTIVE
    assert current.last_observed_at == _time(4)


def test_unknown_failure_cannot_reactivate_missing_resource(harness: _Harness) -> None:
    _add_run(harness.database, 2, _time(2))
    _reconcile(harness, 2)

    class Sessions:
        def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
            return AuthorizedReadSession("synthetic", frozenset({purpose}))

        def status(self) -> SessionStatus:
            return SessionStatus.READY

        def invalidate(self, reason: str) -> None:
            raise AssertionError(f"unexpected invalidation: {reason}")

    class FailingSource:
        provider_name = "synthetic"

        def capabilities(self) -> SourceCapabilities:
            return SourceCapabilities({SourceCapability.RESOURCE_STREAM: CapabilityState.SUPPORTED})

        def open_resource_stream(self, *_: object) -> None:
            raise SourceUnavailable()

    _add_run(harness.database, 3, _time(3))
    service = ResourceFetchService(
        Sessions(),
        FailingSource(),  # type: ignore[arg-type]
        ResourceStore(RuntimePaths(harness.database.path.parent), harness.resources),
        object(),  # type: ignore[arg-type]
    )
    failed = service.fetch(
        ResourceMetadataRecord(
            harness.resource_b,
            harness.content_b,
            "Synthetic Resource 2",
            "synthetic-2.bin",
        ),
        sync_run_key=3,
        verify=True,
        observed_at=_time(3),
    )

    assert failed.fetch_decision.value == "FAILED"
    assert failed.observation is not None
    assert failed.observation.observation_status is ObservationStatus.UNKNOWN
    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.MISSING
    assert resource.last_observed_at == _time(1)
    observations = _resource_observations(harness.database, resource.key)
    assert observations[-1]["observation_status"] == "UNKNOWN"
    assert observations[-1]["availability"] == "ACTIVE"


def test_initial_unknown_observation_does_not_create_active_availability(
    harness: _Harness,
) -> None:
    attachment = AttachmentId("synthetic", "failed-first-read")
    _add_run(harness.database, 3, _time(3))

    resource, observation = harness.resources.observe(
        attachment,
        content_id=harness.content_a,
        sync_run_key=3,
        display_title="Synthetic failed resource",
        original_filename="failed.bin",
        availability=Availability.ACTIVE,
        observation_status=ObservationStatus.UNKNOWN,
        observed_at=_time(3),
    )

    assert resource.availability is Availability.UNKNOWN
    assert observation.observation_status is ObservationStatus.UNKNOWN
    assert observation.availability is Availability.ACTIVE

    _add_run(harness.database, 2, _time(2))
    observed, version, _, created = harness.resources.record_verified(
        attachment,
        content_id=harness.content_a,
        sync_run_key=2,
        display_title="Synthetic observed resource",
        original_filename="observed.bin",
        sha256="f" * 64,
        byte_size=1,
        file_format="unknown",
        declared_mime=None,
        blob_relpath=f"objects/{'f' * 64}",
        safe_display_filename="observed.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(2),
        downloaded_at=_time(2),
    )

    assert created
    assert observed.availability is Availability.ACTIVE
    assert observed.display_title == "Synthetic observed resource"
    assert observed.first_observed_at == observed.last_observed_at == _time(2)
    assert observed.current_version_key == version.key


def test_newer_observed_metadata_and_version_resist_late_older_bytes(
    harness: _Harness,
) -> None:
    attachment = AttachmentId("synthetic", "ordered-observed-resource")
    _add_run(harness.database, 3, _time(3))
    _, newest, _, _ = harness.resources.record_verified(
        attachment,
        content_id=harness.content_a,
        sync_run_key=3,
        display_title="Newest synthetic title",
        original_filename="newest.bin",
        sha256="a" * 64,
        byte_size=3,
        file_format="unknown",
        declared_mime=None,
        blob_relpath=f"objects/{'a' * 64}",
        safe_display_filename="newest.bin",
        observed_at=_time(3),
        downloaded_at=_time(3),
    )

    _add_run(harness.database, 2, _time(2))
    _, older, _, created = harness.resources.record_verified(
        attachment,
        content_id=harness.content_a,
        sync_run_key=2,
        display_title="Older synthetic title",
        original_filename="older.bin",
        sha256="b" * 64,
        byte_size=2,
        file_format="unknown",
        declared_mime=None,
        blob_relpath=f"objects/{'b' * 64}",
        safe_display_filename="older.bin",
        observed_at=_time(2),
        downloaded_at=_time(2),
    )

    resource = harness.resources.get_resource(attachment)
    assert created and older.key != newest.key
    assert resource is not None
    assert resource.display_title == "Newest synthetic title"
    assert resource.first_observed_at == _time(2)
    assert resource.last_observed_at == _time(3)
    assert resource.current_version_key == newest.key
    assert len(harness.resources.list_versions(attachment)) == 2


def test_partial_unknown_does_not_block_delayed_authoritative_read(harness: _Harness) -> None:
    _add_run(harness.database, 4, _time(1))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=4,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.UNAVAILABLE,
        observation_status=ObservationStatus.UNAVAILABLE,
        observed_at=_time(1),
    )
    _add_run(harness.database, 3, _time(3))
    _reconcile(
        harness,
        3,
        coverage=Coverage.PARTIAL,
        pagination_complete=False,
        warnings=(SyncWarning.PAGE_REPORTED_PARTIAL,),
    )

    _add_run(harness.database, 2, _time(2))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=2,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(2),
    )

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.ACTIVE
    assert resource.last_observed_at == _time(2)


def test_equal_timestamp_uses_run_key_for_authoritative_order(harness: _Harness) -> None:
    _add_run(harness.database, 3, _time(3))
    _reconcile(harness, 3)

    _add_run(harness.database, 2, _time(3))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=2,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(3),
    )
    lower_run = harness.resources.get_resource(harness.resource_b)
    assert lower_run is not None and lower_run.availability is Availability.MISSING

    _add_run(harness.database, 4, _time(3))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=4,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(3),
    )
    higher_run = harness.resources.get_resource(harness.resource_b)
    assert higher_run is not None and higher_run.availability is Availability.ACTIVE


def test_late_old_complete_inventory_cannot_override_newer_active_observation(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 3, _time(3))
    harness.resources.observe(
        harness.resource_b,
        content_id=harness.content_b,
        sync_run_key=3,
        display_title="Synthetic Resource 2",
        original_filename="synthetic-2.bin",
        availability=Availability.ACTIVE,
        observed_at=_time(3),
    )
    _add_run(harness.database, 2, _time(2))

    result = _reconcile(harness, 2)

    resource = harness.resources.get_resource(harness.resource_b)
    assert resource is not None and resource.availability is Availability.ACTIVE
    assert resource.last_observed_at == _time(3)
    assert result.resources_marked_missing == 0
    observations = _resource_observations(harness.database, resource.key)
    assert datetime.fromisoformat(str(observations[-1]["observed_at"])) == _time(2)
    assert observations[-1]["observation_status"] == "NOT_OBSERVED"


def test_cross_course_seen_identity_rejects_the_whole_reconciliation(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 2, _time(2))
    target = harness.resources.get_resource(harness.resource_b)
    assert target is not None
    before = len(_resource_observations(harness.database, target.key))
    scope, result = _scope(harness, 2, items_seen=2)

    with pytest.raises(ValueError, match="inventory course"):
        harness.reconciler.reconcile(
            2,
            harness.course,
            result,
            (harness.resource_a, harness.other_resource),
            scope=scope,
            seen_content_ids=(harness.content_a, harness.other_content),
        )

    assert len(_resource_observations(harness.database, target.key)) == before
    assert harness.resources.get_resource(harness.resource_b) == target


def test_missing_or_mismatched_run_scope_is_rejected_before_writes(harness: _Harness) -> None:
    target = harness.resources.get_resource(harness.resource_b)
    assert target is not None
    before = len(_resource_observations(harness.database, target.key))
    scope, result = _scope(harness, 2)

    with pytest.raises(ValueError, match="run does not exist"):
        harness.reconciler.reconcile(
            2,
            harness.course,
            result,
            (harness.resource_a,),
            scope=scope,
            seen_content_ids=(harness.content_a,),
        )

    _add_run(harness.database, 2, _time(2))
    course = harness.domain.get_course(harness.course)
    assert course is not None
    connection = harness.database.connect()
    try:
        provider_key = connection.execute(
            "SELECT provider_key FROM source_provider WHERE name = 'synthetic'"
        ).fetchone()[0]
        connection.execute(
            """INSERT INTO sync_scope_result(
                sync_run_key, provider_key, course_key, data_kind, coverage,
                pages_seen, items_seen, pagination_complete, observed_at
            ) VALUES (2, ?, ?, 'content', 'PARTIAL', 1, 1, 0, ?)""",
            (provider_key, course.key, _time(2).isoformat()),
        )
    finally:
        connection.close()

    with pytest.raises(ValueError, match="recorded sync run"):
        harness.reconciler.reconcile(
            2,
            harness.course,
            result,
            (harness.resource_a,),
            scope=scope,
            seen_content_ids=(harness.content_a,),
        )
    assert len(_resource_observations(harness.database, target.key)) == before


def test_mid_reconciliation_storage_failure_rolls_back_all_lifecycle_writes(
    harness: _Harness,
) -> None:
    _add_run(harness.database, 2, _time(2))
    connection = harness.database.connect()
    try:
        resource_b = harness.resources.get_resource(harness.resource_b)
        assert resource_b is not None
        connection.execute(
            f"""CREATE TRIGGER synthetic_lifecycle_failure
            BEFORE INSERT ON resource_observation
            WHEN NEW.sync_run_key = 2 AND NEW.resource_key = {resource_b.key}
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"""
        )
    finally:
        connection.close()
    before = {
        identifier: harness.resources.get_resource(identifier)
        for identifier in (harness.resource_a, harness.resource_b)
    }

    with pytest.raises(StorageError, match="lifecycle"):
        _reconcile(harness, 2, seen_resources=(), seen_content=())

    assert {
        identifier: harness.resources.get_resource(identifier)
        for identifier in (harness.resource_a, harness.resource_b)
    } == before
    connection = harness.database.connect()
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM resource_observation WHERE sync_run_key = 2"
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
