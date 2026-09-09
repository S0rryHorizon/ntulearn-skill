from __future__ import annotations

import errno
import hashlib
import io
import os
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import ntulearn_skill.storage.resources as resource_module
from ntulearn_skill.core import (
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    FetchDecision,
)
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    InvalidResourcePayload,
    ResourceRepository,
    ResourceStorageError,
    ResourceStore,
    RuntimePaths,
    StorageError,
    detect_file_format,
    safe_filename,
)


def _pdf(label: bytes) -> bytes:
    result = bytearray(b"%PDF-1.4\n% synthetic-only\n")
    object_offset = len(result)
    result.extend(b"1 0 obj\n<< /Type /Catalog /Label (" + label + b") >>\nendobj\n")
    xref_offset = len(result)
    result.extend(b"xref\n0 2\n0000000000 65535 f \n")
    result.extend(f"{object_offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def _office(kind: str) -> bytes:
    target = "word/document.xml" if kind == "docx" else "ppt/presentation.xml"
    root = "document" if kind == "docx" else "presentation"
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types xmlns='urn:synthetic'/>")
        archive.writestr(target, f"<{root} xmlns='urn:synthetic'/>")
    return value.getvalue()


@pytest.fixture
def resource_context(
    tmp_path: Path,
) -> tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId]:
    paths = RuntimePaths(tmp_path / "private")
    database = Database(paths.database)
    domain = DomainRepository(database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() == 3
    course_id = CourseId("synthetic", "resource-course")
    content_id = ContentId("synthetic", "resource-content")
    domain.put_course(course_id, code="PH0000", title="Example Physics Course")
    domain.put_content_node(
        content_id,
        course_id=course_id,
        handler_kind="document",
        title="Synthetic resources",
        position=0,
    )
    return store, resources, database, paths, content_id


def _run(database: Database, offset: int = 0) -> int:
    timestamp = (datetime(2027, 1, 1, tzinfo=UTC) + timedelta(minutes=offset)).isoformat()
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('course', '{}', ?, 'RUNNING')
            """,
            (timestamp,),
        )
        return int(cursor.lastrowid)


def test_metadata_observations_are_append_only_and_need_no_binary(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, resources, database, paths, content_id = resource_context
    attachment = AttachmentId("synthetic", "metadata-only")
    run = _run(database)

    first_resource, first = store.observe(
        attachment,
        content_id=content_id,
        sync_run_key=run,
        display_title="Synthetic reading",
        original_filename="reading.pdf",
        sanitized_metadata={"content_type": "application/pdf"},
    )
    second_resource, second = store.observe(
        attachment,
        content_id=content_id,
        sync_run_key=run,
        display_title="Synthetic reading renamed",
        original_filename="renamed.pdf",
        fetch_decision=FetchDecision.NOT_NEEDED,
    )

    assert first.version_key is None
    assert second.version_key is None
    assert first.key != second.key
    assert first_resource.current_version_key is None
    assert second_resource.current_version_key is None
    assert resources.list_versions(attachment) == ()
    assert not any((paths.root / "objects").rglob("*"))


def test_equal_hash_after_rename_reuses_version_and_changed_hash_preserves_history(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, resources, database, paths, content_id = resource_context
    attachment = AttachmentId("synthetic", "versioned")
    first_bytes = _pdf(b"one")
    second_bytes = _pdf(b"two")

    first = store.ingest(
        io.BytesIO(first_bytes),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database, 1),
        display_title="Lecture slides",
        original_filename="slides.docx",
        observed_at=datetime(2027, 1, 1, 0, 1, tzinfo=UTC),
    )
    renamed = store.ingest(
        io.BytesIO(first_bytes),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database, 2),
        display_title="Lecture slides renamed",
        original_filename="renamed.exe",
        observed_at=datetime(2027, 1, 1, 0, 2, tzinfo=UTC),
    )
    changed = store.ingest(
        io.BytesIO(second_bytes),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database, 3),
        display_title="Lecture slides renamed",
        original_filename="renamed.exe",
        observed_at=datetime(2027, 1, 1, 0, 3, tzinfo=UTC),
    )

    assert first.created_version
    assert not renamed.created_version
    assert renamed.observation.fetch_decision is FetchDecision.REUSED_VERIFIED
    assert first.version.key == renamed.version.key
    assert first.version.browse_relpath.endswith("slides.pdf")
    assert changed.version.version_number == 2
    assert resources.get_current_version(attachment) == changed.version
    assert [item.sha256 for item in resources.list_versions(attachment)] == [
        hashlib.sha256(first_bytes).hexdigest(),
        hashlib.sha256(second_bytes).hexdigest(),
    ]
    assert all(
        (paths.root / item.blob_relpath).is_file() for item in resources.list_versions(attachment)
    )
    current = paths.root / changed.version.browse_relpath
    pointer = current.parent.parent / "current"
    assert pointer.is_symlink()
    assert pointer.resolve() == current


def test_older_ingestion_keeps_newer_authoritative_current(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, resources, database, paths, content_id = resource_context
    attachment = AttachmentId("synthetic", "ordered-resource")
    newer = store.ingest(
        io.BytesIO(_pdf(b"new")),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database, 2),
        display_title="New",
        original_filename="new.pdf",
        observed_at=datetime(2027, 1, 2, tzinfo=UTC),
    )
    older = store.ingest(
        io.BytesIO(_pdf(b"old")),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database, 1),
        display_title="Old",
        original_filename="old.pdf",
        observed_at=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert older.version.key != newer.version.key
    assert resources.get_current_version(attachment) == newer.version
    pointer = paths.root / newer.version.browse_relpath
    assert (pointer.parent.parent / "current").resolve() == pointer


@pytest.mark.parametrize(
    ("value", "file_format", "expected_suffix"),
    [
        ("../CON.docx", "pdf", ".pdf"),
        ("..\\NUL. ", "unknown", ".bin"),
        ("name\x00/part.exe", "pptx", ".pptx"),
        ("Ｆｉｌｅ．ｐｄｆ", "pdf", ".pdf"),
    ],
)
def test_safe_filename_rejects_path_meaning_and_uses_detected_extension(
    value: str, file_format: str, expected_suffix: str
) -> None:
    result = safe_filename(value, file_format, max_bytes=32)
    assert "/" not in result and "\\" not in result and "\x00" not in result
    assert result.endswith(expected_suffix)
    assert len(result.encode("utf-8")) <= 32
    assert result.rstrip(" .") == result


def test_filename_limit_must_fit_trusted_extension() -> None:
    with pytest.raises(ValueError, match="byte limit"):
        safe_filename("x", "docx", max_bytes=5)

    assert len(safe_filename("课.pdf", "pdf", max_bytes=5).encode("utf-8")) == 5
    reserved_after_truncation = safe_filename("CONx.pdf", "pdf", max_bytes=7)
    assert reserved_after_truncation != "CON.pdf"
    assert len(reserved_after_truncation.encode("utf-8")) <= 7


def test_magic_detection_validates_pdf_and_office_containers(tmp_path: Path) -> None:
    pdf = tmp_path / "wrong.docx"
    docx = tmp_path / "wrong.pdf"
    pptx = tmp_path / "wrong.bin"
    pdf.write_bytes(_pdf(b"format"))
    docx.write_bytes(_office("docx"))
    pptx.write_bytes(_office("pptx"))

    assert detect_file_format(pdf) == "pdf"
    assert detect_file_format(docx) == "docx"
    assert detect_file_format(pptx) == "pptx"

    for name, payload in {
        "login.pdf": b"<!doctype html><title>login</title>",
        "truncated.pdf": b"%PDF-1.7\n1 0 obj",
        "empty.pdf": b"",
    }.items():
        candidate = tmp_path / name
        candidate.write_bytes(payload)
        with pytest.raises(InvalidResourcePayload):
            detect_file_format(candidate)

    truncated_zip = tmp_path / "truncated.docx"
    truncated_zip.write_bytes(b"PK\x03\x04\x00\x00")
    with pytest.raises(InvalidResourcePayload, match="container is incomplete"):
        detect_file_format(truncated_zip)


def test_duplicate_display_names_get_distinct_stable_resource_directories(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, _, database, _, content_id = resource_context
    first = store.ingest(
        io.BytesIO(_pdf(b"same")),
        AttachmentId("synthetic", "duplicate-a"),
        content_id=content_id,
        sync_run_key=_run(database, 1),
        display_title="Same",
        original_filename="same.pdf",
    )
    second = store.ingest(
        io.BytesIO(_pdf(b"same")),
        AttachmentId("synthetic", "duplicate-b"),
        content_id=content_id,
        sync_run_key=_run(database, 2),
        display_title="Same",
        original_filename="same.pdf",
    )

    assert first.resource.browse_dir_name != second.resource.browse_dir_name
    assert first.version.browse_relpath != second.version.browse_relpath


def test_lifecycle_observations_never_delete_versions(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, resources, database, paths, content_id = resource_context
    attachment = AttachmentId("synthetic", "lifecycle")
    written = store.ingest(
        io.BytesIO(_pdf(b"retained")),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Retained",
        original_filename="retained.pdf",
    )
    for offset, state in enumerate(
        (
            Availability.UNAVAILABLE,
            Availability.MISSING,
            Availability.REMOVED_CONFIRMED,
            Availability.ACTIVE,
        ),
        start=1,
    ):
        store.observe(
            attachment,
            content_id=content_id,
            sync_run_key=_run(database, offset),
            display_title="Retained",
            original_filename="retained.pdf",
            availability=state,
        )

    assert resources.get_resource(attachment).availability is Availability.ACTIVE  # type: ignore[union-attr]
    assert resources.list_versions(attachment) == (written.version,)
    assert (paths.root / written.version.blob_relpath).is_file()


def test_versions_and_observations_are_database_immutable(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, _, database, _, content_id = resource_context
    result = store.ingest(
        io.BytesIO(_pdf(b"immutable")),
        AttachmentId("synthetic", "immutable"),
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Immutable",
        original_filename="immutable.pdf",
    )
    connection = database.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError, match="versions are immutable"):
            connection.execute(
                "UPDATE resource_version SET sha256 = ? WHERE version_key = ?",
                ("0" * 64, result.version.key),
            )
        with pytest.raises(sqlite3.IntegrityError, match="observations are immutable"):
            connection.execute(
                "DELETE FROM resource_observation WHERE observation_key = ?",
                (result.observation.key,),
            )
    finally:
        connection.close()


def test_canonical_blob_and_browse_version_are_owner_read_only(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, _, database, paths, content_id = resource_context
    result = store.ingest(
        io.BytesIO(_pdf(b"permissions")),
        AttachmentId("synthetic", "permissions"),
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Permissions",
        original_filename="permissions.pdf",
    )
    blob = paths.root / result.version.blob_relpath
    browse = paths.root / result.version.browse_relpath

    assert blob.stat().st_mode & 0o777 == 0o400
    assert browse.stat().st_mode & 0o777 == 0o400
    assert os.stat(blob).st_ino == os.stat(browse).st_ino


def test_hardlink_unavailable_falls_back_to_verified_copies(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, _, database, paths, content_id = resource_context

    def reject_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "synthetic cross-device link")

    monkeypatch.setattr(resource_module.os, "link", reject_link)
    result = store.ingest(
        io.BytesIO(_pdf(b"copy-fallback")),
        AttachmentId("synthetic", "copy-fallback"),
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Copy fallback",
        original_filename="copy.pdf",
    )
    blob = paths.root / result.version.blob_relpath
    browse = paths.root / result.version.browse_relpath

    assert result.view_ready
    assert blob.read_bytes() == browse.read_bytes()
    assert blob.stat().st_ino != browse.stat().st_ino


def test_failed_atomic_blob_publication_leaves_no_partial_or_database_state(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resources, database, paths, content_id = resource_context
    attachment = AttachmentId("synthetic", "publication-failure")
    real_replace = resource_module.os.replace

    def reject_link(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EXDEV, "synthetic cross-device link")

    def reject_object_publish(source: Path, destination: Path) -> None:
        if "objects" in destination.parts:
            raise OSError(errno.EIO, "synthetic atomic publish failure")
        real_replace(source, destination)

    monkeypatch.setattr(resource_module.os, "link", reject_link)
    monkeypatch.setattr(resource_module.os, "replace", reject_object_publish)

    with pytest.raises(ResourceStorageError, match="resource ingestion failed"):
        store.ingest(
            io.BytesIO(_pdf(b"atomic-failure")),
            attachment,
            content_id=content_id,
            sync_run_key=_run(database),
            display_title="Atomic failure",
            original_filename="atomic.pdf",
        )

    assert resources.get_resource(attachment) is None
    assert not any(path.is_file() for path in (paths.root / "objects").rglob("*"))
    assert not any(path.is_file() for path in (paths.root / "tmp").rglob("*"))


@pytest.mark.parametrize("failure_type", [ValueError, TypeError])
def test_stream_exceptions_are_redacted(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
    failure_type: type[Exception],
) -> None:
    store, resources, database, _, content_id = resource_context
    marker = "synthetic-private-url?token=MARKER"

    class FailingStream:
        def read(self, _size: int = -1) -> bytes:
            raise failure_type(marker)

    attachment = AttachmentId("synthetic", "redacted-stream")
    with pytest.raises(ResourceStorageError) as caught:
        store.ingest(
            FailingStream(),  # type: ignore[arg-type]
            attachment,
            content_id=content_id,
            sync_run_key=_run(database),
            display_title="Redacted stream",
            original_filename="redacted.pdf",
        )

    assert str(caught.value) == "resource ingestion failed"
    assert marker not in str(caught.value)
    assert resources.get_resource(attachment) is None


def test_postcommit_storage_failure_returns_repair_warning(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, resources, database, _, content_id = resource_context
    attachment = AttachmentId("synthetic", "postcommit-storage-failure")

    def fail_current(_version: object) -> None:
        raise StorageError("could not open the private metadata database")

    monkeypatch.setattr(store, "_publish_current_if_authoritative", fail_current)
    result = store.ingest(
        io.BytesIO(_pdf(b"postcommit")),
        attachment,
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Postcommit",
        original_filename="postcommit.pdf",
    )

    assert not result.view_ready
    assert result.warning == "browse view requires repair"
    assert resources.get_current_version(attachment) == result.version


def test_preexisting_verified_blob_is_hardened_before_reuse(
    resource_context: tuple[ResourceStore, ResourceRepository, Database, RuntimePaths, ContentId],
) -> None:
    store, _, database, paths, content_id = resource_context
    payload = _pdf(b"preexisting")
    digest = hashlib.sha256(payload).hexdigest()
    blob = paths.root / "objects" / "sha256" / digest[:2] / digest[2:4] / digest
    blob.parent.mkdir(mode=0o700, parents=True)
    blob.write_bytes(payload)
    os.chmod(blob, 0o600)

    result = store.ingest(
        io.BytesIO(payload),
        AttachmentId("synthetic", "preexisting-blob"),
        content_id=content_id,
        sync_run_key=_run(database),
        display_title="Preexisting",
        original_filename="preexisting.pdf",
    )

    assert result.version.sha256 == digest
    assert blob.stat().st_mode & 0o777 == 0o400
