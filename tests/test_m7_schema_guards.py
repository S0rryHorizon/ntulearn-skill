"""Synthetic SQL regressions for M7 evidence and audit integrity."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    migrations = Path(__file__).parents[1] / "src/ntulearn_skill/storage/migrations"
    for migration in sorted(migrations.glob("*.sql")):
        connection.executescript(migration.read_text())
    connection.execute("INSERT INTO source_provider VALUES(1,'synthetic','{}','t','t')")
    for key in (1, 2):
        connection.execute(
            "INSERT INTO source_object VALUES(?,1,'course',?,'t','t')",
            (key, f"synthetic-course-{key}"),
        )
        connection.execute(
            """INSERT INTO course(course_key,source_object_key,code,title,availability,
            first_observed_at,last_observed_at) VALUES(?,?,?,'Synthetic','ACTIVE','t','t')""",
            (key, key, f"PH000{key}"),
        )
    for key, course in ((1, 1), (2, 1), (3, 2)):
        connection.execute(
            """INSERT INTO event(event_key,stable_id,course_key,resolution_state,confidence,
            source_count,created_at,updated_at) VALUES(?,?,?,'RESOLVED',1,0,'t','t')""",
            (key, str(key) * 64, course),
        )
    for key, event, field in (
        (1, 1, "title"),
        (2, 1, "title"),
        (3, 3, "title"),
        (4, 1, "location"),
        (5, 1, "title"),
    ):
        connection.execute(
            """INSERT INTO claim(claim_key,event_key,origin,field_name,value_json,original_text,
            confidence,decision_state,decision_reason,created_at,updated_at)
            VALUES(?,?,'LOCAL_DECISION',?,?,'synthetic',1,'ACCEPTED','synthetic','t','t')""",
            (key, event, field, f'"value-{key}"'),
        )
    connection.execute(
        "INSERT INTO source_object VALUES(3,1,'announcement','synthetic-announcement','t','t')"
    )
    connection.execute(
        "INSERT INTO sync_run(sync_run_key,mode,started_at,status) "
        "VALUES(1,'synthetic','t','RUNNING')"
    )
    connection.execute(
        "INSERT INTO source_observation VALUES(1,3,1,'announcement','t',?,'{}','synthetic')",
        ("a" * 64,),
    )
    connection.execute(
        """INSERT INTO extraction_record(extraction_record_key,input_kind,source_observation_key,
        extractor_name,extractor_version,settings_hash,input_hash,status,extracted_at)
        VALUES(1,'source_observation',1,'synthetic','v1',?,?,'COMPLETE','t')""",
        ("b" * 64, "c" * 64),
    )
    connection.execute(
        """INSERT INTO event_candidate(candidate_key,extraction_record_key,course_key,ordinal,
        source_kind,raw_wording,confidence) VALUES(1,1,1,0,'announcement','synthetic',1)"""
    )
    connection.execute(
        """INSERT INTO event_candidate_field(candidate_field_key,candidate_key,field_name,
        value_json,original_text,source_path,source_observation_key)
        VALUES(1,1,'title','"Synthetic"','Synthetic','title',1)"""
    )
    connection.execute(
        """INSERT INTO event_source(event_source_key,candidate_key,course_key,event_key,
        source_kind,evidence_ref_kind,evidence_ref_key,source_object_key,observed_at,change_kind,
        resolution_state,explanation,created_at,updated_at)
        VALUES(1,1,1,1,'announcement','source_observation',1,3,'t','NONE','MATCHED','test','t','t')"""
    )
    connection.execute(
        """INSERT INTO claim(claim_key,event_key,event_source_key,candidate_field_key,origin,
        field_name,value_json,original_text,confidence,decision_state,decision_reason,
        created_at,updated_at) VALUES(6,1,1,1,'SOURCE','title','"Synthetic"','Synthetic',1,
        'ACCEPTED','synthetic','t','t')"""
    )
    connection.execute(
        "INSERT INTO reconciliation_run VALUES(1,1,'synthetic','v1',?,'COMPLETE','t','t')",
        ("d" * 64,),
    )
    connection.execute(
        """INSERT INTO reconciliation_decision VALUES(1,1,1,1,'CREATED','[1]','[]','[]',1,
        'synthetic','t')"""
    )
    connection.execute(
        "INSERT INTO manual_field_resolution VALUES(1,1,'title',1,NULL,'synthetic','t',1)"
    )
    connection.execute("INSERT INTO manual_identity_resolution VALUES(1,1,1,'synthetic','t',1)")
    connection.execute(
        "INSERT INTO event_conflict VALUES(1,1,'title','OPEN','synthetic',NULL,'t',NULL)"
    )
    connection.commit()
    yield connection
    assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    connection.close()


def test_supersession_dag_rejects_direct_and_transitive_cycles(db: sqlite3.Connection) -> None:
    db.execute("INSERT INTO claim_supersession VALUES(1,2,'synthetic','t')")
    db.execute("INSERT INTO claim_supersession VALUES(2,5,'synthetic','t')")
    for successor, predecessor in ((1, 1), (2, 1), (5, 1)):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO claim_supersession VALUES(?,?,'synthetic','t')",
                (successor, predecessor),
            )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(
            "UPDATE claim_supersession SET successor_claim_key=5 WHERE successor_claim_key=1"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute("DELETE FROM claim_supersession")


@pytest.mark.parametrize("claim_key", [3, 4])
def test_conflict_alternatives_reject_wrong_event_or_field(
    db: sqlite3.Connection, claim_key: int
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="conflict alternative claim mismatch"):
        db.execute("INSERT INTO event_conflict_alternative VALUES(1,?)", (claim_key,))
    db.execute("INSERT INTO event_conflict_alternative VALUES(1,1)")
    with pytest.raises(sqlite3.IntegrityError, match="conflict alternative claim mismatch"):
        db.execute("UPDATE event_conflict_alternative SET claim_key=?", (claim_key,))


@pytest.mark.parametrize("claim_key", [1, 6])
@pytest.mark.parametrize(
    "assignment",
    [
        "value_json='\"rewritten\"'",
        "original_text='rewritten'",
        "source_timezone='UTC'",
        "temporal_precision='DATE_ONLY'",
        "confidence=0.1",
        "field_name='location'",
        "created_at='rewritten'",
    ],
)
def test_claim_assertions_are_immutable(
    db: sqlite3.Connection, claim_key: int, assignment: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"UPDATE claim SET {assignment} WHERE claim_key=?", (claim_key,))


@pytest.mark.parametrize(
    "assignment",
    [
        "course_key=2",
        "source_kind='schedule'",
        "evidence_ref_key=2",
        "source_object_key=1",
        "observed_at='rewritten'",
        "source_timestamp='rewritten'",
        "source_timestamp_semantics='published_at'",
        "created_at='rewritten'",
    ],
)
def test_source_evidence_is_immutable(db: sqlite3.Connection, assignment: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"UPDATE event_source SET {assignment} WHERE event_source_key=1")


@pytest.mark.parametrize(
    "table,assignment",
    [
        ("reconciliation_decision", "explanation='rewritten'"),
        ("reconciliation_decision", "event_key=3"),
        ("manual_field_resolution", "reason='rewritten'"),
        ("manual_field_resolution", "selected_claim_key=3"),
        ("manual_identity_resolution", "reason='rewritten'"),
        ("manual_identity_resolution", "event_key=3"),
    ],
)
def test_decision_history_is_immutable(db: sqlite3.Connection, table: str, assignment: str) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"UPDATE {table} SET {assignment}")


@pytest.mark.parametrize(
    "table",
    [
        "claim",
        "event_source",
        "reconciliation_decision",
        "manual_field_resolution",
        "manual_identity_resolution",
    ],
)
def test_evidence_and_decision_history_cannot_be_deleted(
    db: sqlite3.Connection, table: str
) -> None:
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        db.execute(f"DELETE FROM {table}")


def test_valid_mutable_projection_and_manual_history_transitions(db: sqlite3.Connection) -> None:
    db.execute("UPDATE manual_field_resolution SET active=0 WHERE manual_resolution_key=1")
    db.execute(
        "INSERT INTO manual_field_resolution VALUES(2,1,'title',2,NULL,'new decision','u',1)"
    )
    db.execute("UPDATE manual_identity_resolution SET active=0")
    db.execute("INSERT INTO manual_identity_resolution VALUES(2,1,2,'new identity','u',1)")
    db.execute(
        "UPDATE event_source SET event_key=2, resolution_state='MATCHED', explanation='new', "
        "change_kind='MOVE', updated_at='u' WHERE event_source_key=1"
    )
    db.execute(
        "UPDATE claim SET event_key=2,decision_state='CONFLICTING',decision_reason='new',"
        "updated_at='u' WHERE claim_key=6"
    )
    db.execute("INSERT INTO event_field_projection VALUES(1,'title',1,'\"value-1\"',0,'t')")
    db.execute(
        "UPDATE event_field_projection SET selected_claim_key=2,value_json='\"value-2\"',"
        "updated_at='u' WHERE event_key=1"
    )
    db.execute(
        "UPDATE claim SET decision_state='SUPERSEDED',decision_reason='new' WHERE claim_key=1"
    )
    db.execute("UPDATE event SET title='value-2',updated_at='u' WHERE event_key=1")
    assert db.execute(
        "SELECT reason,active FROM manual_field_resolution ORDER BY 1"
    ).fetchall() == [("new decision", 1), ("synthetic", 0)]
