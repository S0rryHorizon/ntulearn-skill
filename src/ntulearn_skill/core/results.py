"""Stable, privacy-safe result envelopes for the public core API."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass
from datetime import datetime, timedelta
from enum import Enum, StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Generic, TypeVar

from ntulearn_skill.core.identifiers import CourseId
from ntulearn_skill.core.models import Coverage

SCHEMA_VERSION = "1.0"
T = TypeVar("T")


class ErrorCategory(StrEnum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    SESSION_EXPIRED = "session_expired"
    CAPABILITY_UNSUPPORTED = "capability_unsupported"
    INCOMPLETE_COVERAGE = "incomplete_coverage"
    FRESHNESS_UNSATISFIED = "freshness_unsatisfied"
    SOURCE_UNAVAILABLE = "source_unavailable"
    RESOURCE_UNAVAILABLE = "resource_unavailable"
    PARSE_UNSUPPORTED = "parse_unsupported"
    PARSE_FAILED = "parse_failed"
    EXTRACTION_FAILED = "extraction_failed"
    STORAGE_FAILURE = "storage_failure"
    INTEGRITY_FAILURE = "integrity_failure"
    MIGRATION_FAILURE = "migration_failure"
    CONFLICT_REQUIRES_RESOLUTION = "conflict_requires_resolution"
    INVALID_REQUEST = "invalid_request"
    CONFIGURATION_REQUIRED = "configuration_required"


@dataclass(frozen=True, slots=True)
class SafeError:
    category: ErrorCategory
    code: str
    message: str
    operation: str
    scope: str
    retryable: bool
    coverage_impact: Coverage


@dataclass(frozen=True, slots=True)
class SafeWarning:
    code: str
    message: str
    scope: str


@dataclass(frozen=True, slots=True)
class ProvenanceView:
    source_kind: str
    source_key: int
    provider: str
    course: CourseId | None = None
    version_key: int | None = None
    locator: Mapping[str, object] | None = None

    def __post_init__(self) -> None:
        if self.source_key <= 0:
            raise ValueError("provenance source key must be positive")
        if not self.source_kind.strip() or not self.provider.strip():
            raise ValueError("provenance source kind and provider must be non-empty")
        if self.version_key is not None and self.version_key <= 0:
            raise ValueError("provenance version key must be positive")
        if self.locator is not None:
            object.__setattr__(self, "locator", MappingProxyType(dict(self.locator)))


@dataclass(frozen=True, slots=True)
class CoverageView:
    provider: str
    data_kind: str
    coverage: Coverage
    course: CourseId | None = None
    window_since: datetime | None = None
    window_until: datetime | None = None
    observed_at: datetime | None = None
    evidence: str = "sync_state"


@dataclass(frozen=True, slots=True)
class FreshnessView:
    scope: str
    status: str
    as_of: datetime | None
    age_seconds: int | None
    satisfied: bool
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConflictView:
    event_key: int
    field_name: str
    reason: str
    alternative_claim_keys: tuple[int, ...]


_PRIVATE_FIELDS = frozenset(
    {
        "local_path",
        "blob_path",
        "blob_relpath",
        "browse_path",
        "browse_relpath",
        "artifact_path",
        "artifact_relpath",
        "source_path",
    }
)


def _json_value(value: object, *, include_local_paths: bool, field_name: str = "") -> object:
    if field_name in _PRIVATE_FIELDS and not include_local_paths:
        return None
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, timedelta):
        return int(value.total_seconds())
    if isinstance(value, Path):
        return str(value) if include_local_paths else None
    if is_dataclass(value) and not isinstance(value, type):
        return {
            item.name: _json_value(
                getattr(value, item.name),
                include_local_paths=include_local_paths,
                field_name=item.name,
            )
            for item in fields(value)
            if include_local_paths or item.name not in _PRIVATE_FIELDS
        }
    if isinstance(value, Mapping):
        if any(not isinstance(key, (str, Enum)) for key in value):
            raise TypeError("result contains an unsupported mapping key")
        normalized = (
            (key.value if isinstance(key, Enum) else key, item) for key, item in value.items()
        )
        return {
            key: _json_value(item, include_local_paths=include_local_paths, field_name=key)
            for key, item in normalized
            if include_local_paths or key not in _PRIVATE_FIELDS
        }
    if isinstance(value, (set, frozenset)):
        converted = [_json_value(item, include_local_paths=include_local_paths) for item in value]
        return sorted(converted, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, (tuple, list)):
        return [_json_value(item, include_local_paths=include_local_paths) for item in value]
    raise TypeError("result contains an unsupported value")


@dataclass(frozen=True, slots=True)
class ResultEnvelope(Generic[T]):
    operation: str
    items: tuple[T, ...] = ()
    provenance: tuple[ProvenanceView, ...] = ()
    coverage: tuple[CoverageView, ...] = ()
    freshness: tuple[FreshnessView, ...] = ()
    conflicts: tuple[ConflictView, ...] = ()
    warnings: tuple[SafeWarning, ...] = ()
    errors: tuple[SafeError, ...] = ()
    completeness: Coverage = Coverage.UNKNOWN
    as_of: datetime | None = None
    refresh_attempted: bool = False
    local_reads: int = 1
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.operation.strip():
            raise ValueError("result operation must be non-empty")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported result schema version")
        if self.local_reads < 0 or self.local_reads > 2:
            raise ValueError("local read count must be between zero and two")
        for name in (
            "items",
            "provenance",
            "coverage",
            "freshness",
            "conflicts",
            "warnings",
            "errors",
        ):
            object.__setattr__(self, name, tuple(getattr(self, name)))

    @property
    def ok(self) -> bool:
        return not self.errors and self.completeness is not Coverage.FAILED

    @property
    def conclusive_empty(self) -> bool:
        return not self.items and self.completeness is Coverage.COMPLETE and not self.errors

    def to_dict(self, *, include_local_paths: bool = False) -> dict[str, object]:
        """Return the versioned machine representation, suppressing paths by default."""

        return _json_value(self, include_local_paths=include_local_paths)  # type: ignore[return-value]


__all__ = [
    "SCHEMA_VERSION",
    "ConflictView",
    "CoverageView",
    "ErrorCategory",
    "FreshnessView",
    "ProvenanceView",
    "ResultEnvelope",
    "SafeError",
    "SafeWarning",
]
