"""Synthetic checks for daily local-query coverage and material changes."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ntulearn_skill.cli import run
from ntulearn_skill.client import TimeWindow
from ntulearn_skill.core import AttachmentId, ContentId, CourseId, Coverage
from ntulearn_skill.core.api import CoreService, CourseRef, SourceLocatorRef
from ntulearn_skill.core.models import Availability, ObservationStatus
from ntulearn_skill.integrations.codex import CodexToolDispatcher
from ntulearn_skill.search import SourceReference, SourceReferenceKind
from ntulearn_skill.storage import Database, DomainRepository, ResourceRepository, RuntimePaths
from ntulearn_skill.sync.freshness import FreshnessRequirement
from ntulearn_skill.sync.state import ScopeKey, SyncAttemptOutcome, SyncStateRepository

NOW = datetime(2037, 3, 4, 5, 6, tzinfo=UTC)


def _sync_run(database: Database, observed_at: datetime) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-daily-query', '{}', ?, 'RUNNING')""",
            (observed_at.isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _runtime(tmp_path: Path) -> tuple[Path, CoreService, int]:
    root = tmp_path / "synthetic-private-runtime"
    paths = RuntimePaths(root).ensure()
    database = Database(paths.database)
    domain = DomainRepository(database)
    domain.initialize()
    course = CourseId("synthetic", "course-one")
    content = ContentId("synthetic", "content-one")
    resource = AttachmentId("synthetic", "resource-one")
    course_record = domain.put_course(
        course,
        code="PH0000",
        title="Invented Physics",
        observed_at=NOW - timedelta(days=20),
    )
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Invented lecture",
        position=0,
        observed_at=NOW - timedelta(days=20),
    )
    resources = ResourceRepository(database)
    first = NOW - timedelta(days=10)
    repeated = NOW - timedelta(days=5)
    changed = NOW - timedelta(days=1)
    common = {
        "content_id": content,
        "display_title": "Invented lecture notes",
        "original_filename": "invented-notes.pdf",
    }
    resources.observe(
        resource,
        sync_run_key=_sync_run(database, first),
        sanitized_metadata={"revision": "one"},
        observed_at=first,
        **common,
    )
    resources.observe(
        resource,
        sync_run_key=_sync_run(database, repeated),
        sanitized_metadata={"revision": "one"},
        observed_at=repeated,
        **common,
    )
    _record, observation = resources.observe(
        resource,
        sync_run_key=_sync_run(database, changed),
        sanitized_metadata={"revision": "two"},
        candidate_modified_at="2037-03-03T04:00:00Z",
        observed_at=changed,
        **common,
    )
    assert observation.key > 0
    return root, CoreService(database, runtime_paths=paths, now=lambda: NOW), course_record.key


def test_recent_changes_materials_require_a_changed_observation(tmp_path: Path) -> None:
    _root, service, course_key = _runtime(tmp_path)

    result = service.get_recent_material_changes(
        CourseRef(local_key=course_key),
        window=TimeWindow(NOW - timedelta(days=7), NOW),
    )

    assert [item.change_kinds for item in result.items] == [("METADATA_CHANGED",)]
    assert result.items[0].observed_at == NOW - timedelta(days=1)
    assert result.provenance[0].source_kind == "resource_observation"
    assert result.provenance[0].source_key == result.items[0].observation_key
    observation_coverage = next(
        item for item in result.coverage if item.data_kind == "resource_observations"
    )
    assert observation_coverage.window_since == NOW - timedelta(days=7)
    assert observation_coverage.window_until == NOW

    source = service.resolve_source(
        SourceLocatorRef(
            SourceReference(
                SourceReferenceKind.RESOURCE_OBSERVATION, result.items[0].observation_key
            )
        )
    )
    assert source.items[0].observation["sanitized_metadata"] == {"revision": "two"}


def test_incomplete_observation_is_not_a_material_update(tmp_path: Path) -> None:
    root, service, course_key = _runtime(tmp_path)
    repository = ResourceRepository(service.database)

    def observe(hours: int, status: ObservationStatus, availability: Availability) -> int:
        at = NOW - timedelta(hours=hours)
        _resource, observation = repository.observe(
            AttachmentId("synthetic", "resource-one"),
            content_id=ContentId("synthetic", "content-one"),
            sync_run_key=_sync_run(service.database, at),
            display_title="Invented lecture notes",
            original_filename="invented-notes.pdf",
            sanitized_metadata={"revision": "two"},
            observation_status=status,
            availability=availability,
            observed_at=at,
        )
        return observation.key

    unknown = observe(3, ObservationStatus.UNKNOWN, Availability.UNKNOWN)
    window = TimeWindow(NOW - timedelta(hours=4), NOW)
    assert service.get_recent_material_changes(CourseRef(local_key=course_key), window).items == ()
    observe(2, ObservationStatus.OBSERVED, Availability.ACTIVE)
    assert service.get_recent_material_changes(CourseRef(local_key=course_key), window).items == ()

    unavailable = observe(1, ObservationStatus.UNAVAILABLE, Availability.UNAVAILABLE)
    result = service.get_recent_material_changes(CourseRef(local_key=course_key), window)
    assert [(item.observation_key, item.change_kinds) for item in result.items] == [
        (unavailable, ("AVAILABILITY_CHANGED",))
    ]
    retained = service.resolve_source(
        SourceLocatorRef(SourceReference(SourceReferenceKind.RESOURCE_OBSERVATION, unknown))
    )
    assert retained.items[0].observation["observation_status"] == "UNKNOWN"
    output = io.StringIO()
    assert (
        run(
            ["--json", "--root", str(root), "recent-materials", str(course_key), "--days", "1"],
            stdout=output,
            now=lambda: NOW,
        )
        == 2
    )
    envelope = json.loads(output.getvalue())
    assert envelope["refresh_attempted"] is False
    assert all(item["observation_key"] != unknown for item in envelope["items"])


def test_metadata_only_observation_does_not_make_a_reused_version_look_new(
    tmp_path: Path,
) -> None:
    _root, service, course_key = _runtime(tmp_path)
    observations = (
        (NOW - timedelta(hours=10), "REUSED_VERIFIED"),
        (NOW - timedelta(hours=5), "DEFERRED"),
        (NOW - timedelta(hours=1), "REUSED_VERIFIED"),
    )
    run_keys = tuple(_sync_run(service.database, item[0]) for item in observations)
    with service.database.transaction() as connection:
        resource_key = int(connection.execute("SELECT resource_key FROM resource").fetchone()[0])
        fingerprint = str(
            connection.execute(
                """SELECT metadata_fingerprint FROM resource_observation
                ORDER BY observation_key DESC LIMIT 1"""
            ).fetchone()[0]
        )
        cursor = connection.execute(
            """INSERT INTO resource_version(
                resource_key, version_number, sha256, byte_size, file_format,
                downloaded_at, blob_relpath, browse_relpath, verification_status
            ) VALUES (?, 1, ?, 1, 'pdf', ?, 'objects/v1', 'courses/v1.pdf', 'VERIFIED')""",
            (resource_key, "e" * 64, (NOW - timedelta(hours=10)).isoformat()),
        )
        assert cursor.lastrowid is not None
        version_key = int(cursor.lastrowid)
        for index, ((observed_at, decision), run_key) in enumerate(
            zip(observations, run_keys, strict=True)
        ):
            observed_version = None if index == 1 else version_key
            connection.execute(
                """INSERT INTO resource_observation(
                    resource_key, sync_run_key, observation_status, original_filename,
                    sanitized_metadata_json, metadata_fingerprint, availability,
                    observed_at, fetch_decision, version_key
                ) VALUES (?, ?, 'OBSERVED', 'invented-notes.pdf', ?, ?, 'ACTIVE', ?, ?, ?)""",
                (
                    resource_key,
                    run_key,
                    json.dumps({"revision": "two"}),
                    fingerprint,
                    observed_at.isoformat(),
                    decision,
                    observed_version,
                ),
            )

    result = service.get_recent_material_changes(
        CourseRef(local_key=course_key),
        TimeWindow(NOW - timedelta(hours=7), NOW),
    )

    assert result.items == ()


def test_library_status_reports_local_progress_denominators(tmp_path: Path) -> None:
    _root, service, course_key = _runtime(tmp_path)

    with service.database.transaction() as connection:
        resource_key = int(connection.execute("SELECT resource_key FROM resource").fetchone()[0])
        cursor = connection.execute(
            """INSERT INTO resource_version(
                resource_key, version_number, sha256, byte_size, file_format,
                downloaded_at, blob_relpath, browse_relpath, verification_status
            ) VALUES (?, 1, ?, 1, 'pdf', ?, 'objects/synthetic',
                      'courses/synthetic.pdf', 'VERIFIED')""",
            (resource_key, "a" * 64, NOW.isoformat()),
        )
        assert cursor.lastrowid is not None
        version_key = int(cursor.lastrowid)
        connection.execute(
            "UPDATE resource SET current_version_key = ? WHERE resource_key = ?",
            (version_key, resource_key),
        )
        jobs = (
            (
                "parse",
                "b" * 64,
                json.dumps({"version_key": version_key}),
                "PENDING",
                None,
            ),
            (
                "reconcile",
                "c" * 64,
                json.dumps({"course_key": course_key}),
                "FAILED",
                "synthetic_failure",
            ),
            (
                "reconcile",
                "d" * 64,
                json.dumps({"course_key": course_key + 999}),
                "PENDING",
                None,
            ),
        )
        for kind, identity, payload, status, error in jobs:
            connection.execute(
                """INSERT INTO local_job(
                    job_kind, input_identity, payload_json, status, max_attempts,
                    available_at, last_error_category, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 3, ?, ?, ?, ?)""",
                (
                    kind,
                    identity,
                    payload,
                    status,
                    NOW.isoformat(),
                    error,
                    NOW.isoformat(),
                    NOW.isoformat(),
                ),
            )

    result = service.get_library_status(CourseRef(local_key=course_key))

    assert len(result.items) == 1
    item = result.items[0]
    assert item.course_key == course_key
    assert item.content_nodes == 1
    assert item.discovered_materials == 1
    assert item.downloaded_materials == 1
    assert item.parsed_materials == 0
    assert item.completely_parsed_materials == 0
    assert item.indexed_chunks == 0
    assert item.total_local_jobs == 2
    assert item.pending_local_jobs == 1
    assert item.running_local_jobs == 0
    assert item.succeeded_local_jobs == 0
    assert item.failed_local_jobs == 1


def test_all_course_library_status_preserves_each_known_course_freshness(tmp_path: Path) -> None:
    _root, service, _course_key = _runtime(tmp_path)
    course = CourseId("synthetic", "course-one")
    states = SyncStateRepository(service.database)
    current_run = _sync_run(service.database, NOW)
    old_run = _sync_run(service.database, NOW - timedelta(days=20))
    states.record_attempt(
        ScopeKey("synthetic", None, "courses"),
        run_key=current_run,
        attempted_at=NOW,
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=timedelta(days=1),
    )
    states.record_attempt(
        ScopeKey("synthetic", course, "content"),
        run_key=old_run,
        attempted_at=NOW - timedelta(days=20),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=timedelta(days=1),
    )

    result = service.get_library_status(
        freshness=FreshnessRequirement.with_max_age(timedelta(days=1))
    )

    assert result.completeness is not Coverage.COMPLETE
    assert any(item.status == "STALE" for item in result.freshness)
    assert {item.code for item in result.errors} == {
        "sync_engine_unavailable",
        "freshness_unsatisfied",
    }


def test_daily_support_cli_is_cache_only_and_source_resolvable(tmp_path: Path) -> None:
    root, _service, course_key = _runtime(tmp_path)
    output = io.StringIO()

    status_code = run(
        ["--json", "--root", str(root), "library-status", "--course", str(course_key)],
        stdout=output,
        now=lambda: NOW,
    )
    status = json.loads(output.getvalue())
    assert status_code == 2
    assert status["command"] == "library-status"
    assert status["items"][0]["discovered_materials"] == 1
    assert status["refresh_attempted"] is False

    output = io.StringIO()
    changes_code = run(
        [
            "--json",
            "--root",
            str(root),
            "recent-materials",
            str(course_key),
            "--days",
            "7",
        ],
        stdout=output,
        now=lambda: NOW,
    )
    changes = json.loads(output.getvalue())
    assert changes_code == 2
    assert changes["items"][0]["change_kinds"] == ["METADATA_CHANGED"]
    assert changes["refresh_attempted"] is False
    assert changes["provenance"][0]["source_kind"] == "resource_observation"


def test_codex_dispatcher_exposes_typed_daily_core_calls(tmp_path: Path) -> None:
    _root, service, course_key = _runtime(tmp_path)
    dispatcher = CodexToolDispatcher(service)

    status = dispatcher.call(
        "get_library_status",
        {"course_key": course_key, "freshness": {"mode": "cache_only"}},
    )
    changes = dispatcher.call(
        "get_recent_material_changes",
        {
            "course_key": course_key,
            "window_since": (NOW - timedelta(days=7)).isoformat(),
            "window_until": NOW.isoformat(),
            "freshness": {"mode": "cache_only"},
        },
    )

    assert status["operation"] == "get_library_status"
    assert status["items"][0]["discovered_materials"] == 1  # type: ignore[index]
    assert changes["operation"] == "get_recent_material_changes"
    assert changes["items"][0]["change_kinds"] == ["METADATA_CHANGED"]  # type: ignore[index]
