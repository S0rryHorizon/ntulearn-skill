"""Argument parsing with bounded, privacy-safe usage failures."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import NoReturn


class UsageError(ValueError):
    """A command-line shape error whose message contains no supplied value."""


class SafeArgumentParser(argparse.ArgumentParser):
    """Do not reflect private command arguments in parser error output."""

    def error(self, message: str) -> NoReturn:
        del message
        raise UsageError("invalid command arguments")


def _bounded_integer(*, minimum: int, maximum: int | None = None) -> Callable[[str], int]:
    def convert(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError("invalid integer") from None
        if parsed < minimum or (maximum is not None and parsed > maximum):
            raise argparse.ArgumentTypeError("integer outside allowed range")
        return parsed

    return convert


_positive_key = _bounded_integer(minimum=1)
_result_limit = _bounded_integer(minimum=1, maximum=100)
_neighbor_count = _bounded_integer(minimum=0, maximum=5)
_context_window = _bounded_integer(minimum=0, maximum=5)
_upcoming_days = _bounded_integer(minimum=1, maximum=3650)
_max_age_seconds = _bounded_integer(minimum=1, maximum=315_360_000)


def _confidence(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid confidence") from None
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("confidence outside allowed range")
    return parsed


def _common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    parser.add_argument("--root", default=argparse.SUPPRESS, metavar="PRIVATE_ROOT")
    parser.add_argument(
        "--browser-capture",
        default=argparse.SUPPRESS,
        metavar="PRIVATE_MANIFEST",
        help="Use a private host browser observation bundle for source reads",
    )


def _freshness_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--freshness",
        choices=("cache-only", "allow-stale", "refresh-if-stale", "require-current"),
        default=None,
    )
    parser.add_argument("--max-age-seconds", type=_max_age_seconds)


def build_parser() -> SafeArgumentParser:
    parser = SafeArgumentParser(
        prog="ntulearn",
        description="Query a private local NTULearn index",
        epilog=(
            "Exit codes: 0 complete, 1 operational failure, "
            "2 partial/stale/unknown, 3 complete empty, 64 invalid usage."
        ),
    )
    _common_options(parser)
    commands = parser.add_subparsers(dest="command", required=True)

    courses = commands.add_parser("courses", help="List locally known courses")
    _common_options(courses)
    _freshness_options(courses)

    materials = commands.add_parser("materials", help="List course materials")
    _common_options(materials)
    materials.add_argument("course_key", type=_positive_key)
    _freshness_options(materials)

    search = commands.add_parser("search", help="Search the deterministic local index")
    _common_options(search)
    search.add_argument("query")
    search.add_argument("--course", dest="course_key", type=_positive_key)
    search.add_argument("--limit", type=_result_limit, default=20)
    search.add_argument("--neighbors", type=_neighbor_count, default=1)
    _freshness_options(search)

    announcements = commands.add_parser("announcements", help="List course announcements")
    _common_options(announcements)
    announcements.add_argument("course_key", type=_positive_key)
    _freshness_options(announcements)

    assessments = commands.add_parser("assessments", help="List course assessments")
    _common_options(assessments)
    assessments.add_argument("course_key", type=_positive_key)
    _freshness_options(assessments)

    events = commands.add_parser("events", help="List canonical events")
    _common_options(events)
    events.add_argument("--course", dest="course_key", type=_positive_key)
    events.add_argument("--show-conflicts", action="store_true")
    events.add_argument("--next", dest="next_window")
    _freshness_options(events)

    upcoming = commands.add_parser("upcoming", help="List events in an upcoming window")
    _common_options(upcoming)
    upcoming.add_argument("--course", dest="course_key", type=_positive_key)
    upcoming.add_argument("--days", type=_upcoming_days, default=7)
    _freshness_options(upcoming)

    source = commands.add_parser("source", help="Resolve a local provenance locator")
    _common_options(source)
    source.add_argument("key", type=_positive_key)
    source.add_argument(
        "--kind",
        choices=("source_object", "source_observation", "source_locator"),
        default="source_locator",
    )
    source.add_argument("--context-window", type=_context_window, default=1)

    resource = commands.add_parser("resource", help="Read local resource metadata")
    _common_options(resource)
    resource.add_argument("resource_key", type=_positive_key)
    resource.add_argument("--version", type=_positive_key)
    resource.add_argument("--include-local-path", action="store_true")

    resolution = commands.add_parser(
        "manual-resolution", aliases=["manualresolution"], help="Record a local event decision"
    )
    _common_options(resolution)
    resolution_commands = resolution.add_subparsers(dest="resolution_kind", required=True)
    field = resolution_commands.add_parser("field", help="Resolve one projected event field")
    field.add_argument("event_key", type=_positive_key)
    field.add_argument(
        "field_name",
        choices=(
            "title",
            "event_type",
            "start_time",
            "end_time",
            "due_time",
            "location",
            "status",
        ),
    )
    selection = field.add_mutually_exclusive_group(required=True)
    selection.add_argument("--claim-key", type=_positive_key)
    selection.add_argument("--value-json")
    field.add_argument("--reason", required=True)
    field.add_argument("--original-text")
    field.add_argument("--precision", choices=("EXACT_TIME", "DATE_ONLY", "WEEK_ONLY"))
    field.add_argument("--source-timezone")
    field.add_argument("--confidence", type=_confidence, default=1.0)
    identity = resolution_commands.add_parser("identity", help="Resolve event-source identity")
    identity.add_argument("event_source_key", type=_positive_key)
    identity.add_argument("event_key", type=_positive_key)
    identity.add_argument("--reason", required=True)

    sync = commands.add_parser("sync", help="Run an explicitly requested read-only sync")
    _common_options(sync)
    sync.add_argument("course_key", type=_positive_key, nargs="?")
    sync.add_argument("--quick", action="store_true")
    sync.add_argument("--verify", action="store_true")
    sync.add_argument("--no-fetch", action="store_true")

    fetch = commands.add_parser("fetch", help="Fetch one explicitly selected resource")
    _common_options(fetch)
    fetch.add_argument("resource_key", type=_positive_key)
    fetch.add_argument("--verify", action="store_true")
    return parser


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


__all__ = ["UsageError", "build_parser", "parse_args"]
