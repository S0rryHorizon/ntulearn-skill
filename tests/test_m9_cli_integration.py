"""Synthetic SQLite integration checks for the M9 CLI bindings."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_search_adversarial import _course, _harness, _ingest_and_parse

from ntulearn_skill.cli import run
from ntulearn_skill.client import AnnouncementSourceRecord, AssessmentSourceRecord
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
)
from ntulearn_skill.core.api import CoreService
from ntulearn_skill.events import EventRepository
from ntulearn_skill.integrations.codex import CodexToolDispatcher
from ntulearn_skill.storage import Database, DomainRepository, ResourceRepository, RuntimePaths

NOW = datetime(2036, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def populated_runtime(tmp_path: Path) -> tuple[Path, int]:
    root = tmp_path / "private-synthetic-runtime"
    paths = RuntimePaths(root).ensure()
    database = Database(paths.database)
    domain = DomainRepository(database)
    assert domain.initialize() >= 9

    course = CourseId("synthetic", "course-one")
    content = ContentId("synthetic", "content-one")
    attachment = AttachmentId("synthetic", "resource-one")
    course_record = domain.put_course(
        course,
        code="PH0000",
        title="Synthetic Physics",
        observed_at=NOW,
    )
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Synthetic topic",
        position=0,
        observed_at=NOW,
    )
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-cli', '{}', ?, 'RUNNING')""",
            (NOW.isoformat(),),
        )
        assert cursor.lastrowid is not None
        sync_run_key = int(cursor.lastrowid)

    ResourceRepository(database).observe(
        attachment,
        content_id=content,
        sync_run_key=sync_run_key,
        display_title="Synthetic handout",
        original_filename="synthetic-handout.pdf",
        observed_at=NOW,
    )
    events = EventRepository(database)
    events.observe_announcement(
        AnnouncementSourceRecord(
            AnnouncementId("synthetic", "announcement-one"),
            course,
            "Synthetic notice",
            "A wholly invented announcement.",
            Availability.ACTIVE,
        ),
        sync_run_key=sync_run_key,
        observed_at=NOW,
    )
    events.observe_assessment(
        AssessmentSourceRecord(
            AssessmentId("synthetic", "assessment-one"),
            course,
            content,
            None,
            "Synthetic quiz",
            AssessmentSubtype.QUIZ,
            "A wholly invented assessment.",
            Availability.ACTIVE,
        ),
        sync_run_key=sync_run_key,
        observed_at=NOW,
    )
    with database.connect() as connection:
        source_object_key = int(
            connection.execute(
                """SELECT source_object_key FROM source_object
                WHERE object_kind = 'attachment' AND remote_key = 'resource-one'"""
            ).fetchone()[0]
        )
    assert course_record.key > 0
    return root, source_object_key


def _invoke(root: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    code = run(["--json", "--root", str(root), *arguments], stdout=output)
    return code, json.loads(output.getvalue())


def test_basic_read_commands_bind_to_an_initialized_synthetic_database(
    populated_runtime: tuple[Path, int],
) -> None:
    root, source_object_key = populated_runtime
    courses_code, courses = _invoke(root, "courses")
    assert courses_code == 2
    assert courses["items"][0]["code"] == "PH0000"  # type: ignore[index]
    course_key = str(courses["items"][0]["local_key"])  # type: ignore[index]

    expected = (
        (("materials", course_key), "Synthetic handout"),
        (("resource", "1"), "Synthetic handout"),
        (("search", "Synthetic handout"), "Synthetic handout"),
        (("announcements", course_key), "Synthetic notice"),
        (("assessments", course_key), "Synthetic quiz"),
    )
    for arguments, marker in expected:
        code, payload = _invoke(root, *arguments)
        assert code in {0, 2}
        assert marker in json.dumps(payload["items"], ensure_ascii=False)

    source_code, source = _invoke(root, "source", str(source_object_key), "--kind", "source_object")
    assert source_code == 0
    assert "resource-one" in json.dumps(source["items"], ensure_ascii=False)


def test_default_path_redaction_survives_real_core_serialization(
    populated_runtime: tuple[Path, int],
) -> None:
    root, _source_object_key = populated_runtime

    _code, payload = _invoke(root, "resource", "1")

    assert "local_path" not in payload["items"][0]  # type: ignore[index]
    assert str(root) not in json.dumps(payload)


def test_sync_without_a_configured_source_returns_a_typed_safe_error(
    populated_runtime: tuple[Path, int],
) -> None:
    root, _source_object_key = populated_runtime
    _courses_code, courses = _invoke(root, "courses")
    course_key = str(courses["items"][0]["local_key"])  # type: ignore[index]

    code, payload = _invoke(root, "sync", course_key)

    assert code == 1
    assert payload["errors"] == [
        {
            "category": "configuration_required",
            "code": "sync_engine_unavailable",
            "message": "A configured read-only sync engine is required.",
            "operation": "sync_course",
            "scope": "sync",
            "retryable": True,
            "coverage_impact": "FAILED",
        }
    ]
    assert str(root) not in json.dumps(payload)


class _NoRefreshEngine:
    def __init__(self, database: Database) -> None:
        self.domain = DomainRepository(database)
        self.source = SimpleNamespace(provider_name="synthetic")
        self.calls: list[object] = []

    def refresh_scope(self, scope: object) -> None:
        self.calls.append(scope)
        raise AssertionError("cache-only search must not call the provider")


@pytest.fixture
def versioned_search(tmp_path: Path) -> tuple[CoreService, _NoRefreshEngine, int, int, set[int]]:
    harness = _harness(tmp_path)
    course_id, content_id = _course(
        harness, "current-only", code="PH0001", title="Invented Versioned Course"
    )
    old, _ = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="versioned-document",
        pages=("Legacyversionbeacon exists only in the first invented version.",),
        minute=1,
    )
    new, _ = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="versioned-document",
        pages=("Currentversionbeacon appears in the new invented version.",),
        minute=2,
    )
    another, _ = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="another-document",
        pages=("Currentversionbeacon appears in another current document.",),
        minute=3,
    )
    course_record = harness.domain.get_course(course_id)
    assert course_record is not None
    engine = _NoRefreshEngine(harness.database)
    service = CoreService(
        harness.database,
        runtime_paths=harness.paths,
        sync_engine=engine,  # type: ignore[arg-type]
        now=lambda: NOW,
    )
    return (
        service,
        engine,
        course_record.key,
        old.version.key,
        {
            new.version.key,
            another.version.key,
        },
    )


def _search_cli(service: CoreService, *arguments: str) -> dict[str, object]:
    output = io.StringIO()
    code = run(["--json", "search", *arguments], service=service, stdout=output)
    assert code in {0, 1, 2, 3}
    return json.loads(output.getvalue())


@pytest.mark.parametrize("scoped", [False, True])
def test_current_only_cli_uses_real_versioned_index_without_refresh(
    versioned_search: tuple[CoreService, _NoRefreshEngine, int, int, set[int]], scoped: bool
) -> None:
    service, engine, course_key, old_key, new_keys = versioned_search
    scope = ("--course", str(course_key)) if scoped else ()
    default = _search_cli(service, "Legacyversionbeacon", *scope)
    current_old = _search_cli(service, "Legacyversionbeacon", *scope, "--current-only")
    current_new = _search_cli(service, "Currentversionbeacon", *scope, "--current-only")

    assert [item["version_key"] for item in default["items"]] == [old_key]  # type: ignore[index]
    assert current_old["items"] == []
    assert {item["version_key"] for item in current_new["items"]} == new_keys  # type: ignore[index]
    assert all(item["source"] for item in current_new["items"])  # type: ignore[index]
    assert default["refresh_attempted"] is current_old["refresh_attempted"] is False
    assert current_new["refresh_attempted"] is False
    assert default["source_completeness"] == current_old["source_completeness"]
    assert default["freshness"] == current_old["freshness"]
    assert engine.calls == []


@pytest.mark.parametrize("scoped", [False, True])
def test_current_only_dispatcher_uses_real_versioned_index_and_strict_boolean(
    versioned_search: tuple[CoreService, _NoRefreshEngine, int, int, set[int]], scoped: bool
) -> None:
    service, engine, course_key, old_key, new_keys = versioned_search
    dispatcher = CodexToolDispatcher(service)
    scope = {"course_key": course_key} if scoped else {}
    default = dispatcher.call("search", {"query": "Legacyversionbeacon", **scope})
    explicit_false = dispatcher.call(
        "search", {"query": "Legacyversionbeacon", "current_only": False, **scope}
    )
    current_old = dispatcher.call(
        "search", {"query": "Legacyversionbeacon", "current_only": True, **scope}
    )
    current_new = dispatcher.call(
        "search", {"query": "Currentversionbeacon", "current_only": True, **scope}
    )

    assert [item["version_key"] for item in default["items"]] == [old_key]  # type: ignore[index]
    assert explicit_false["items"] == default["items"]
    assert current_old["items"] == []
    assert {item["version_key"] for item in current_new["items"]} == new_keys  # type: ignore[index]
    assert default["source_completeness"] == current_old["source_completeness"]
    assert default["freshness"] == current_old["freshness"]
    assert all(
        result["refresh_attempted"] is False
        for result in (default, explicit_false, current_old, current_new)
    )
    assert engine.calls == []


@pytest.mark.parametrize("scoped", [False, True])
def test_current_only_real_cursor_stays_in_scope_and_rejects_changed_flag(
    versioned_search: tuple[CoreService, _NoRefreshEngine, int, int, set[int]], scoped: bool
) -> None:
    service, engine, course_key, _old_key, new_keys = versioned_search
    scope = ("--course", str(course_key)) if scoped else ()
    first = _search_cli(service, "Currentversionbeacon", *scope, "--current-only", "--limit", "1")
    cursor = first["next_cursor"]
    assert isinstance(cursor, str)
    second = _search_cli(
        service,
        "Currentversionbeacon",
        *scope,
        "--current-only",
        "--limit",
        "1",
        "--cursor",
        cursor,
    )
    changed_flag = _search_cli(
        service, "Currentversionbeacon", *scope, "--limit", "1", "--cursor", cursor
    )
    assert {first["items"][0]["version_key"], second["items"][0]["version_key"]} == new_keys  # type: ignore[index]
    assert changed_flag["errors"]
    assert changed_flag["items"] == []

    dispatcher = CodexToolDispatcher(service)
    tool_scope = {"course_key": course_key} if scoped else {}
    tool_first = dispatcher.call(
        "search",
        {"query": "Currentversionbeacon", "current_only": True, "limit": 1, **tool_scope},
    )
    tool_cursor = tool_first["next_cursor"]
    assert isinstance(tool_cursor, str)
    tool_second = dispatcher.call(
        "search",
        {
            "query": "Currentversionbeacon",
            "current_only": True,
            "limit": 1,
            "cursor": tool_cursor,
            **tool_scope,
        },
    )
    tool_changed = dispatcher.call(
        "search",
        {"query": "Currentversionbeacon", "limit": 1, "cursor": tool_cursor, **tool_scope},
    )
    assert {
        tool_first["items"][0]["version_key"],  # type: ignore[index]
        tool_second["items"][0]["version_key"],  # type: ignore[index]
    } == new_keys
    assert tool_changed["errors"] and tool_changed["items"] == []
    assert engine.calls == []
