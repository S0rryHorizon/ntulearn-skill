"""Stable JSON and compact human rendering for core result envelopes."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

JSON_SCHEMA_VERSION = "1.0"


def json_envelope(
    result: object,
    *,
    command: str,
    include_local_paths: bool = False,
) -> dict[str, object]:
    """Return the stable CLI schema without exposing paths by default."""

    if not hasattr(result, "to_dict"):
        raise TypeError("core result does not implement the stable result schema")
    raw = result.to_dict(include_local_paths=include_local_paths)
    if not isinstance(raw, Mapping):
        raise TypeError("core result schema is unavailable")
    payload = dict(raw)
    if payload.get("schema_version") != JSON_SCHEMA_VERSION:
        raise ValueError("unsupported core result schema version")
    payload["command"] = command
    return payload


def usage_error_envelope(command: str | None = None) -> dict[str, object]:
    return {
        "schema_version": JSON_SCHEMA_VERSION,
        "operation": "cli.parse",
        "command": command,
        "completeness": "FAILED",
        "items": [],
        "as_of": None,
        "freshness": [],
        "coverage": [],
        "conflicts": [],
        "provenance": [],
        "warnings": [],
        "errors": [
            {
                "category": "invalid_request",
                "code": "CLI_INVALID_ARGUMENTS",
                "message": "invalid command arguments",
                "operation": "cli.parse",
                "scope": "cli",
                "retryable": False,
                "coverage_impact": "FAILED",
            }
        ],
        "refresh_attempted": False,
        "local_reads": 0,
    }


def render_json(payload: Mapping[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _brief(value: object) -> str:
    if isinstance(value, Mapping):
        return ", ".join(
            f"{key}={json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(',', ':'))}"
            for key, item in value.items()
            if item is not None
        )
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def render_human(payload: Mapping[str, object]) -> str:
    completeness = str(payload.get("completeness", "FAILED")).upper()
    lines = [f"Completeness: {completeness}"]
    as_of = payload.get("as_of")
    lines.append(f"As of: {as_of if as_of is not None else 'unknown'}")
    freshness = payload.get("freshness")
    lines.append("Freshness:")
    if isinstance(freshness, Sequence) and freshness:
        lines.extend(f"  - {_brief(item)}" for item in freshness)
    elif freshness:
        lines.append(f"  - {_brief(freshness)}")
    else:
        lines.append("  - unavailable")
    coverage = payload.get("coverage")
    lines.append("Coverage:")
    if isinstance(coverage, Sequence) and coverage:
        lines.extend(f"  - {_brief(item)}" for item in coverage)
    else:
        lines.append("  - unavailable")
    conflicts = payload.get("conflicts")
    lines.append("Conflicts:")
    if isinstance(conflicts, Sequence) and conflicts:
        lines.extend(f"  - {_brief(item)}" for item in conflicts)
    else:
        lines.append("  - none")
    provenance = payload.get("provenance")
    lines.append("Provenance:")
    if isinstance(provenance, Sequence) and provenance:
        lines.extend(f"  - {_brief(item)}" for item in provenance)
    else:
        lines.append("  - unavailable")
    warnings = payload.get("warnings")
    if isinstance(warnings, Sequence) and warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {_brief(item)}" for item in warnings)
    errors = payload.get("errors")
    if isinstance(errors, Sequence) and errors:
        lines.append("Errors:")
        lines.extend(f"  - {_brief(item)}" for item in errors)
    items = payload.get("items")
    lines.append("Items:")
    if isinstance(items, Sequence) and items:
        lines.extend(f"  - {_brief(item)}" for item in items)
    elif completeness == "COMPLETE":
        lines.append("  - no items in complete local coverage")
    else:
        lines.append("  - no items in available local coverage; absence is not conclusive")
    return "\n".join(lines)


__all__ = [
    "JSON_SCHEMA_VERSION",
    "json_envelope",
    "render_human",
    "render_json",
    "usage_error_envelope",
]
