from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest

from ntulearn_skill.core.identifiers import AttachmentId, ContentId, CourseId
from ntulearn_skill.storage.database import Database
from ntulearn_skill.storage.paths import RuntimePaths
from ntulearn_skill.storage.repository import DomainRepository
from ntulearn_skill.storage.resources import (
    InvalidResourcePayload,
    ResourceRepository,
    ResourceStorageError,
    ResourceStore,
)


@dataclass(frozen=True)
class ResourceHarness:
    paths: RuntimePaths
    database: Database
    repository: ResourceRepository
    store: ResourceStore
    content_id: ContentId
    sync_run_key: int


class InterruptedStream:
    def __init__(self, prefix: bytes, private_marker: str) -> None:
        self.prefix = prefix
        self.private_marker = private_marker
        self.read_count = 0

    def read(self, _size: int = -1) -> bytes:
        self.read_count += 1
        if self.read_count == 1:
            return self.prefix
        raise OSError(f"synthetic stream failure: {self.private_marker}")


def _synthetic_pdf(label: bytes = b"invented") -> bytes:
    """Build a tiny valid one-page PDF without copying a real document."""

    content = b"q\n% " + label + b"\nQ\n"
    objects = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 72 72] /Resources << >> /Contents 4 0 R >>",
        b"<< /Length "
        + str(len(content)).encode("ascii")
        + b" >>\nstream\n"
        + content
        + b"endstream",
    )
    result = bytearray(b"%PDF-1.4\n% synthetic-only\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode("ascii"))
        result.extend(body)
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        result.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def _table_count(database: Database, table: str) -> int:
    allowed = {"source_object", "resource", "resource_version", "resource_observation"}
    if table not in allowed:
        raise ValueError("unsupported test table")
    connection = database.connect()
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def _regular_files(root: Path) -> tuple[Path, ...]:
    if not root.exists():
        return ()
    return tuple(path for path in root.rglob("*") if path.is_file() and not path.is_symlink())


@pytest.fixture
def harness(tmp_path: Path) -> ResourceHarness:
    paths = RuntimePaths(tmp_path / "private-runtime")
    database = Database(paths.database)
    repository = ResourceRepository(database)
    store = ResourceStore(paths, repository)
    assert store.initialize() == 6

    domain = DomainRepository(database)
    course_id = CourseId("synthetic", "adversarial-course")
    content_id = ContentId("synthetic", "adversarial-content")
    domain.put_course(course_id, code="PH0000", title="Invented Physics Course")
    domain.put_content_node(
        content_id,
        course_id=course_id,
        handler_kind="document",
        title="Invented Materials",
        position=0,
    )
    timestamp = datetime(2027, 1, 8, tzinfo=UTC).isoformat(timespec="microseconds")
    connection = database.connect()
    try:
        cursor = connection.execute(
            """
            INSERT INTO sync_run(mode, requested_scope_json, started_at, ended_at, status)
            VALUES ('synthetic', '{}', ?, ?, 'SUCCEEDED')
            """,
            (timestamp, timestamp),
        )
        sync_run_key = int(cursor.lastrowid)
        connection.commit()
    finally:
        connection.close()
    return ResourceHarness(paths, database, repository, store, content_id, sync_run_key)


def _ingest(
    harness: ResourceHarness,
    payload: bytes,
    *,
    attachment: str = "adversarial-attachment",
):
    return harness.store.ingest(
        BytesIO(payload),
        AttachmentId("synthetic", attachment),
        content_id=harness.content_id,
        sync_run_key=harness.sync_run_key,
        display_title="Invented Resource",
        original_filename="invented-source.pdf",
        declared_mime="application/pdf",
    )


def test_interrupted_stream_leaves_no_temp_blob_or_metadata(
    harness: ResourceHarness,
) -> None:
    private_marker = "private-looking-stream-marker"
    stream = InterruptedStream(b"partial synthetic bytes", private_marker)

    with pytest.raises(ResourceStorageError) as caught:
        harness.store.ingest(
            stream,
            AttachmentId("synthetic", "interrupted-attachment"),
            content_id=harness.content_id,
            sync_run_key=harness.sync_run_key,
            display_title="Interrupted Invented Resource",
            original_filename="interrupted.pdf",
        )

    assert str(caught.value) == "resource ingestion failed"
    assert private_marker not in str(caught.value)
    assert _regular_files(harness.paths.root / "tmp") == ()
    assert _regular_files(harness.paths.root / "objects") == ()
    assert _table_count(harness.database, "resource") == 0
    assert _table_count(harness.database, "resource_version") == 0
    assert _table_count(harness.database, "resource_observation") == 0


def test_size_limit_rejects_stream_without_leaving_partial_state(
    harness: ResourceHarness,
) -> None:
    limited = ResourceStore(harness.paths, harness.repository, maximum_bytes=32)

    with pytest.raises(InvalidResourcePayload, match="exceeds configured size"):
        limited.ingest(
            BytesIO(b"x" * 33),
            AttachmentId("synthetic", "oversize-attachment"),
            content_id=harness.content_id,
            sync_run_key=harness.sync_run_key,
            display_title="Oversize Invented Resource",
            original_filename="oversize.bin",
        )

    assert _regular_files(harness.paths.root / "tmp") == ()
    assert _regular_files(harness.paths.root / "objects") == ()
    assert _table_count(harness.database, "resource") == 0
    assert _table_count(harness.database, "resource_version") == 0
    assert _table_count(harness.database, "resource_observation") == 0


def test_existing_corrupt_canonical_blob_is_rejected_without_new_observation(
    harness: ResourceHarness,
) -> None:
    payload = _synthetic_pdf()
    first = _ingest(harness, payload)
    blob = harness.paths.root.joinpath(*Path(first.version.blob_relpath).parts)
    os.chmod(blob, 0o600)
    blob.write_bytes(b"z" * len(payload))
    observations_before = _table_count(harness.database, "resource_observation")

    with pytest.raises(ResourceStorageError) as caught:
        _ingest(harness, payload)

    assert str(caught.value) == "stored resource file failed verification"
    assert str(harness.paths.root) not in str(caught.value)
    assert _table_count(harness.database, "resource_version") == 1
    assert _table_count(harness.database, "resource_observation") == observations_before
    assert harness.repository.get_current_version(first.resource.remote_id) == first.version
    assert _regular_files(harness.paths.root / "tmp") == ()


def test_database_failure_retains_complete_unexposed_blob_for_safe_reuse(
    harness: ResourceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    attachment = "database-failure-attachment"
    first = _ingest(harness, _synthetic_pdf(b"database-baseline"), attachment=attachment)
    payload = _synthetic_pdf(b"database-failure")
    digest = hashlib.sha256(payload).hexdigest()
    orphan = harness.paths.root / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
    resources_before = _table_count(harness.database, "resource")
    versions_before = _table_count(harness.database, "resource_version")
    observations_before = _table_count(harness.database, "resource_observation")
    real_record = harness.repository.record_verified

    def fail_record(*_args: object, **_kwargs: object) -> None:
        raise ResourceStorageError("resource metadata operation failed")

    monkeypatch.setattr(harness.repository, "record_verified", fail_record)

    with pytest.raises(ResourceStorageError, match="resource metadata operation failed"):
        _ingest(harness, payload, attachment=attachment)

    assert _regular_files(harness.paths.root / "tmp") == ()
    if orphan.exists():
        assert orphan.read_bytes() == payload
        assert stat.S_IMODE(orphan.stat().st_mode) == 0o400
    assert _table_count(harness.database, "resource") == resources_before
    assert _table_count(harness.database, "resource_version") == versions_before
    assert _table_count(harness.database, "resource_observation") == observations_before
    assert harness.repository.get_current_version(first.resource.remote_id) == first.version

    monkeypatch.setattr(harness.repository, "record_verified", real_record)
    second = _ingest(harness, payload, attachment=attachment)
    assert second.created_version
    assert second.version.version_number == 2
    assert harness.repository.get_current_version(second.resource.remote_id) == second.version
    assert second.version.sha256 == digest
    assert len(_regular_files(harness.paths.root / "objects")) == 2


def test_postcommit_browse_failure_is_explicit_and_repairable(
    harness: ResourceHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = _ingest(harness, _synthetic_pdf(b"version-one"))
    current = harness.paths.root.joinpath(*Path(first.version.browse_relpath).parts).parent.parent
    current = current / "current"
    assert current.resolve().read_bytes() == _synthetic_pdf(b"version-one")

    real_materialize = harness.store._materialize_version

    def fail_materialize(*_args: object, **_kwargs: object) -> None:
        raise OSError("synthetic postcommit browse failure")

    monkeypatch.setattr(harness.store, "_materialize_version", fail_materialize)
    second_payload = _synthetic_pdf(b"version-two")
    second = _ingest(harness, second_payload)

    assert second.created_version
    assert not second.view_ready
    assert second.warning == "browse view requires repair"
    assert harness.repository.get_current_version(second.resource.remote_id) == second.version
    assert current.resolve().read_bytes() == _synthetic_pdf(b"version-one")

    monkeypatch.setattr(harness.store, "_materialize_version", real_materialize)
    assert harness.store.repair_views() == ()
    assert current.is_symlink()
    assert not current.readlink().is_absolute()
    assert current.resolve().read_bytes() == second_payload


def test_canonical_blob_symlink_is_rejected_without_touching_target(
    harness: ResourceHarness,
) -> None:
    payload = _synthetic_pdf(b"symlink-attack")
    digest = hashlib.sha256(payload).hexdigest()
    destination = harness.paths.root / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
    destination.parent.mkdir(mode=0o700, parents=True)
    outside = harness.paths.root.parent / "outside-target"
    outside.write_bytes(payload)
    destination.symlink_to(outside)

    with pytest.raises(ResourceStorageError) as caught:
        _ingest(harness, payload, attachment="symlink-attachment")

    assert str(caught.value) == "stored resource file is unsafe"
    assert str(outside) not in str(caught.value)
    assert outside.read_bytes() == payload
    assert destination.is_symlink()
    assert _table_count(harness.database, "resource") == 0
    assert _table_count(harness.database, "resource_version") == 0
    assert _table_count(harness.database, "resource_observation") == 0


def test_verified_versions_and_observations_are_immutable_evidence(
    harness: ResourceHarness,
) -> None:
    result = _ingest(harness, _synthetic_pdf(b"immutable"))
    blob = harness.paths.root.joinpath(*Path(result.version.blob_relpath).parts)
    browse = harness.paths.root.joinpath(*Path(result.version.browse_relpath).parts)

    assert stat.S_IMODE(blob.stat().st_mode) == 0o400
    assert stat.S_IMODE(browse.stat().st_mode) == 0o400
    assert blob.read_bytes() == browse.read_bytes()

    connection = harness.database.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="resource versions are immutable"):
            connection.execute(
                "UPDATE resource_version SET byte_size = byte_size + 1 WHERE version_key = ?",
                (result.version.key,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="resource versions are immutable"):
            connection.execute(
                "DELETE FROM resource_version WHERE version_key = ?", (result.version.key,)
            )
        with pytest.raises(sqlite3.IntegrityError, match="resource observations are immutable"):
            connection.execute(
                "UPDATE resource_observation SET original_filename = 'changed' "
                "WHERE observation_key = ?",
                (result.observation.key,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="resource observations are immutable"):
            connection.execute(
                "DELETE FROM resource_observation WHERE observation_key = ?",
                (result.observation.key,),
            )
    finally:
        connection.close()

    assert harness.repository.get_current_version(result.resource.remote_id) == result.version
