from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from test_reconciliation_adversarial import _announce, _Harness, _harness

from ntulearn_skill.core import Coverage
from ntulearn_skill.core.api import CoreService, CourseRef, EventFilter
from ntulearn_skill.events import ClaimDecisionState, EventReconciler


def _new_terminal_extraction(
    harness: _Harness,
    observation_key: int,
    *,
    status: str,
    version: str,
    extractor_name: str | None = None,
) -> int:
    database = harness.database
    with database.transaction() as connection:
        previous = connection.execute(
            """SELECT * FROM extraction_record
            WHERE input_kind = 'source_observation' AND source_observation_key = ?
            ORDER BY extraction_record_key DESC LIMIT 1""",
            (observation_key,),
        ).fetchone()
        assert previous is not None
        cursor = connection.execute(
            """INSERT INTO extraction_record(
                input_kind, source_observation_key, extractor_name, extractor_version,
                settings_hash, input_hash, status, warning_codes_json, extracted_at
            ) VALUES ('source_observation', ?, ?, ?, ?, ?, ?, '[]', ?)""",
            (
                observation_key,
                extractor_name or str(previous["extractor_name"]),
                version,
                str(previous["settings_hash"]),
                str(previous["input_hash"]),
                status,
                datetime(2031, 1, 1, tzinfo=UTC).isoformat(),
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def test_new_successful_zero_candidate_extraction_retires_old_interpretation(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    initial_empty = harness.reconciler.reconcile_course(harness.first_course)
    assert initial_empty.events == ()
    observation_key = _announce(
        harness,
        "obsolete-false-positive",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
    )
    first = harness.reconciler.reconcile_course(harness.first_course)
    assert len(first.events) == 1
    old_event_key = first.events[0].key
    with harness.database.connect() as connection:
        historical_counts = tuple(
            int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in ("event_candidate", "event_source", "claim")
        )

    _new_terminal_extraction(
        harness,
        observation_key,
        status="COMPLETE",
        version="synthetic-fixed-zero-candidate",
    )
    result = harness.reconciler.reconcile_course(harness.first_course)

    assert not result.cache_hit
    assert result.events == ()
    assert result.unresolved_sources == ()
    assert EventReconciler(harness.database).get_event(old_event_key) is None
    historical_sources = EventReconciler(harness.database).list_event_sources()
    historical_claims = EventReconciler(harness.database).list_claims(old_event_key)
    assert historical_sources
    assert historical_claims
    assert {claim.decision_state for claim in historical_claims} == {ClaimDecisionState.REJECTED}
    assert {claim.decision_reason for claim in historical_claims} == {
        "superseded deterministic extraction"
    }
    with harness.database.connect() as connection:
        assert (
            tuple(
                int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in ("event_candidate", "event_source", "claim")
            )
            == historical_counts
        )
        assert (
            connection.execute(
                """SELECT COUNT(*) FROM search_document
                WHERE entity_kind IN ('event', 'claim')"""
            ).fetchone()[0]
            == 0
        )


def test_failed_or_different_extractor_does_not_retire_last_success(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    observation_key = _announce(
        harness,
        "retained-success",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
    )
    _new_terminal_extraction(
        harness,
        observation_key,
        status="FAILED",
        version="synthetic-failed-newer",
    )
    _new_terminal_extraction(
        harness,
        observation_key,
        status="COMPLETE",
        version="synthetic-complementary",
        extractor_name="independent-synthetic-extractor",
    )

    result = harness.reconciler.reconcile_course(harness.first_course)

    assert len(result.events) == 1
    assert result.events[0].title == "Quiz 1"


def test_new_partial_extraction_is_active_and_keeps_partial_coverage(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    observation_key = _announce(
        harness,
        "partial-current",
        "Quiz 1 due 2030-02-10 10:00 UTC.",
        ordinal=1,
    )
    with harness.database.connect() as connection:
        old_extraction = int(
            connection.execute(
                """SELECT extraction_record_key FROM extraction_record
                WHERE source_observation_key = ?""",
                (observation_key,),
            ).fetchone()[0]
        )
    partial_extraction = _new_terminal_extraction(
        harness,
        observation_key,
        status="PARTIAL",
        version="synthetic-partial-newer",
    )
    with harness.database.transaction() as connection:
        candidate = connection.execute(
            """INSERT INTO event_candidate(
                extraction_record_key, course_key, ordinal, source_kind,
                raw_wording, confidence
            ) SELECT ?, course_key, ordinal, source_kind, raw_wording, confidence
              FROM event_candidate WHERE extraction_record_key = ?
              RETURNING candidate_key""",
            (partial_extraction, old_extraction),
        ).fetchone()
        assert candidate is not None
        connection.execute(
            """INSERT INTO event_candidate_field(
                candidate_key, field_name, value_json, original_text, source_path,
                temporal_precision, source_timezone, source_observation_key, locator_key
            ) SELECT ?, field_name, value_json, original_text, source_path,
                temporal_precision, source_timezone, source_observation_key, locator_key
              FROM event_candidate_field
              WHERE candidate_key = (
                SELECT candidate_key FROM event_candidate WHERE extraction_record_key = ?
              )""",
            (int(candidate["candidate_key"]), old_extraction),
        )

    reconciled = EventReconciler(
        harness.database, resolver_version="active-partial-v2"
    ).reconcile_course(harness.first_course)
    queried = CoreService(harness.database).get_events(
        EventFilter(course=CourseRef(remote_id=harness.first_course))
    )

    assert len(reconciled.events) == 1
    assert any(
        item.data_kind == "event_derivation" and item.coverage is Coverage.PARTIAL
        for item in queried.coverage
    )
