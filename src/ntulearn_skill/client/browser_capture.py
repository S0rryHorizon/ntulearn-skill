"""Strict adapter for private, host-produced browser observation bundles.

This module does not automate a browser or perform network requests.  It accepts only a
bounded snapshot produced by an already authenticated host browser interaction and exposes
that snapshot through the existing read-only source contracts.
"""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, TypeVar

from ntulearn_skill.client.contracts import (
    AnnouncementSourceRecord,
    AssessmentSourceRecord,
    AuthenticationRequired,
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    DueSourceRecord,
    EphemeralByteStream,
    Page,
    PageRequest,
    ReadPurpose,
    ResourceMetadataRecord,
    ScheduleSourceRecord,
    SessionExpired,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    SourceProtocolError,
    SourceUnavailable,
    TimeWindow,
    UnsupportedCapability,
)
from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    AttachmentId,
    Availability,
    ContentId,
    CourseId,
    Coverage,
    GradingColumnId,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.storage.paths import validate_private_path

PROVIDER_NAME = "ntulearn-browser-capture"
SCHEMA_VERSION = 1
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RESOURCE_BYTES = 100 * 1024 * 1024
MAX_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_CAPTURE_TTL = timedelta(hours=24)
MAX_FUTURE_SKEW = timedelta(minutes=5)
BROWSER_CAPTURE_ERROR_MESSAGE = (
    "the browser capture did not match a supported shape; check that its private directory is "
    "mode 0700 and its regular manifest file is mode 0600"
)

_CAPTURE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_REMOTE_ID = re.compile(r"[A-Za-z0-9._-]{1,512}\Z")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class _ResourceFile:
    root: Path
    parts: tuple[str, ...]
    downloaded_at: datetime
    root_device: int
    root_inode: int
    device: int
    inode: int
    byte_size: int


class BrowserCaptureManifestError(SourceProtocolError):
    """Privacy-safe capture failure with a fixed remediation hint."""

    def __init__(self) -> None:
        super().__init__()
        self.args = (BROWSER_CAPTURE_ERROR_MESSAGE,)


@dataclass(frozen=True, slots=True)
class BrowserCaptureBundle:
    """Validated private snapshot and its typed source records."""

    capture_id: str
    capture_start_at: datetime
    captured_at: datetime
    expires_at: datetime
    authentication_status: SessionStatus
    course: CourseSourceRecord
    course_coverage: Coverage
    content: tuple[ContentSourceRecord, ...]
    content_coverage: Coverage
    announcements: tuple[AnnouncementSourceRecord, ...]
    announcement_coverage: Coverage
    assessments: tuple[AssessmentSourceRecord, ...]
    assessment_coverage: Coverage
    resource_files: Mapping[AttachmentId, _ResourceFile]
    source_paths: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "content", tuple(self.content))
        object.__setattr__(self, "announcements", tuple(self.announcements))
        object.__setattr__(self, "assessments", tuple(self.assessments))
        object.__setattr__(self, "resource_files", MappingProxyType(dict(self.resource_files)))
        object.__setattr__(self, "source_paths", MappingProxyType(dict(self.source_paths)))

    @classmethod
    def load(cls, manifest_path: str | Path) -> BrowserCaptureBundle:
        """Load one private manifest without reflecting private input in failures."""

        try:
            lexical = Path(os.path.abspath(os.fspath(Path(manifest_path).expanduser())))
            validate_private_path(lexical)
            payload = _read_manifest(lexical)
            return _parse_bundle(
                json.loads(
                    payload.decode("utf-8"),
                    object_pairs_hook=_unique_object,
                    parse_constant=_invalid_json_constant,
                ),
                lexical.parent,
            )
        except BrowserCaptureManifestError:
            raise
        except Exception:
            raise BrowserCaptureManifestError() from None


def prepare_browser_capture_directory(bundle_root: str | Path) -> Path:
    """Create or validate an owner-only directory for one private capture."""

    try:
        lexical = Path(os.path.abspath(os.fspath(Path(bundle_root).expanduser())))
        validate_private_path(lexical)
        lexical.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = _open_capture_directory(lexical)
        try:
            if stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o700:
                raise ValueError
        finally:
            os.close(descriptor)
        return lexical
    except BrowserCaptureManifestError:
        raise
    except Exception:
        raise BrowserCaptureManifestError() from None


def write_browser_capture_manifest(manifest_path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Create a new private manifest without a group/other-readable write window."""

    lexical: Path | None = None
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    created = False
    try:
        lexical = Path(os.path.abspath(os.fspath(Path(manifest_path).expanduser())))
        if not isinstance(payload, Mapping):
            raise ValueError
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_MANIFEST_BYTES:
            raise ValueError
        prepare_browser_capture_directory(lexical.parent)
        directory_descriptor = _open_capture_directory(lexical.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
        file_descriptor = os.open(lexical.name, flags, 0o600, dir_fd=directory_descriptor)
        created = True
        metadata = os.fstat(file_descriptor)
        if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ValueError
        remaining = memoryview(encoded)
        while remaining:
            written = os.write(file_descriptor, remaining)
            if written <= 0:
                raise OSError
            remaining = remaining[written:]
        os.close(file_descriptor)
        file_descriptor = None
        return lexical
    except BrowserCaptureManifestError:
        raise
    except Exception:
        if created and directory_descriptor is not None and lexical is not None:
            try:
                os.unlink(lexical.name, dir_fd=directory_descriptor)
            except OSError:
                pass
        raise BrowserCaptureManifestError() from None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if directory_descriptor is not None:
            try:
                os.close(directory_descriptor)
            except OSError:
                pass


def _open_capture_directory(path: Path) -> int:
    flags = (
        os.O_RDONLY
        | os.O_DIRECTORY
        | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise ValueError
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_manifest(path: Path) -> bytes:
    file_flags = (
        os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    directory_descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        directory_descriptor = _open_capture_directory(path.parent)
        file_descriptor = os.open(path.name, file_flags, dir_fd=directory_descriptor)
        metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) & 0o077
            or metadata.st_size > MAX_MANIFEST_BYTES
        ):
            raise ValueError
        with os.fdopen(file_descriptor, "rb", closefd=True) as stream:
            file_descriptor = None
            payload = stream.read(MAX_MANIFEST_BYTES + 1)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise ValueError
        return payload
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)


class BrowserCaptureSessionProvider:
    """Session marker for an authenticated host-captured snapshot.

    Metadata reads consume the immutable snapshot even after its freshness deadline.  Resource
    streams separately require an unexpired capture, so stale local bytes cannot masquerade as a
    fresh remote verification.
    """

    def __init__(
        self,
        bundle: BrowserCaptureBundle,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.bundle = bundle
        self._now = now
        self._invalidated = False
        self._marker = object()

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        if self._invalidated or self.bundle.authentication_status is SessionStatus.UNAVAILABLE:
            raise AuthenticationRequired()
        if self.bundle.authentication_status is SessionStatus.EXPIRED:
            raise SessionExpired()
        return AuthorizedReadSession(PROVIDER_NAME, frozenset({purpose}), self._marker)

    def status(self) -> SessionStatus:
        if self._invalidated:
            return SessionStatus.UNAVAILABLE
        if self.bundle.authentication_status is not SessionStatus.READY:
            return self.bundle.authentication_status
        if _utc(self._now()) > self.bundle.expires_at:
            return SessionStatus.EXPIRED
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        del reason
        self._invalidated = True
        self._marker = object()


class BrowserCaptureProvider:
    """Read-only source provider backed by one host browser observation bundle."""

    provider_name = PROVIDER_NAME

    @property
    def requires_resource_context_refresh(self) -> bool:
        return False

    def __init__(
        self,
        bundle: BrowserCaptureBundle,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.bundle = bundle
        self._now = now
        self._session_marker: object | None = None
        self._resources = {
            resource.remote_id: resource for item in bundle.content for resource in item.resources
        }
        self._assessments_by_content = {item.content_id: item for item in bundle.assessments}

    @classmethod
    def from_manifest(
        cls,
        manifest_path: str | Path,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> BrowserCaptureProvider:
        return cls(BrowserCaptureBundle.load(manifest_path), now=now)

    @property
    def observed_at(self) -> datetime:
        """Timestamp of the host observation, never the local ingestion time."""

        return self.bundle.captured_at

    @property
    def capture_context(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "capture_id": self.bundle.capture_id,
                "capture_source_kind": "host_browser_ui",
                **{
                    f"capture_{kind}_source_path": path
                    for kind, path in self.bundle.source_paths.items()
                },
            }
        )

    def capabilities(self) -> SourceCapabilities:
        supported = {
            SourceCapability.COURSE_DISCOVERY: CapabilityState.SUPPORTED,
            SourceCapability.CONTENT_TREE: CapabilityState.SUPPORTED,
            SourceCapability.RESOURCE_METADATA: CapabilityState.SUPPORTED,
            SourceCapability.RESOURCE_STREAM: (
                CapabilityState.SUPPORTED
                if self.bundle.resource_files
                else CapabilityState.UNSUPPORTED
            ),
            SourceCapability.ANNOUNCEMENTS: (
                CapabilityState.SUPPORTED
                if self.bundle.announcement_coverage is not Coverage.UNKNOWN
                else CapabilityState.UNKNOWN
            ),
            SourceCapability.ASSESSMENT_DETAILS: (
                CapabilityState.SUPPORTED
                if self.bundle.assessment_coverage is not Coverage.UNKNOWN
                else CapabilityState.UNKNOWN
            ),
        }
        return SourceCapabilities(supported)

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        self._require(session, ReadPurpose.DISCOVERY)
        return _page((self.bundle.course,), page, self.bundle.course_coverage)

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]:
        self._require(session, ReadPurpose.CONTENT)
        if course != self.bundle.course.remote_id:
            raise SourceUnavailable()
        items = tuple(item for item in self.bundle.content if item.parent_id == parent)
        return _page(items, page, self.bundle.content_coverage)

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self._require(session, ReadPurpose.RESOURCE_METADATA)
        try:
            return self._resources[resource]
        except KeyError:
            raise SourceUnavailable() from None

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self._require(session, ReadPurpose.RESOURCE_STREAM)
        if _utc(self._now()) > self.bundle.expires_at:
            raise SessionExpired()
        try:
            captured = self.bundle.resource_files[resource]
        except KeyError:
            raise UnsupportedCapability(
                SourceCapability.RESOURCE_STREAM, CapabilityState.UNSUPPORTED
            ) from None
        stream = _open_resource_file(captured)
        return EphemeralByteStream(iter(lambda: stream.read(64 * 1024), b""), close=stream.close)

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]:
        self._require(session, ReadPurpose.ANNOUNCEMENTS)
        if course != self.bundle.course.remote_id:
            raise SourceUnavailable()
        if self.bundle.announcement_coverage is Coverage.UNKNOWN:
            raise UnsupportedCapability(SourceCapability.ANNOUNCEMENTS, CapabilityState.UNKNOWN)
        return _page(self.bundle.announcements, page, self.bundle.announcement_coverage)

    def get_assessment(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        content: ContentId,
    ) -> AssessmentSourceRecord:
        self._require(session, ReadPurpose.ASSESSMENTS)
        if course != self.bundle.course.remote_id:
            raise SourceUnavailable()
        if self.bundle.assessment_coverage is Coverage.UNKNOWN:
            raise UnsupportedCapability(
                SourceCapability.ASSESSMENT_DETAILS, CapabilityState.UNKNOWN
            )
        try:
            return self._assessments_by_content[content]
        except KeyError:
            raise SourceUnavailable() from None

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]:
        del session, course, window, page
        raise UnsupportedCapability(SourceCapability.SCHEDULE_ITEMS, CapabilityState.UNKNOWN)

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]:
        del session, course, window, page
        raise UnsupportedCapability(SourceCapability.DUE_ITEMS, CapabilityState.UNKNOWN)

    def _require(self, session: AuthorizedReadSession, purpose: ReadPurpose) -> None:
        if (
            not isinstance(session, AuthorizedReadSession)
            or session.provider != PROVIDER_NAME
            or not session.permits(purpose)
        ):
            raise AuthenticationRequired()
        if self._session_marker is None:
            self._session_marker = session.marker
        elif session.marker is not self._session_marker:
            raise AuthenticationRequired()


def _unique_object(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _invalid_json_constant(_value: str) -> None:
    raise ValueError


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(UTC)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return _utc(parsed)


def _mapping(
    value: object,
    *,
    required: set[str],
    optional: set[str] | frozenset[str] = frozenset(),
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != required | (set(value) & optional):
        raise ValueError
    if not required.issubset(value):
        raise ValueError
    return value


def _array(value: object, *, maximum: int) -> Sequence[object]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    return value


def _text(value: object, *, maximum: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError
    if any(ord(character) < 32 and character not in "\n\r\t" for character in value):
        raise ValueError
    return value


def _optional_text(value: object, *, maximum: int) -> str | None:
    return None if value is None else _text(value, maximum=maximum)


def _remote(value: object) -> str:
    remote_id = _text(value, maximum=512)
    if _REMOTE_ID.fullmatch(remote_id) is None:
        raise ValueError
    return remote_id


def _availability(value: object) -> Availability:
    if not isinstance(value, str):
        raise ValueError
    return Availability(value)


def _coverage(value: object) -> Coverage:
    if value not in {Coverage.PARTIAL.value, Coverage.UNKNOWN.value}:
        raise ValueError
    return Coverage(value)


def _source_path(value: object) -> str:
    path = _text(value, maximum=2048)
    if not path.startswith("/") or "?" in path or "#" in path or "://" in path:
        raise ValueError
    return path


def _scope(value: object, *, items: bool) -> tuple[Coverage, str, object]:
    required = {"coverage", "source_page_path", "items" if items else "item"}
    scope = _mapping(value, required=required)
    source_page_path = _source_path(scope["source_page_path"])
    return (
        _coverage(scope["coverage"]),
        source_page_path,
        scope["items" if items else "item"],
    )


def _source_time(value: object) -> SourceTime | None:
    if value is None:
        return None
    item = _mapping(
        value,
        required={"text"},
        optional={"source_timezone"},
    )
    source_text = _text(item["text"], maximum=4096)
    timezone = _optional_text(item.get("source_timezone"), maximum=128)
    instant = _visible_instant(source_text, timezone)
    if instant is not None:
        return SourceTime(instant, source_text, timezone, TemporalPrecision.EXACT_TIME)
    contains_clock = re.search(r"(?<!\d)\d{1,2}:\d{2}(?!\d)", source_text) is not None
    precision = (
        TemporalPrecision.DATE_ONLY
        if not contains_clock
        and re.search(r"(?<!\d)\d{2,4}[/.-]\d{1,2}[/.-]\d{1,2}(?!\d)", source_text)
        else TemporalPrecision.UNKNOWN
    )
    return SourceTime(None, source_text, timezone, precision)


def _visible_instant(source_text: str, source_timezone: str | None) -> datetime | None:
    match = re.search(
        r"(?<!\d)(\d{2,4})[/.-](\d{1,2})[/.-](\d{1,2})[ T]+"
        r"(\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s+(AM|PM))?(?![\dA-Za-z])",
        source_text,
        re.IGNORECASE,
    )
    zone = _visible_timezone(source_timezone)
    if match is None or zone is None:
        return None
    suffix = source_text[match.end() :].strip()
    if suffix:
        inline_zone = re.fullmatch(r"\(([^()]*)\)", suffix)
        if (
            inline_zone is None
            or source_timezone is None
            or inline_zone.group(1).casefold() != source_timezone.casefold()
        ):
            return None
    year_text, month_text, day_text, hour_text, minute_text, second_text, meridiem = match.groups()
    year, month, day = int(year_text), int(month_text), int(day_text)
    hour, minute = int(hour_text), int(minute_text)
    second = 0 if second_text is None else int(second_text)
    if year < 100:
        year += 2000
    if meridiem is not None:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if meridiem.casefold() == "pm" else 0)
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=zone).astimezone(UTC)
    except ValueError:
        return None


def _visible_timezone(value: str | None) -> timezone | None:
    if value is None:
        return None
    match = re.fullmatch(r"UTC(?:([+-])(\d{1,2})(?::?(\d{2}))?)?", value, re.IGNORECASE)
    if match is not None:
        sign, hours, minutes = match.groups()
        hour_value = int(hours or 0)
        minute_value = int(minutes or 0)
        if hour_value > 23 or minute_value >= 60:
            return None
        offset = timedelta(hours=hour_value, minutes=minute_value)
        if sign == "-":
            offset = -offset
        if not -timedelta(hours=24) < offset < timedelta(hours=24):
            return None
        return timezone(offset)
    return None


def _parse_bundle(value: object, bundle_root: Path) -> BrowserCaptureBundle:
    manifest = _mapping(
        value,
        required={
            "schema_version",
            "source_kind",
            "capture_id",
            "capture_start_at",
            "captured_at",
            "expires_at",
            "authentication_status",
            "course",
            "content",
            "announcements",
            "assessments",
        },
    )
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != SCHEMA_VERSION:
        raise ValueError
    if manifest["source_kind"] != "host_browser_ui":
        raise ValueError
    capture_id = _text(manifest["capture_id"], maximum=128)
    if _CAPTURE_ID.fullmatch(capture_id) is None:
        raise ValueError
    capture_start_at = _timestamp(manifest["capture_start_at"])
    captured_at = _timestamp(manifest["captured_at"])
    expires_at = _timestamp(manifest["expires_at"])
    if not capture_start_at <= captured_at < expires_at <= captured_at + MAX_CAPTURE_TTL:
        raise ValueError
    if captured_at > datetime.now(UTC) + MAX_FUTURE_SKEW:
        raise ValueError
    status_value = _text(manifest["authentication_status"], maximum=16)
    authentication_status = SessionStatus(status_value)

    course_coverage, course_path, course_value = _scope(manifest["course"], items=False)
    course = _parse_course(course_value)
    content_coverage, content_path, content_value = _scope(manifest["content"], items=True)
    content, files = _parse_content(
        content_value,
        course.remote_id,
        bundle_root,
        capture_start_at,
        captured_at,
    )
    announcement_coverage, announcement_path, announcement_value = _scope(
        manifest["announcements"], items=True
    )
    announcements = tuple(
        _parse_announcement(item, course.remote_id)
        for item in _array(announcement_value, maximum=500)
    )
    assessment_coverage, assessment_path, assessment_value = _scope(
        manifest["assessments"], items=True
    )
    assessments = tuple(
        _parse_assessment(item, course.remote_id) for item in _array(assessment_value, maximum=500)
    )
    _unique_ids(content, announcements, assessments)
    content_ids = {item.remote_id for item in content}
    if any(item.content_id not in content_ids for item in assessments):
        raise ValueError
    content = _project_assessment_content(content, assessments)
    return BrowserCaptureBundle(
        capture_id,
        capture_start_at,
        captured_at,
        expires_at,
        authentication_status,
        course,
        course_coverage,
        content,
        content_coverage,
        announcements,
        announcement_coverage,
        assessments,
        assessment_coverage,
        files,
        {
            "course": course_path,
            "content": content_path,
            "announcements": announcement_path,
            "assessments": assessment_path,
        },
    )


def _parse_course(value: object) -> CourseSourceRecord:
    item = _mapping(
        value,
        required={"remote_id", "code", "title", "availability"},
        optional={"term"},
    )
    return CourseSourceRecord(
        CourseId(PROVIDER_NAME, _remote(item["remote_id"])),
        _text(item["code"], maximum=256),
        _text(item["title"], maximum=4096),
        _optional_text(item.get("term"), maximum=512),
        _availability(item["availability"]),
    )


def _parse_content(
    value: object,
    course: CourseId,
    bundle_root: Path,
    capture_start_at: datetime,
    captured_at: datetime,
) -> tuple[tuple[ContentSourceRecord, ...], Mapping[AttachmentId, _ResourceFile]]:
    raw_items = _array(value, maximum=10_000)
    records: list[ContentSourceRecord] = []
    files: dict[AttachmentId, _ResourceFile] = {}
    ids: set[ContentId] = set()
    for value_item in raw_items:
        item = _mapping(
            value_item,
            required={
                "remote_id",
                "parent_remote_id",
                "handler_kind",
                "title",
                "position",
                "availability",
                "is_container",
                "resources",
            },
            optional={"sanitized_metadata"},
        )
        remote_id = ContentId(PROVIDER_NAME, _remote(item["remote_id"]))
        if remote_id in ids:
            raise ValueError
        ids.add(remote_id)
        parent_value = item["parent_remote_id"]
        parent_id = (
            None if parent_value is None else ContentId(PROVIDER_NAME, _remote(parent_value))
        )
        position = item["position"]
        if isinstance(position, bool) or not isinstance(position, int) or position < 0:
            raise ValueError
        if not isinstance(item["is_container"], bool):
            raise ValueError
        metadata = item.get("sanitized_metadata", {})
        if not isinstance(metadata, Mapping) or not set(metadata).issubset(
            {"content_type", "display_style", "module_label"}
        ):
            raise ValueError
        resources: list[ResourceMetadataRecord] = []
        for raw_resource in _array(item["resources"], maximum=100):
            resource, captured_file = _parse_resource(
                raw_resource, remote_id, bundle_root, capture_start_at, captured_at
            )
            resources.append(resource)
            if captured_file is not None:
                files[resource.remote_id] = captured_file
        records.append(
            ContentSourceRecord(
                remote_id,
                course,
                parent_id,
                _text(item["handler_kind"], maximum=256),
                _text(item["title"], maximum=4096),
                position,
                _availability(item["availability"]),
                item["is_container"],
                dict(metadata),
                tuple(resources),
            )
        )
    if any(item.parent_id is not None and item.parent_id not in ids for item in records):
        raise ValueError
    _validate_content_tree(records)
    total_size = sum(item.byte_size for item in files.values())
    if total_size > MAX_BUNDLE_BYTES:
        raise ValueError
    return tuple(records), files


def _parse_resource(
    value: object,
    content: ContentId,
    bundle_root: Path,
    capture_start_at: datetime,
    captured_at: datetime,
) -> tuple[ResourceMetadataRecord, _ResourceFile | None]:
    item = _mapping(
        value,
        required={"display_title", "original_filename"},
        optional={"declared_mime", "candidate_modified_at", "file"},
    )
    remote_id = AttachmentId(PROVIDER_NAME, f"ui-file:{content.value}")
    modified = item.get("candidate_modified_at")
    record = ResourceMetadataRecord(
        remote_id,
        content,
        _text(item["display_title"], maximum=4096),
        _text(item["original_filename"], maximum=4096),
        _optional_text(item.get("declared_mime"), maximum=256),
        None if modified is None else _timestamp(modified),
    )
    file_value = item.get("file")
    if file_value is None:
        return record, None
    captured_file = _mapping(
        file_value,
        required={"relative_path", "downloaded_at", "fresh_for_capture"},
    )
    if captured_file["fresh_for_capture"] is not True:
        raise ValueError
    downloaded_at = _timestamp(captured_file["downloaded_at"])
    if not capture_start_at <= downloaded_at <= captured_at + MAX_FUTURE_SKEW:
        raise ValueError
    resource_file = _resource_path(bundle_root, captured_file["relative_path"], downloaded_at)
    return record, resource_file


def _resource_path(bundle_root: Path, value: object, downloaded_at: datetime) -> _ResourceFile:
    relative_text = _text(value, maximum=1024)
    if "\\" in relative_text:
        raise ValueError
    relative = PurePosixPath(relative_text)
    if (
        relative.is_absolute()
        or not relative.parts
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError
    lexical_root = Path(os.path.abspath(os.fspath(bundle_root)))
    if lexical_root.is_symlink():
        raise ValueError
    root_metadata = lexical_root.lstat()
    if not stat.S_ISDIR(root_metadata.st_mode):
        raise ValueError
    candidate = lexical_root.joinpath(*relative.parts)
    current = lexical_root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError
    resolved_root = lexical_root.resolve()
    resolved = candidate.resolve(strict=True)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError
    metadata = candidate.lstat()
    if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_RESOURCE_BYTES:
        raise ValueError
    return _ResourceFile(
        resolved_root,
        tuple(relative.parts),
        downloaded_at,
        root_metadata.st_dev,
        root_metadata.st_ino,
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
    )


def _open_resource_file(value: _ResourceFile) -> Any:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    file_descriptor: int | None = None
    try:
        descriptor = os.open(value.root, directory_flags)
        root_metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_dev != value.root_device
            or root_metadata.st_ino != value.root_inode
        ):
            raise SourceUnavailable()
        for part in value.parts[:-1]:
            next_descriptor = os.open(part, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(value.parts[-1], file_flags, dir_fd=descriptor)
        metadata = os.fstat(file_descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_dev != value.device
            or metadata.st_ino != value.inode
            or metadata.st_size != value.byte_size
        ):
            raise SourceUnavailable()
        stream = os.fdopen(file_descriptor, "rb", closefd=True)
        file_descriptor = None
        return stream
    except SourceUnavailable:
        raise
    except OSError:
        raise SourceUnavailable() from None
    finally:
        if file_descriptor is not None:
            try:
                os.close(file_descriptor)
            except OSError:
                pass
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _parse_announcement(value: object, course: CourseId) -> AnnouncementSourceRecord:
    item = _mapping(
        value,
        required={"remote_id", "title", "body", "availability"},
        optional={
            "created_at",
            "modified_at",
            "published_at",
            "available_from",
            "available_until",
        },
    )
    return AnnouncementSourceRecord(
        AnnouncementId(PROVIDER_NAME, _remote(item["remote_id"])),
        course,
        _text(item["title"], maximum=4096),
        _text(item["body"], maximum=256 * 1024, empty=True),
        _availability(item["availability"]),
        _source_time(item.get("created_at")),
        _source_time(item.get("modified_at")),
        _source_time(item.get("published_at")),
        _source_time(item.get("available_from")),
        _source_time(item.get("available_until")),
    )


def _parse_assessment(value: object, course: CourseId) -> AssessmentSourceRecord:
    item = _mapping(
        value,
        required={
            "content_remote_id",
            "title",
            "availability",
        },
        optional={
            "grading_column_remote_id",
            "kind_text",
            "instructions_text",
            "created_at",
            "modified_at",
            "available_from",
            "available_until",
            "open_at",
            "close_at",
            "due_at",
            "grading_due_at",
            "generic_due_at",
        },
    )
    grading_value = item.get("grading_column_remote_id")
    return AssessmentSourceRecord(
        AssessmentId(
            PROVIDER_NAME,
            f"ui-assessment:{_remote(item['content_remote_id'])}",
        ),
        course,
        ContentId(PROVIDER_NAME, _remote(item["content_remote_id"])),
        None if grading_value is None else GradingColumnId(PROVIDER_NAME, _remote(grading_value)),
        _text(item["title"], maximum=4096),
        _assessment_subtype(item.get("kind_text")),
        ""
        if item.get("instructions_text") is None
        else _text(item["instructions_text"], maximum=256 * 1024, empty=True),
        _availability(item["availability"]),
        _source_time(item.get("created_at")),
        _source_time(item.get("modified_at")),
        _source_time(item.get("available_from")),
        _source_time(item.get("available_until")),
        _source_time(item.get("open_at")),
        _source_time(item.get("close_at")),
        _source_time(item.get("due_at")),
        _source_time(item.get("grading_due_at")),
        _source_time(item.get("generic_due_at")),
    )


def _assessment_subtype(value: object) -> AssessmentSubtype:
    if value is None:
        return AssessmentSubtype.OTHER
    label = _text(value, maximum=256).casefold()
    mappings = (
        ("assignment", AssessmentSubtype.ASSIGNMENT),
        ("quiz", AssessmentSubtype.QUIZ),
        ("test", AssessmentSubtype.TEST),
        ("exam", AssessmentSubtype.EXAM),
        ("presentation", AssessmentSubtype.PRESENTATION),
    )
    return next((kind for token, kind in mappings if token in label), AssessmentSubtype.OTHER)


def _project_assessment_content(
    content: Sequence[ContentSourceRecord],
    assessments: Sequence[AssessmentSourceRecord],
) -> tuple[ContentSourceRecord, ...]:
    assessment_ids = {item.content_id for item in assessments}
    return tuple(
        replace(item, handler_kind=f"assessment:{item.handler_kind}")
        if item.remote_id in assessment_ids
        and "assessment" not in item.handler_kind.casefold()
        and "asmt" not in item.handler_kind.casefold()
        else item
        for item in content
    )


def _unique_ids(
    content: Sequence[ContentSourceRecord],
    announcements: Sequence[AnnouncementSourceRecord],
    assessments: Sequence[AssessmentSourceRecord],
) -> None:
    for records in (content, announcements, assessments):
        ids = [item.remote_id for item in records]
        if len(ids) != len(set(ids)):
            raise ValueError
    resources = [resource.remote_id for item in content for resource in item.resources]
    if len(resources) != len(set(resources)):
        raise ValueError


def _validate_content_tree(records: Sequence[ContentSourceRecord]) -> None:
    parents = {item.remote_id: item.parent_id for item in records}
    for start in parents:
        seen: set[ContentId] = set()
        current: ContentId | None = start
        while current is not None:
            if current in seen:
                raise ValueError
            seen.add(current)
            current = parents.get(current)


def _page(items: Sequence[_T], request: PageRequest, coverage: Coverage) -> Page[_T]:
    if not isinstance(request, PageRequest):
        raise SourceProtocolError()
    if request.cursor is None:
        offset = 0
    elif request.cursor.isdigit():
        offset = int(request.cursor)
    else:
        raise SourceProtocolError()
    if offset < 0 or offset > len(items):
        raise SourceProtocolError()
    selected = tuple(items[offset : offset + request.page_size])
    next_offset = offset + len(selected)
    next_cursor = str(next_offset) if next_offset < len(items) else None
    return Page(selected, next_cursor, coverage)


__all__ = [
    "BROWSER_CAPTURE_ERROR_MESSAGE",
    "BrowserCaptureBundle",
    "BrowserCaptureManifestError",
    "BrowserCaptureProvider",
    "BrowserCaptureSessionProvider",
    "MAX_BUNDLE_BYTES",
    "MAX_CAPTURE_TTL",
    "MAX_MANIFEST_BYTES",
    "MAX_RESOURCE_BYTES",
    "PROVIDER_NAME",
    "SCHEMA_VERSION",
    "prepare_browser_capture_directory",
    "write_browser_capture_manifest",
]
