"""Synthetic M8 regressions for exact-scope state and bounded freshness refresh."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from ntulearn_skill.client.contracts import CapabilityState, SourceCapability, TimeWindow
from ntulearn_skill.core import CourseId, Coverage
from ntulearn_skill.storage import Database, DomainRepository
from ntulearn_skill.storage.migration import MigrationRunner, load_migrations
from ntulearn_skill.sync.freshness import (
    FreshnessMode,
    FreshnessRequirement,
    FreshnessStatus,
    FreshnessWarning,
    evaluate_freshness,
    retrieve_with_freshness,
)
from ntulearn_skill.sync.models import SyncWarning
from ntulearn_skill.sync.state import (
    ScopeKey,
    SyncAttemptOutcome,
    SyncState,
    SyncStateRepository,
)


def _instant(hour: int) -> datetime:
    return datetime(2030, 1, 1, hour, tzinfo=UTC)


@pytest.fixture
def state_store(tmp_path: Path) -> tuple[Database, SyncStateRepository, CourseId]:
    database = Database(tmp_path / "private-synthetic" / "metadata.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() >= 8
    course_id = CourseId("synthetic", "m8-example-course")
    domain.put_course(course_id, code="PH0000", title="Example Physics Course")
    return database, SyncStateRepository(database), course_id


def _add_run(database: Database, run_key: int, at: datetime) -> None:
    with database.transaction() as connection:
        connection.execute(
            """
            INSERT INTO sync_run(
                sync_run_key, mode, requested_scope_json, started_at, ended_at, status
            ) VALUES (?, 'synthetic', '{}', ?, ?, 'SUCCEEDED')
            """,
            (run_key, at.isoformat(), at.isoformat()),
        )


def _snapshot(
    scope: ScopeKey,
    *,
    attempted_at: datetime,
    complete_at: datetime | None,
    coverage: Coverage = Coverage.COMPLETE,
    outcome: SyncAttemptOutcome = SyncAttemptOutcome.SUCCEEDED,
    configured_max_age: timedelta | None = None,
) -> SyncState:
    return SyncState(
        scope=scope,
        latest_attempt_run_key=2,
        last_attempt_at=attempted_at,
        last_attempt_outcome=outcome,
        latest_coverage=coverage,
        warning_codes=(),
        latest_success_run_key=1,
        last_success_at=complete_at,
        latest_complete_run_key=None if complete_at is None else 1,
        last_complete_at=complete_at,
        configured_max_age=configured_max_age,
    )


def test_migration_adds_durable_freshness_columns(
    state_store: tuple[Database, object, object],
) -> None:
    database = state_store[0]
    connection = database.connect()
    try:
        versions = [
            int(row[0]) for row in connection.execute("SELECT version FROM schema_migration")
        ]
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(sync_state)").fetchall()
        }
    finally:
        connection.close()

    assert max(versions) >= 8
    assert {
        "last_attempt_at",
        "last_attempt_outcome",
        "last_success_at",
        "last_complete_at",
        "warning_codes_json",
        "configured_max_age_seconds",
    } <= columns


def test_migration_backfills_existing_sync_state_without_losing_complete_marker(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "private-upgrade" / "metadata.sqlite3")
    assert MigrationRunner(database, migrations=load_migrations()[:7]).migrate() == 7
    timestamp = _instant(1).isoformat(timespec="microseconds")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO source_provider VALUES (1, 'synthetic', '{}', ?, ?)",
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO sync_run(sync_run_key, mode, started_at, ended_at, status)
            VALUES (1, 'synthetic', ?, ?, 'SUCCEEDED')
            """,
            (timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO sync_state(
                provider_key, data_kind, latest_attempt_run_key,
                latest_success_run_key, latest_complete_run_key, coverage, updated_at
            ) VALUES (1, 'courses', 1, 1, 1, 'COMPLETE', ?)
            """,
            (timestamp,),
        )

    assert MigrationRunner(database).migrate() >= 8
    connection = database.connect()
    try:
        row = connection.execute("SELECT * FROM sync_state").fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row["last_attempt_at"] == row["last_success_at"] == row["last_complete_at"]
    assert row["last_attempt_outcome"] == "SUCCEEDED"
    assert row["latest_complete_run_key"] == 1


def test_scope_key_is_typed_and_normalizes_an_exact_window() -> None:
    utc_window = TimeWindow(_instant(1), _instant(2))
    local_zone = timezone(timedelta(hours=8))
    local_window = TimeWindow(
        datetime(2030, 1, 1, 9, tzinfo=local_zone),
        datetime(2030, 1, 1, 10, tzinfo=local_zone),
    )
    course = CourseId("synthetic", "m8-example-course")

    assert (
        ScopeKey("synthetic", course, "announcements", utc_window).window_key
        == ScopeKey("synthetic", course, "announcements", local_window).window_key
    )
    with pytest.raises(ValueError, match="provider"):
        ScopeKey("other", course, "announcements")
    with pytest.raises(ValueError, match="aware"):
        TimeWindow(datetime(2030, 1, 1), datetime(2030, 1, 2))


def test_repository_preserves_independent_ordered_markers_and_exact_scope(
    state_store: tuple[Database, SyncStateRepository, CourseId],
) -> None:
    database, repository, course_id = state_store
    full_scope = ScopeKey("synthetic", course_id, "announcements")
    window = TimeWindow(_instant(0), _instant(1))
    window_scope = ScopeKey("synthetic", course_id, "announcements", window)
    for run_key, at in ((1, _instant(1)), (2, _instant(2)), (3, _instant(0)), (4, _instant(3))):
        _add_run(database, run_key, at)

    repository.record_attempt(
        full_scope,
        run_key=1,
        attempted_at=_instant(1),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=timedelta(hours=2),
        capabilities={SourceCapability.ANNOUNCEMENTS: CapabilityState.SUPPORTED},
    )
    failed = repository.record_attempt(
        full_scope,
        run_key=2,
        attempted_at=_instant(2),
        outcome=SyncAttemptOutcome.FAILED,
        coverage=Coverage.PARTIAL,
        warning_codes=(SyncWarning.SOURCE_FAILURE,),
    )

    assert failed.latest_attempt_run_key == 2
    assert failed.last_attempt_outcome is SyncAttemptOutcome.FAILED
    assert failed.latest_coverage is Coverage.PARTIAL
    assert failed.warning_codes == (SyncWarning.SOURCE_FAILURE,)
    assert failed.latest_success_run_key == failed.latest_complete_run_key == 1
    assert failed.last_success_at == failed.last_complete_at == _instant(1)
    assert failed.configured_max_age == timedelta(hours=2)
    assert failed.capabilities[SourceCapability.ANNOUNCEMENTS] is CapabilityState.SUPPORTED

    older = repository.record_attempt(
        full_scope,
        run_key=3,
        attempted_at=_instant(0),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=timedelta(minutes=1),
        capabilities={SourceCapability.ANNOUNCEMENTS: CapabilityState.UNSUPPORTED},
    )
    assert older.latest_attempt_run_key == 2
    assert older.latest_complete_run_key == 1
    assert older.configured_max_age == timedelta(hours=2)
    assert older.capabilities[SourceCapability.ANNOUNCEMENTS] is CapabilityState.SUPPORTED

    repository.record_attempt(
        window_scope,
        run_key=4,
        attempted_at=_instant(3),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
    )
    assert repository.get(window_scope) is not None
    assert repository.get(full_scope) == older
    other_window = ScopeKey(
        "synthetic", course_id, "announcements", TimeWindow(_instant(1), _instant(2))
    )
    assert repository.get(other_window) is None


def test_repository_rejects_unsafe_or_inconsistent_attempt_values(
    state_store: tuple[Database, SyncStateRepository, CourseId],
) -> None:
    database, repository, course_id = state_store
    scope = ScopeKey("synthetic", course_id, "announcements")
    _add_run(database, 1, _instant(1))

    with pytest.raises(ValueError, match="timezone-aware"):
        repository.record_attempt(
            scope,
            run_key=1,
            attempted_at=datetime(2030, 1, 1),
            outcome=SyncAttemptOutcome.SUCCEEDED,
            coverage=Coverage.COMPLETE,
        )
    with pytest.raises(ValueError, match="failed attempt"):
        repository.record_attempt(
            scope,
            run_key=1,
            attempted_at=_instant(1),
            outcome=SyncAttemptOutcome.FAILED,
            coverage=Coverage.COMPLETE,
        )
    with pytest.raises(ValueError, match="safe typed vocabulary"):
        repository.record_attempt(
            scope,
            run_key=1,
            attempted_at=_instant(1),
            outcome=SyncAttemptOutcome.FAILED,
            coverage=Coverage.FAILED,
            warning_codes=("private payload",),  # type: ignore[arg-type]
        )


def test_freshness_requirements_compute_age_and_currentness() -> None:
    scope = ScopeKey("synthetic", None, "courses")
    state = _snapshot(
        scope,
        attempted_at=_instant(1),
        complete_at=_instant(1),
        configured_max_age=timedelta(hours=2),
    )

    fresh = evaluate_freshness(
        state, FreshnessRequirement.with_max_age(timedelta(hours=2)), now=_instant(3)
    )
    stale = evaluate_freshness(
        state, FreshnessRequirement.with_max_age(timedelta(minutes=30)), now=_instant(3)
    )
    configured = evaluate_freshness(state, FreshnessRequirement.refresh_if_stale(), now=_instant(3))
    require_current = evaluate_freshness(
        state, FreshnessRequirement.require_current(), now=_instant(3)
    )

    assert fresh.status is FreshnessStatus.CURRENT and fresh.satisfied
    assert fresh.age == fresh.max_age == timedelta(hours=2)
    assert stale.status is FreshnessStatus.STALE and stale.should_refresh
    assert configured.satisfied and not configured.should_refresh
    assert not require_current.satisfied and require_current.should_refresh
    assert FreshnessWarning.CURRENT_COVERAGE_UNESTABLISHED in require_current.warning_codes

    fresh_but_partial = _snapshot(
        scope,
        attempted_at=_instant(3),
        complete_at=_instant(1),
        coverage=Coverage.PARTIAL,
    )
    partial_current = evaluate_freshness(
        fresh_but_partial, FreshnessRequirement.require_current(), now=_instant(3)
    )
    assert not partial_current.satisfied and partial_current.should_refresh
    assert partial_current.latest_coverage is Coverage.PARTIAL

    unconfigured = evaluate_freshness(
        None, FreshnessRequirement.refresh_if_stale(), now=_instant(3)
    )
    assert unconfigured.status is FreshnessStatus.UNKNOWN
    assert not unconfigured.satisfied and not unconfigured.should_refresh
    assert FreshnessWarning.MAX_AGE_UNCONFIGURED in unconfigured.warning_codes


def test_allow_stale_reports_configured_age_and_newer_incomplete_attempt() -> None:
    scope = ScopeKey("synthetic", None, "courses")
    old = _snapshot(
        scope,
        attempted_at=_instant(1),
        complete_at=_instant(1),
        configured_max_age=timedelta(minutes=30),
    )
    stale_allowed = evaluate_freshness(old, FreshnessRequirement.allow_stale(), now=_instant(3))
    cache_only = evaluate_freshness(old, FreshnessRequirement.cache_only(), now=_instant(3))

    assert stale_allowed.status is cache_only.status is FreshnessStatus.STALE
    assert stale_allowed.satisfied and cache_only.satisfied
    assert not stale_allowed.should_refresh and not cache_only.should_refresh

    failed = _snapshot(
        scope,
        attempted_at=_instant(2),
        complete_at=_instant(1),
        coverage=Coverage.PARTIAL,
        outcome=SyncAttemptOutcome.FAILED,
        configured_max_age=timedelta(hours=4),
    )
    decision = evaluate_freshness(
        failed, FreshnessRequirement.with_max_age(timedelta(hours=4)), now=_instant(3)
    )

    assert decision.status is FreshnessStatus.STALE
    assert decision.latest_coverage is Coverage.PARTIAL
    assert decision.last_attempt_outcome is SyncAttemptOutcome.FAILED
    assert FreshnessWarning.LATEST_ATTEMPT_INCOMPLETE in decision.warning_codes
    assert not decision.satisfied and decision.should_refresh


def test_same_timestamp_higher_failed_run_stays_visible_for_all_policies(
    state_store: tuple[Database, SyncStateRepository, CourseId],
) -> None:
    database, repository, course_id = state_store
    scope = ScopeKey("synthetic", course_id, "announcements")
    for run_key in (1, 2):
        _add_run(database, run_key, _instant(1))
    repository.record_attempt(
        scope,
        run_key=1,
        attempted_at=_instant(1),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
        configured_max_age=timedelta(hours=4),
    )
    state = repository.record_attempt(
        scope,
        run_key=2,
        attempted_at=_instant(1),
        outcome=SyncAttemptOutcome.FAILED,
        coverage=Coverage.PARTIAL,
        warning_codes=(SyncWarning.SOURCE_FAILURE,),
    )

    cache = evaluate_freshness(state, FreshnessRequirement.cache_only(), now=_instant(2))
    allowed = evaluate_freshness(state, FreshnessRequirement.allow_stale(), now=_instant(2))
    bounded = evaluate_freshness(
        state,
        FreshnessRequirement.with_max_age(timedelta(hours=4)),
        now=_instant(2),
    )

    assert state.latest_attempt_run_key == 2
    assert state.latest_complete_run_key == 1
    assert cache.status is allowed.status is bounded.status is FreshnessStatus.STALE
    assert cache.satisfied and allowed.satisfied
    assert not cache.should_refresh and not allowed.should_refresh
    assert not bounded.satisfied and bounded.should_refresh
    for decision in (cache, allowed, bounded):
        assert decision.latest_coverage is Coverage.PARTIAL
        assert decision.last_attempt_outcome is SyncAttemptOutcome.FAILED
        assert FreshnessWarning.LATEST_ATTEMPT_INCOMPLETE in decision.warning_codes


@pytest.mark.parametrize(
    "requirement,historical",
    [
        (FreshnessRequirement.cache_only(), False),
        (FreshnessRequirement.with_max_age(timedelta(seconds=1)), True),
    ],
)
def test_cache_only_and_historical_reads_never_refresh(
    requirement: FreshnessRequirement, historical: bool
) -> None:
    scope = ScopeKey("synthetic", None, "courses")
    refresh_calls = 0
    local_reads = 0

    def load_local() -> str:
        nonlocal local_reads
        local_reads += 1
        return "synthetic cached result"

    def refresh(_: ScopeKey) -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    result = retrieve_with_freshness(
        scope,
        requirement,
        load_local=load_local,
        load_state=lambda _: None,
        refresh=refresh,
        now=_instant(3),
        historical=historical,
    )

    assert result.value == "synthetic cached result"
    assert refresh_calls == 0
    assert local_reads == result.local_reads == 1


def test_targeted_refresh_runs_once_and_retries_local_once(
    state_store: tuple[Database, SyncStateRepository, CourseId],
) -> None:
    database, repository, course_id = state_store
    scope = ScopeKey("synthetic", course_id, "announcements")
    _add_run(database, 1, _instant(1))
    _add_run(database, 2, _instant(3))
    repository.record_attempt(
        scope,
        run_key=1,
        attempted_at=_instant(1),
        outcome=SyncAttemptOutcome.SUCCEEDED,
        coverage=Coverage.COMPLETE,
    )
    values = iter(("cached", "refreshed"))
    refresh_calls = 0

    def refresh(target: ScopeKey) -> None:
        nonlocal refresh_calls
        refresh_calls += 1
        repository.record_attempt(
            target,
            run_key=2,
            attempted_at=_instant(3),
            outcome=SyncAttemptOutcome.SUCCEEDED,
            coverage=Coverage.COMPLETE,
        )

    result = retrieve_with_freshness(
        scope,
        FreshnessRequirement.require_current(),
        load_local=lambda: next(values),
        load_state=repository.get,
        refresh=refresh,
        now=_instant(3),
    )

    assert result.value == "refreshed"
    assert result.refresh_attempted and result.local_reads == 2
    assert refresh_calls == 1
    assert result.decision.satisfied
    assert result.decision.status is FreshnessStatus.CURRENT


def test_refresh_failure_returns_only_a_safe_warning_without_retrying_local() -> None:
    scope = ScopeKey("synthetic", None, "courses")
    calls = 0

    def load_local() -> str:
        nonlocal calls
        calls += 1
        return "cached"

    def fail_refresh(_: ScopeKey) -> None:
        raise RuntimeError("private-source-payload")

    result = retrieve_with_freshness(
        scope,
        FreshnessRequirement.with_max_age(timedelta(seconds=1)),
        load_local=load_local,
        load_state=lambda _: None,
        refresh=fail_refresh,
        now=_instant(3),
    )

    assert calls == result.local_reads == 1
    assert result.refresh_attempted
    assert result.decision.warning_codes[-1] is FreshnessWarning.TARGETED_REFRESH_FAILED
    assert "private-source-payload" not in repr(result)


def test_successful_callback_does_not_loop_when_freshness_remains_unsatisfied() -> None:
    scope = ScopeKey("synthetic", None, "courses")
    refresh_calls = 0
    local_reads = 0

    def load_local() -> str:
        nonlocal local_reads
        local_reads += 1
        return "cached"

    def refresh(_: ScopeKey) -> None:
        nonlocal refresh_calls
        refresh_calls += 1

    result = retrieve_with_freshness(
        scope,
        FreshnessRequirement.require_current(),
        load_local=load_local,
        load_state=lambda _: None,
        refresh=refresh,
        now=_instant(3),
    )

    assert refresh_calls == 1
    assert local_reads == result.local_reads == 2
    assert result.refresh_attempted and not result.decision.satisfied


def test_requirement_rejects_invalid_mode_duration_combinations() -> None:
    with pytest.raises(ValueError, match="positive"):
        FreshnessRequirement(FreshnessMode.MAX_AGE, timedelta())
    with pytest.raises(ValueError, match="only MAX_AGE"):
        FreshnessRequirement(FreshnessMode.CACHE_ONLY, timedelta(minutes=1))
