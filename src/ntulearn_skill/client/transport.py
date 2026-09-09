"""Fixed, operation-based transport guard for authenticated source reads."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from ntulearn_skill.client.contracts import (
    AuthenticationRequired,
    AuthorizedReadSession,
    ReadPolicyViolation,
    ReadPurpose,
    SessionExpired,
    SourceAccessDenied,
    SourceProtocolError,
    SourceUnavailable,
)


class ReadOperation(StrEnum):
    DISCOVER_COURSES = "discover_courses"
    LIST_CONTENT_CHILDREN = "list_content_children"
    GET_CONTENT_DETAIL = "get_content_detail"
    LIST_ANNOUNCEMENTS = "list_announcements"
    LIST_DUE_ITEMS = "list_due_items"
    OPEN_RESOURCE_STREAM = "open_resource_stream"


@dataclass(frozen=True, slots=True, repr=False)
class SafeReadRequest:
    operation: ReadOperation
    path: str
    query: tuple[tuple[str, str], ...] = ()

    def __repr__(self) -> str:
        return f"SafeReadRequest(operation={self.operation.value!r}, target=<redacted>)"


@dataclass(frozen=True, slots=True, repr=False)
class ResolvedReadRequest:
    operation: ReadOperation
    target: str
    query: tuple[tuple[str, str], ...] = ()
    is_ephemeral_redirect: bool = False

    def __repr__(self) -> str:
        return f"ResolvedReadRequest(operation={self.operation.value!r}, target=<redacted>)"


@dataclass(slots=True, repr=False)
class WireResponse:
    status_code: int
    headers: Mapping[str, str] = field(default_factory=dict)
    json_body: object | None = None
    body_chunks: Iterable[bytes] | None = None
    close: Callable[[], None] | None = None

    def __repr__(self) -> str:
        return f"WireResponse(status_code={self.status_code}, body=<redacted>)"


@dataclass(frozen=True, slots=True)
class _OperationPolicy:
    path_pattern: re.Pattern[str]
    purpose: ReadPurpose
    required_query: Mapping[str, str]
    numeric_query: frozenset[str] = frozenset()
    required_text_query: frozenset[str] = frozenset()
    optional_query: frozenset[str] = frozenset()
    permits_redirect: bool = False


_SEGMENT = r"[A-Za-z0-9._~-]+"
_POLICIES = {
    ReadOperation.DISCOVER_COURSES: _OperationPolicy(
        re.compile(r"/learn/api/v1/users/me/memberships"),
        ReadPurpose.DISCOVERY,
        {
            "organization": "false",
            "includeCount": "true",
            "expand": "course.effectiveAvailability",
            "sort": "lastAccessDate(desc:nullslast)",
        },
        frozenset({"limit", "offset"}),
    ),
    ReadOperation.LIST_CONTENT_CHILDREN: _OperationPolicy(
        re.compile(rf"/learn/api/v1/courses/{_SEGMENT}/contents/{_SEGMENT}/children"),
        ReadPurpose.CONTENT,
        {
            "@view": "Summary",
            "expand": "assignedGroups,selfEnrollmentGroups.group,gradebookCategory",
            "includeInActivityTracking": "true",
        },
        frozenset({"limit", "offset"}),
    ),
    ReadOperation.GET_CONTENT_DETAIL: _OperationPolicy(
        re.compile(rf"/learn/api/v1/courses/{_SEGMENT}/contents/{_SEGMENT}"),
        ReadPurpose.ASSESSMENTS,
        {
            "expand": "assignedGroups,selfEnrollmentGroups.group,alignedGoals,gradebookCategory",
            "includeInActivityTracking": "false",
        },
    ),
    ReadOperation.LIST_ANNOUNCEMENTS: _OperationPolicy(
        re.compile(rf"/learn/api/v1/courses/{_SEGMENT}/announcements"),
        ReadPurpose.ANNOUNCEMENTS,
        {"sort": "startDateRestriction(desc)"},
        frozenset({"limit", "offset"}),
    ),
    ReadOperation.LIST_DUE_ITEMS: _OperationPolicy(
        re.compile(rf"/learn/api/v1/courses/{_SEGMENT}/calendars/dueDateCalendarItems"),
        ReadPurpose.DUE_ITEMS,
        {"date_compare": "greaterOrEqual", "includeCount": "true"},
        frozenset({"limit", "offset"}),
        frozenset({"date"}),
    ),
    ReadOperation.OPEN_RESOURCE_STREAM: _OperationPolicy(
        re.compile(rf"/bbcswebdav/pid-{_SEGMENT}-dt-content-rid-{_SEGMENT}/xid-{_SEGMENT}"),
        ReadPurpose.RESOURCE_STREAM,
        {
            "isInlineRender": "true",
            "xythos-download": "true",
            "render": "inline",
        },
        optional_query=frozenset({"locale"}),
        permits_redirect=True,
    ),
}

SafeLog = Callable[[str, Mapping[str, str | int | bool]], None]


class ReadOnlyTransport:
    """Allow only fixed retrieval operations whose complete request shape is validated.

    A GET method alone is not a safety property. There is deliberately no public mechanism for
    replacing these policies with arbitrary route regular expressions.
    """

    def __init__(
        self,
        executor: Callable[[ResolvedReadRequest, AuthorizedReadSession, bool], WireResponse],
        *,
        redirect_hosts: frozenset[str] = frozenset(),
        logger: SafeLog | None = None,
    ) -> None:
        self._executor = executor
        self._redirect_hosts = self._validate_hosts(redirect_hosts)
        self._logger = logger

    def supports(self, operation: ReadOperation) -> bool:
        return operation in _POLICIES

    def send(self, request: SafeReadRequest, session: AuthorizedReadSession) -> WireResponse:
        policy = self._validate(request, session)
        response = self._execute(
            ResolvedReadRequest(request.operation, request.path, request.query), session, True
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            if not policy.permits_redirect:
                self._safe_close(response)
                raise SessionExpired()
            location = self._header(response.headers, "location")
            self._safe_close(response)
            target = self._validate_redirect(location)
            response = self._execute(
                ResolvedReadRequest(request.operation, target, (), True), session, False
            )
            self._log("source_read_redirect", request.operation, response.status_code, True)
        else:
            self._log("source_read", request.operation, response.status_code, False)
        self._raise_for_status(response)
        return response

    def _validate(
        self, request: SafeReadRequest, session: AuthorizedReadSession
    ) -> _OperationPolicy:
        if not isinstance(request, SafeReadRequest):
            raise ReadPolicyViolation()
        policy = _POLICIES.get(request.operation)
        if policy is None:
            raise ReadPolicyViolation()
        if not isinstance(session, AuthorizedReadSession) or not session.permits(policy.purpose):
            raise AuthenticationRequired()
        parsed = urlsplit(request.path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or not request.path.startswith("/")
            or "%" in request.path
            or "\\" in request.path
            or any(ord(character) < 33 or ord(character) == 127 for character in request.path)
            or "//" in request.path
            or any(part in {".", ".."} for part in request.path.split("/"))
            or policy.path_pattern.fullmatch(request.path) is None
        ):
            raise ReadPolicyViolation()
        try:
            query = dict(request.query)
        except (TypeError, ValueError):
            raise ReadPolicyViolation() from None
        keys = [key for key, _ in request.query]
        required = (
            set(policy.required_query) | set(policy.numeric_query) | set(policy.required_text_query)
        )
        allowed = required | set(policy.optional_query)
        if (
            len(keys) != len(set(keys))
            or not required.issubset(query)
            or not set(query).issubset(allowed)
            or any(
                not isinstance(key, str)
                or not isinstance(value, str)
                or not key
                or any(ord(character) < 32 or ord(character) == 127 for character in key + value)
                for key, value in request.query
            )
            or any(query.get(key) != value for key, value in policy.required_query.items())
            or any(not self._valid_number(query.get(key), key) for key in policy.numeric_query)
            or any(
                not query[key] or len(query[key]) > 64 or not query[key].isascii()
                for key in policy.required_text_query
            )
            or any(
                not query[key] or len(query[key]) > 32
                for key in policy.optional_query
                if key in query
            )
        ):
            raise ReadPolicyViolation()
        return policy

    @staticmethod
    def _valid_number(value: str | None, key: str) -> bool:
        if value is None or not value.isascii() or not value.isdigit():
            return False
        number = int(value)
        return 0 <= number <= 1_000_000_000 and (key != "limit" or 1 <= number <= 1_000)

    @staticmethod
    def _validate_hosts(hosts: frozenset[str]) -> frozenset[str]:
        validated: set[str] = set()
        for host in hosts:
            try:
                parsed = urlsplit(f"//{host}")
                port = parsed.port
            except ValueError:
                raise ValueError("redirect host allowlist is invalid") from None
            if (
                not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or (port is not None and port != 443)
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("redirect host allowlist is invalid")
            validated.add(parsed.hostname.lower())
        return frozenset(validated)

    def _validate_redirect(self, location: str | None) -> str:
        if not location:
            raise SourceProtocolError()
        try:
            parsed = urlsplit(location)
            port = parsed.port
        except (TypeError, ValueError):
            raise ReadPolicyViolation() from None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() not in self._redirect_hosts
            or parsed.username is not None
            or parsed.password is not None
            or (port is not None and port != 443)
            or parsed.fragment
        ):
            raise ReadPolicyViolation()
        return location

    def _execute(
        self,
        request: ResolvedReadRequest,
        session: AuthorizedReadSession,
        forward_credentials: bool,
    ) -> WireResponse:
        try:
            response = self._executor(request, session, forward_credentials)
        except AuthenticationRequired:
            raise AuthenticationRequired() from None
        except SessionExpired:
            raise SessionExpired() from None
        except SourceAccessDenied:
            raise SourceAccessDenied() from None
        except ReadPolicyViolation:
            raise ReadPolicyViolation() from None
        except Exception:
            raise SourceUnavailable() from None
        if not isinstance(response, WireResponse):
            raise SourceProtocolError()
        if (
            isinstance(response.status_code, bool)
            or not isinstance(response.status_code, int)
            or not 100 <= response.status_code <= 599
        ):
            self._safe_close(response)
            raise SourceProtocolError()
        return response

    @staticmethod
    def _raise_for_status(response: WireResponse) -> None:
        if response.status_code in {401, 419, 440}:
            ReadOnlyTransport._safe_close(response)
            raise SessionExpired()
        if response.status_code == 403:
            ReadOnlyTransport._safe_close(response)
            raise SourceAccessDenied()
        if not 200 <= response.status_code < 300:
            ReadOnlyTransport._safe_close(response)
            raise SourceUnavailable()

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        try:
            for key, value in headers.items():
                if isinstance(key, str) and key.lower() == name.lower():
                    return value if isinstance(value, str) else None
        except Exception:
            return None
        return None

    @staticmethod
    def _safe_close(response: WireResponse) -> None:
        if response.close is not None:
            try:
                response.close()
            except Exception:
                pass

    def _log(
        self, event: str, operation: ReadOperation, status_code: int, redirected: bool
    ) -> None:
        if self._logger is not None:
            try:
                self._logger(
                    event,
                    {
                        "operation": operation.value,
                        "status_code": status_code,
                        "redirected": redirected,
                    },
                )
            except Exception:
                pass
