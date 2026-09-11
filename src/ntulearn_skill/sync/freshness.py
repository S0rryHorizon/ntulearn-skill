"""Deterministic local freshness evaluation and one-shot targeted refresh."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Generic, TypeVar

from ntulearn_skill.core import Coverage
from ntulearn_skill.sync.state import ScopeKey, SyncAttemptOutcome, SyncState


class FreshnessMode(StrEnum):
    CACHE_ONLY = "CACHE_ONLY"
    ALLOW_STALE = "ALLOW_STALE"
    MAX_AGE = "MAX_AGE"
    REFRESH_IF_STALE = "REFRESH_IF_STALE"
    REQUIRE_CURRENT = "REQUIRE_CURRENT"


class FreshnessStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


class FreshnessWarning(StrEnum):
    NO_COMPLETE_OBSERVATION = "no_complete_observation"
    MAX_AGE_UNCONFIGURED = "max_age_unconfigured"
    NO_MAX_AGE_GUARANTEE = "no_max_age_guarantee"
    LATEST_ATTEMPT_INCOMPLETE = "latest_attempt_incomplete"
    CURRENT_COVERAGE_UNESTABLISHED = "current_coverage_unestablished"
    HISTORICAL_LOCAL_ONLY = "historical_local_only"
    TARGETED_REFRESH_UNAVAILABLE = "targeted_refresh_unavailable"
    TARGETED_REFRESH_FAILED = "targeted_refresh_failed"


@dataclass(frozen=True, slots=True)
class FreshnessRequirement:
    mode: FreshnessMode
    max_age: timedelta | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.mode, FreshnessMode):
            raise TypeError("freshness mode must use the typed vocabulary")
        if self.mode is FreshnessMode.MAX_AGE:
            if self.max_age is None or self.max_age <= timedelta():
                raise ValueError("MAX_AGE requires a positive duration")
        elif self.max_age is not None:
            raise ValueError("only MAX_AGE accepts an explicit duration")

    @classmethod
    def cache_only(cls) -> FreshnessRequirement:
        return cls(FreshnessMode.CACHE_ONLY)

    @classmethod
    def allow_stale(cls) -> FreshnessRequirement:
        return cls(FreshnessMode.ALLOW_STALE)

    @classmethod
    def with_max_age(cls, max_age: timedelta) -> FreshnessRequirement:
        return cls(FreshnessMode.MAX_AGE, max_age)

    @classmethod
    def refresh_if_stale(cls) -> FreshnessRequirement:
        return cls(FreshnessMode.REFRESH_IF_STALE)

    @classmethod
    def require_current(cls) -> FreshnessRequirement:
        return cls(FreshnessMode.REQUIRE_CURRENT)


@dataclass(frozen=True, slots=True)
class FreshnessDecision:
    status: FreshnessStatus
    as_of: datetime | None
    age: timedelta | None
    max_age: timedelta | None
    latest_coverage: Coverage | None
    last_attempt_outcome: SyncAttemptOutcome | None
    satisfied: bool
    should_refresh: bool
    warning_codes: tuple[FreshnessWarning, ...] = ()
    successful_observation_at: datetime | None = None
    successful_observation_age: timedelta | None = None
    complete_snapshot_at: datetime | None = None
    complete_snapshot_age: timedelta | None = None
    ttl_configured: bool = False


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("freshness evaluation time must be timezone-aware")
    return value.astimezone(UTC)


def evaluate_freshness(
    state: SyncState | None,
    requirement: FreshnessRequirement,
    *,
    now: datetime,
) -> FreshnessDecision:
    """Evaluate an exact scope without accessing a source or mutating state."""

    current_time = _aware_utc(now)
    # ``as_of``/``age`` retain the original complete-snapshot meaning for API
    # compatibility.  Successful observations are reported independently so a
    # failed attempt cannot masquerade as a newer successful read; a successful
    # partial attempt advances only the successful-observation marker.
    as_of = None if state is None else state.last_complete_at
    age = None if as_of is None else max(current_time - as_of, timedelta())
    successful_observation_at = None if state is None else state.last_success_at
    successful_observation_age = (
        None
        if successful_observation_at is None
        else max(current_time - successful_observation_at, timedelta())
    )
    warning_codes: list[FreshnessWarning] = []
    if age is None:
        warning_codes.append(FreshnessWarning.NO_COMPLETE_OBSERVATION)

    max_age = requirement.max_age
    if requirement.mode is not FreshnessMode.MAX_AGE:
        max_age = None if state is None else state.configured_max_age
    if requirement.mode is FreshnessMode.REFRESH_IF_STALE:
        if max_age is None:
            warning_codes.append(FreshnessWarning.MAX_AGE_UNCONFIGURED)
    if max_age is None:
        warning_codes.append(FreshnessWarning.NO_MAX_AGE_GUARANTEE)

    complete_known = age is not None
    if age is not None and max_age is not None:
        status = FreshnessStatus.CURRENT if age <= max_age else FreshnessStatus.STALE
    elif complete_known:
        status = FreshnessStatus.CURRENT
    else:
        status = FreshnessStatus.UNKNOWN

    latest_incomplete = bool(
        state is not None
        and (
            state.last_attempt_outcome is SyncAttemptOutcome.FAILED
            or state.latest_coverage is not Coverage.COMPLETE
        )
        and (
            state.last_complete_at is None
            or (
                state.last_attempt_at,
                state.latest_attempt_run_key,
            )
            > (
                state.last_complete_at,
                state.latest_complete_run_key or 0,
            )
        )
    )
    if latest_incomplete:
        warning_codes.append(FreshnessWarning.LATEST_ATTEMPT_INCOMPLETE)
        status = FreshnessStatus.STALE if complete_known else FreshnessStatus.UNKNOWN

    mode = requirement.mode
    if mode in {FreshnessMode.CACHE_ONLY, FreshnessMode.ALLOW_STALE}:
        satisfied = True
        should_refresh = False
    elif mode is FreshnessMode.REFRESH_IF_STALE and max_age is None:
        satisfied = False
        should_refresh = False
    elif mode is FreshnessMode.REQUIRE_CURRENT:
        satisfied = bool(
            state is not None
            and state.last_attempt_outcome is SyncAttemptOutcome.SUCCEEDED
            and state.latest_coverage is Coverage.COMPLETE
            and state.last_complete_at == state.last_attempt_at
            and state.last_attempt_at >= current_time
        )
        should_refresh = not satisfied
        if not satisfied:
            warning_codes.append(FreshnessWarning.CURRENT_COVERAGE_UNESTABLISHED)
            status = FreshnessStatus.STALE if complete_known else FreshnessStatus.UNKNOWN
    else:
        satisfied = status is FreshnessStatus.CURRENT
        should_refresh = not satisfied

    return FreshnessDecision(
        status=status,
        as_of=as_of,
        age=age,
        max_age=max_age,
        latest_coverage=None if state is None else state.latest_coverage,
        last_attempt_outcome=None if state is None else state.last_attempt_outcome,
        satisfied=satisfied,
        should_refresh=should_refresh,
        warning_codes=tuple(warning_codes),
        successful_observation_at=successful_observation_at,
        successful_observation_age=successful_observation_age,
        complete_snapshot_at=as_of,
        complete_snapshot_age=age,
        ttl_configured=max_age is not None,
    )


T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FreshnessResult(Generic[T]):
    value: T
    decision: FreshnessDecision
    refresh_attempted: bool
    local_reads: int


def retrieve_with_freshness(
    scope: ScopeKey,
    requirement: FreshnessRequirement,
    *,
    load_local: Callable[[], T],
    load_state: Callable[[ScopeKey], SyncState | None],
    refresh: Callable[[ScopeKey], None] | None,
    now: datetime,
    historical: bool = False,
) -> FreshnessResult[T]:
    """Read locally, optionally refresh one exact scope, then retry locally once."""

    evaluation_time = _aware_utc(now)
    value = load_local()
    decision = evaluate_freshness(load_state(scope), requirement, now=evaluation_time)
    if not decision.should_refresh:
        return FreshnessResult(value, decision, False, 1)
    if historical:
        decision = replace(
            decision,
            warning_codes=decision.warning_codes + (FreshnessWarning.HISTORICAL_LOCAL_ONLY,),
        )
        return FreshnessResult(value, decision, False, 1)
    if refresh is None:
        decision = replace(
            decision,
            warning_codes=decision.warning_codes + (FreshnessWarning.TARGETED_REFRESH_UNAVAILABLE,),
        )
        return FreshnessResult(value, decision, False, 1)

    try:
        refresh(scope)
    except Exception:
        decision = replace(
            decision,
            warning_codes=decision.warning_codes + (FreshnessWarning.TARGETED_REFRESH_FAILED,),
        )
        return FreshnessResult(value, decision, True, 1)

    refreshed_value = load_local()
    refreshed_decision = evaluate_freshness(load_state(scope), requirement, now=evaluation_time)
    return FreshnessResult(refreshed_value, refreshed_decision, True, 2)


__all__ = [
    "FreshnessDecision",
    "FreshnessMode",
    "FreshnessRequirement",
    "FreshnessResult",
    "FreshnessStatus",
    "FreshnessWarning",
    "evaluate_freshness",
    "retrieve_with_freshness",
]
