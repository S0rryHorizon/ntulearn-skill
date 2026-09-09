"""NTULearn-specific translation behind the typed read-only source boundary."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from urllib.parse import parse_qs, urlsplit

from ntulearn_skill.client.contracts import (
    AuthenticationRequired,
    AuthorizedReadSession,
    CapabilityState,
    ContentSourceRecord,
    CourseSourceRecord,
    EphemeralByteStream,
    Page,
    PageRequest,
    ReadPurpose,
    ResourceMetadataRecord,
    SourceAccessDenied,
    SourceCapabilities,
    SourceCapability,
    SourceProtocolError,
    SourceUnavailable,
)
from ntulearn_skill.client.transport import ReadOnlyTransport, ReadOperation, SafeReadRequest
from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId, Coverage

_COURSE_QUERY = (
    ("organization", "false"),
    ("includeCount", "true"),
    ("expand", "course.effectiveAvailability"),
    ("sort", "lastAccessDate(desc:nullslast)"),
)
_CONTENT_QUERY = (
    ("@view", "Summary"),
    ("expand", "assignedGroups,selfEnrollmentGroups.group,gradebookCategory"),
    ("includeInActivityTracking", "true"),
)
_UNKNOWN_CAPABILITIES = frozenset(
    {
        SourceCapability.AUTOMATIC_SESSION_RENEWAL,
        SourceCapability.REMOTE_DELTA,
        SourceCapability.CONDITIONAL_RESOURCE_READ,
        SourceCapability.COMPLETE_CROSS_COURSE_CALENDAR,
    }
)


class NtulearnSourceAdapter:
    """Translate validated source response shapes into provider-neutral records.

    There is no automatic browser or SSO bridge. A concrete session provider and guarded transport
    must be supplied by the caller. Resource streaming is unsupported unless a route factory can
    derive the fixed, guarded retrieval operation from a discovered attachment.
    """

    def __init__(
        self,
        transport: ReadOnlyTransport,
        *,
        source_origin: str,
        provider_name: str = "ntulearn",
        resource_request_factory: Callable[[AttachmentId], SafeReadRequest] | None = None,
    ) -> None:
        origin = urlsplit(source_origin)
        try:
            port = origin.port
        except ValueError:
            raise ValueError("source_origin must be an HTTPS origin") from None
        if (
            origin.scheme != "https"
            or not origin.hostname
            or origin.username is not None
            or origin.password is not None
            or (port is not None and port != 443)
            or origin.path not in {"", "/"}
            or origin.query
            or origin.fragment
        ):
            raise ValueError("source_origin must be an HTTPS origin")
        if not provider_name.strip():
            raise ValueError("provider_name must be non-empty")
        self._transport = transport
        self._origin = f"https://{origin.hostname.lower()}"
        self._provider_name = provider_name
        self._resource_request_factory = resource_request_factory
        self._authorization_marker: object | None = None
        self._authorized_courses: set[CourseId] = set()
        self._containers: dict[CourseId, set[ContentId]] = {}
        self._resources: dict[AttachmentId, ResourceMetadataRecord] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def capabilities(self) -> SourceCapabilities:
        supported = {
            SourceCapability.COURSE_DISCOVERY,
            SourceCapability.CONTENT_TREE,
            SourceCapability.RESOURCE_METADATA,
        }
        if self._resource_request_factory is not None:
            supported.add(SourceCapability.RESOURCE_STREAM)
        states = {
            capability: (
                CapabilityState.SUPPORTED
                if capability in supported
                else CapabilityState.UNKNOWN
                if capability in _UNKNOWN_CAPABILITIES
                else CapabilityState.UNSUPPORTED
            )
            for capability in SourceCapability
        }
        return SourceCapabilities(MappingProxyType(states))

    def list_courses(
        self, session: AuthorizedReadSession, page: PageRequest
    ) -> Page[CourseSourceRecord]:
        self._require_session(session, ReadPurpose.DISCOVERY)
        self._activate_session(session)
        self.capabilities().require(SourceCapability.COURSE_DISCOVERY)
        expected_path = "/learn/api/v1/users/me/memberships"
        offset = self._read_cursor("courses", page.cursor)
        request = SafeReadRequest(
            ReadOperation.DISCOVER_COURSES,
            expected_path,
            (*_COURSE_QUERY, ("limit", str(page.page_size)), ("offset", str(offset))),
        )
        response = self._transport.send(request, session)
        try:
            payload = self._json(response.json_body)
            translated = self.translate_courses_payload(
                payload, page=page, expected_path=expected_path
            )
        finally:
            ReadOnlyTransport._safe_close(response)
        self._authorized_courses.update(item.remote_id for item in translated.items)
        return translated

    def list_content(
        self,
        session: AuthorizedReadSession,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
    ) -> Page[ContentSourceRecord]:
        self._require_session(session, ReadPurpose.CONTENT)
        self._require_active_session(session)
        self.capabilities().require(SourceCapability.CONTENT_TREE)
        self._require_course(course)
        if parent is not None:
            self._require_id(parent, ContentId)
            if parent not in self._containers.get(course, set()):
                raise SourceAccessDenied()
        course_segment = self._segment(course.value)
        parent_segment = "ROOT" if parent is None else self._segment(parent.value)
        expected_path = f"/learn/api/v1/courses/{course_segment}/contents/{parent_segment}/children"
        scope = self._content_scope(course, parent)
        offset = self._read_cursor(scope, page.cursor)
        request = SafeReadRequest(
            ReadOperation.LIST_CONTENT_CHILDREN,
            expected_path,
            (*_CONTENT_QUERY, ("limit", str(page.page_size)), ("offset", str(offset))),
        )
        response = self._transport.send(request, session)
        try:
            payload = self._json(response.json_body)
            translated = self.translate_content_payload(
                payload,
                course=course,
                parent=parent,
                page=page,
                expected_path=expected_path,
            )
        finally:
            ReadOnlyTransport._safe_close(response)
        containers = self._containers.setdefault(course, set())
        for item in translated.items:
            if item.is_container:
                containers.add(item.remote_id)
            for resource in item.resources:
                self._resources[resource.remote_id] = resource
        return translated

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self._require_session(session, ReadPurpose.RESOURCE_METADATA)
        self._require_active_session(session)
        self.capabilities().require(SourceCapability.RESOURCE_METADATA)
        self._require_id(resource, AttachmentId)
        record = self._resources.get(resource)
        if record is None:
            raise SourceAccessDenied()
        return record

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self._require_session(session, ReadPurpose.RESOURCE_STREAM)
        self.capabilities().require(SourceCapability.RESOURCE_STREAM)
        self._require_active_session(session)
        self._require_id(resource, AttachmentId)
        record = self._resources.get(resource)
        if record is None or self._resource_request_factory is None:
            raise SourceAccessDenied()
        try:
            request = self._resource_request_factory(resource)
        except Exception:
            raise SourceUnavailable() from None
        resource_segment = self._segment(resource.value)
        content_segment = self._segment(record.content_id.value)
        expected_path = (
            f"/bbcswebdav/pid-{content_segment}-dt-content-rid-{resource_segment}"
            f"/xid-{resource_segment}"
        )
        if (
            not isinstance(request, SafeReadRequest)
            or request.operation is not ReadOperation.OPEN_RESOURCE_STREAM
            or request.path != expected_path
        ):
            raise SourceProtocolError()
        response = self._transport.send(request, session)
        if response.body_chunks is None:
            ReadOnlyTransport._safe_close(response)
            raise SourceProtocolError()
        return EphemeralByteStream(response.body_chunks, close=response.close)

    def translate_courses_payload(
        self,
        payload: Mapping[str, object],
        *,
        page: PageRequest,
        expected_path: str = "/learn/api/v1/users/me/memberships",
    ) -> Page[CourseSourceRecord]:
        offset = self._read_cursor("courses", page.cursor)
        results, next_offset, page_coverage = self._page_parts(payload, offset, expected_path)
        items: list[CourseSourceRecord] = []
        for raw in results:
            membership = self._mapping(raw)
            course = self._mapping(membership.get("course"))
            remote_id = CourseId(self.provider_name, self._required_text(course, "id"))
            code = self._first_text(course, "courseId", "displayId")
            title = self._first_text(course, "displayName", "name")
            if code is None or title is None:
                raise SourceProtocolError()
            term_value = course.get("term")
            term = None
            if isinstance(term_value, str) and term_value.strip():
                term = term_value
            elif isinstance(term_value, Mapping):
                term = self._first_text(term_value, "name", "displayName")
            availability_value = membership.get("isAvailable", course.get("isAvailable"))
            if isinstance(availability_value, bool):
                availability = (
                    Availability.ACTIVE if availability_value else Availability.UNAVAILABLE
                )
            else:
                availability = Availability.UNKNOWN
            items.append(CourseSourceRecord(remote_id, code, title, term, availability))
        next_cursor = None if next_offset is None else self._make_cursor("courses", next_offset)
        return Page(tuple(items), next_cursor, page_coverage)

    def translate_content_payload(
        self,
        payload: Mapping[str, object],
        *,
        course: CourseId,
        parent: ContentId | None,
        page: PageRequest,
        expected_path: str,
    ) -> Page[ContentSourceRecord]:
        self._require_id(course, CourseId)
        if parent is not None:
            self._require_id(parent, ContentId)
        scope = self._content_scope(course, parent)
        offset = self._read_cursor(scope, page.cursor)
        results, next_offset, page_coverage = self._page_parts(payload, offset, expected_path)
        items: list[ContentSourceRecord] = []
        for raw in results:
            source = self._mapping(raw)
            source_course = source.get("courseId")
            if source_course is not None and source_course != course.value:
                raise SourceProtocolError()
            remote_id = ContentId(self.provider_name, self._required_text(source, "id"))
            title = self._required_text(source, "title")
            position = source.get("position")
            if isinstance(position, bool) or not isinstance(position, int) or position < 0:
                raise SourceProtocolError()
            handler = source.get("contentHandler")
            if isinstance(handler, Mapping):
                handler_kind = self._first_text(handler, "id", "name")
            else:
                handler_kind = handler if isinstance(handler, str) and handler.strip() else None
            if handler_kind is None:
                raise SourceProtocolError()
            detail = self._handler_detail(source.get("contentDetail"), handler_kind)
            is_container = bool(
                detail.get("isFolder") is True
                or source.get("hasChildren") is True
                or "folder" in handler_kind.lower()
                or "module" in handler_kind.lower()
            )
            display_style = source.get("renderType")
            metadata = {
                "content_type": handler_kind,
                "display_style": display_style
                if isinstance(display_style, str) and len(display_style) <= 100
                else "",
            }
            resources = self._resource_metadata(source, detail, remote_id)
            items.append(
                ContentSourceRecord(
                    remote_id=remote_id,
                    course_id=course,
                    parent_id=parent,
                    handler_kind=handler_kind,
                    title=title,
                    position=position,
                    availability=self._content_availability(source),
                    is_container=is_container,
                    sanitized_metadata=MappingProxyType(metadata),
                    resources=resources,
                )
            )
        next_cursor = None if next_offset is None else self._make_cursor(scope, next_offset)
        return Page(tuple(items), next_cursor, page_coverage)

    def _resource_metadata(
        self,
        source: Mapping[str, object],
        detail: Mapping[str, object],
        content_id: ContentId,
    ) -> tuple[ResourceMetadataRecord, ...]:
        candidates: object = source.get("attachments")
        if candidates is None:
            candidates = detail.get("attachments")
        if candidates is None and isinstance(detail.get("file"), Mapping):
            candidates = [detail["file"]]
        if candidates is None:
            return ()
        if not isinstance(candidates, Sequence) or isinstance(candidates, (str, bytes, bytearray)):
            raise SourceProtocolError()
        records: list[ResourceMetadataRecord] = []
        for value in candidates:
            item = self._mapping(value)
            remote_id = AttachmentId(self.provider_name, self._required_text(item, "id"))
            filename = self._first_text(item, "fileName", "filename")
            if filename is None:
                raise SourceProtocolError()
            display_title = self._first_text(item, "displayName", "title") or filename
            records.append(
                ResourceMetadataRecord(
                    remote_id,
                    content_id,
                    display_title,
                    filename,
                    self._first_text(item, "mimeType", "contentType"),
                    self._parse_optional_time(item.get("modifiedDate")),
                )
            )
        return tuple(records)

    def _page_parts(
        self, payload: Mapping[str, object], requested_offset: int, expected_path: str
    ) -> tuple[list[object], int | None, Coverage]:
        results = payload.get("results")
        paging = payload.get("paging")
        if not isinstance(results, list) or not isinstance(paging, Mapping):
            raise SourceProtocolError()
        offset = paging.get("offset")
        limit = paging.get("limit")
        count = paging.get("count")
        if (
            isinstance(offset, bool)
            or not isinstance(offset, int)
            or offset != requested_offset
            or isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1_000
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 0
            or count < offset + len(results)
            or len(results) > limit
        ):
            raise SourceProtocolError()
        next_page = paging.get("nextPage")
        if next_page is None or next_page == "":
            if requested_offset + len(results) < count:
                raise SourceProtocolError()
            return results, None, Coverage.COMPLETE
        if not isinstance(next_page, str):
            raise SourceProtocolError()
        if requested_offset + len(results) >= count:
            raise SourceProtocolError()
        try:
            parsed = urlsplit(next_page)
            port = parsed.port
        except ValueError:
            raise SourceProtocolError() from None
        if parsed.fragment or parsed.path != expected_path:
            raise SourceProtocolError()
        if parsed.scheme or parsed.netloc:
            if (
                parsed.scheme != "https"
                or (port is not None and port != 443)
                or f"https://{parsed.hostname}" != self._origin
            ):
                raise SourceProtocolError()
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            raise SourceProtocolError() from None
        if not set(query).issubset(
            {
                "organization",
                "includeCount",
                "limit",
                "offset",
                "expand",
                "sort",
                "@view",
                "includeInActivityTracking",
            }
        ):
            raise SourceProtocolError()
        offsets = query.get("offset")
        if offsets is None or len(offsets) != 1 or not offsets[0].isascii():
            raise SourceProtocolError()
        try:
            next_offset = int(offsets[0])
        except ValueError:
            raise SourceProtocolError() from None
        expected_offset = requested_offset + len(results)
        if not results:
            if next_offset <= requested_offset:
                raise SourceProtocolError()
            return results, next_offset, Coverage.PARTIAL
        if next_offset != expected_offset:
            raise SourceProtocolError()
        return results, next_offset, Coverage.COMPLETE

    @staticmethod
    def _json(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise SourceProtocolError()
        return value

    @staticmethod
    def _mapping(value: object) -> Mapping[str, object]:
        if not isinstance(value, Mapping):
            raise SourceProtocolError()
        return value

    @staticmethod
    def _first_text(source: Mapping[str, object], *names: str) -> str | None:
        for name in names:
            value = source.get(name)
            if isinstance(value, str) and value.strip():
                return value
        return None

    @classmethod
    def _required_text(cls, source: Mapping[str, object], name: str) -> str:
        value = cls._first_text(source, name)
        if value is None:
            raise SourceProtocolError()
        return value

    @staticmethod
    def _handler_detail(details: object, handler: str) -> Mapping[str, object]:
        if not isinstance(details, Mapping):
            return {}
        selected = details.get(handler)
        return selected if isinstance(selected, Mapping) else details

    @staticmethod
    def _content_availability(source: Mapping[str, object]) -> Availability:
        visibility = source.get("visibility")
        if isinstance(visibility, str):
            normalized = visibility.upper()
            if normalized in {"VISIBLE", "AVAILABLE"}:
                return Availability.ACTIVE
            if normalized in {"HIDDEN", "UNAVAILABLE"}:
                return Availability.UNAVAILABLE
        return Availability.UNKNOWN

    @staticmethod
    def _parse_optional_time(value: object) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, int) and not isinstance(value, bool):
            try:
                return datetime.fromtimestamp(value / 1_000, tz=UTC)
            except (OSError, OverflowError, ValueError):
                raise SourceProtocolError() from None
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise SourceProtocolError() from None
            if parsed.tzinfo is None:
                raise SourceProtocolError()
            return parsed.astimezone(UTC)
        raise SourceProtocolError()

    def _require_session(self, session: AuthorizedReadSession, purpose: ReadPurpose) -> None:
        if (
            not isinstance(session, AuthorizedReadSession)
            or session.provider != self.provider_name
            or not session.permits(purpose)
        ):
            raise AuthenticationRequired()

    def _require_course(self, course: CourseId) -> None:
        self._require_id(course, CourseId)
        if course not in self._authorized_courses:
            raise SourceAccessDenied()

    def _activate_session(self, session: AuthorizedReadSession) -> None:
        if self._authorization_marker is session.marker:
            return
        self._authorization_marker = session.marker
        self._authorized_courses.clear()
        self._containers.clear()
        self._resources.clear()

    def _require_active_session(self, session: AuthorizedReadSession) -> None:
        if self._authorization_marker is not session.marker:
            raise SourceAccessDenied()

    def _require_id(self, value: object, expected: type[object]) -> None:
        if type(value) is not expected or getattr(value, "provider", None) != self.provider_name:
            raise TypeError(f"expected {expected.__name__} for provider")

    @staticmethod
    def _segment(value: str) -> str:
        if (
            value in {".", ".."}
            or not value.isascii()
            or any(not (character.isalnum() or character in "-._~") for character in value)
        ):
            raise SourceProtocolError()
        return value

    @staticmethod
    def _make_cursor(scope: str, offset: int) -> str:
        return f"{scope}:{offset}"

    @staticmethod
    def _read_cursor(scope: str, cursor: str | None) -> int:
        if cursor is None:
            return 0
        prefix = f"{scope}:"
        if not cursor.startswith(prefix):
            raise SourceProtocolError()
        value = cursor[len(prefix) :]
        if not value.isdigit():
            raise SourceProtocolError()
        return int(value)

    def _content_scope(self, course: CourseId, parent: ContentId | None) -> str:
        raw = f"{course.provider}\0{course.value}\0{'' if parent is None else parent.value}"
        digest = hashlib.sha256(raw.encode()).hexdigest()[:20]
        return f"content-{digest}"
