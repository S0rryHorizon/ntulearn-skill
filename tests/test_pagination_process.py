"""Cross-process pagination checks over synthetic private runtimes."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ntulearn_skill.client import AnnouncementSourceRecord, ScheduleSourceRecord
from ntulearn_skill.core import (
    AnnouncementId,
    AttachmentId,
    Availability,
    CalendarItemId,
    ContentId,
    CourseId,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.events import DeterministicEventExtractor, EventReconciler, EventRepository
from ntulearn_skill.storage import Database, DomainRepository, ResourceRepository, RuntimePaths


def _runtime(tmp_path: Path, name: str = "private-synthetic-runtime") -> tuple[Path, Database]:
    root = tmp_path / name
    paths = RuntimePaths(root).ensure()
    database = Database(paths.database)
    assert DomainRepository(database).initialize() == 10
    return root, database


def _courses(database: Database, count: int = 3) -> None:
    domain = DomainRepository(database)
    for ordinal in range(count):
        domain.put_course(
            CourseId("synthetic", f"course-{ordinal}"),
            code="PH0000",
            title="Same synthetic title",
        )


def _cli(root: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    process = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ntulearn_skill.cli import main; raise SystemExit(main())",
            "--json",
            "--root",
            str(root),
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert process.stdout, process.stderr
    return process.returncode, json.loads(process.stdout)


def _item_ids(payload: dict[str, object], field: str) -> list[object]:
    items = payload["items"]
    assert isinstance(items, list)
    return [item[field] for item in items]


def _cursor(payload: dict[str, object]) -> str:
    cursor = payload["next_cursor"]
    assert isinstance(cursor, str) and cursor
    return cursor


def _reported_window(payload: dict[str, object]) -> tuple[str, str]:
    coverage = payload["coverage"]
    assert isinstance(coverage, list)
    windows = {
        (item["window_since"], item["window_until"])
        for item in coverage
        if item["window_since"] is not None and item["window_until"] is not None
    }
    assert len(windows) == 1
    since, until = windows.pop()
    assert isinstance(since, str) and isinstance(until, str)
    return since, until


def test_cli_cursor_survives_process_exit_and_wal_cleanup(tmp_path: Path) -> None:
    root, database = _runtime(tmp_path)
    _courses(database)
    keeper = database.connect()
    keeper.execute("SELECT count(*) FROM course").fetchone()
    wal = Path(f"{database.path}-wal")
    assert wal.exists()

    first_code, first = _cli(root, "courses", "--limit", "1")
    assert first_code == 2
    assert _item_ids(first, "local_key") == [1]
    assert wal.exists()

    keeper.close()
    assert not wal.exists()
    second_code, second = _cli(root, "courses", "--limit", "1", "--cursor", _cursor(first))
    third_code, third = _cli(root, "courses", "--limit", "1", "--cursor", _cursor(second))

    assert second_code == third_code == 2
    assert _item_ids(second, "local_key") == [2]
    assert _item_ids(third, "local_key") == [3]
    assert third["next_cursor"] is None
    assert third["truncated"] is False


def test_cli_search_rebuilds_a_dirty_index_before_issuing_a_cross_process_cursor(
    tmp_path: Path,
) -> None:
    root, database = _runtime(tmp_path)
    _courses(database)
    connection = database.connect()
    try:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.execute("SELECT count(*) FROM course").fetchone()[0] == 3
        connection.execute("DELETE FROM search_document")
        connection.execute(
            "UPDATE search_index_state SET indexed_generation = -1 WHERE singleton_key = 1"
        )
        connection.commit()
        state = connection.execute(
            """SELECT source_generation, indexed_generation,
                      (SELECT count(*) FROM search_document) AS document_count
               FROM search_index_state WHERE singleton_key = 1"""
        ).fetchone()
        assert state is not None
        assert int(state["source_generation"]) >= 0
        assert int(state["indexed_generation"]) == -1
        assert int(state["document_count"]) == 0
    finally:
        connection.close()

    first_code, first = _cli(root, "search", "synthetic", "--limit", "2")
    connection = database.connect()
    try:
        repaired = connection.execute(
            """SELECT source_generation, indexed_generation,
                      (SELECT count(*) FROM search_document) AS document_count,
                      (SELECT count(*) FROM course) AS course_count
               FROM search_index_state WHERE singleton_key = 1"""
        ).fetchone()
        assert repaired is not None
        assert int(repaired["source_generation"]) == int(repaired["indexed_generation"])
        assert int(repaired["document_count"]) == 3
        assert int(repaired["course_count"]) == 3
    finally:
        connection.close()
    second_code, second = _cli(
        root,
        "search",
        "synthetic",
        "--limit",
        "2",
        "--cursor",
        _cursor(first),
    )

    assert first_code == second_code == 2
    first_ids = _item_ids(first, "search_document_key")
    second_ids = _item_ids(second, "search_document_key")
    assert len(first_ids) == 2
    assert len(second_ids) == 1
    assert len(set(first_ids + second_ids)) == 3
    assert second["next_cursor"] is None


@pytest.mark.parametrize("mutation", ["insert", "delete", "sort_update"])
def test_cli_cursor_rejects_committed_sort_set_changes(tmp_path: Path, mutation: str) -> None:
    root, database = _runtime(tmp_path, f"private-{mutation}")
    _courses(database)
    _first_code, first = _cli(root, "courses", "--limit", "1")

    if mutation == "insert":
        DomainRepository(database).put_course(
            CourseId("synthetic", "inserted"), code="AA0000", title="Inserted"
        )
    else:
        with database.transaction() as connection:
            if mutation == "delete":
                connection.execute("DELETE FROM course WHERE course_key = 3")
            else:
                connection.execute("UPDATE course SET code = 'AA0000' WHERE course_key = 3")

    code, rejected = _cli(root, "courses", "--limit", "1", "--cursor", _cursor(first))

    assert code == 1
    assert rejected["items"] == []
    assert rejected["errors"][0]["category"] == "invalid_request"


def test_cli_cursor_binds_operation_limit_and_private_runtime(tmp_path: Path) -> None:
    root, database = _runtime(tmp_path, "private-origin")
    _courses(database)
    _first_code, first = _cli(root, "courses", "--limit", "1")
    cursor = _cursor(first)

    limit_code, limit_result = _cli(root, "courses", "--limit", "2", "--cursor", cursor)
    operation_code, operation_result = _cli(
        root, "announcements", "1", "--limit", "1", "--cursor", cursor
    )
    other_root = tmp_path / "private-byte-copy"
    other_paths = RuntimePaths(other_root).ensure()
    shutil.copyfile(database.path, other_paths.database)
    other_paths.database.chmod(0o600)
    assert other_paths.database.read_bytes() == database.path.read_bytes()
    runtime_code, runtime_result = _cli(other_root, "courses", "--limit", "1", "--cursor", cursor)

    assert limit_code == operation_code == runtime_code == 1
    for result in (limit_result, operation_result, runtime_result):
        assert result["errors"][0]["category"] == "invalid_request"


def test_cli_cursor_binds_course_identity(tmp_path: Path) -> None:
    root, database = _runtime(tmp_path, "private-course-binding")
    domain = DomainRepository(database)
    first_course = CourseId("synthetic", "first-course")
    second_course = CourseId("synthetic", "second-course")
    first_record = domain.put_course(first_course, code="PH0001", title="Synthetic first")
    second_record = domain.put_course(second_course, code="PH0002", title="Synthetic second")
    repository = EventRepository(database)
    observed_at = datetime.now(UTC)
    for course in (first_course, second_course):
        for ordinal in range(2):
            repository.observe_announcement(
                AnnouncementSourceRecord(
                    AnnouncementId("synthetic", f"{course.value}-{ordinal}"),
                    course,
                    f"Synthetic announcement {ordinal}",
                    "Synthetic body",
                    Availability.ACTIVE,
                ),
                sync_run_key=_sync_run(database, observed_at),
                observed_at=observed_at,
            )

    _first_code, first = _cli(root, "announcements", str(first_record.key), "--limit", "1")
    code, rejected = _cli(
        root,
        "announcements",
        str(second_record.key),
        "--limit",
        "1",
        "--cursor",
        _cursor(first),
    )

    assert code == 1
    assert rejected["errors"][0]["category"] == "invalid_request"


def _sync_run(database: Database, observed_at: datetime) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-pagination', '{}', ?, 'RUNNING')""",
            (observed_at.isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _recent_material_runtime(tmp_path: Path) -> tuple[Path, int]:
    root, database = _runtime(tmp_path, "private-recent")
    domain = DomainRepository(database)
    course = CourseId("synthetic", "recent-course")
    content = ContentId("synthetic", "recent-content")
    course_record = domain.put_course(course, code="PH0001", title="Synthetic recent course")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Synthetic content",
        position=0,
    )
    resources = ResourceRepository(database)
    now = datetime.now(UTC)
    for ordinal in range(3):
        observed_at = now - timedelta(hours=ordinal + 1)
        resources.observe(
            AttachmentId("synthetic", f"recent-{ordinal}"),
            content_id=content,
            sync_run_key=_sync_run(database, observed_at),
            display_title=f"Synthetic recent material {ordinal}",
            original_filename=f"synthetic-{ordinal}.pdf",
            sanitized_metadata={"revision": "one"},
            observed_at=observed_at,
        )
    return root, course_record.key


def _event_runtime(tmp_path: Path) -> Path:
    root, database = _runtime(tmp_path, "private-events")
    course = CourseId("synthetic", "event-course")
    DomainRepository(database).put_course(course, code="PH0002", title="Synthetic event course")
    repository = EventRepository(database)
    extractor = DeterministicEventExtractor(database)
    now = datetime.now(UTC)
    for ordinal in range(3):
        starts_at = now + timedelta(days=ordinal + 1)
        source_time = SourceTime(
            starts_at,
            starts_at.isoformat(),
            "UTC",
            TemporalPrecision.EXACT_TIME,
        )
        observation = repository.observe_schedule(
            ScheduleSourceRecord(
                CalendarItemId("synthetic", f"schedule-{ordinal}"),
                course,
                f"Synthetic class {ordinal}",
                Availability.ACTIVE,
                start_at=source_time,
                end_at=None,
                location="Synthetic room",
            ),
            sync_run_key=_sync_run(database, now),
            observed_at=now,
        )
        assert extractor.extract_observation(observation.observation.key).candidates
    assert len(EventReconciler(database).reconcile_course(course).events) == 3
    return root


def test_recent_cli_reports_and_reuses_its_actual_window(tmp_path: Path) -> None:
    root, course_key = _recent_material_runtime(tmp_path)
    first_code, first = _cli(
        root, "recent-materials", str(course_key), "--days", "7", "--limit", "1"
    )
    since, until = _reported_window(first)

    second_code, second = _cli(
        root,
        "recent-materials",
        str(course_key),
        "--limit",
        "1",
        "--window-since",
        since,
        "--window-until",
        until,
        "--cursor",
        _cursor(first),
    )

    assert first_code == second_code == 2
    assert _reported_window(second) == (since, until)
    assert set(_item_ids(first, "observation_key")).isdisjoint(_item_ids(second, "observation_key"))
    mismatch_code, mismatch = _cli(
        root,
        "recent-materials",
        str(course_key),
        "--limit",
        "1",
        "--window-since",
        since,
        "--window-until",
        (datetime.fromisoformat(until) + timedelta(seconds=1)).isoformat(),
        "--cursor",
        _cursor(first),
    )
    assert mismatch_code == 1
    assert mismatch["errors"][0]["category"] == "invalid_request"


@pytest.mark.parametrize(
    ("first_arguments", "continuation_command"),
    [
        (("upcoming", "--days", "7"), "upcoming"),
        (("events", "--next", "7d"), "events"),
    ],
)
def test_event_cli_reports_reusable_window_and_keeps_page_views_aligned(
    tmp_path: Path,
    first_arguments: tuple[str, ...],
    continuation_command: str,
) -> None:
    root = _event_runtime(tmp_path)
    first_code, first = _cli(root, *first_arguments, "--limit", "1")
    since, until = _reported_window(first)
    first_cursor = _cursor(first)
    items = first["items"]
    provenance = first["provenance"]
    assert isinstance(items, list) and len(items) == 1
    assert isinstance(provenance, list)
    source_keys = {source["evidence_key"] for source in items[0]["sources"]}
    assert {item["source_key"] for item in provenance} == source_keys
    assert items[0]["review"] is not None

    second_code, second = _cli(
        root,
        continuation_command,
        "--limit",
        "1",
        "--window-since",
        since,
        "--window-until",
        until,
        "--cursor",
        first_cursor,
    )

    assert first_code == second_code == 2
    assert _reported_window(second) == (since, until)
    assert set(_item_ids(first, "key")).isdisjoint(_item_ids(second, "key"))
    mismatch_code, mismatch = _cli(
        root,
        continuation_command,
        "--limit",
        "1",
        "--window-since",
        since,
        "--window-until",
        (datetime.fromisoformat(until) + timedelta(seconds=1)).isoformat(),
        "--cursor",
        first_cursor,
    )
    assert mismatch_code == 1
    assert mismatch["errors"][0]["category"] == "invalid_request"


def test_rolling_window_cursor_requires_matching_explicit_bounds(tmp_path: Path) -> None:
    root = _event_runtime(tmp_path)
    _first_code, first = _cli(root, "upcoming", "--days", "7", "--limit", "1")
    since, until = _reported_window(first)
    cursor = _cursor(first)

    missing_code, missing = _cli(root, "upcoming", "--window-since", since, "--cursor", cursor)
    mismatch_code, mismatch = _cli(
        root,
        "upcoming",
        "--limit",
        "1",
        "--window-since",
        since,
        "--window-until",
        (datetime.fromisoformat(until) + timedelta(seconds=1)).isoformat(),
        "--cursor",
        cursor,
    )

    assert missing_code == 64
    assert missing["errors"][0]["category"] == "invalid_request"
    assert mismatch_code == 1
    assert mismatch["errors"][0]["category"] == "invalid_request"
