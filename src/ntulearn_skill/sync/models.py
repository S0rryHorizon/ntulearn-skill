"""Coverage-bearing synchronization results."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType

from ntulearn_skill.core import CourseId, Coverage, SyncRunStatus


class SyncWarning(StrEnum):
    PAGE_REPORTED_PARTIAL = "page_reported_partial"
    PAGE_COVERAGE_UNKNOWN = "page_coverage_unknown"
    PAGINATION_CYCLE = "pagination_cycle"
    PAGE_CAP_REACHED = "page_cap_reached"
    CONTENT_NODE_CAP_REACHED = "content_node_cap_reached"
    DUPLICATE_CONTENT_NODE = "duplicate_content_node"
    CAPTURE_REPLAY_ASSUMED_NOT_REVERIFIED = "capture_replay_assumed_not_reverified"
    SOURCE_FAILURE = "source_failure"


@dataclass(frozen=True, slots=True)
class ScopeResult:
    provider: str
    course_id: CourseId | None
    data_kind: str
    coverage: Coverage
    pages_seen: int
    items_seen: int
    pagination_complete: bool
    failure_category: str | None
    warnings: tuple[SyncWarning, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    key: int
    mode: str
    started_at: datetime
    ended_at: datetime
    status: SyncRunStatus
    counts: Mapping[str, int] = field(default_factory=dict)
    warnings: tuple[SyncWarning, ...] = ()
    error_category: str | None = None
    scopes: tuple[ScopeResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "counts", MappingProxyType(dict(self.counts)))
