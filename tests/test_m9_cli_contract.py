"""M9 CLI contracts over fully synthetic core results."""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from ntulearn_skill.cli import run
from ntulearn_skill.cli._main import (
    EXIT_COMPLETE,
    EXIT_EMPTY,
    EXIT_FAILURE,
    EXIT_INCOMPLETE,
    EXIT_USAGE,
)
from ntulearn_skill.cli._parser import build_parser
from ntulearn_skill.core import Coverage
from ntulearn_skill.core.api import CoreService, SyncPolicy
from ntulearn_skill.core.results import (
    CoverageView,
    ErrorCategory,
    FreshnessView,
    ProvenanceView,
    ResultEnvelope,
    SafeError,
)
from ntulearn_skill.sync.freshness import FreshnessMode, FreshnessRequirement

NOW = datetime(2035, 1, 2, 3, 4, tzinfo=UTC)


class _Service:
    def __init__(self, result: ResultEnvelope[object]) -> None:
        self.result = result
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def __getattr__(self, name: str) -> Any:
        def call(*args: object, **kwargs: object) -> ResultEnvelope[object]:
            self.calls.append((name, args, kwargs))
            return self.result

        return call


def _result(
    completeness: Coverage = Coverage.COMPLETE,
    *,
    items: tuple[object, ...] = ({"title": "Synthetic item"},),
    errors: tuple[SafeError, ...] = (),
) -> ResultEnvelope[object]:
    return ResultEnvelope(
        "synthetic.query",
        items=items,
        provenance=(ProvenanceView("source_locator", 9, "synthetic", locator={"page": 2}),),
        coverage=(CoverageView("synthetic", "materials", completeness, observed_at=NOW),),
        freshness=(FreshnessView("synthetic:materials", "CURRENT", NOW, 0, True),),
        errors=errors,
        completeness=completeness,
        as_of=NOW,
    )


@pytest.mark.parametrize(
    ("argv", "method"),
    [
        (["courses"], "list_courses"),
        (["materials", "1"], "list_materials"),
        (["search", "synthetic query"], "search"),
        (["search", "synthetic query", "--course", "1"], "search_course"),
        (["announcements", "1"], "get_announcements"),
        (["assessments", "1"], "get_assessments"),
        (["events"], "get_events"),
        (["upcoming", "--days", "3"], "get_upcoming_events"),
        (["events", "--next", "2w"], "get_upcoming_events"),
        (["source", "1"], "resolve_source"),
        (["resource", "1"], "get_resource"),
        (
            [
                "manual-resolution",
                "field",
                "1",
                "title",
                "--value-json",
                '"Synthetic title"',
                "--reason",
                "Synthetic decision",
            ],
            "resolve_event_candidate",
        ),
        (
            [
                "manualresolution",
                "identity",
                "1",
                "2",
                "--reason",
                "Synthetic identity decision",
            ],
            "resolve_event_candidate",
        ),
        (["sync", "1"], "sync_course"),
        (["sync"], "sync_all"),
        (["sync", "--quick"], "quick_sync"),
        (["sync", "1", "--quick"], "quick_sync"),
        (["fetch", "1"], "fetch_resource"),
    ],
)
def test_commands_delegate_once_to_the_stable_core_api(argv: list[str], method: str) -> None:
    service = _Service(_result())
    output = io.StringIO()

    code = run(argv, service=service, stdout=output, now=lambda: NOW)

    assert code == EXIT_COMPLETE
    assert [call[0] for call in service.calls] == [method]


def test_cli_forwards_bounded_list_and_search_continuations() -> None:
    service = _Service(_result())

    assert (
        run(
            ["courses", "--limit", "2", "--cursor", "synthetic-cursor"],
            service=service,
            stdout=io.StringIO(),
        )
        == EXIT_COMPLETE
    )
    course_filter = service.calls[0][1][0]
    assert course_filter.limit == 2
    assert course_filter.cursor == "synthetic-cursor"

    service.calls.clear()
    assert (
        run(
            [
                "search",
                "synthetic query",
                "--limit",
                "3",
                "--cursor",
                "synthetic-search-cursor",
            ],
            service=service,
            stdout=io.StringIO(),
        )
        == EXIT_COMPLETE
    )
    search_query = service.calls[0][1][0]
    assert search_query.limit == 3
    assert search_query.cursor == "synthetic-search-cursor"


@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("current_only", [False, True])
def test_search_current_only_flag_maps_to_the_existing_filter(
    scoped: bool, current_only: bool
) -> None:
    service = _Service(_result())
    args = ["search", "synthetic query"]
    if scoped:
        args.extend(["--course", "1"])
    if current_only:
        args.append("--current-only")

    assert run(args, service=service, stdout=io.StringIO()) == EXIT_COMPLETE
    method, arguments, _keywords = service.calls[0]
    assert method == ("search_course" if scoped else "search")
    query = arguments[1] if scoped else arguments[0]
    assert query.filters.include_historical_versions is not current_only


def test_cli_continuation_uses_explicit_window_across_different_clock_values() -> None:
    service = _Service(_result())
    window_arguments = [
        "--window-since",
        "2035-01-02T00:00:00+00:00",
        "--window-until",
        "2035-01-09T00:00:00+00:00",
    ]
    assert (
        run(
            ["upcoming", *window_arguments],
            service=service,
            stdout=io.StringIO(),
            now=lambda: NOW,
        )
        == EXIT_COMPLETE
    )
    assert (
        run(
            ["upcoming", *window_arguments, "--cursor", "synthetic-cursor"],
            service=service,
            stdout=io.StringIO(),
            now=lambda: NOW + timedelta(days=1),
        )
        == EXIT_COMPLETE
    )
    assert service.calls[0][1][0] == service.calls[1][1][0]
    assert service.calls[1][2]["cursor"] == "synthetic-cursor"


@pytest.mark.parametrize(
    "arguments",
    [
        ["upcoming", "--cursor", "synthetic-cursor"],
        ["events", "--next", "7d", "--cursor", "synthetic-cursor"],
        ["recent-materials", "1", "--cursor", "synthetic-cursor"],
    ],
)
def test_cli_rejects_cursor_with_a_rolling_window(arguments: list[str]) -> None:
    service = _Service(_result())
    code = run(
        arguments,
        service=service,
        stdout=io.StringIO(),
        now=lambda: NOW,
    )

    assert code == EXIT_USAGE
    assert service.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        ["upcoming", "--window-since", "2035-01-02T00:00:00+00:00"],
        ["events", "--next", "7d", "--window-until", "2035-01-09T00:00:00+00:00"],
        ["recent-materials", "1", "--window-since", "2035-01-02T00:00:00+00:00"],
    ],
)
def test_cli_rejects_an_incomplete_explicit_window(arguments: list[str]) -> None:
    service = _Service(_result())

    code = run(arguments, service=service, stdout=io.StringIO(), now=lambda: NOW)

    assert code == EXIT_USAGE
    assert service.calls == []


def test_json_preserves_the_core_schema_and_context() -> None:
    service = _Service(_result())
    output = io.StringIO()

    code = run(["--json", "courses"], service=service, stdout=output)
    payload = json.loads(output.getvalue())

    assert code == EXIT_COMPLETE
    assert payload["schema_version"] == "1.0"
    assert payload["operation"] == "synthetic.query"
    assert payload["command"] == "courses"
    assert payload["completeness"] == "COMPLETE"
    assert payload["as_of"] == NOW.isoformat()
    assert payload["coverage"][0]["data_kind"] == "materials"
    assert payload["freshness"][0]["status"] == "CURRENT"
    assert payload["provenance"][0]["locator"] == {"page": 2}


def test_local_paths_require_an_explicit_resource_opt_in() -> None:
    result = ResultEnvelope(
        "resource.get",
        items=({"key": 1, "local_path": Path("/private/synthetic.pdf")},),
        completeness=Coverage.COMPLETE,
        as_of=NOW,
    )
    service = _Service(result)
    hidden = io.StringIO()
    shown = io.StringIO()

    assert run(["--json", "resource", "1"], service=service, stdout=hidden) == EXIT_COMPLETE
    assert (
        run(
            ["--json", "resource", "1", "--include-local-path"],
            service=service,
            stdout=shown,
        )
        == EXIT_COMPLETE
    )

    assert "local_path" not in json.loads(hidden.getvalue())["items"][0]
    assert json.loads(shown.getvalue())["items"][0]["local_path"] == "/private/synthetic.pdf"


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (_result(), EXIT_COMPLETE),
        (_result(items=()), EXIT_EMPTY),
        (_result(Coverage.PARTIAL), EXIT_INCOMPLETE),
        (_result(Coverage.STALE), EXIT_INCOMPLETE),
        (_result(Coverage.UNKNOWN), EXIT_INCOMPLETE),
        (
            _result(
                Coverage.FAILED,
                items=(),
                errors=(
                    SafeError(
                        ErrorCategory.STORAGE_FAILURE,
                        "SYNTHETIC_FAILURE",
                        "synthetic safe failure",
                        "synthetic.query",
                        "synthetic",
                        False,
                        Coverage.FAILED,
                    ),
                ),
            ),
            EXIT_FAILURE,
        ),
    ],
)
def test_exit_codes_distinguish_result_meaning(
    result: ResultEnvelope[object], expected: int
) -> None:
    assert run(["courses"], service=_Service(result), stdout=io.StringIO()) == expected


def test_human_output_exposes_freshness_coverage_and_source_locators() -> None:
    output = io.StringIO()

    assert run(["courses"], service=_Service(_result()), stdout=output) == EXIT_COMPLETE

    rendered = output.getvalue()
    assert "As of: 2035-01-02T03:04:00+00:00" in rendered
    assert "Freshness:" in rendered and 'status="CURRENT"' in rendered
    assert "Coverage:" in rendered and 'data_kind="materials"' in rendered
    assert "Provenance:" in rendered and 'locator={"page":2}' in rendered


def test_incomplete_empty_output_does_not_claim_nonexistence() -> None:
    output = io.StringIO()

    code = run(["courses"], service=_Service(_result(Coverage.PARTIAL, items=())), stdout=output)

    assert code == EXIT_INCOMPLETE
    assert "absence is not conclusive" in output.getvalue()


def test_usage_error_is_json_safe_and_does_not_reflect_private_input() -> None:
    private = "private-course-secret"
    output = io.StringIO()

    code = run(["--json", "materials", private], service=_Service(_result()), stdout=output)
    payload = json.loads(output.getvalue())

    assert code == EXIT_USAGE
    assert payload["completeness"] == "FAILED"
    assert payload["errors"][0]["code"] == "CLI_INVALID_ARGUMENTS"
    assert private not in output.getvalue()


@pytest.mark.parametrize(
    ("argv", "private_value"),
    [
        (["--json", "materials", "-1"], "-1"),
        (
            [
                "--json",
                "courses",
                "--freshness",
                "cache-only",
                "--max-age-seconds",
                "60",
            ],
            "60",
        ),
        (
            [
                "--json",
                "manual-resolution",
                "field",
                "1",
                "private-invalid-field",
                "--value-json",
                '"Synthetic"',
                "--reason",
                "private-invalid-reason",
            ],
            "private-invalid-field",
        ),
        (["--json", "upcoming", "--days", "100000000000000000000"], "100000000000000000000"),
        (["--json", "sync", "1", "--max-jobs", "10001"], "10001"),
    ],
)
def test_semantic_usage_errors_precede_runtime_setup_and_do_not_reflect_input(
    argv: list[str], private_value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory_calls: list[str | Path | None] = []

    def factory(root: str | Path | None) -> _Service:
        factory_calls.append(root)
        return _Service(_result())

    monkeypatch.setattr(CoreService, "from_runtime", factory)
    output = io.StringIO()

    code = run(argv, stdout=output)
    payload = json.loads(output.getvalue())

    assert code == EXIT_USAGE
    assert factory_calls == []
    assert payload["errors"][0]["category"] == "invalid_request"
    assert payload["errors"][0]["code"] == "CLI_INVALID_ARGUMENTS"
    assert private_value not in output.getvalue()


def test_max_age_alone_builds_a_bounded_max_age_requirement() -> None:
    service = _Service(_result())

    code = run(["courses", "--max-age-seconds", "60"], service=service, stdout=io.StringIO())

    assert code == EXIT_COMPLETE
    requirement = service.calls[0][1][1]
    assert isinstance(requirement, FreshnessRequirement)
    assert requirement.mode is FreshnessMode.MAX_AGE
    assert requirement.max_age == timedelta(seconds=60)


def test_runtime_root_defaults_private_and_accepts_an_explicit_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = _Service(_result())
    roots: list[str | Path | None] = []

    def factory(root: str | Path | None) -> _Service:
        roots.append(root)
        return service

    monkeypatch.setattr(CoreService, "from_runtime", factory)

    assert run(["courses"], stdout=io.StringIO()) == EXIT_COMPLETE
    explicit = run(["courses", "--root", "/private/synthetic-root"], stdout=io.StringIO())
    assert explicit == EXIT_COMPLETE
    assert roots == [None, "/private/synthetic-root"]


def test_sync_uses_the_documented_bounded_default_window() -> None:
    service = _Service(_result())

    assert run(["sync"], service=service, stdout=io.StringIO(), now=lambda: NOW) == EXIT_COMPLETE

    policy = service.calls[0][1][0]
    assert isinstance(policy, SyncPolicy)
    assert policy.window.since == NOW - timedelta(days=30)
    assert policy.window.until == NOW + timedelta(days=365)


def test_sync_forwards_a_bounded_local_job_budget() -> None:
    service = _Service(_result())

    assert (
        run(
            ["sync", "1", "--max-jobs", "256"],
            service=service,
            stdout=io.StringIO(),
            now=lambda: NOW,
        )
        == EXIT_COMPLETE
    )

    policy = service.calls[0][1][1]
    assert isinstance(policy, SyncPolicy)
    assert policy.max_jobs == 256


def test_help_documents_the_exit_code_contract() -> None:
    help_text = " ".join(build_parser().format_help().split())

    assert "0 complete" in help_text
    assert "2 partial/stale/unknown" in help_text
    assert "3 complete empty" in help_text
    assert "64 invalid usage" in help_text
