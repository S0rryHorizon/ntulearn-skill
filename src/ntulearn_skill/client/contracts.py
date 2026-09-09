"""Typed source and session contracts for read-only synchronization."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import Generic, Protocol, TypeVar

from ntulearn_skill.core import (
    AnnouncementId,
    AssessmentId,
    AssessmentSubtype,
    AttachmentId,
    Availability,
    CalendarItemId,
    ContentId,
    CourseId,
    Coverage,
    GradingColumnId,
    SourceTime,
)


class ReadPurpose(StrEnum):
    DISCOVERY = "discovery"
    CONTENT = "content"
    RESOURCE_METADATA = "resource_metadata"
    RESOURCE_STREAM = "resource_stream"
    ANNOUNCEMENTS = "announcements"
    ASSESSMENTS = "assessments"
    SCHEDULE = "schedule"
    DUE_ITEMS = "due_items"


class SessionStatus(StrEnum):
    READY = "READY"
    EXPIRED = "EXPIRED"
    UNAVAILABLE = "UNAVAILABLE"


class SourceCapability(StrEnum):
    COURSE_DISCOVERY = "course_discovery"
    CONTENT_TREE = "content_tree"
    RESOURCE_METADATA = "resource_metadata"
    RESOURCE_STREAM = "resource_stream"
    ANNOUNCEMENTS = "announcements"
    ASSESSMENT_DETAILS = "assessment_details"
    SCHEDULE_ITEMS = "schedule_items"
    DUE_ITEMS = "due_items"
    AUTOMATIC_SESSION_RENEWAL = "automatic_session_renewal"
    REMOTE_DELTA = "remote_delta"
    CONDITIONAL_RESOURCE_READ = "conditional_resource_read"
    COMPLETE_CROSS_COURSE_CALENDAR = "complete_cross_course_calendar"


class CapabilityState(StrEnum):
    SUPPORTED = "SUPPORTED"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceCapabilities:
    """Explicit provider capabilities; unknown behavior never becomes an optimization."""

    states: Mapping[SourceCapability, CapabilityState]

    def __post_init__(self) -> None:
        copied = dict(self.states)
        if any(
            not isinstance(capability, SourceCapability) or not isinstance(state, CapabilityState)
            for capability, state in copied.items()
        ):
            raise ValueError("capability map contains an invalid value")
        object.__setattr__(self, "states", MappingProxyType(copied))

    def state(self, capability: SourceCapability) -> CapabilityState:
        return self.states.get(capability, CapabilityState.UNSUPPORTED)

    def require(self, capability: SourceCapability) -> None:
        state = self.state(capability)
        if state is not CapabilityState.SUPPORTED:
            raise UnsupportedCapability(capability, state)


class SourceError(RuntimeError):
    """Base for privacy-safe source errors with a stable category."""

    category = "source_error"


class AuthenticationRequired(SourceError):
    category = "authentication_required"

    def __init__(self) -> None:
        super().__init__("an authorized read session is required")


class SessionExpired(SourceError):
    category = "session_expired"

    def __init__(self) -> None:
        super().__init__("the authorized read session expired")


class SourceAccessDenied(SourceError):
    category = "source_access_denied"

    def __init__(self) -> None:
        super().__init__("the source refused this authorized read")


class ReadPolicyViolation(SourceError):
    category = "read_policy_violation"

    def __init__(self) -> None:
        super().__init__("the requested operation is outside the read-only policy")


class SourceProtocolError(SourceError):
    category = "source_protocol_error"

    def __init__(self) -> None:
        super().__init__("the source response did not match a supported shape")


class SourceUnavailable(SourceError):
    category = "source_unavailable"

    def __init__(self) -> None:
        super().__init__("the source read could not be completed")


class PaginationLimitReached(SourceError):
    category = "pagination_limit_reached"

    def __init__(self) -> None:
        super().__init__("the configured pagination limit was reached")


class PaginationCycle(SourceError):
    category = "pagination_cycle"

    def __init__(self) -> None:
        super().__init__("the source returned a pagination cycle")


class UnsupportedCapability(SourceError):
    category = "unsupported_capability"

    def __init__(self, capability: SourceCapability, state: CapabilityState) -> None:
        self.capability = capability
        self.state = state
        super().__init__(f"source capability is {state.value.lower()}: {capability.value}")


_SOURCE_ERROR_CATEGORIES = frozenset(
    {
        SourceError.category,
        AuthenticationRequired.category,
        SessionExpired.category,
        SourceAccessDenied.category,
        ReadPolicyViolation.category,
        SourceProtocolError.category,
        SourceUnavailable.category,
        PaginationLimitReached.category,
        PaginationCycle.category,
        UnsupportedCapability.category,
    }
)


def safe_source_error_category(value: object) -> str:
    """Return only a fixed public category from an untrusted source boundary."""

    if isinstance(value, str) and value in _SOURCE_ERROR_CATEGORIES:
        return value
    return SourceUnavailable.category


@dataclass(frozen=True, slots=True, repr=False)
class AuthorizedReadSession:
    """Opaque marker owned by a session provider.

    Authentication material is deliberately not a field on this object. Concrete providers keep it
    in their own private state keyed by the marker. The marker identifies one authorization
    context, rather than one requested purpose: sessions acquired for different purposes in the
    same valid context must carry the same marker object.
    """

    provider: str
    purposes: frozenset[ReadPurpose]
    marker: object = field(default_factory=object, compare=False)

    def __post_init__(self) -> None:
        purposes = frozenset(self.purposes)
        if (
            not isinstance(self.provider, str)
            or not self.provider.strip()
            or not self.provider.isascii()
            or any(not (character.isalnum() or character in "-._") for character in self.provider)
            or not purposes
            or any(not isinstance(purpose, ReadPurpose) for purpose in purposes)
            or type(self.marker) is not object
        ):
            raise ValueError("session provider and purposes must be non-empty")
        object.__setattr__(self, "purposes", purposes)

    def permits(self, purpose: ReadPurpose) -> bool:
        return purpose in self.purposes

    def __repr__(self) -> str:
        return f"AuthorizedReadSession(provider={self.provider!r}, purposes=<redacted>)"


class SessionProvider(Protocol):
    """Supply purpose-limited views of one stable authorization context.

    Repeated ``acquire`` calls for the same still-valid authorization context must return sessions
    whose ``marker`` is the same object, including when ``purpose`` differs. A provider must change
    the marker when it moves to a different authorization context (for example after re-login or
    account switching). Source adapters use marker identity to prevent discoveries made under one
    context from authorizing reads under another.
    """

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession: ...

    def status(self) -> SessionStatus: ...

    def invalidate(self, reason: str) -> None: ...


@dataclass(frozen=True, slots=True)
class PageRequest:
    cursor: str | None = None
    page_size: int = 100

    def __post_init__(self) -> None:
        if (
            isinstance(self.page_size, bool)
            or not isinstance(self.page_size, int)
            or self.page_size <= 0
            or self.page_size > 1_000
        ):
            raise ValueError("page_size must be between 1 and 1000")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or not self.cursor
            or not self.cursor.isascii()
            or len(self.cursor) > 256
        ):
            raise ValueError("cursor must be a non-empty ASCII value")


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    next_cursor: str | None
    coverage_for_page: Coverage

    def __post_init__(self) -> None:
        object.__setattr__(self, "items", tuple(self.items))
        if self.coverage_for_page not in {
            Coverage.COMPLETE,
            Coverage.PARTIAL,
            Coverage.UNKNOWN,
        }:
            raise ValueError("page coverage must describe the current remote observation")
        if self.next_cursor is not None and (
            not isinstance(self.next_cursor, str)
            or not self.next_cursor
            or not self.next_cursor.isascii()
            or len(self.next_cursor) > 256
        ):
            raise ValueError("next_cursor must be non-empty when present")


@dataclass(frozen=True, slots=True)
class CourseSourceRecord:
    remote_id: CourseId
    code: str
    title: str
    term: str | None
    availability: Availability


@dataclass(frozen=True, slots=True)
class ResourceMetadataRecord:
    remote_id: AttachmentId
    content_id: ContentId
    display_title: str
    original_filename: str
    declared_mime: str | None = None
    candidate_modified_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ContentSourceRecord:
    remote_id: ContentId
    course_id: CourseId
    parent_id: ContentId | None
    handler_kind: str
    title: str
    position: int
    availability: Availability
    is_container: bool
    sanitized_metadata: Mapping[str, object] = field(default_factory=dict)
    resources: tuple[ResourceMetadataRecord, ...] = ()


@dataclass(frozen=True, slots=True)
class TimeWindow:
    """A bounded, aware source-query window."""

    since: datetime
    until: datetime

    def __post_init__(self) -> None:
        if (
            self.since.tzinfo is None
            or self.since.utcoffset() is None
            or self.until.tzinfo is None
            or self.until.utcoffset() is None
            or self.since >= self.until
        ):
            raise ValueError("time window must contain ordered aware timestamps")


@dataclass(frozen=True, slots=True)
class AnnouncementSourceRecord:
    remote_id: AnnouncementId
    course_id: CourseId
    title: str
    body: str
    availability: Availability
    created_at: SourceTime | None = None
    modified_at: SourceTime | None = None
    published_at: SourceTime | None = None
    available_from: SourceTime | None = None
    available_until: SourceTime | None = None


@dataclass(frozen=True, slots=True)
class AssessmentSourceRecord:
    remote_id: AssessmentId
    course_id: CourseId
    content_id: ContentId
    grading_column_id: GradingColumnId | None
    title: str
    subtype: AssessmentSubtype
    instructions: str
    availability: Availability
    created_at: SourceTime | None = None
    modified_at: SourceTime | None = None
    available_from: SourceTime | None = None
    available_until: SourceTime | None = None
    open_at: SourceTime | None = None
    close_at: SourceTime | None = None
    due_at: SourceTime | None = None
    grading_due_at: SourceTime | None = None
    generic_due_at: SourceTime | None = None


@dataclass(frozen=True, slots=True)
class ScheduleSourceRecord:
    remote_id: CalendarItemId
    course_id: CourseId
    title: str
    availability: Availability
    start_at: SourceTime | None = None
    end_at: SourceTime | None = None
    location: str | None = None


@dataclass(frozen=True, slots=True)
class DueSourceRecord:
    remote_id: CalendarItemId
    course_id: CourseId
    title: str
    calendar_id: str
    item_source_id: str | None
    item_source_type: str | None
    availability: Availability
    # These fields retain the provider's source names.  They are not automatically
    # interpreted as event start/due semantics.
    source_start_at: SourceTime | None = None
    source_end_at: SourceTime | None = None
    due_at: SourceTime | None = None


class EphemeralByteStream:
    """One-shot resource bytes that retain no transport URL outside the transport layer."""

    def __init__(self, chunks: Iterable[bytes], *, close: object | None = None) -> None:
        self._close = close
        self._closed = False
        self._buffer = bytearray()
        try:
            self._iterator: Iterator[bytes] | None = iter(chunks)
        except Exception:
            self.close()
            raise SourceUnavailable() from None

    def __repr__(self) -> str:
        return "EphemeralByteStream(<redacted>)"

    def __enter__(self) -> EphemeralByteStream:
        if self._closed:
            raise RuntimeError("resource stream is closed")
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __iter__(self) -> Iterator[bytes]:
        if self._closed:
            raise RuntimeError("resource stream is closed or already consumed")
        try:
            while chunk := self.read(64 * 1024):
                yield chunk
        finally:
            self.close()

    def read(self, size: int = -1) -> bytes:
        """Read like a binary file while closing the remote response on EOF or failure."""

        if self._closed:
            return b""
        if size == 0:
            return b""
        if size < -1:
            raise ValueError("size must be -1 or non-negative")
        try:
            if size == -1:
                while self._iterator is not None:
                    self._read_next()
                result = bytes(self._buffer)
                self._buffer.clear()
                self.close()
                return result
            while len(self._buffer) < size and self._iterator is not None:
                self._read_next()
            result = bytes(self._buffer[:size])
            del self._buffer[:size]
            if self._iterator is None and not self._buffer:
                self.close()
            return result
        except SourceProtocolError:
            self.close()
            raise SourceProtocolError() from None
        except Exception:
            self.close()
            raise SourceUnavailable() from None

    def _read_next(self) -> None:
        assert self._iterator is not None
        try:
            chunk = next(self._iterator)
        except StopIteration:
            self._iterator = None
            return
        if not isinstance(chunk, bytes):
            raise SourceProtocolError()
        self._buffer.extend(chunk)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        callback = self._close
        self._close = None
        if callable(callback):
            try:
                callback()
            except Exception:
                pass


class SourceProvider(Protocol):
    @property
    def provider_name(self) -> str: ...

    @property
    def requires_resource_context_refresh(self) -> bool:
        """Whether standalone byte reads require fresh discovery-bound authorization context."""
        ...

    def capabilities(self) -> SourceCapabilities: ...

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]: ...

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]: ...

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord: ...

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream: ...

    def list_announcements(
        self, session: AuthorizedReadSession, course: CourseId, page: PageRequest
    ) -> Page[AnnouncementSourceRecord]: ...

    def get_assessment(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        content: ContentId,
    ) -> AssessmentSourceRecord: ...

    def list_schedule_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[ScheduleSourceRecord]: ...

    def list_due_items(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        window: TimeWindow,
        page: PageRequest,
    ) -> Page[DueSourceRecord]: ...
