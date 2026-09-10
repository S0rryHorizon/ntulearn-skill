"""Thin, privacy-safe Codex tool dispatch over the stable core API."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta
from math import isfinite
from typing import TypeAlias, cast

from ntulearn_skill.core.api import (
    AnnouncementFilter,
    AssessmentFilter,
    CandidateFieldName,
    CoreService,
    CourseFilter,
    CourseRef,
    EventFilter,
    FreshnessRequirement,
    ManualFieldResolution,
    ManualIdentityResolution,
    MaterialFilter,
    ResourceRef,
    SearchQuery,
    SourceLocatorRef,
    SourceReference,
    SourceReferenceKind,
    SyncPolicy,
    TimeWindow,
)
from ntulearn_skill.core.models import Coverage, TemporalPrecision
from ntulearn_skill.core.results import ErrorCategory, ResultEnvelope, SafeError

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]

TOOL_NAMES = (
    "list_courses",
    "list_materials",
    "get_library_status",
    "get_recent_material_changes",
    "search",
    "get_announcements",
    "get_assessments",
    "get_events",
    "get_upcoming_events",
    "resolve_source",
    "get_resource",
    "quick_sync",
    "sync_course",
    "sync_all",
    "fetch_resource",
    "resolve_event_candidate",
)


class _InvalidToolRequest(ValueError):
    """Internal marker for a bounded request-shape failure."""


def _arguments(
    value: Mapping[str, object], *, allowed: frozenset[str], required: frozenset[str] = frozenset()
) -> Mapping[str, object]:
    if (
        not isinstance(value, Mapping)
        or not required <= value.keys()
        or not value.keys() <= allowed
    ):
        raise _InvalidToolRequest("invalid tool arguments")
    if any(not isinstance(key, str) for key in value):
        raise _InvalidToolRequest("invalid tool arguments")
    return value


def _integer(value: object, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _InvalidToolRequest("invalid tool arguments")
    if maximum is not None and value > maximum:
        raise _InvalidToolRequest("invalid tool arguments")
    return value


def _boolean(value: object) -> bool:
    if not isinstance(value, bool):
        raise _InvalidToolRequest("invalid tool arguments")
    return value


def _text(value: object, *, maximum: int = 1_000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _InvalidToolRequest("invalid tool arguments")
    return value


def _number(value: object, *, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _InvalidToolRequest("invalid tool arguments")
    number = float(value)
    if not isfinite(number) or not minimum <= number <= maximum:
        raise _InvalidToolRequest("invalid tool arguments")
    return number


def _json(value: object, *, depth: int = 0) -> JsonValue:
    if depth > 10:
        raise _InvalidToolRequest("invalid tool arguments")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not isfinite(value):
            raise _InvalidToolRequest("invalid tool arguments")
        return value
    if isinstance(value, list):
        if len(value) > 1_000:
            raise _InvalidToolRequest("invalid tool arguments")
        return [_json(item, depth=depth + 1) for item in value]
    if isinstance(value, Mapping):
        if len(value) > 1_000 or any(not isinstance(key, str) for key in value):
            raise _InvalidToolRequest("invalid tool arguments")
        return {str(key): _json(item, depth=depth + 1) for key, item in value.items()}
    raise _InvalidToolRequest("invalid tool arguments")


def _course(value: object) -> CourseRef:
    return CourseRef(local_key=_integer(value))


def _resource(value: object) -> ResourceRef:
    return ResourceRef(local_key=_integer(value))


def _freshness(value: object) -> FreshnessRequirement:
    if value is None:
        return FreshnessRequirement.cache_only()
    if not isinstance(value, Mapping):
        raise _InvalidToolRequest("invalid tool arguments")
    request = _arguments(
        value,
        allowed=frozenset({"mode", "max_age_seconds"}),
        required=frozenset({"mode"}),
    )
    mode = request["mode"]
    if mode == "cache_only" and "max_age_seconds" not in request:
        return FreshnessRequirement.cache_only()
    if mode == "allow_stale" and "max_age_seconds" not in request:
        return FreshnessRequirement.allow_stale()
    if mode == "refresh_if_stale" and "max_age_seconds" not in request:
        return FreshnessRequirement.refresh_if_stale()
    if mode == "require_current" and "max_age_seconds" not in request:
        return FreshnessRequirement.require_current()
    if mode == "max_age" and "max_age_seconds" in request:
        seconds = _integer(request["max_age_seconds"], maximum=315_360_000)
        return FreshnessRequirement.with_max_age(timedelta(seconds=seconds))
    raise _InvalidToolRequest("invalid tool arguments")


def _time(value: object) -> datetime:
    text = _text(value, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise _InvalidToolRequest("invalid tool arguments") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidToolRequest("invalid tool arguments")
    return parsed


def _window(arguments: Mapping[str, object]) -> TimeWindow:
    return TimeWindow(_time(arguments["window_since"]), _time(arguments["window_until"]))


def _failure(operation: str, code: str, category: ErrorCategory) -> ResultEnvelope[object]:
    return ResultEnvelope(
        operation,
        errors=(
            SafeError(
                category,
                code,
                "the Codex tool request could not be completed safely",
                operation,
                "local_runtime",
                False,
                Coverage.FAILED,
            ),
        ),
        completeness=Coverage.FAILED,
        local_reads=0,
    )


class CodexToolDispatcher:
    """Validate a bounded tool shape, call one core method, and return its envelope."""

    def __init__(self, service: CoreService) -> None:
        self._service = service

    @property
    def tool_names(self) -> tuple[str, ...]:
        return TOOL_NAMES

    def call(self, tool_name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Call one allowlisted core operation and emit its version/PATH-safe JSON form."""

        if not isinstance(tool_name, str) or tool_name not in TOOL_NAMES:
            return _failure(
                "codex.dispatch", "CODEX_UNKNOWN_TOOL", ErrorCategory.INVALID_REQUEST
            ).to_dict()
        try:
            result = self._dispatch(tool_name, arguments)
        except (KeyError, TypeError, ValueError):
            result = _failure(tool_name, "CODEX_INVALID_ARGUMENTS", ErrorCategory.INVALID_REQUEST)
        except Exception:
            result = _failure(tool_name, "CODEX_OPERATION_FAILED", ErrorCategory.STORAGE_FAILURE)
        try:
            return result.to_dict(include_local_paths=False)
        except Exception:
            return _failure(
                tool_name, "CODEX_SERIALIZATION_FAILED", ErrorCategory.INTEGRITY_FAILURE
            ).to_dict(include_local_paths=False)

    def _dispatch(
        self, tool_name: str, raw_arguments: Mapping[str, object]
    ) -> ResultEnvelope[object]:
        if tool_name == "list_courses":
            arguments = _arguments(raw_arguments, allowed=frozenset({"freshness"}))
            return cast(
                ResultEnvelope[object],
                self._service.list_courses(CourseFilter(), _freshness(arguments.get("freshness"))),
            )
        if tool_name == "list_materials":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"course_key", "freshness"}),
                required=frozenset({"course_key"}),
            )
            return cast(
                ResultEnvelope[object],
                self._service.list_materials(
                    _course(arguments["course_key"]),
                    MaterialFilter(),
                    _freshness(arguments.get("freshness")),
                ),
            )
        if tool_name == "get_library_status":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"course_key", "freshness"}),
            )
            course_value = arguments.get("course_key")
            return cast(
                ResultEnvelope[object],
                self._service.get_library_status(
                    None if course_value is None else _course(course_value),
                    _freshness(arguments.get("freshness")),
                ),
            )
        if tool_name == "get_recent_material_changes":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset(
                    {"course_key", "window_since", "window_until", "limit", "freshness"}
                ),
                required=frozenset({"course_key", "window_since", "window_until"}),
            )
            return cast(
                ResultEnvelope[object],
                self._service.get_recent_material_changes(
                    _course(arguments["course_key"]),
                    _window(arguments),
                    _freshness(arguments.get("freshness")),
                    limit=_integer(arguments.get("limit", 100), maximum=100),
                ),
            )
        if tool_name == "search":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"query", "course_key", "limit", "neighbor_count", "freshness"}),
                required=frozenset({"query"}),
            )
            query = SearchQuery(
                _text(arguments["query"]),
                limit=_integer(arguments.get("limit", 20), maximum=100),
                neighbor_count=_integer(arguments.get("neighbor_count", 1), minimum=0, maximum=5),
            )
            freshness = _freshness(arguments.get("freshness"))
            if arguments.get("course_key") is None:
                return self._service.search(query, freshness)
            return self._service.search_course(_course(arguments["course_key"]), query, freshness)
        if tool_name in {"get_announcements", "get_assessments"}:
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"course_key", "freshness"}),
                required=frozenset({"course_key"}),
            )
            course = _course(arguments["course_key"])
            freshness = _freshness(arguments.get("freshness"))
            if tool_name == "get_announcements":
                return cast(
                    ResultEnvelope[object],
                    self._service.get_announcements(course, AnnouncementFilter(), freshness),
                )
            return cast(
                ResultEnvelope[object],
                self._service.get_assessments(course, AssessmentFilter(), freshness),
            )
        if tool_name == "get_events":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"course_key", "freshness"}),
            )
            course_value = arguments.get("course_key")
            event_filter = EventFilter(
                course=None if course_value is None else _course(course_value),
            )
            return cast(
                ResultEnvelope[object],
                self._service.get_events(event_filter, _freshness(arguments.get("freshness"))),
            )
        if tool_name == "get_upcoming_events":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"window_since", "window_until", "course_key", "freshness"}),
                required=frozenset({"window_since", "window_until"}),
            )
            course_value = arguments.get("course_key")
            return cast(
                ResultEnvelope[object],
                self._service.get_upcoming_events(
                    _window(arguments),
                    None if course_value is None else _course(course_value),
                    _freshness(arguments.get("freshness")),
                ),
            )
        if tool_name == "resolve_source":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"kind", "key", "context_window"}),
                required=frozenset({"kind", "key"}),
            )
            reference = SourceReference(
                SourceReferenceKind(_text(arguments["kind"], maximum=32)),
                _integer(arguments["key"]),
            )
            return self._service.resolve_source(
                SourceLocatorRef(reference),
                _integer(arguments.get("context_window", 1), minimum=0, maximum=5),
            )
        if tool_name == "get_resource":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"resource_key", "version_key"}),
                required=frozenset({"resource_key"}),
            )
            version = arguments.get("version_key")
            return cast(
                ResultEnvelope[object],
                self._service.get_resource(
                    _resource(arguments["resource_key"]),
                    None if version is None else _integer(version),
                    False,
                ),
            )
        if tool_name in {"quick_sync", "sync_course", "sync_all"}:
            allowed = {
                "window_since",
                "window_until",
                "fetch_resources",
                "verify_resources",
                "max_jobs",
            }
            if tool_name != "sync_all":
                allowed.add("course_key")
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset(allowed),
                required=(
                    frozenset({"window_since", "window_until"})
                    if tool_name == "sync_all"
                    else frozenset({"course_key", "window_since", "window_until"})
                ),
            )
            policy = SyncPolicy(
                window=_window(arguments),
                fetch_resources=_boolean(arguments.get("fetch_resources", True)),
                verify_resources=_boolean(arguments.get("verify_resources", False)),
                max_jobs=_integer(arguments.get("max_jobs", 64), maximum=10_000),
            )
            if tool_name == "sync_all":
                return self._service.sync_all(policy)
            course = _course(arguments["course_key"])
            if tool_name == "quick_sync":
                return self._service.quick_sync(course, policy)
            return self._service.sync_course(course, policy)
        if tool_name == "fetch_resource":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"resource_key", "verify"}),
                required=frozenset({"resource_key"}),
            )
            return self._service.fetch_resource(
                _resource(arguments["resource_key"]),
                _boolean(arguments.get("verify", False)),
            )
        if tool_name == "resolve_event_candidate":
            return self._resolve_event_candidate(raw_arguments)
        raise _InvalidToolRequest("invalid tool arguments")

    def _resolve_event_candidate(
        self, raw_arguments: Mapping[str, object]
    ) -> ResultEnvelope[object]:
        decision_kind = raw_arguments.get("decision_kind")
        if decision_kind == "identity":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset({"decision_kind", "event_source_key", "event_key", "reason"}),
                required=frozenset({"decision_kind", "event_source_key", "event_key", "reason"}),
            )
            identity_decision = ManualIdentityResolution(
                _integer(arguments["event_source_key"]),
                _integer(arguments["event_key"]),
                _text(arguments["reason"], maximum=4_096),
            )
            return cast(
                ResultEnvelope[object],
                self._service.resolve_event_candidate(identity_decision),
            )
        if decision_kind == "field":
            arguments = _arguments(
                raw_arguments,
                allowed=frozenset(
                    {
                        "decision_kind",
                        "event_key",
                        "field_name",
                        "reason",
                        "selected_claim_key",
                        "value",
                        "original_text",
                        "precision",
                        "source_timezone",
                        "confidence",
                    }
                ),
                required=frozenset({"decision_kind", "event_key", "field_name", "reason"}),
            )
            has_claim = "selected_claim_key" in arguments
            has_value = "value" in arguments
            if has_claim == has_value:
                raise _InvalidToolRequest("invalid tool arguments")
            precision_value = arguments.get("precision")
            timezone_value = arguments.get("source_timezone")
            original_text = arguments.get("original_text")
            field_decision = ManualFieldResolution(
                _integer(arguments["event_key"]),
                CandidateFieldName(_text(arguments["field_name"], maximum=64)),
                _text(arguments["reason"], maximum=4_096),
                selected_claim_key=(
                    _integer(arguments["selected_claim_key"]) if has_claim else None
                ),
                value=_json(arguments["value"]) if has_value else None,
                original_text=(
                    "local user decision"
                    if original_text is None
                    else _text(original_text, maximum=4_096)
                ),
                precision=(
                    None
                    if precision_value is None
                    else TemporalPrecision(_text(precision_value, maximum=32))
                ),
                source_timezone=(
                    None if timezone_value is None else _text(timezone_value, maximum=128)
                ),
                confidence=_number(arguments.get("confidence", 1.0), minimum=0, maximum=1),
            )
            return cast(
                ResultEnvelope[object],
                self._service.resolve_event_candidate(field_decision),
            )
        raise _InvalidToolRequest("invalid tool arguments")


__all__ = ["TOOL_NAMES", "CodexToolDispatcher", "JsonValue"]
