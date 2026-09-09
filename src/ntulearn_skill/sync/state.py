"""Exact-scope synchronization state and order-aware persistence."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from types import MappingProxyType

from ntulearn_skill.client.contracts import CapabilityState, SourceCapability, TimeWindow
from ntulearn_skill.core import CourseId, Coverage
from ntulearn_skill.core.models import from_storage_time, to_storage_time
from ntulearn_skill.storage import Database, StorageError
from ntulearn_skill.sync.models import SyncWarning


class SyncAttemptOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ScopeKey:
    """Identity for one provider/course/data-kind/window synchronization scope."""

    provider: str
    course_id: CourseId | None
    data_kind: str
    time_window: TimeWindow | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.provider, str)
            or not isinstance(self.data_kind, str)
            or not self.provider.strip()
            or not self.data_kind.strip()
        ):
            raise ValueError("scope provider and data kind must be non-empty")
        if self.course_id is not None:
            if type(self.course_id) is not CourseId:
                raise TypeError("scope course_id must be a CourseId")
            if self.course_id.provider != self.provider:
                raise ValueError("scope course provider must match the scope provider")
        if self.time_window is not None and not isinstance(self.time_window, TimeWindow):
            raise TypeError("scope time_window must be a TimeWindow")

    @property
    def window_key(self) -> str:
        if self.time_window is None:
            return ""
        return json.dumps(
            [
                to_storage_time(self.time_window.since),
                to_storage_time(self.time_window.until),
            ],
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class SyncState:
    scope: ScopeKey
    latest_attempt_run_key: int
    last_attempt_at: datetime
    last_attempt_outcome: SyncAttemptOutcome
    latest_coverage: Coverage
    warning_codes: tuple[SyncWarning, ...]
    latest_success_run_key: int | None
    last_success_at: datetime | None
    latest_complete_run_key: int | None
    last_complete_at: datetime | None
    configured_max_age: timedelta | None
    capabilities: Mapping[SourceCapability, CapabilityState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.latest_attempt_run_key <= 0:
            raise ValueError("latest attempt run key must be positive")
        if not isinstance(self.last_attempt_outcome, SyncAttemptOutcome) or not isinstance(
            self.latest_coverage, Coverage
        ):
            raise TypeError("sync state uses an invalid typed vocabulary")
        attempt_at = _aware_utc(self.last_attempt_at, "last_attempt_at")
        success_at = _optional_aware_utc(self.last_success_at, "last_success_at")
        complete_at = _optional_aware_utc(self.last_complete_at, "last_complete_at")
        if (self.latest_success_run_key is None) != (success_at is None):
            raise ValueError("last success run and timestamp must be present together")
        if (self.latest_complete_run_key is None) != (complete_at is None):
            raise ValueError("last complete run and timestamp must be present together")
        if self.latest_success_run_key is not None and self.latest_success_run_key <= 0:
            raise ValueError("latest success run key must be positive")
        if self.latest_complete_run_key is not None and self.latest_complete_run_key <= 0:
            raise ValueError("latest complete run key must be positive")
        if success_at is not None and complete_at is not None and complete_at > success_at:
            raise ValueError("last complete observation cannot be newer than last success")
        _max_age_seconds(self.configured_max_age)
        _warning_json(self.warning_codes)
        _capability_json(self.capabilities)
        object.__setattr__(self, "last_attempt_at", attempt_at)
        object.__setattr__(self, "last_success_at", success_at)
        object.__setattr__(self, "last_complete_at", complete_at)
        object.__setattr__(self, "warning_codes", tuple(self.warning_codes))
        object.__setattr__(self, "capabilities", MappingProxyType(dict(self.capabilities)))

    def age_at(self, now: datetime) -> timedelta | None:
        normalized = _aware_utc(now, "now")
        if self.last_complete_at is None:
            return None
        return max(normalized - self.last_complete_at, timedelta())


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_aware_utc(value: datetime | None, name: str) -> datetime | None:
    return None if value is None else _aware_utc(value, name)


def _warning_json(warning_codes: Sequence[SyncWarning]) -> str:
    copied = tuple(warning_codes)
    if len(copied) > 100 or any(not isinstance(code, SyncWarning) for code in copied):
        raise ValueError("warning codes must use the safe typed vocabulary")
    return json.dumps([code.value for code in copied], separators=(",", ":"))


def _max_age_seconds(value: timedelta | None) -> int | None:
    if value is None:
        return None
    seconds = value.total_seconds()
    if seconds <= 0 or seconds != int(seconds):
        raise ValueError("configured max age must be a positive whole number of seconds")
    return int(seconds)


def _capability_json(values: Mapping[SourceCapability, CapabilityState]) -> str:
    copied = dict(values)
    if any(
        not isinstance(capability, SourceCapability) or not isinstance(state, CapabilityState)
        for capability, state in copied.items()
    ):
        raise ValueError("capability map contains an invalid value")
    return json.dumps(
        {capability.value: state.value for capability, state in copied.items()},
        sort_keys=True,
        separators=(",", ":"),
    )


class SyncStateRepository:
    """Persist independent attempt, success, and complete markers for exact scopes."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def get(self, scope: ScopeKey) -> SyncState | None:
        connection = self.database.connect()
        try:
            row = self._select(connection, scope)
            return None if row is None else self._from_row(row, scope)
        except (json.JSONDecodeError, sqlite3.Error, TypeError, ValueError):
            raise StorageError("sync state operation failed") from None
        finally:
            connection.close()

    def record_attempt(
        self,
        scope: ScopeKey,
        *,
        run_key: int,
        attempted_at: datetime,
        outcome: SyncAttemptOutcome,
        coverage: Coverage,
        warning_codes: Sequence[SyncWarning] = (),
        configured_max_age: timedelta | None = None,
        capabilities: Mapping[SourceCapability, CapabilityState] = MappingProxyType({}),
    ) -> SyncState:
        if isinstance(run_key, bool) or not isinstance(run_key, int) or run_key <= 0:
            raise ValueError("run_key must be a positive integer")
        if not isinstance(outcome, SyncAttemptOutcome) or not isinstance(coverage, Coverage):
            raise TypeError("sync outcome and coverage must use their typed vocabularies")
        if outcome is SyncAttemptOutcome.FAILED and coverage is Coverage.COMPLETE:
            raise ValueError("a failed attempt cannot establish complete coverage")
        attempted = to_storage_time(_aware_utc(attempted_at, "attempted_at"))
        warnings = _warning_json(warning_codes)
        max_age_seconds = _max_age_seconds(configured_max_age)
        capability_json = _capability_json(capabilities)
        success_run = run_key if outcome is SyncAttemptOutcome.SUCCEEDED else None
        complete_run = success_run if coverage is Coverage.COMPLETE else None
        success_at = attempted if success_run is not None else None
        complete_at = attempted if complete_run is not None else None
        try:
            with self.database.transaction() as connection:
                provider_key = self._provider_key(connection, scope.provider, attempted)
                course_key = self._course_key(connection, scope)
                connection.execute(
                    """
                    INSERT INTO sync_state(
                        provider_key, course_key, data_kind, window_key,
                        latest_attempt_run_key, latest_success_run_key,
                        latest_complete_run_key, coverage, checkpoint_json,
                        capability_json, stale_after, updated_at, last_attempt_at,
                        last_attempt_outcome, last_success_at, last_complete_at,
                        warning_codes_json, configured_max_age_seconds
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '{}', ?, NULL, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT DO UPDATE SET
                        latest_attempt_run_key = CASE
                            WHEN excluded.last_attempt_at > sync_state.last_attempt_at
                              OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                  AND excluded.latest_attempt_run_key
                                      > sync_state.latest_attempt_run_key)
                            THEN excluded.latest_attempt_run_key
                            ELSE sync_state.latest_attempt_run_key
                        END,
                        coverage = CASE
                            WHEN excluded.last_attempt_at > sync_state.last_attempt_at
                              OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                  AND excluded.latest_attempt_run_key
                                      > sync_state.latest_attempt_run_key)
                            THEN excluded.coverage ELSE sync_state.coverage
                        END,
                        last_attempt_outcome = CASE
                            WHEN excluded.last_attempt_at > sync_state.last_attempt_at
                              OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                  AND excluded.latest_attempt_run_key
                                      > sync_state.latest_attempt_run_key)
                            THEN excluded.last_attempt_outcome
                            ELSE sync_state.last_attempt_outcome
                        END,
                        warning_codes_json = CASE
                            WHEN excluded.last_attempt_at > sync_state.last_attempt_at
                              OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                  AND excluded.latest_attempt_run_key
                                      > sync_state.latest_attempt_run_key)
                            THEN excluded.warning_codes_json
                            ELSE sync_state.warning_codes_json
                        END,
                        last_attempt_at = MAX(
                            sync_state.last_attempt_at, excluded.last_attempt_at
                        ),
                        latest_success_run_key = CASE
                            WHEN excluded.last_success_at IS NOT NULL AND (
                                sync_state.last_success_at IS NULL
                                OR excluded.last_success_at > sync_state.last_success_at
                                OR (excluded.last_success_at = sync_state.last_success_at
                                    AND excluded.latest_success_run_key
                                        > sync_state.latest_success_run_key)
                            ) THEN excluded.latest_success_run_key
                            ELSE sync_state.latest_success_run_key
                        END,
                        last_success_at = CASE
                            WHEN excluded.last_success_at IS NULL THEN sync_state.last_success_at
                            WHEN sync_state.last_success_at IS NULL THEN excluded.last_success_at
                            ELSE MAX(sync_state.last_success_at, excluded.last_success_at)
                        END,
                        latest_complete_run_key = CASE
                            WHEN excluded.last_complete_at IS NOT NULL AND (
                                sync_state.last_complete_at IS NULL
                                OR excluded.last_complete_at > sync_state.last_complete_at
                                OR (excluded.last_complete_at = sync_state.last_complete_at
                                    AND excluded.latest_complete_run_key
                                        > sync_state.latest_complete_run_key)
                            ) THEN excluded.latest_complete_run_key
                            ELSE sync_state.latest_complete_run_key
                        END,
                        last_complete_at = CASE
                            WHEN excluded.last_complete_at IS NULL THEN sync_state.last_complete_at
                            WHEN sync_state.last_complete_at IS NULL THEN excluded.last_complete_at
                            ELSE MAX(sync_state.last_complete_at, excluded.last_complete_at)
                        END,
                        capability_json = CASE
                            WHEN excluded.capability_json != '{}' AND (
                                excluded.last_attempt_at > sync_state.last_attempt_at
                                OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                    AND excluded.latest_attempt_run_key
                                        > sync_state.latest_attempt_run_key)
                            ) THEN excluded.capability_json
                            ELSE sync_state.capability_json
                        END,
                        configured_max_age_seconds = CASE
                            WHEN excluded.configured_max_age_seconds IS NOT NULL AND (
                                excluded.last_attempt_at > sync_state.last_attempt_at
                                OR (excluded.last_attempt_at = sync_state.last_attempt_at
                                    AND excluded.latest_attempt_run_key
                                        > sync_state.latest_attempt_run_key)
                            ) THEN excluded.configured_max_age_seconds
                            ELSE sync_state.configured_max_age_seconds
                        END,
                        updated_at = MAX(sync_state.updated_at, excluded.updated_at)
                    """,
                    (
                        provider_key,
                        course_key,
                        scope.data_kind,
                        scope.window_key,
                        run_key,
                        success_run,
                        complete_run,
                        coverage.value,
                        capability_json,
                        attempted,
                        attempted,
                        outcome.value,
                        success_at,
                        complete_at,
                        warnings,
                        max_age_seconds,
                    ),
                )
                row = self._select(connection, scope)
                assert row is not None
                state = self._from_row(row, scope)
                stale_after = (
                    None
                    if state.last_complete_at is None or state.configured_max_age is None
                    else to_storage_time(state.last_complete_at + state.configured_max_age)
                )
                connection.execute(
                    "UPDATE sync_state SET stale_after = ? WHERE sync_state_key = ?",
                    (stale_after, int(row["sync_state_key"])),
                )
            result = self.get(scope)
            assert result is not None
            return result
        except sqlite3.Error:
            raise StorageError("sync state operation failed") from None

    @staticmethod
    def _provider_key(connection: sqlite3.Connection, provider: str, observed_at: str) -> int:
        connection.execute(
            """
            INSERT INTO source_provider(name, created_at, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
                created_at = MIN(created_at, excluded.created_at),
                updated_at = MAX(updated_at, excluded.updated_at)
            """,
            (provider, observed_at, observed_at),
        )
        row = connection.execute(
            "SELECT provider_key FROM source_provider WHERE name = ?", (provider,)
        ).fetchone()
        assert row is not None
        return int(row[0])

    @staticmethod
    def _course_key(connection: sqlite3.Connection, scope: ScopeKey) -> int | None:
        if scope.course_id is None:
            return None
        row = connection.execute(
            """
            SELECT course.course_key
            FROM course
            JOIN source_object USING (source_object_key)
            JOIN source_provider USING (provider_key)
            WHERE source_provider.name = ?
              AND source_object.object_kind = 'course'
              AND source_object.remote_key = ?
            """,
            (scope.provider, scope.course_id.value),
        ).fetchone()
        if row is None:
            raise ValueError("sync scope course has not been observed")
        return int(row[0])

    @staticmethod
    def _select(connection: sqlite3.Connection, scope: ScopeKey) -> sqlite3.Row | None:
        course_remote_key = None if scope.course_id is None else scope.course_id.value
        row: sqlite3.Row | None = connection.execute(
            """
            SELECT sync_state.*, source_provider.name AS provider,
                   source_object.remote_key AS course_remote_key
            FROM sync_state
            JOIN source_provider USING (provider_key)
            LEFT JOIN course USING (course_key)
            LEFT JOIN source_object USING (source_object_key)
            WHERE source_provider.name = ?
              AND ((? IS NULL AND sync_state.course_key IS NULL)
                   OR source_object.remote_key = ?)
              AND sync_state.data_kind = ? AND sync_state.window_key = ?
            """,
            (
                scope.provider,
                course_remote_key,
                course_remote_key,
                scope.data_kind,
                scope.window_key,
            ),
        ).fetchone()
        return row

    @staticmethod
    def _from_row(row: sqlite3.Row, scope: ScopeKey) -> SyncState:
        warning_values = json.loads(str(row["warning_codes_json"]))
        capability_values = json.loads(str(row["capability_json"]))
        if not isinstance(warning_values, list) or not isinstance(capability_values, dict):
            raise ValueError("stored sync state JSON has an invalid shape")
        warnings = tuple(SyncWarning(str(value)) for value in warning_values)
        _warning_json(warnings)
        capabilities = {
            SourceCapability(str(capability)): CapabilityState(str(state))
            for capability, state in capability_values.items()
        }
        max_age_value = row["configured_max_age_seconds"]
        return SyncState(
            scope=scope,
            latest_attempt_run_key=int(row["latest_attempt_run_key"]),
            last_attempt_at=from_storage_time(str(row["last_attempt_at"])),
            last_attempt_outcome=SyncAttemptOutcome(str(row["last_attempt_outcome"])),
            latest_coverage=Coverage(str(row["coverage"])),
            warning_codes=warnings,
            latest_success_run_key=None
            if row["latest_success_run_key"] is None
            else int(row["latest_success_run_key"]),
            last_success_at=None
            if row["last_success_at"] is None
            else from_storage_time(str(row["last_success_at"])),
            latest_complete_run_key=None
            if row["latest_complete_run_key"] is None
            else int(row["latest_complete_run_key"]),
            last_complete_at=None
            if row["last_complete_at"] is None
            else from_storage_time(str(row["last_complete_at"])),
            configured_max_age=None
            if max_age_value is None
            else timedelta(seconds=int(max_age_value)),
            capabilities=capabilities,
        )


__all__ = ["ScopeKey", "SyncAttemptOutcome", "SyncState", "SyncStateRepository"]
