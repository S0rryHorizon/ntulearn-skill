"""Thin command dispatch over :mod:`ntulearn_skill.core.api`."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import NoReturn, TextIO

from ntulearn_skill.cli._parser import UsageError, parse_args
from ntulearn_skill.cli._presentation import (
    json_envelope,
    render_human,
    render_json,
    usage_error_envelope,
)
from ntulearn_skill.client import (
    BROWSER_CAPTURE_ERROR_MESSAGE,
    BrowserCaptureManifestError,
    SourceError,
    TimeWindow,
    safe_source_error_category,
)
from ntulearn_skill.core import Coverage, TemporalPrecision
from ntulearn_skill.core.api import CoreService, CourseRef, ManualResolution, ResourceRef
from ntulearn_skill.core.results import ErrorCategory, ResultEnvelope, SafeError
from ntulearn_skill.search import SearchQuery
from ntulearn_skill.sync.freshness import FreshnessRequirement

EXIT_COMPLETE = 0
EXIT_FAILURE = 1
EXIT_INCOMPLETE = 2
EXIT_EMPTY = 3
EXIT_USAGE = 64

_KNOWN_COMMANDS = frozenset(
    {
        "courses",
        "materials",
        "library-status",
        "recent-materials",
        "search",
        "announcements",
        "assessments",
        "events",
        "upcoming",
        "source",
        "resource",
        "manual-resolution",
        "manualresolution",
        "sync",
        "fetch",
        "visual",
    }
)


def _freshness(args: argparse.Namespace) -> FreshnessRequirement:
    maximum = args.max_age_seconds
    if maximum is not None:
        if args.freshness is not None:
            raise UsageError("invalid command arguments")
        return FreshnessRequirement.with_max_age(timedelta(seconds=maximum))
    factories = {
        "cache-only": FreshnessRequirement.cache_only,
        "allow-stale": FreshnessRequirement.allow_stale,
        "refresh-if-stale": FreshnessRequirement.refresh_if_stale,
        "require-current": FreshnessRequirement.require_current,
    }
    return factories[args.freshness or "cache-only"]()


def _course(key: int) -> CourseRef:
    return CourseRef(local_key=key)


def _resource(key: int) -> ResourceRef:
    return ResourceRef(local_key=key)


def _duration(text: str) -> timedelta:
    if len(text) < 2 or not text[:-1].isdigit():
        raise UsageError("invalid command arguments")
    amount = int(text[:-1])
    if amount <= 0:
        raise UsageError("invalid command arguments")
    units = {"h": 3_600, "d": 86_400, "w": 604_800}
    try:
        duration = timedelta(seconds=amount * units[text[-1]])
    except KeyError:
        raise UsageError("invalid command arguments") from None
    except OverflowError:
        raise UsageError("invalid command arguments") from None
    if duration > timedelta(days=3650):
        raise UsageError("invalid command arguments")
    return duration


def _absolute_window(args: argparse.Namespace) -> TimeWindow | None:
    since_text = getattr(args, "window_since", None)
    until_text = getattr(args, "window_until", None)
    if (since_text is None) != (until_text is None):
        raise UsageError("invalid command arguments")
    if since_text is None:
        return None
    if not isinstance(since_text, str) or not isinstance(until_text, str):
        raise UsageError("invalid command arguments")
    try:
        since = datetime.fromisoformat(since_text)
        until = datetime.fromisoformat(until_text)
        return TimeWindow(since, until)
    except (TypeError, ValueError):
        raise UsageError("invalid command arguments") from None


def _search_query(args: argparse.Namespace) -> SearchQuery:
    from ntulearn_skill.search import SearchFilters

    return SearchQuery(
        args.query,
        SearchFilters(include_historical_versions=not args.current_only),
        args.limit,
        args.neighbors,
        args.cursor,
    )


def _invalid_json_constant(_value: str) -> NoReturn:
    raise ValueError


def _manual_decision(args: argparse.Namespace) -> ManualResolution:
    from ntulearn_skill.events import (
        CandidateFieldName,
        ManualFieldResolution,
        ManualIdentityResolution,
    )

    if args.resolution_kind == "identity":
        return ManualIdentityResolution(args.event_source_key, args.event_key, args.reason)
    value = None
    if args.value_json is not None:
        try:
            value = json.loads(
                args.value_json,
                parse_constant=_invalid_json_constant,
            )
        except (json.JSONDecodeError, ValueError):
            raise UsageError("invalid command arguments") from None
    precision = None if args.precision is None else TemporalPrecision(args.precision)
    return ManualFieldResolution(
        args.event_key,
        CandidateFieldName(args.field_name),
        args.reason,
        selected_claim_key=args.claim_key,
        value=value,
        original_text=args.original_text or "local user decision",
        precision=precision,
        source_timezone=args.source_timezone,
        confidence=args.confidence,
    )


def _validate_args(args: argparse.Namespace) -> None:
    """Reject semantic CLI errors before runtime setup or a core call."""

    if hasattr(args, "max_age_seconds"):
        _freshness(args)
    if args.command == "search":
        _search_query(args)
    elif args.command in {"events", "upcoming", "recent-materials"}:
        fixed = _absolute_window(args)
        if fixed is None and getattr(args, "cursor", None) is not None:
            raise UsageError("invalid command arguments")
        if args.command == "events" and args.next_window is not None:
            _duration(args.next_window)
    elif args.command in {"manual-resolution", "manualresolution"}:
        _manual_decision(args)


def _dispatch(
    args: argparse.Namespace,
    service: CoreService,
    *,
    now: Callable[[], datetime],
) -> object:
    from ntulearn_skill.core.api import (
        AnnouncementFilter,
        AssessmentFilter,
        CourseFilter,
        EventFilter,
        MaterialFilter,
        SourceLocatorRef,
        SyncPolicy,
    )
    from ntulearn_skill.search import (
        SourceReference,
        SourceReferenceKind,
    )

    command = args.command
    if command == "courses":
        return service.list_courses(
            CourseFilter(limit=args.limit, cursor=args.cursor), _freshness(args)
        )
    if command == "materials":
        return service.list_materials(
            _course(args.course_key),
            MaterialFilter(limit=args.limit, cursor=args.cursor),
            _freshness(args),
        )
    if command == "library-status":
        course = None if args.course_key is None else _course(args.course_key)
        return service.get_library_status(course, _freshness(args))
    if command == "recent-materials":
        window = _absolute_window(args)
        if window is None:
            until = now().astimezone(UTC)
            window = TimeWindow(until - timedelta(days=args.days), until)
        return service.get_recent_material_changes(
            _course(args.course_key),
            window,
            _freshness(args),
            limit=args.limit,
            cursor=args.cursor,
        )
    if command == "search":
        query = _search_query(args)
        if args.course_key is None:
            return service.search(query, _freshness(args))
        return service.search_course(_course(args.course_key), query, _freshness(args))
    if command == "announcements":
        return service.get_announcements(
            _course(args.course_key),
            AnnouncementFilter(limit=args.limit, cursor=args.cursor),
            _freshness(args),
        )
    if command == "assessments":
        return service.get_assessments(
            _course(args.course_key),
            AssessmentFilter(limit=args.limit, cursor=args.cursor),
            _freshness(args),
        )
    if command == "events" and args.next_window is None and _absolute_window(args) is None:
        return service.get_events(
            EventFilter(
                course=None if args.course_key is None else _course(args.course_key),
                limit=args.limit,
                cursor=args.cursor,
            ),
            _freshness(args),
        )
    if command in {"events", "upcoming"}:
        window = _absolute_window(args)
        if window is None:
            duration = (
                _duration(args.next_window) if command == "events" else timedelta(days=args.days)
            )
            if duration <= timedelta():
                raise UsageError("invalid command arguments")
            since = now().astimezone(UTC)
            window = TimeWindow(since, since + duration)
        return service.get_upcoming_events(
            window,
            None if args.course_key is None else _course(args.course_key),
            _freshness(args),
            limit=args.limit,
            cursor=args.cursor,
        )
    if command == "source":
        reference = SourceReference(SourceReferenceKind(args.kind), args.key)
        return service.resolve_source(
            SourceLocatorRef(reference),
            args.context_window,
            include_visual_history=args.include_visual_history,
        )
    if command == "resource":
        return service.get_resource(
            _resource(args.resource_key), args.version, args.include_local_path
        )
    if command in {"manual-resolution", "manualresolution"}:
        return service.resolve_event_candidate(_manual_decision(args))
    if command == "sync":
        current_time = now().astimezone(UTC)
        policy = SyncPolicy(
            window=TimeWindow(
                current_time - timedelta(days=30),
                current_time + timedelta(days=365),
            ),
            fetch_resources=not args.no_fetch,
            verify_resources=args.verify,
            max_jobs=args.max_jobs,
        )
        course = None if args.course_key is None else _course(args.course_key)
        if args.quick:
            return service.quick_sync(course, policy)
        if course is not None:
            return service.sync_course(course, policy)
        return service.sync_all(policy)
    if command == "fetch":
        return service.fetch_resource(_resource(args.resource_key), args.verify)
    if command == "visual":
        if args.visual_action == "prepare":
            return service.prepare_visual_evidence(args.parse_key, dpi=args.dpi)
        return service.import_visual_evidence(args.bundle_path)
    raise UsageError("invalid command arguments")


def _failure_result(
    operation: str,
    category: ErrorCategory,
    code: str,
    message: str = "the command could not be completed safely",
) -> ResultEnvelope[object]:
    return ResultEnvelope(
        operation,
        errors=(
            SafeError(
                category,
                code,
                message,
                operation,
                "local_runtime",
                False,
                Coverage.FAILED,
            ),
        ),
        completeness=Coverage.FAILED,
        local_reads=0,
    )


def _exit_code(result: object) -> int:
    if not isinstance(result, ResultEnvelope):
        return EXIT_FAILURE
    if not result.ok:
        return EXIT_FAILURE
    if result.conclusive_empty:
        return EXIT_EMPTY
    if result.completeness is Coverage.COMPLETE:
        return EXIT_COMPLETE
    return EXIT_INCOMPLETE


def _requested_json(argv: Sequence[str] | None) -> bool:
    return bool(argv is not None and "--json" in argv)


def _needs_source(args: argparse.Namespace) -> bool:
    if args.command in {"sync", "fetch"}:
        return True
    if not hasattr(args, "freshness"):
        return False
    return bool(
        args.max_age_seconds is not None
        or args.freshness not in {None, "cache-only", "allow-stale"}
    )


def _safe_command(argv: Sequence[str] | None) -> str | None:
    if argv is None:
        return None
    return next((item for item in argv if item in _KNOWN_COMMANDS), None)


def run(
    argv: Sequence[str] | None = None,
    *,
    service: CoreService | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> int:
    """Parse, call exactly one core operation, render, and return a documented exit code."""

    output = sys.stdout if stdout is None else stdout
    errors = sys.stderr if stderr is None else stderr
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    json_requested = _requested_json(arguments)
    command = _safe_command(arguments)
    try:
        args = parse_args(arguments)
    except UsageError:
        payload = usage_error_envelope(command)
        destination = output if json_requested else errors
        print(render_json(payload) if json_requested else render_human(payload), file=destination)
        return EXIT_USAGE
    except SystemExit as error:
        return int(error.code or 0)

    json_requested = bool(getattr(args, "json", False))
    command = str(args.command)
    try:
        _validate_args(args)
    except (UsageError, TypeError, ValueError, OverflowError):
        payload = usage_error_envelope(command)
        destination = output if json_requested else errors
        print(render_json(payload) if json_requested else render_human(payload), file=destination)
        return EXIT_USAGE
    try:
        if service is None:
            from ntulearn_skill.core.api import CoreService

            capture = getattr(args, "browser_capture", None)
            configure_capture = capture if _needs_source(args) else None
            root = getattr(args, "root", None)
            service = (
                CoreService.from_runtime(root)
                if configure_capture is None
                else CoreService.from_runtime(root, browser_capture=configure_capture)
            )
        result = _dispatch(args, service, now=now)
        payload = json_envelope(
            result,
            command=command,
            include_local_paths=bool(getattr(args, "include_local_path", False)),
        )
    except UsageError:
        payload = usage_error_envelope(command)
        destination = output if json_requested else errors
        print(render_json(payload) if json_requested else render_human(payload), file=destination)
        return EXIT_USAGE
    except (TypeError, ValueError):
        result = _failure_result(
            "cli.dispatch", ErrorCategory.INVALID_REQUEST, "CLI_INVALID_REQUEST"
        )
        payload = json_envelope(result, command=command)
    except SourceError as error:
        category_name = safe_source_error_category(error.category)
        category = {
            "authentication_required": ErrorCategory.AUTHENTICATION_REQUIRED,
            "session_expired": ErrorCategory.SESSION_EXPIRED,
            "unsupported_capability": ErrorCategory.CAPABILITY_UNSUPPORTED,
        }.get(category_name, ErrorCategory.SOURCE_UNAVAILABLE)
        message = (
            BROWSER_CAPTURE_ERROR_MESSAGE
            if isinstance(error, BrowserCaptureManifestError)
            else "the command could not be completed safely"
        )
        result = _failure_result("cli.dispatch", category, category_name, message)
        payload = json_envelope(result, command=command)
    except Exception:
        result = _failure_result(
            "cli.dispatch", ErrorCategory.STORAGE_FAILURE, "CLI_OPERATION_FAILED"
        )
        payload = json_envelope(result, command=command)

    destination = output if json_requested or _exit_code(result) != EXIT_FAILURE else errors
    print(render_json(payload) if json_requested else render_human(payload), file=destination)
    return _exit_code(result)


def main() -> int:
    return run()


__all__ = [
    "EXIT_COMPLETE",
    "EXIT_EMPTY",
    "EXIT_FAILURE",
    "EXIT_INCOMPLETE",
    "EXIT_USAGE",
    "main",
    "run",
]
