from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ntulearn_skill.cli import run
from ntulearn_skill.client import (
    BrowserCaptureBundle,
    BrowserCaptureProvider,
    BrowserCaptureSessionProvider,
    ReadPurpose,
    SessionExpired,
    SourceProtocolError,
    SourceUnavailable,
)
from ntulearn_skill.core import AttachmentId, ContentId, TemporalPrecision
from ntulearn_skill.storage import Database
from tests.browser_capture_fixture import synthetic_manifest, write_synthetic_browser_bundle


def _write_manifest(root: Path, payload: dict[str, object]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    return manifest


def _invoke(runtime: Path, manifest: Path, *arguments: str) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    code = run(
        [
            "--json",
            "--root",
            str(runtime),
            "--browser-capture",
            str(manifest),
            *arguments,
        ],
        stdout=output,
    )
    return code, json.loads(output.getvalue())


def test_manifest_maps_visible_fields_and_derives_unobserved_namespaces(tmp_path: Path) -> None:
    captured_at = datetime.now(UTC)
    manifest = write_synthetic_browser_bundle(tmp_path / "bundle", captured_at)

    bundle = BrowserCaptureBundle.load(manifest)

    assert bundle.course.remote_id.value == "_synthetic_course_1"
    file_node = next(item for item in bundle.content if item.remote_id.value == "_synthetic_file_1")
    assert file_node.resources[0].remote_id == AttachmentId(
        "ntulearn-browser-capture", "ui-file:_synthetic_file_1"
    )
    assessment = bundle.assessments[0]
    assessment_content = next(
        item for item in bundle.content if item.remote_id == assessment.content_id
    )
    assert assessment_content.handler_kind == "assessment:Assignment"
    assert assessment.remote_id.value == "ui-assessment:_synthetic_assessment_1"
    assert assessment.content_id == ContentId("ntulearn-browser-capture", "_synthetic_assessment_1")
    assert assessment.due_at is not None
    assert assessment.due_at.precision is TemporalPrecision.EXACT_TIME
    assert assessment.due_at.instant == datetime(2036, 9, 4, 15, 59, tzinfo=UTC)
    assert bundle.announcements[0].published_at is not None
    assert bundle.announcements[0].published_at.precision is TemporalPrecision.UNKNOWN


@pytest.mark.parametrize(
    ("identity_kind", "unsafe_value"),
    [
        ("course", "https://example.invalid/course?id=secret"),
        ("content", "content?token=secret"),
        ("announcement", "announcement#private"),
        ("grading", "/private/gradep"),
    ],
)
def test_manifest_rejects_transport_syntax_in_all_observed_identities(
    tmp_path: Path, identity_kind: str, unsafe_value: str
) -> None:
    captured_at = datetime.now(UTC)
    bundle = tmp_path / identity_kind
    manifest = write_synthetic_browser_bundle(bundle, captured_at)
    payload = synthetic_manifest(captured_at)
    if identity_kind == "course":
        payload["course"]["item"]["remote_id"] = unsafe_value  # type: ignore[index]
    elif identity_kind == "content":
        payload["content"]["items"][0]["remote_id"] = unsafe_value  # type: ignore[index]
    elif identity_kind == "announcement":
        payload["announcements"]["items"][0]["remote_id"] = unsafe_value  # type: ignore[index]
    else:
        payload["assessments"]["items"][0]["grading_column_remote_id"] = unsafe_value  # type: ignore[index]
    _write_manifest(bundle, payload)

    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(manifest)


@pytest.mark.parametrize(
    ("wording", "source_timezone", "precision", "expected"),
    [
        (
            "36/9/4 11:59 PM (UTC+8)",
            "UTC+8",
            TemporalPrecision.EXACT_TIME,
            datetime(2036, 9, 4, 15, 59, tzinfo=UTC),
        ),
        (
            "36/9/4 12:00 AM (UTC+8)",
            "UTC+8",
            TemporalPrecision.EXACT_TIME,
            datetime(2036, 9, 3, 16, 0, tzinfo=UTC),
        ),
        ("36/9/4 11:59 PM unexpected", "UTC+8", TemporalPrecision.UNKNOWN, None),
        ("36/9/4 11:59 PM (UTC+9)", "UTC+8", TemporalPrecision.UNKNOWN, None),
        ("36/9/4 11:59 PM", "Asia/Singapore", TemporalPrecision.UNKNOWN, None),
        ("36/9/4 11:59 PM", "UTC+08:99", TemporalPrecision.UNKNOWN, None),
    ],
)
def test_visible_time_parsing_is_bounded_around_ampm_and_timezone(
    tmp_path: Path,
    wording: str,
    source_timezone: str,
    precision: TemporalPrecision,
    expected: datetime | None,
) -> None:
    captured_at = datetime.now(UTC)
    bundle = tmp_path / str(len(list(tmp_path.iterdir())))
    write_synthetic_browser_bundle(bundle, captured_at)
    payload = synthetic_manifest(captured_at)
    payload["assessments"]["items"][0]["due_at"] = {  # type: ignore[index]
        "text": wording,
        "source_timezone": source_timezone,
    }
    manifest = _write_manifest(bundle, payload)

    due = BrowserCaptureBundle.load(manifest).assessments[0].due_at

    assert due is not None
    assert due.precision is precision
    assert due.instant == expected


def test_cli_rejects_unsafe_identity_without_echo_or_persistence(tmp_path: Path) -> None:
    captured_at = datetime.now(UTC)
    bundle = tmp_path / "bundle"
    write_synthetic_browser_bundle(bundle, captured_at)
    payload = synthetic_manifest(captured_at)
    private_identity = "https://example.invalid/item?token=private-value"
    payload["announcements"]["items"][0]["remote_id"] = private_identity  # type: ignore[index]
    manifest = _write_manifest(bundle, payload)
    runtime = tmp_path / "runtime"

    code, result = _invoke(runtime, manifest, "sync")

    assert code == 1
    assert result["errors"][0]["code"] == "source_protocol_error"  # type: ignore[index]
    assert private_identity not in json.dumps(result)
    with Database(runtime / "db" / "metadata.sqlite3").connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_object").fetchone()[0] == 0


def test_schema_rejects_complete_coverage_urls_and_path_escape(tmp_path: Path) -> None:
    captured_at = datetime.now(UTC)
    payload = synthetic_manifest(captured_at)
    payload["course"]["coverage"] = "COMPLETE"  # type: ignore[index]
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "complete", payload))

    payload = synthetic_manifest(captured_at)
    payload["course"]["source_page_path"] = "/course?token=private"  # type: ignore[index]
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "url", payload))

    payload = synthetic_manifest(captured_at)
    resource = payload["content"]["items"][1]["resources"][0]  # type: ignore[index]
    resource["file"]["relative_path"] = "../outside.pdf"
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "escape", payload))

    payload = synthetic_manifest(captured_at)
    payload["schema_version"] = True
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "bool-version", payload))

    payload = synthetic_manifest(captured_at + timedelta(minutes=10))
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "future", payload))

    payload = synthetic_manifest(captured_at)
    duplicate = dict(payload["content"]["items"][1]["resources"][0])  # type: ignore[index]
    duplicate.pop("file")
    payload["content"]["items"][1]["resources"].append(duplicate)  # type: ignore[index]
    (tmp_path / "duplicate" / "files").mkdir(parents=True)
    (tmp_path / "duplicate" / "files" / "synthetic-course-overview.pdf").write_bytes(b"x")
    with pytest.raises(SourceProtocolError, match="supported shape"):
        BrowserCaptureBundle.load(_write_manifest(tmp_path / "duplicate", payload))


def test_resource_open_revalidates_file_identity_after_bundle_load(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    root = tmp_path / "bundle"
    manifest = write_synthetic_browser_bundle(root, now)
    provider = BrowserCaptureProvider.from_manifest(manifest, now=lambda: now)
    sessions = BrowserCaptureSessionProvider(provider.bundle, now=lambda: now)
    resource = next(item.resources[0] for item in provider.bundle.content if item.resources)
    original = root / "files" / "synthetic-course-overview.pdf"
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"private replacement")
    original.unlink()
    original.symlink_to(outside)

    with pytest.raises(SourceUnavailable):
        provider.open_resource_stream(
            sessions.acquire(ReadPurpose.RESOURCE_STREAM), resource.remote_id
        )


def test_expired_capture_keeps_metadata_timestamp_but_rejects_resource_stream(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    captured_at = now - timedelta(days=2)
    manifest = write_synthetic_browser_bundle(tmp_path / "bundle", captured_at)
    provider = BrowserCaptureProvider.from_manifest(manifest, now=lambda: now)
    sessions = BrowserCaptureSessionProvider(provider.bundle, now=lambda: now)
    resource = next(item.resources[0] for item in provider.bundle.content if item.resources)

    assert sessions.status().value == "EXPIRED"
    with pytest.raises(SessionExpired):
        provider.open_resource_stream(
            sessions.acquire(ReadPurpose.RESOURCE_STREAM), resource.remote_id
        )

    runtime = tmp_path / "runtime"
    code, result = _invoke(runtime, manifest, "sync")
    assert code == 2
    assert result["completeness"] == "UNKNOWN"
    database = Database(runtime / "db" / "metadata.sqlite3")
    with database.connect() as connection:
        course_time = connection.execute("SELECT last_observed_at FROM course").fetchone()[0]
        state_time = connection.execute(
            "SELECT last_attempt_at FROM sync_state WHERE data_kind = 'content'"
        ).fetchone()[0]
    assert course_time == captured_at.isoformat(timespec="microseconds")
    assert state_time == captured_at.isoformat(timespec="microseconds")


def test_cli_sync_uses_capture_through_existing_pipeline_and_replay_is_idempotent(
    tmp_path: Path,
) -> None:
    captured_at = datetime.now(UTC)
    manifest = write_synthetic_browser_bundle(tmp_path / "bundle", captured_at)
    runtime = tmp_path / "runtime"

    first_code, first = _invoke(runtime, manifest, "sync", "--verify")
    assert first_code == 2
    assert first["completeness"] == "UNKNOWN"

    database = Database(runtime / "db" / "metadata.sqlite3")
    tables = ("resource_version", "parsed_document", "event_candidate", "event", "local_job")
    with database.connect() as connection:
        before = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        request = json.loads(
            connection.execute(
                "SELECT requested_scope_json FROM sync_run ORDER BY sync_run_key LIMIT 1"
            ).fetchone()[0]
        )
        assessment_count = int(connection.execute("SELECT COUNT(*) FROM assessment").fetchone()[0])
    assert before["resource_version"] == 1
    assert request["capture_id"] == "synthetic-capture-0001"
    assert request["capture_content_source_path"].endswith("/outline")
    assert assessment_count == 1

    second_code, second = _invoke(runtime, manifest, "sync", "--verify")
    assert second_code == 2
    assert second["completeness"] == "UNKNOWN"
    with database.connect() as connection:
        after = {
            table: int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }
        verified_times = {
            row[0]
            for row in connection.execute(
                "SELECT verified_at FROM resource_fetch_receipt WHERE verified_at IS NOT NULL"
            )
        }
        replay_receipt = connection.execute(
            """SELECT fetch_decision, policy_reason
            FROM resource_fetch_receipt ORDER BY fetch_receipt_key DESC LIMIT 1"""
        ).fetchone()
    assert after == before
    assert verified_times == {captured_at.isoformat(timespec="microseconds")}
    assert tuple(replay_receipt) == ("NOT_NEEDED", "within_verification_interval")
    assert any(
        warning["code"] == "capture_replay_assumed_not_reverified" for warning in second["warnings"]
    )


def test_cache_only_cli_does_not_open_supplied_capture(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    private_value = tmp_path / "missing-private-manifest.json"

    code, result = _invoke(runtime, private_value, "courses", "--freshness", "cache-only")

    assert code == 2
    assert result["errors"] == []
    assert os.fspath(private_value) not in json.dumps(result)
