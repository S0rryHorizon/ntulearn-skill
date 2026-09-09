"""Synthetic SQLite integration checks for the M9 CLI bindings."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

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
from ntulearn_skill.events import EventRepository
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
