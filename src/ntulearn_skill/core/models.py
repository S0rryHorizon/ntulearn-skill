"""Small domain value objects shared by storage and later core use cases."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


class Availability(StrEnum):
    ACTIVE = "ACTIVE"
    MISSING = "MISSING"
    UNAVAILABLE = "UNAVAILABLE"
    REMOVED_CONFIRMED = "REMOVED_CONFIRMED"
    UNKNOWN = "UNKNOWN"


class Coverage(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    FAILED = "FAILED"


class SyncRunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    SUCCEEDED_WITH_WARNINGS = "SUCCEEDED_WITH_WARNINGS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ObservationStatus(StrEnum):
    OBSERVED = "OBSERVED"
    NOT_OBSERVED = "NOT_OBSERVED"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


class FetchDecision(StrEnum):
    FETCHED = "FETCHED"
    REUSED_VERIFIED = "REUSED_VERIFIED"
    DEFERRED = "DEFERRED"
    NOT_NEEDED = "NOT_NEEDED"
    FAILED = "FAILED"


class VerificationStatus(StrEnum):
    VERIFIED = "VERIFIED"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"


class TemporalPrecision(StrEnum):
    EXACT_TIME = "EXACT_TIME"
    DATE_ONLY = "DATE_ONLY"
    WEEK_ONLY = "WEEK_ONLY"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True, slots=True)
class SourceTime:
    """A source time with normalized instant and retained source semantics."""

    instant: datetime | None
    source_text: str
    source_timezone: str | None
    precision: TemporalPrecision

    def __post_init__(self) -> None:
        if self.instant is not None:
            if self.instant.tzinfo is None or self.instant.utcoffset() is None:
                raise ValueError("instant must be timezone-aware")
            if self.precision is not TemporalPrecision.EXACT_TIME:
                raise ValueError("only exact source times may carry an instant")
            if not self.source_timezone:
                raise ValueError("exact source times must retain the source timezone")
            object.__setattr__(self, "instant", self.instant.astimezone(UTC))
        if self.precision is TemporalPrecision.EXACT_TIME and self.instant is None:
            raise ValueError("exact times require an instant")
        if not self.source_text:
            raise ValueError("source_text must be retained")


def utc_now() -> datetime:
    """Return an aware UTC timestamp for persisted local observations."""

    return datetime.now(UTC)


def to_storage_time(value: datetime) -> str:
    """Normalize an aware timestamp to a stable UTC representation."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def from_storage_time(value: str) -> datetime:
    """Parse a timestamp persisted by :func:`to_storage_time`."""

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return parsed.astimezone(UTC)
