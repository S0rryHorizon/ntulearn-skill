from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

import pytest

from ntulearn_skill.core import ContentId, CourseId
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.search import (
    SearchEntityKind,
    SearchFilters,
    SearchQuery,
    SearchService,
    SourceReferenceKind,
)
from ntulearn_skill.storage import Database, DomainRepository

_NOW = "2030-01-01T00:00:00+00:00"
_HASH = "1" * 64


@dataclass(frozen=True)
class _Harness:
    database: Database
    course: CourseId
    course_key: int
    content_key: int
    provider_key: int


def _harness(tmp_path: Path) -> _Harness:
    database = Database(tmp_path / "private-synthetic" / "metadata.sqlite3")
    domain = DomainRepository(database)
    assert domain.initialize() >= 7
    course = CourseId("synthetic", "m7-search-course")
    content = ContentId("synthetic", "m7-search-content")
    domain.put_course(course, code="PH0000", title="Synthetic Search Course")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Synthetic Event Sources",
        position=0,
    )
    connection = database.connect()
    try:
        row = connection.execute(
            """
            SELECT course.course_key, content.content_key, provider.provider_key
            FROM course
            JOIN content_node content ON content.course_key = course.course_key
            JOIN source_object object ON object.source_object_key = course.source_object_key
            JOIN source_provider provider ON provider.provider_key = object.provider_key
            WHERE provider.name = ? AND object.remote_key = ?
            """,
            (course.provider, course.value),
        ).fetchone()
        assert row is not None
        return _Harness(
            database,
            course,
            int(row["course_key"]),
            int(row["content_key"]),
            int(row["provider_key"]),
        )
    finally:
        connection.close()


def _sync_run(connection: sqlite3.Connection, ordinal: int) -> int:
    cursor = connection.execute(
        """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
        VALUES ('synthetic-m7-search', '{}', ?, 'SUCCEEDED')""",
        (f"2030-01-{ordinal:02d}T00:00:00+00:00",),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _source_object(
    connection: sqlite3.Connection, harness: _Harness, remote_key: str, object_kind: str
) -> int:
    cursor = connection.execute(
        """INSERT INTO source_object(
            provider_key, object_kind, remote_key, first_observed_at, last_observed_at
        ) VALUES (?, ?, ?, ?, ?)""",
        (harness.provider_key, object_kind, remote_key, _NOW, _NOW),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _observation_candidate(
    connection: sqlite3.Connection,
    harness: _Harness,
    *,
    ordinal: int,
    remote_key: str,
    title: str,
    due_text: str,
) -> tuple[int, int, int, int, int]:
    run_key = _sync_run(connection, ordinal)
    object_key = _source_object(connection, harness, remote_key, "announcement")
    snapshot = {"title": title, "body": f"{title} due {due_text}."}
    cursor = connection.execute(
        """INSERT INTO source_observation(
            source_object_key, sync_run_key, data_kind, observed_at,
            observation_hash, snapshot_json, raw_wording
        ) VALUES (?, ?, 'announcement', ?, ?, ?, ?)""",
        (
            object_key,
            run_key,
            f"2030-01-{ordinal:02d}T12:00:00+00:00",
            f"{ordinal:x}".rjust(64, "0"),
            json.dumps(snapshot),
            snapshot["body"],
        ),
    )
    assert cursor.lastrowid is not None
    observation_key = int(cursor.lastrowid)
    cursor = connection.execute(
        """INSERT INTO extraction_record(
            input_kind, source_observation_key, extractor_name, extractor_version,
            settings_hash, input_hash, status, extracted_at
        ) VALUES ('source_observation', ?, 'synthetic', '1', ?, ?, 'COMPLETE', ?)""",
        (observation_key, _HASH, f"{ordinal + 10:x}".rjust(64, "0"), _NOW),
    )
    assert cursor.lastrowid is not None
    extraction_key = int(cursor.lastrowid)
    cursor = connection.execute(
        """INSERT INTO event_candidate(
            extraction_record_key, course_key, ordinal, source_kind, raw_wording, confidence
        ) VALUES (?, ?, 0, 'announcement', ?, 1.0)""",
        (extraction_key, harness.course_key, snapshot["body"]),
    )
    assert cursor.lastrowid is not None
    candidate_key = int(cursor.lastrowid)
    fields: list[int] = []
    for field_name, value, original_text in (
        ("title", title, title),
        ("due_time", {"instant": due_text}, due_text),
    ):
        cursor = connection.execute(
            """INSERT INTO event_candidate_field(
                candidate_key, field_name, value_json, original_text, source_path,
                source_observation_key
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                candidate_key,
                field_name,
                json.dumps(value, separators=(",", ":")),
                original_text,
                field_name,
                observation_key,
            ),
        )
        assert cursor.lastrowid is not None
        fields.append(int(cursor.lastrowid))
    return object_key, observation_key, candidate_key, fields[0], fields[1]


def _event(connection: sqlite3.Connection, harness: _Harness, *, title: str, due_text: str) -> int:
    cursor = connection.execute(
        """INSERT INTO event(
            stable_id, course_key, event_type, title, due_time_json, status,
            resolution_state, confidence, source_count, created_at, updated_at
        ) VALUES (?, ?, 'exam', ?, ?, 'SCHEDULED', 'RESOLVED', 1.0, 1, ?, ?)""",
        (
            sha256(title.encode()).hexdigest(),
            harness.course_key,
            title,
            json.dumps({"instant": due_text}),
            _NOW,
            _NOW,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _event_source(
    connection: sqlite3.Connection,
    harness: _Harness,
    *,
    candidate_key: int,
    event_key: int | None,
    evidence_kind: str,
    evidence_key: int,
    source_object_key: int,
) -> int:
    cursor = connection.execute(
        """INSERT INTO event_source(
            candidate_key, course_key, event_key, source_kind, evidence_ref_kind,
            evidence_ref_key, source_object_key, observed_at, change_kind,
            resolution_state, explanation, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'NONE', 'MATCHED', 'synthetic match', ?, ?)""",
        (
            candidate_key,
            harness.course_key,
            event_key,
            "document" if evidence_kind == "source_locator" else "announcement",
            evidence_kind,
            evidence_key,
            source_object_key,
            _NOW if evidence_kind == "source_observation" else None,
            _NOW,
            _NOW,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _claim(
    connection: sqlite3.Connection,
    *,
    event_key: int | None,
    event_source_key: int,
    candidate_field_key: int,
    field_name: str,
    value: object,
    original_text: str,
    decision_state: str = "ACCEPTED",
) -> int:
    cursor = connection.execute(
        """INSERT INTO claim(
            event_key, event_source_key, candidate_field_key, origin, field_name,
            value_json, original_text, confidence, decision_state, decision_reason,
            created_at, updated_at
        ) VALUES (?, ?, ?, 'SOURCE', ?, ?, ?, 1.0, ?, 'synthetic decision', ?, ?)""",
        (
            event_key,
            event_source_key,
            candidate_field_key,
            field_name,
            json.dumps(value, separators=(",", ":")),
            original_text,
            decision_state,
            _NOW,
            _NOW,
        ),
    )
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def test_event_title_and_claims_keep_field_level_observation_evidence(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    old_due = "2030-03-15T10:00:00+00:00"
    new_due = "2030-03-17T10:00:00+00:00"
    title = "Synthetic Midterm"
    with harness.database.transaction() as connection:
        object_key, old_observation, candidate_key, title_field, old_due_field = (
            _observation_candidate(
                connection,
                harness,
                ordinal=1,
                remote_key="old-announcement",
                title=title,
                due_text=old_due,
            )
        )
        event_key = _event(connection, harness, title=title, due_text=old_due)
        source_key = _event_source(
            connection,
            harness,
            candidate_key=candidate_key,
            event_key=event_key,
            evidence_kind="source_observation",
            evidence_key=old_observation,
            source_object_key=object_key,
        )
        title_claim = _claim(
            connection,
            event_key=event_key,
            event_source_key=source_key,
            candidate_field_key=title_field,
            field_name="title",
            value=title,
            original_text=title,
        )
        old_due_claim = _claim(
            connection,
            event_key=event_key,
            event_source_key=source_key,
            candidate_field_key=old_due_field,
            field_name="due_time",
            value={"instant": old_due},
            original_text=old_due,
        )
        connection.executemany(
            """INSERT INTO event_field_projection(
                event_key, field_name, selected_claim_key, value_json, uncertain, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?)""",
            (
                (event_key, "title", title_claim, json.dumps(title), _NOW),
                (
                    event_key,
                    "due_time",
                    old_due_claim,
                    json.dumps({"instant": old_due}, separators=(",", ":")),
                    _NOW,
                ),
            ),
        )

    search = SearchService(harness.database)
    event_hit = search.search(
        SearchQuery(
            title,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.EVENT})),
        )
    ).items[0]
    assert event_hit.source.kind is SourceReferenceKind.SOURCE_OBSERVATION
    assert event_hit.source.key == old_observation
    assert event_hit.matching_text == ""
    assert not search.search(
        SearchQuery(
            old_due,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.EVENT})),
        )
    ).items

    old_hit = search.search(
        SearchQuery(
            old_due,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
        )
    ).items[0]
    old_reference = old_hit.source

    with harness.database.transaction() as connection:
        new_object, new_observation, new_candidate, _new_title_field, new_due_field = (
            _observation_candidate(
                connection,
                harness,
                ordinal=2,
                remote_key="new-announcement",
                title=title,
                due_text=new_due,
            )
        )
        new_source = _event_source(
            connection,
            harness,
            candidate_key=new_candidate,
            event_key=event_key,
            evidence_kind="source_observation",
            evidence_key=new_observation,
            source_object_key=new_object,
        )
        new_due_claim = _claim(
            connection,
            event_key=event_key,
            event_source_key=new_source,
            candidate_field_key=new_due_field,
            field_name="due_time",
            value={"instant": new_due},
            original_text=new_due,
        )
        connection.execute(
            "UPDATE claim SET decision_state = 'SUPERSEDED', updated_at = ? WHERE claim_key = ?",
            (_NOW, old_due_claim),
        )
        connection.execute(
            """UPDATE event_field_projection
            SET selected_claim_key = ?, value_json = ?, updated_at = ?
            WHERE event_key = ? AND field_name = 'due_time'""",
            (
                new_due_claim,
                json.dumps({"instant": new_due}, separators=(",", ":")),
                _NOW,
                event_key,
            ),
        )
        connection.execute(
            """UPDATE event
            SET due_time_json = ?, source_count = 2, updated_at = ? WHERE event_key = ?""",
            (json.dumps({"instant": new_due}), _NOW, event_key),
        )
        connection.execute(
            """INSERT INTO claim(
                event_key, origin, field_name, value_json, original_text, confidence,
                decision_state, decision_reason, created_at, updated_at
            ) VALUES (?, 'LOCAL_DECISION', 'location', ?, 'Manualphantombeacon', 1.0,
                      'ACCEPTED', 'synthetic manual value', ?, ?)""",
            (event_key, json.dumps("Manualphantombeacon"), _NOW, _NOW),
        )

    old_after = search.search(
        SearchQuery(
            old_due,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
        )
    ).items[0]
    assert old_after.source == old_reference
    resolved_old = search.resolve_source(old_reference)
    assert resolved_old.observation is not None
    assert resolved_old.observation["body"] == f"{title} due {old_due}."
    assert (
        search.search(
            SearchQuery(
                new_due,
                SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
            )
        )
        .items[0]
        .source.key
        == new_observation
    )
    assert not search.search(
        SearchQuery(
            "Manualphantombeacon",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
        )
    ).items

    unresolved_due = "2030-06-21T10:00:00+00:00"
    with harness.database.transaction() as connection:
        unresolved_object, unresolved_observation, unresolved_candidate, _, unresolved_field = (
            _observation_candidate(
                connection,
                harness,
                ordinal=3,
                remote_key="unresolved-announcement",
                title="Unresolved Synthetic Quiz",
                due_text=unresolved_due,
            )
        )
        unresolved_source = _event_source(
            connection,
            harness,
            candidate_key=unresolved_candidate,
            event_key=None,
            evidence_kind="source_observation",
            evidence_key=unresolved_observation,
            source_object_key=unresolved_object,
        )
        _claim(
            connection,
            event_key=None,
            event_source_key=unresolved_source,
            candidate_field_key=unresolved_field,
            field_name="due_time",
            value={"instant": unresolved_due},
            original_text=unresolved_due,
            decision_state="UNRESOLVED",
        )
    unresolved_hit = search.search(
        SearchQuery(
            unresolved_due,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
        )
    ).items[0]
    assert unresolved_hit.source.key == unresolved_observation


def test_locator_claim_resolves_exact_version_and_fts_rolls_back_with_event(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    title = "Locator Backed Quiz"
    with harness.database.transaction() as connection:
        object_key = _source_object(connection, harness, "locator-document", "attachment")
        cursor = connection.execute(
            """INSERT INTO resource(
                source_object_key, content_key, display_title, availability, browse_dir_name,
                first_observed_at, last_observed_at
            ) VALUES (?, ?, 'Synthetic locator document', 'ACTIVE', 'locator-document', ?, ?)""",
            (object_key, harness.content_key, _NOW, _NOW),
        )
        assert cursor.lastrowid is not None
        resource_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO resource_version(
                resource_key, version_number, sha256, byte_size, file_format, downloaded_at,
                blob_relpath, browse_relpath, verification_status
            ) VALUES (?, 1, ?, 1, 'pdf', ?, 'blobs/synthetic.pdf',
                      'courses/synthetic.pdf', 'VERIFIED')""",
            (resource_key, _HASH, _NOW),
        )
        assert cursor.lastrowid is not None
        version_key = int(cursor.lastrowid)
        connection.execute(
            "UPDATE resource SET current_version_key = ? WHERE resource_key = ?",
            (version_key, resource_key),
        )
        cursor = connection.execute(
            """INSERT INTO parsed_document(
                version_key, resource_sha256, parser_name, parser_version, engine_version,
                settings_hash, settings_json, status, coverage, parsed_at
            ) VALUES (?, ?, 'synthetic', '1', '1', ?, '{}', 'COMPLETE', 'COMPLETE', ?)""",
            (version_key, _HASH, _HASH, _NOW),
        )
        assert cursor.lastrowid is not None
        parse_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO document_chunk(
                parse_key, ordinal, kind, native_text, locator_json
            ) VALUES (?, 0, 'page', ?, '{"physical_page_index":0}')""",
            (parse_key, f"{title} starts at 09:00."),
        )
        assert cursor.lastrowid is not None
        chunk_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO source_locator(
                version_key, chunk_key, format, physical_page_index, structured_json
            ) VALUES (?, ?, 'pdf', 0, '{"physical_page_index":0}')""",
            (version_key, chunk_key),
        )
        assert cursor.lastrowid is not None
        locator_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO extraction_record(
                input_kind, version_key, parse_key, extractor_name, extractor_version,
                settings_hash, input_hash, status, extracted_at
            ) VALUES ('resource_version', ?, ?, 'synthetic', '1', ?, ?, 'COMPLETE', ?)""",
            (version_key, parse_key, _HASH, "2" * 64, _NOW),
        )
        assert cursor.lastrowid is not None
        extraction_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO event_candidate(
                extraction_record_key, course_key, ordinal, source_kind, raw_wording, confidence
            ) VALUES (?, ?, 0, 'document', ?, 1.0)""",
            (extraction_key, harness.course_key, title),
        )
        assert cursor.lastrowid is not None
        candidate_key = int(cursor.lastrowid)
        cursor = connection.execute(
            """INSERT INTO event_candidate_field(
                candidate_key, field_name, value_json, original_text, source_path, locator_key
            ) VALUES (?, 'title', ?, ?, 'page[0]', ?)""",
            (candidate_key, json.dumps(title), title, locator_key),
        )
        assert cursor.lastrowid is not None
        title_field = int(cursor.lastrowid)
        event_key = _event(connection, harness, title=title, due_text="2030-04-01")
        source_key = _event_source(
            connection,
            harness,
            candidate_key=candidate_key,
            event_key=event_key,
            evidence_kind="source_locator",
            evidence_key=locator_key,
            source_object_key=object_key,
        )
        title_claim = _claim(
            connection,
            event_key=event_key,
            event_source_key=source_key,
            candidate_field_key=title_field,
            field_name="title",
            value=title,
            original_text=title,
        )
        connection.execute(
            """INSERT INTO event_field_projection(
                event_key, field_name, selected_claim_key, value_json, uncertain, updated_at
            ) VALUES (?, 'title', ?, ?, 0, ?)""",
            (event_key, title_claim, json.dumps(title), _NOW),
        )

    search = SearchService(harness.database)
    claim_hit = search.search(
        SearchQuery(
            title,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CLAIM})),
        )
    ).items[0]
    assert claim_hit.source.kind is SourceReferenceKind.SOURCE_LOCATOR
    assert claim_hit.version_key == version_key
    assert claim_hit.locator == {"physical_page_index": 0}
    resolved = search.resolve_source(claim_hit.source, context_window=0)
    assert resolved.version_key == version_key
    assert tuple(chunk.key for chunk in resolved.chunks) == (chunk_key,)

    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with harness.database.transaction() as connection:
            rollback_title = "Rolledbackeventbeacon"
            rollback_object, rollback_observation, rollback_candidate, rollback_field, _ = (
                _observation_candidate(
                    connection,
                    harness,
                    ordinal=3,
                    remote_key="rollback-announcement",
                    title=rollback_title,
                    due_text="2030-05-01T10:00:00+00:00",
                )
            )
            rollback_event = _event(
                connection,
                harness,
                title=rollback_title,
                due_text="2030-05-01T10:00:00+00:00",
            )
            rollback_source = _event_source(
                connection,
                harness,
                candidate_key=rollback_candidate,
                event_key=rollback_event,
                evidence_kind="source_observation",
                evidence_key=rollback_observation,
                source_object_key=rollback_object,
            )
            rollback_claim = _claim(
                connection,
                event_key=rollback_event,
                event_source_key=rollback_source,
                candidate_field_key=rollback_field,
                field_name="title",
                value=rollback_title,
                original_text=rollback_title,
            )
            connection.execute(
                """INSERT INTO event_field_projection(
                    event_key, field_name, selected_claim_key, value_json, uncertain, updated_at
                ) VALUES (?, 'title', ?, ?, 0, ?)""",
                (rollback_event, rollback_claim, json.dumps(rollback_title), _NOW),
            )
            SearchIndex.refresh_dirty(connection)
            count = connection.execute(
                """SELECT count(*) FROM search_document_fts
                WHERE search_document_fts MATCH 'Rolledbackeventbeacon'"""
            ).fetchone()
            assert count is not None and int(count[0]) == 2
            raise RuntimeError("synthetic rollback")

    assert not search.search(
        SearchQuery(
            "Rolledbackeventbeacon",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.EVENT})),
        )
    ).items
    assert search.search(
        SearchQuery(
            title,
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.EVENT})),
        )
    ).items
