"""Immutable resource binaries, observations, versions, and browse views."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import unicodedata
import uuid
import xml.etree.ElementTree as element_tree
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from ntulearn_skill.core.identifiers import AttachmentId, ContentId, require_identifier
from ntulearn_skill.core.models import (
    Availability,
    FetchDecision,
    ObservationStatus,
    VerificationStatus,
    from_storage_time,
    to_storage_time,
    utc_now,
)
from ntulearn_skill.storage.database import Database, StorageError
from ntulearn_skill.storage.migration import MigrationRunner
from ntulearn_skill.storage.paths import RuntimePaths, ensure_private_directory
from ntulearn_skill.storage.repository import DomainRepository

_RESOURCE_METADATA_FIELDS = frozenset(
    {"content_length", "content_type", "display_name", "handler_kind", "revision"}
)
_JSON_SCALARS = (str, int, float, bool, type(None))
_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
_FORMAT_EXTENSIONS = {"pdf": ".pdf", "docx": ".docx", "pptx": ".pptx", "unknown": ".bin"}
_LINK_FALLBACK_ERRORS = {errno.EXDEV, errno.EPERM, errno.EACCES, errno.ENOTSUP}


class ResourceStorageError(StorageError):
    """Privacy-safe resource storage failure."""


class InvalidResourcePayload(ResourceStorageError):
    """A stream is empty or is an obvious transport/error payload."""


@dataclass(frozen=True, slots=True)
class ResourceRecord:
    key: int
    remote_id: AttachmentId
    content_key: int
    display_title: str
    availability: Availability
    browse_dir_name: str
    current_version_key: int | None
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class ResourceVersionRecord:
    key: int
    resource_key: int
    version_number: int
    sha256: str
    byte_size: int
    file_format: str
    declared_mime: str | None
    downloaded_at: datetime
    blob_relpath: str
    browse_relpath: str
    verification_status: VerificationStatus


@dataclass(frozen=True, slots=True)
class ResourceObservationRecord:
    key: int
    resource_key: int
    sync_run_key: int
    observation_status: ObservationStatus
    original_filename: str
    sanitized_metadata: dict[str, object]
    metadata_fingerprint: str
    candidate_modified_at: str | None
    candidate_revision: str | None
    availability: Availability
    observed_at: datetime
    fetch_decision: FetchDecision
    version_key: int | None


@dataclass(frozen=True, slots=True)
class ResourceWriteResult:
    resource: ResourceRecord
    version: ResourceVersionRecord
    observation: ResourceObservationRecord
    created_version: bool
    view_ready: bool
    warning: str | None = None


def _safe_metadata(value: Mapping[str, object]) -> tuple[str, str]:
    if not set(value).issubset(_RESOURCE_METADATA_FIELDS):
        raise ValueError("resource metadata contains a field outside the allowlist")
    if any(not isinstance(item, _JSON_SCALARS) for item in value.values()):
        raise ValueError("resource metadata values must be JSON scalar values")
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        raise ValueError("resource metadata must be JSON-serializable") from None
    return encoded, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_component(value: str, *, fallback: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        raise ValueError("component byte limit must be positive")

    def truncate(candidate: str) -> str:
        while len(candidate.encode("utf-8")) > max_bytes:
            candidate = candidate[:-1]
        return candidate.rstrip(" .")

    normalized = unicodedata.normalize("NFKC", value)
    cleaned = "".join(
        "_" if character in "/\\:" or unicodedata.category(character).startswith("C") else character
        for character in normalized
    )
    cleaned = " ".join(cleaned.split()).strip(" .")
    if not cleaned or cleaned in {".", ".."}:
        cleaned = fallback
    if cleaned.split(".", 1)[0].upper() in _DEVICE_NAMES:
        cleaned = f"_{cleaned}"
    cleaned = truncate(cleaned)
    if not cleaned:
        cleaned = truncate(fallback) or "_"
    if cleaned.split(".", 1)[0].upper() in _DEVICE_NAMES:
        cleaned = truncate(f"_{cleaned}")
    return cleaned


def safe_filename(original_filename: str, file_format: str, *, max_bytes: int = 180) -> str:
    """Return a display filename whose extension is controlled by detected bytes."""

    if file_format not in _FORMAT_EXTENSIONS:
        raise ValueError("unsupported detected file format")
    extension = _FORMAT_EXTENSIONS[file_format]
    if max_bytes <= len(extension.encode("utf-8")):
        raise ValueError("filename byte limit is too small")
    normalized = unicodedata.normalize("NFKC", original_filename)
    base = normalized.rsplit(".", 1)[0] if "." in normalized else normalized
    stem = _safe_component(base, fallback="resource", max_bytes=max_bytes - len(extension))
    return f"{stem}{extension}"


def detect_file_format(path: Path) -> str:
    """Detect supported formats from bytes; filenames and MIME are not trusted."""

    size = path.stat().st_size
    if size == 0:
        raise InvalidResourcePayload("resource payload is empty")
    with path.open("rb") as stream:
        head = stream.read(8192)
    stripped = head.lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<?xml")) or stripped[:1] in {
        b"{",
        b"[",
    }:
        raise InvalidResourcePayload("resource payload is an unexpected response document")
    if head.startswith(b"%PDF-"):
        with path.open("rb") as stream:
            stream.seek(max(0, size - 4096))
            tail = stream.read()
        matches = tuple(re.finditer(rb"startxref\s+([0-9]+)\s+%%EOF", tail))
        if not matches or tail[matches[-1].end() :].strip(b"\x00\t\r\n "):
            raise InvalidResourcePayload("resource PDF is incomplete")
        xref_offset = int(matches[-1].group(1))
        if xref_offset >= size:
            raise InvalidResourcePayload("resource PDF is incomplete")
        with path.open("rb") as stream:
            stream.seek(xref_offset)
            xref = stream.read(4096)
        xref_stream = re.match(rb"[0-9]+\s+[0-9]+\s+obj\b", xref) and re.search(
            rb"/Type\s*/XRef\b", xref
        )
        if not xref.startswith(b"xref") and not xref_stream:
            raise InvalidResourcePayload("resource PDF is incomplete")
        return "pdf"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")) and not zipfile.is_zipfile(
        path
    ):
        raise InvalidResourcePayload("resource container is incomplete")
    if zipfile.is_zipfile(path):
        try:
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 4096:
                    raise InvalidResourcePayload("resource container has too many entries")
                names: set[str] = set()
                info_by_name: dict[str, zipfile.ZipInfo] = {}
                for entry in entries:
                    if len(entry.filename.encode("utf-8")) > 1024:
                        raise InvalidResourcePayload("resource container entry is unsafe")
                    item = PurePosixPath(entry.filename)
                    if item.is_absolute() or ".." in item.parts:
                        raise InvalidResourcePayload("resource container entry is unsafe")
                    names.add(entry.filename)
                    info_by_name[entry.filename] = entry
                marker = None
                if "word/document.xml" in names:
                    marker = "word/document.xml"
                elif "ppt/presentation.xml" in names:
                    marker = "ppt/presentation.xml"
                if marker is not None and "[Content_Types].xml" in names:
                    if {"word/document.xml", "ppt/presentation.xml"}.issubset(names):
                        raise InvalidResourcePayload("resource container type is ambiguous")
                    for required in ("[Content_Types].xml", marker):
                        info = info_by_name[required]
                        if info.flag_bits & 0x1 or info.file_size > 8 * 1024 * 1024:
                            raise InvalidResourcePayload("resource container entry is unsafe")
                        with archive.open(info) as member:
                            payload = member.read(info.file_size + 1)
                        if len(payload) != info.file_size:
                            raise InvalidResourcePayload("resource container entry is invalid")
                        try:
                            root = element_tree.fromstring(payload)
                        except element_tree.ParseError:
                            raise InvalidResourcePayload(
                                "resource container entry is invalid"
                            ) from None
                        local_name = root.tag.rsplit("}", 1)[-1]
                        expected_name = {
                            "[Content_Types].xml": "Types",
                            "word/document.xml": "document",
                            "ppt/presentation.xml": "presentation",
                        }[required]
                        if local_name != expected_name:
                            raise InvalidResourcePayload("resource container entry is invalid")
        except (OSError, zipfile.BadZipFile):
            raise InvalidResourcePayload("resource container is invalid") from None
        if "[Content_Types].xml" in names and "word/document.xml" in names:
            return "docx"
        if "[Content_Types].xml" in names and "ppt/presentation.xml" in names:
            return "pptx"
    return "unknown"


class ResourceRepository:
    """Transactional metadata access for logical resources and immutable evidence."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> int:
        return MigrationRunner(self.database).migrate()

    @staticmethod
    def _content_context(
        connection: sqlite3.Connection, remote_id: ContentId
    ) -> tuple[int, int, str, str, str]:
        row = connection.execute(
            """
            SELECT n.content_key, c.course_key, c.code, c.browse_dir_name,
                   p.name AS provider
            FROM content_node n
            JOIN source_object nso ON nso.source_object_key = n.source_object_key
            JOIN source_provider p ON p.provider_key = nso.provider_key
            JOIN course c ON c.course_key = n.course_key
            WHERE p.name = ? AND nso.object_kind = 'content' AND nso.remote_key = ?
            """,
            (remote_id.provider, remote_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("referenced content does not exist")
        course_key = int(row["course_key"])
        course_directory = row["browse_dir_name"]
        if course_directory is None:
            course_directory = (
                f"{_safe_component(str(row['code']), fallback='course', max_bytes=80)}"
                f"--c_{course_key:08x}"
            )
            connection.execute(
                "UPDATE course SET browse_dir_name = ? WHERE course_key = ?",
                (course_directory, course_key),
            )
        return (
            int(row["content_key"]),
            course_key,
            str(row["code"]),
            str(course_directory),
            str(row["provider"]),
        )

    @staticmethod
    def _ensure_resource(
        connection: sqlite3.Connection,
        attachment_id: AttachmentId,
        content_id: ContentId,
        display_title: str,
        availability: Availability,
        observation_status: ObservationStatus,
        sync_run_key: int,
        timestamp: str,
    ) -> tuple[int, str, str, bool]:
        content_key, _course_key, _code, course_dir, provider = ResourceRepository._content_context(
            connection, content_id
        )
        if provider != attachment_id.provider:
            raise ValueError("resource and content providers must match")
        source_key = DomainRepository._source_object_key(
            connection, attachment_id, "attachment", timestamp
        )
        row = connection.execute(
            """
            SELECT resource_key, content_key, browse_dir_name, last_observed_at
            FROM resource WHERE source_object_key = ?
            """,
            (source_key,),
        ).fetchone()
        if row is None:
            initial_availability = (
                Availability.UNKNOWN
                if observation_status is ObservationStatus.UNKNOWN
                else availability
            )
            resource_dir = (
                f"{_safe_component(display_title, fallback='resource', max_bytes=80)}"
                f"--r_{source_key:08x}"
            )
            cursor = connection.execute(
                """
                INSERT INTO resource(
                    source_object_key, content_key, display_title, availability,
                    browse_dir_name, first_observed_at, last_observed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    source_key,
                    content_key,
                    display_title,
                    initial_availability.value,
                    resource_dir,
                    timestamp,
                    timestamp,
                ),
            )
            assert cursor.lastrowid is not None
            return (
                int(cursor.lastrowid),
                course_dir,
                resource_dir,
                observation_status is ObservationStatus.OBSERVED,
            )
        if int(row["content_key"]) != content_key:
            raise ValueError("a resource cannot move to another content node")
        resource_key = int(row["resource_key"])
        latest_observed = connection.execute(
            """
            SELECT observed_at, sync_run_key
            FROM resource_observation
            WHERE resource_key = ? AND observation_status = 'OBSERVED'
            ORDER BY observed_at DESC, sync_run_key DESC, observation_key DESC
            LIMIT 1
            """,
            (resource_key,),
        ).fetchone()
        is_latest = bool(
            observation_status is ObservationStatus.OBSERVED
            and (
                latest_observed is None
                or (timestamp, sync_run_key)
                >= (
                    str(latest_observed["observed_at"]),
                    int(latest_observed["sync_run_key"]),
                )
            )
        )
        if is_latest:
            if latest_observed is None:
                connection.execute(
                    """
                    UPDATE resource
                    SET display_title = ?, first_observed_at = ?, last_observed_at = ?
                    WHERE resource_key = ?
                    """,
                    (display_title, timestamp, timestamp, resource_key),
                )
            else:
                connection.execute(
                    """
                    UPDATE resource SET display_title = ?, last_observed_at = ?
                    WHERE resource_key = ?
                    """,
                    (display_title, timestamp, resource_key),
                )
        if observation_status in {
            ObservationStatus.OBSERVED,
            ObservationStatus.NOT_OBSERVED,
            ObservationStatus.UNAVAILABLE,
        }:
            latest_authoritative = connection.execute(
                """
                SELECT observed_at, sync_run_key
                FROM resource_observation
                WHERE resource_key = ?
                  AND observation_status IN ('OBSERVED', 'NOT_OBSERVED', 'UNAVAILABLE')
                ORDER BY observed_at DESC, sync_run_key DESC, observation_key DESC
                LIMIT 1
                """,
                (resource_key,),
            ).fetchone()
            if latest_authoritative is None or (
                timestamp,
                sync_run_key,
            ) >= (
                str(latest_authoritative["observed_at"]),
                int(latest_authoritative["sync_run_key"]),
            ):
                connection.execute(
                    "UPDATE resource SET availability = ? WHERE resource_key = ?",
                    (availability.value, resource_key),
                )
        if observation_status is ObservationStatus.OBSERVED and latest_observed is not None:
            connection.execute(
                """
                UPDATE resource SET first_observed_at = MIN(first_observed_at, ?)
                WHERE resource_key = ?
                """,
                (timestamp, resource_key),
            )
        return resource_key, course_dir, str(row["browse_dir_name"]), is_latest

    def observe(
        self,
        attachment_id: AttachmentId,
        *,
        content_id: ContentId,
        sync_run_key: int,
        display_title: str,
        original_filename: str,
        availability: Availability = Availability.ACTIVE,
        observation_status: ObservationStatus = ObservationStatus.OBSERVED,
        fetch_decision: FetchDecision = FetchDecision.DEFERRED,
        sanitized_metadata: Mapping[str, object] | None = None,
        candidate_modified_at: str | None = None,
        candidate_revision: str | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[ResourceRecord, ResourceObservationRecord]:
        """Append a metadata-only observation without fabricating a binary version."""

        if fetch_decision in {FetchDecision.FETCHED, FetchDecision.REUSED_VERIFIED}:
            raise ValueError("a verified fetch decision requires a binary version")
        resource, _version, observation, _created = self._record(
            attachment_id,
            content_id=content_id,
            sync_run_key=sync_run_key,
            display_title=display_title,
            original_filename=original_filename,
            availability=availability,
            observation_status=observation_status,
            fetch_decision=fetch_decision,
            sanitized_metadata=sanitized_metadata,
            candidate_modified_at=candidate_modified_at,
            candidate_revision=candidate_revision,
            observed_at=observed_at,
        )
        return resource, observation

    def record_verified(
        self,
        attachment_id: AttachmentId,
        *,
        content_id: ContentId,
        sync_run_key: int,
        display_title: str,
        original_filename: str,
        sha256: str,
        byte_size: int,
        file_format: str,
        declared_mime: str | None,
        blob_relpath: str,
        safe_display_filename: str,
        availability: Availability = Availability.ACTIVE,
        observation_status: ObservationStatus = ObservationStatus.OBSERVED,
        sanitized_metadata: Mapping[str, object] | None = None,
        candidate_modified_at: str | None = None,
        candidate_revision: str | None = None,
        observed_at: datetime | None = None,
        downloaded_at: datetime | None = None,
    ) -> tuple[ResourceRecord, ResourceVersionRecord, ResourceObservationRecord, bool]:
        resource, version, observation, created = self._record(
            attachment_id,
            content_id=content_id,
            sync_run_key=sync_run_key,
            display_title=display_title,
            original_filename=original_filename,
            availability=availability,
            observation_status=observation_status,
            fetch_decision=FetchDecision.FETCHED,
            sanitized_metadata=sanitized_metadata,
            candidate_modified_at=candidate_modified_at,
            candidate_revision=candidate_revision,
            observed_at=observed_at,
            sha256=sha256,
            byte_size=byte_size,
            file_format=file_format,
            declared_mime=declared_mime,
            blob_relpath=blob_relpath,
            safe_display_filename=safe_display_filename,
            downloaded_at=downloaded_at,
        )
        assert version is not None
        return resource, version, observation, created

    def _record(
        self,
        attachment_id: AttachmentId,
        *,
        content_id: ContentId,
        sync_run_key: int,
        display_title: str,
        original_filename: str,
        availability: Availability,
        observation_status: ObservationStatus,
        fetch_decision: FetchDecision,
        sanitized_metadata: Mapping[str, object] | None,
        candidate_modified_at: str | None,
        candidate_revision: str | None,
        observed_at: datetime | None,
        sha256: str | None = None,
        byte_size: int | None = None,
        file_format: str | None = None,
        declared_mime: str | None = None,
        blob_relpath: str | None = None,
        safe_display_filename: str | None = None,
        downloaded_at: datetime | None = None,
    ) -> tuple[
        ResourceRecord,
        ResourceVersionRecord | None,
        ResourceObservationRecord,
        bool,
    ]:
        require_identifier(attachment_id, AttachmentId)
        require_identifier(content_id, ContentId)
        if sync_run_key <= 0:
            raise ValueError("sync_run_key must be positive")
        if not display_title.strip():
            raise ValueError("display title must be non-empty")
        metadata_json, fingerprint = _safe_metadata(sanitized_metadata or {})
        timestamp = to_storage_time(observed_at or utc_now())
        downloaded_timestamp = to_storage_time(downloaded_at or utc_now())
        binary_supplied = sha256 is not None
        if binary_supplied and (
            byte_size is None
            or file_format not in _FORMAT_EXTENSIONS
            or blob_relpath is None
            or safe_display_filename is None
        ):
            raise ValueError("verified binary metadata is incomplete")
        created_version = False
        version_key: int | None = None
        try:
            with self.database.transaction() as connection:
                resource_key, course_dir, resource_dir, is_latest = self._ensure_resource(
                    connection,
                    attachment_id,
                    content_id,
                    display_title,
                    availability,
                    observation_status,
                    sync_run_key,
                    timestamp,
                )
                if binary_supplied:
                    assert sha256 is not None
                    assert byte_size is not None
                    assert file_format is not None
                    assert blob_relpath is not None
                    assert safe_display_filename is not None
                    existing = connection.execute(
                        """
                        SELECT * FROM resource_version
                        WHERE resource_key = ? AND sha256 = ?
                        """,
                        (resource_key, sha256),
                    ).fetchone()
                    if existing is None:
                        number = int(
                            connection.execute(
                                """
                                SELECT COALESCE(MAX(version_number), 0) + 1
                                FROM resource_version WHERE resource_key = ?
                                """,
                                (resource_key,),
                            ).fetchone()[0]
                        )
                        browse_relpath = str(
                            PurePosixPath("courses")
                            / course_dir
                            / "materials"
                            / "unknown"
                            / resource_dir
                            / "versions"
                            / (f"v{number:06d}--sha256-{sha256[:12]}--{safe_display_filename}")
                        )
                        cursor = connection.execute(
                            """
                            INSERT INTO resource_version(
                                resource_key, version_number, sha256, byte_size, file_format,
                                declared_mime, downloaded_at, blob_relpath, browse_relpath,
                                verification_status
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'VERIFIED')
                            """,
                            (
                                resource_key,
                                number,
                                sha256,
                                byte_size,
                                file_format,
                                declared_mime,
                                downloaded_timestamp,
                                blob_relpath,
                                browse_relpath,
                            ),
                        )
                        assert cursor.lastrowid is not None
                        version_key = int(cursor.lastrowid)
                        created_version = True
                    else:
                        if (
                            int(existing["byte_size"]) != byte_size
                            or str(existing["file_format"]) != file_format
                            or str(existing["blob_relpath"]) != blob_relpath
                        ):
                            raise ResourceStorageError("stored resource version is inconsistent")
                        version_key = int(existing["version_key"])
                        fetch_decision = FetchDecision.REUSED_VERIFIED
                    if is_latest:
                        connection.execute(
                            "UPDATE resource SET current_version_key = ? WHERE resource_key = ?",
                            (version_key, resource_key),
                        )
                cursor = connection.execute(
                    """
                    INSERT INTO resource_observation(
                        resource_key, sync_run_key, observation_status, original_filename,
                        sanitized_metadata_json, metadata_fingerprint, candidate_modified_at,
                        candidate_revision, availability, observed_at, fetch_decision, version_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource_key,
                        sync_run_key,
                        observation_status.value,
                        original_filename,
                        metadata_json,
                        fingerprint,
                        candidate_modified_at,
                        candidate_revision,
                        availability.value,
                        timestamp,
                        fetch_decision.value,
                        version_key,
                    ),
                )
                assert cursor.lastrowid is not None
                observation_key = int(cursor.lastrowid)
                resource_row = connection.execute(
                    """
                    SELECT r.*, p.name AS provider, so.remote_key
                    FROM resource r
                    JOIN source_object so ON so.source_object_key = r.source_object_key
                    JOIN source_provider p ON p.provider_key = so.provider_key
                    WHERE r.resource_key = ?
                    """,
                    (resource_key,),
                ).fetchone()
                assert resource_row is not None
                resource_record = self._resource_from_row(resource_row)
                version_record = None
                if version_key is not None:
                    version_row = connection.execute(
                        "SELECT * FROM resource_version WHERE version_key = ?", (version_key,)
                    ).fetchone()
                    assert version_row is not None
                    version_record = self._version_from_row(version_row)
                observation_row = connection.execute(
                    "SELECT * FROM resource_observation WHERE observation_key = ?",
                    (observation_key,),
                ).fetchone()
                assert observation_row is not None
                observation_record = self._observation_from_row(observation_row)
            return resource_record, version_record, observation_record, created_version
        except ResourceStorageError:
            raise
        except sqlite3.Error:
            raise ResourceStorageError("resource metadata operation failed") from None

    def get_resource(self, remote_id: AttachmentId) -> ResourceRecord | None:
        require_identifier(remote_id, AttachmentId)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT r.*, p.name AS provider, so.remote_key
                FROM resource r
                JOIN source_object so ON so.source_object_key = r.source_object_key
                JOIN source_provider p ON p.provider_key = so.provider_key
                WHERE p.name = ? AND so.object_kind = 'attachment' AND so.remote_key = ?
                """,
                (remote_id.provider, remote_id.value),
            ).fetchone()
            return None if row is None else self._resource_from_row(row)
        except sqlite3.Error:
            raise ResourceStorageError("resource metadata operation failed") from None
        finally:
            connection.close()

    def get_version(self, version_key: int | None) -> ResourceVersionRecord | None:
        if version_key is None:
            return None
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM resource_version WHERE version_key = ?", (version_key,)
            ).fetchone()
            return None if row is None else self._version_from_row(row)
        except sqlite3.Error:
            raise ResourceStorageError("resource version operation failed") from None
        finally:
            connection.close()

    def get_current_version(self, remote_id: AttachmentId) -> ResourceVersionRecord | None:
        resource = self.get_resource(remote_id)
        return None if resource is None else self.get_version(resource.current_version_key)

    def get_observation(self, observation_key: int) -> ResourceObservationRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM resource_observation WHERE observation_key = ?",
                (observation_key,),
            ).fetchone()
            return None if row is None else self._observation_from_row(row)
        except sqlite3.Error:
            raise ResourceStorageError("resource observation operation failed") from None
        finally:
            connection.close()

    def list_versions(self, remote_id: AttachmentId) -> tuple[ResourceVersionRecord, ...]:
        resource = self.get_resource(remote_id)
        if resource is None:
            return ()
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM resource_version
                WHERE resource_key = ? ORDER BY version_number
                """,
                (resource.key,),
            ).fetchall()
            return tuple(self._version_from_row(row) for row in rows)
        finally:
            connection.close()

    def all_verified_versions(
        self,
    ) -> tuple[tuple[ResourceRecord, ResourceVersionRecord], ...]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """
                SELECT r.*, p.name AS provider, so.remote_key,
                       v.version_key AS v_version_key, v.resource_key AS v_resource_key,
                       v.version_number AS v_version_number, v.sha256 AS v_sha256,
                       v.byte_size AS v_byte_size, v.file_format AS v_file_format,
                       v.declared_mime AS v_declared_mime, v.downloaded_at AS v_downloaded_at,
                       v.blob_relpath AS v_blob_relpath, v.browse_relpath AS v_browse_relpath,
                       v.verification_status AS v_verification_status
                FROM resource_version v
                JOIN resource r ON r.resource_key = v.resource_key
                JOIN source_object so ON so.source_object_key = r.source_object_key
                JOIN source_provider p ON p.provider_key = so.provider_key
                WHERE v.verification_status = 'VERIFIED'
                ORDER BY r.resource_key, v.version_number
                """
            ).fetchall()
            result = []
            for row in rows:
                resource = self._resource_from_row(row)
                version = ResourceVersionRecord(
                    key=int(row["v_version_key"]),
                    resource_key=int(row["v_resource_key"]),
                    version_number=int(row["v_version_number"]),
                    sha256=str(row["v_sha256"]),
                    byte_size=int(row["v_byte_size"]),
                    file_format=str(row["v_file_format"]),
                    declared_mime=None
                    if row["v_declared_mime"] is None
                    else str(row["v_declared_mime"]),
                    downloaded_at=from_storage_time(str(row["v_downloaded_at"])),
                    blob_relpath=str(row["v_blob_relpath"]),
                    browse_relpath=str(row["v_browse_relpath"]),
                    verification_status=VerificationStatus(str(row["v_verification_status"])),
                )
                result.append((resource, version))
            return tuple(result)
        finally:
            connection.close()

    @staticmethod
    def _resource_from_row(row: sqlite3.Row) -> ResourceRecord:
        return ResourceRecord(
            key=int(row["resource_key"]),
            remote_id=AttachmentId(str(row["provider"]), str(row["remote_key"])),
            content_key=int(row["content_key"]),
            display_title=str(row["display_title"]),
            availability=Availability(str(row["availability"])),
            browse_dir_name=str(row["browse_dir_name"]),
            current_version_key=None
            if row["current_version_key"] is None
            else int(row["current_version_key"]),
            first_observed_at=from_storage_time(str(row["first_observed_at"])),
            last_observed_at=from_storage_time(str(row["last_observed_at"])),
        )

    @staticmethod
    def _version_from_row(row: sqlite3.Row) -> ResourceVersionRecord:
        return ResourceVersionRecord(
            key=int(row["version_key"]),
            resource_key=int(row["resource_key"]),
            version_number=int(row["version_number"]),
            sha256=str(row["sha256"]),
            byte_size=int(row["byte_size"]),
            file_format=str(row["file_format"]),
            declared_mime=None if row["declared_mime"] is None else str(row["declared_mime"]),
            downloaded_at=from_storage_time(str(row["downloaded_at"])),
            blob_relpath=str(row["blob_relpath"]),
            browse_relpath=str(row["browse_relpath"]),
            verification_status=VerificationStatus(str(row["verification_status"])),
        )

    @staticmethod
    def _observation_from_row(row: sqlite3.Row) -> ResourceObservationRecord:
        return ResourceObservationRecord(
            key=int(row["observation_key"]),
            resource_key=int(row["resource_key"]),
            sync_run_key=int(row["sync_run_key"]),
            observation_status=ObservationStatus(str(row["observation_status"])),
            original_filename=str(row["original_filename"]),
            sanitized_metadata=json.loads(str(row["sanitized_metadata_json"])),
            metadata_fingerprint=str(row["metadata_fingerprint"]),
            candidate_modified_at=None
            if row["candidate_modified_at"] is None
            else str(row["candidate_modified_at"]),
            candidate_revision=None
            if row["candidate_revision"] is None
            else str(row["candidate_revision"]),
            availability=Availability(str(row["availability"])),
            observed_at=from_storage_time(str(row["observed_at"])),
            fetch_decision=FetchDecision(str(row["fetch_decision"])),
            version_key=None if row["version_key"] is None else int(row["version_key"]),
        )


class ResourceStore:
    """Coordinate streaming ingestion across the canonical store, SQLite, and browse view."""

    def __init__(
        self,
        paths: RuntimePaths,
        repository: ResourceRepository,
        *,
        maximum_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        if maximum_bytes <= 0:
            raise ValueError("maximum_bytes must be positive")
        self.paths = paths
        self.repository = repository
        self.maximum_bytes = maximum_bytes

    def initialize(self) -> int:
        self.paths.ensure()
        return self.repository.initialize()

    def observe(
        self,
        attachment_id: AttachmentId,
        *,
        content_id: ContentId,
        sync_run_key: int,
        display_title: str,
        original_filename: str,
        availability: Availability = Availability.ACTIVE,
        observation_status: ObservationStatus = ObservationStatus.OBSERVED,
        fetch_decision: FetchDecision = FetchDecision.DEFERRED,
        sanitized_metadata: Mapping[str, object] | None = None,
        candidate_modified_at: str | None = None,
        candidate_revision: str | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[ResourceRecord, ResourceObservationRecord]:
        return self.repository.observe(
            attachment_id,
            content_id=content_id,
            sync_run_key=sync_run_key,
            display_title=display_title,
            original_filename=original_filename,
            availability=availability,
            observation_status=observation_status,
            fetch_decision=fetch_decision,
            sanitized_metadata=sanitized_metadata,
            candidate_modified_at=candidate_modified_at,
            candidate_revision=candidate_revision,
            observed_at=observed_at,
        )

    def ingest(
        self,
        stream: BinaryIO,
        attachment_id: AttachmentId,
        *,
        content_id: ContentId,
        sync_run_key: int,
        display_title: str,
        original_filename: str,
        declared_mime: str | None = None,
        availability: Availability = Availability.ACTIVE,
        observation_status: ObservationStatus = ObservationStatus.OBSERVED,
        sanitized_metadata: Mapping[str, object] | None = None,
        candidate_modified_at: str | None = None,
        candidate_revision: str | None = None,
        observed_at: datetime | None = None,
    ) -> ResourceWriteResult:
        require_identifier(attachment_id, AttachmentId)
        require_identifier(content_id, ContentId)
        self.paths.ensure()
        temporary = self.paths.root / "tmp" / f"download-{uuid.uuid4().hex}.part"
        digest = hashlib.sha256()
        byte_size = 0
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as target:
                while True:
                    try:
                        chunk = stream.read(1024 * 1024)
                    except Exception:
                        raise ResourceStorageError("resource ingestion failed") from None
                    if not isinstance(chunk, bytes):
                        raise ResourceStorageError("resource ingestion failed")
                    if not chunk:
                        break
                    byte_size += len(chunk)
                    if byte_size > self.maximum_bytes:
                        raise InvalidResourcePayload("resource payload exceeds configured size")
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            file_format = detect_file_format(temporary)
            content_hash = digest.hexdigest()
            filename = safe_filename(original_filename, file_format)
            blob_relpath = str(
                PurePosixPath("objects")
                / "sha256"
                / content_hash[:2]
                / content_hash[2:4]
                / content_hash
            )
            blob_path, _blob_created = self._publish_blob(
                temporary, blob_relpath, content_hash, byte_size
            )
            resource, version, observation, created = self.repository.record_verified(
                attachment_id,
                content_id=content_id,
                sync_run_key=sync_run_key,
                display_title=display_title,
                original_filename=original_filename,
                sha256=content_hash,
                byte_size=byte_size,
                file_format=file_format,
                declared_mime=declared_mime,
                blob_relpath=blob_relpath,
                safe_display_filename=filename,
                availability=availability,
                observation_status=observation_status,
                sanitized_metadata=sanitized_metadata,
                candidate_modified_at=candidate_modified_at,
                candidate_revision=candidate_revision,
                observed_at=observed_at,
            )
            try:
                self._materialize_version(blob_path, version)
                self._publish_current_if_authoritative(version)
            except (OSError, StorageError):
                return ResourceWriteResult(
                    resource,
                    version,
                    observation,
                    created,
                    False,
                    "browse view requires repair",
                )
            return ResourceWriteResult(resource, version, observation, created, True)
        except (InvalidResourcePayload, ResourceStorageError, TypeError, ValueError):
            raise
        except Exception:
            raise ResourceStorageError("resource ingestion failed") from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def repair_views(self) -> tuple[str, ...]:
        """Rebuild the derived browse view from authoritative DB rows and canonical blobs."""

        warnings: list[str] = []
        for resource, version in self.repository.all_verified_versions():
            blob = self._absolute_relpath(version.blob_relpath)
            self._verify_file(blob, version.sha256, version.byte_size)
            try:
                self._materialize_version(blob, version)
                if resource.current_version_key == version.key:
                    self._publish_current_if_authoritative(version)
            except (OSError, ResourceStorageError):
                warnings.append(f"resource-{resource.key}-view")
        return tuple(warnings)

    def _absolute_relpath(self, value: str) -> Path:
        relative = PurePosixPath(value)
        if relative.is_absolute() or not relative.parts or ".." in relative.parts:
            raise ResourceStorageError("stored resource path is unsafe")
        destination = self.paths.root.joinpath(*relative.parts)
        resolved_parent = destination.parent.resolve()
        try:
            resolved_parent.relative_to(self.paths.root.resolve())
        except ValueError:
            raise ResourceStorageError("stored resource path is unsafe") from None
        return destination

    def _ensure_directory(self, directory: Path) -> None:
        root = self.paths.root.resolve()
        try:
            relative = directory.relative_to(root)
        except ValueError:
            raise ResourceStorageError("resource directory is unsafe") from None
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ResourceStorageError("resource directory is unsafe")
            ensure_private_directory(current, boundary=root)

    def _publish_blob(
        self, temporary: Path, relpath: str, expected_hash: str, expected_size: int
    ) -> tuple[Path, bool]:
        destination = self._absolute_relpath(relpath)
        self._ensure_directory(destination.parent)
        if destination.exists() or destination.is_symlink():
            self._verify_file(destination, expected_hash, expected_size)
            os.chmod(destination, 0o400, follow_symlinks=False)
            return destination, False
        try:
            os.link(temporary, destination)
        except OSError as error:
            if error.errno not in _LINK_FALLBACK_ERRORS:
                if error.errno == errno.EEXIST:
                    self._verify_file(destination, expected_hash, expected_size)
                    os.chmod(destination, 0o400, follow_symlinks=False)
                    return destination, False
                raise
            staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
            try:
                with temporary.open("rb") as source, staging.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                self._verify_file(staging, expected_hash, expected_size)
                os.replace(staging, destination)
            finally:
                staging.unlink(missing_ok=True)
        os.chmod(destination, 0o400, follow_symlinks=False)
        self._verify_file(destination, expected_hash, expected_size)
        return destination, True

    def _materialize_version(self, blob: Path, version: ResourceVersionRecord) -> None:
        destination = self._absolute_relpath(version.browse_relpath)
        self._ensure_directory(destination.parent)
        if destination.exists() or destination.is_symlink():
            self._verify_file(destination, version.sha256, version.byte_size)
            return
        staging = destination.parent / f".{destination.name}.{uuid.uuid4().hex}.part"
        try:
            try:
                os.link(blob, staging)
            except OSError as error:
                if error.errno not in _LINK_FALLBACK_ERRORS:
                    raise
                with blob.open("rb") as source, staging.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
            os.chmod(staging, 0o400, follow_symlinks=False)
            self._verify_file(staging, version.sha256, version.byte_size)
            os.replace(staging, destination)
        finally:
            staging.unlink(missing_ok=True)

    def _publish_current(self, version: ResourceVersionRecord) -> None:
        version_path = self._absolute_relpath(version.browse_relpath)
        resource_directory = version_path.parent.parent
        current = resource_directory / "current"
        relative_target = PurePosixPath("versions") / version_path.name
        staging = resource_directory / f".current-{uuid.uuid4().hex}"
        try:
            os.symlink(str(relative_target), staging)
            os.replace(staging, current)
        finally:
            staging.unlink(missing_ok=True)

    def _publish_current_if_authoritative(self, version: ResourceVersionRecord) -> None:
        """Serialize a pointer write with a recheck of SQLite's current version."""

        try:
            with self.repository.database.transaction() as connection:
                row = connection.execute(
                    "SELECT current_version_key FROM resource WHERE resource_key = ?",
                    (version.resource_key,),
                ).fetchone()
                if row is not None and row["current_version_key"] == version.key:
                    self._publish_current(version)
        except (sqlite3.Error, StorageError):
            raise ResourceStorageError("resource current-version check failed") from None

    @staticmethod
    def _verify_file(path: Path, expected_hash: str, expected_size: int) -> None:
        if path.is_symlink():
            raise ResourceStorageError("stored resource file is unsafe")
        try:
            metadata = path.stat()
        except OSError:
            raise ResourceStorageError("stored resource file is unavailable") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size != expected_size:
            raise ResourceStorageError("stored resource file failed verification")
        digest = hashlib.sha256()
        try:
            with path.open("rb") as stream:
                while chunk := stream.read(1024 * 1024):
                    digest.update(chunk)
        except OSError:
            raise ResourceStorageError("stored resource file failed verification") from None
        if digest.hexdigest() != expected_hash:
            raise ResourceStorageError("stored resource file failed verification")
