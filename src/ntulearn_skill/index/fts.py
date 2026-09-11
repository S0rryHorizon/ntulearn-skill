"""Disposable, transactionally invalidated SQLite FTS5 index."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ntulearn_skill.events._activity import active_claim_sql
from ntulearn_skill.storage.database import Database, StorageError


class SearchIndexError(StorageError):
    """Privacy-safe full-text index failure."""


@dataclass(frozen=True, slots=True)
class IndexRebuildResult:
    document_count: int
    fts_count: int
    source_generation: int


_SEMANTIC_TYPE_SQL = """
COALESCE(
    (
        SELECT mc.semantic_type
        FROM material_classification_selection selection
        JOIN material_classification mc
          ON mc.classification_key = selection.classification_key
        JOIN classification_run cr
          ON cr.classification_run_key = mc.classification_run_key
        WHERE selection.resource_key = r.resource_key
          AND (cr.version_key IS NULL OR cr.version_key IS v.version_key)
        ORDER BY selection.selection_key DESC
        LIMIT 1
    ),
    (
        SELECT mc.semantic_type
        FROM classification_run cr
        JOIN material_classification mc
          ON mc.classification_run_key = cr.classification_run_key
        WHERE cr.resource_key = r.resource_key
          AND (cr.version_key IS NULL OR cr.version_key IS v.version_key)
          AND cr.status <> 'FAILED'
        ORDER BY cr.created_at DESC, cr.classification_run_key DESC, mc.rank
        LIMIT 1
    ),
    ''
)
"""

_SEMANTIC_CONFIDENCE_SQL = """
COALESCE(
    (
        SELECT mc.confidence
        FROM material_classification_selection selection
        JOIN material_classification mc
          ON mc.classification_key = selection.classification_key
        JOIN classification_run cr
          ON cr.classification_run_key = mc.classification_run_key
        WHERE selection.resource_key = r.resource_key
          AND (cr.version_key IS NULL OR cr.version_key IS v.version_key)
        ORDER BY selection.selection_key DESC
        LIMIT 1
    ),
    (
        SELECT mc.confidence
        FROM classification_run cr
        JOIN material_classification mc
          ON mc.classification_run_key = cr.classification_run_key
        WHERE cr.resource_key = r.resource_key
          AND (cr.version_key IS NULL OR cr.version_key IS v.version_key)
          AND cr.status <> 'FAILED'
        ORDER BY cr.created_at DESC, cr.classification_run_key DESC, mc.rank
        LIMIT 1
    )
)
"""

_FILENAME_SQL = """
COALESCE(
    (
        SELECT observation.original_filename
        FROM resource_observation observation
        WHERE observation.resource_key = r.resource_key
          AND observation.version_key = v.version_key
        ORDER BY observation.observed_at DESC, observation.observation_key DESC
        LIMIT 1
    ),
    (
        SELECT observation.original_filename
        FROM resource_observation observation
        WHERE observation.resource_key = r.resource_key
          AND (v.version_key IS NULL OR observation.version_key IS NULL)
        ORDER BY observation.observed_at DESC, observation.observation_key DESC
        LIMIT 1
    ),
    ''
)
"""

_CURRENT_USABLE_PARSE_SQL = """
parsed.status IN ('COMPLETE', 'PARTIAL')
AND NOT EXISTS (
    SELECT 1
    FROM parsed_document newer_parse
    WHERE newer_parse.version_key = parsed.version_key
      AND newer_parse.status IN ('COMPLETE', 'PARTIAL')
      AND (
          newer_parse.parsed_at > parsed.parsed_at
          OR (
              newer_parse.parsed_at = parsed.parsed_at
              AND newer_parse.parse_key > parsed.parse_key
          )
      )
)
"""


class SearchIndex:
    """Build search documents only from canonical relational rows."""

    version = "fts5-2"

    def __init__(self, database: Database) -> None:
        self.database = database

    def rebuild(self) -> IndexRebuildResult:
        try:
            with self.database.transaction() as connection:
                return self.rebuild_in_transaction(connection)
        except sqlite3.Error:
            raise SearchIndexError("full-text index rebuild failed") from None

    def ensure_current(self, connection: sqlite3.Connection) -> IndexRebuildResult | None:
        state = connection.execute(
            """SELECT source_generation, indexed_generation
            FROM search_index_state WHERE singleton_key = 1"""
        ).fetchone()
        if state is None:
            raise SearchIndexError("full-text index state is unavailable")
        if int(state["source_generation"]) == int(state["indexed_generation"]):
            return None
        return self.refresh_dirty(connection)

    def is_current(self) -> bool:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """SELECT source_generation = indexed_generation
                FROM search_index_state WHERE singleton_key = 1"""
            ).fetchone()
            return row is not None and bool(row[0])
        except sqlite3.Error:
            raise SearchIndexError("full-text index state lookup failed") from None
        finally:
            connection.close()

    def rebuild_in_transaction(self, connection: sqlite3.Connection) -> IndexRebuildResult:
        """Replace derived rows atomically inside the caller's write transaction."""

        return self._rebuild_documents(connection)

    @classmethod
    def refresh_dirty(cls, connection: sqlite3.Connection) -> IndexRebuildResult:
        """Refresh complete projections for courses touched by dirty resources."""

        state = connection.execute(
            """SELECT source_generation, indexed_generation
            FROM search_index_state WHERE singleton_key = 1"""
        ).fetchone()
        if state is None:
            raise SearchIndexError("full-text index state is unavailable")
        generation = int(state["source_generation"])
        if int(state["indexed_generation"]) < 0:
            # Migration 0005 marks an existing database globally dirty.  A write
            # before the first search may also queue a narrow dirty subset; that
            # subset must not hide canonical rows created by older migrations.
            return cls._rebuild_documents(connection)
        dirty_courses = {
            int(row[0])
            for row in connection.execute("SELECT course_key FROM search_dirty_course ORDER BY 1")
        }
        dirty_resources = tuple(
            int(row[0])
            for row in connection.execute(
                "SELECT resource_key FROM search_dirty_resource ORDER BY 1"
            )
        )
        if not dirty_courses and not dirty_resources:
            # Migration 0005 marks an existing database globally dirty because earlier
            # migrations could already contain canonical rows.
            return cls._rebuild_documents(connection)

        # Event and claim documents can carry a resource key through their source locator.
        # Replacing only material/chunk rows for a dirty resource would therefore delete those
        # projections without recreating them.  Include both the resource's current owner and any
        # course represented by its existing derived rows so a move or deletion also clears the
        # old course projection coherently.
        affected_courses = set(dirty_courses)
        for resource_key in dirty_resources:
            affected_courses.update(
                int(row[0])
                for row in connection.execute(
                    "SELECT DISTINCT course_key FROM search_document WHERE resource_key = ?",
                    (resource_key,),
                )
            )
            row = connection.execute(
                """SELECT n.course_key FROM resource r
                JOIN content_node n ON n.content_key = r.content_key
                WHERE r.resource_key = ?""",
                (resource_key,),
            ).fetchone()
            if row is not None:
                affected_courses.add(int(row["course_key"]))

        for course_key in sorted(affected_courses):
            connection.execute("DELETE FROM search_document WHERE course_key = ?", (course_key,))
            cls._insert_courses(connection, course_key=course_key)
            cls._insert_content(connection, course_key=course_key)
            cls._insert_announcements(connection, course_key=course_key)
            cls._insert_assessments(connection, course_key=course_key)
            cls._insert_events(connection, course_key=course_key)
            cls._insert_claims(connection, course_key=course_key)
            cls._insert_materials(connection, course_key=course_key)
            cls._insert_native_chunks(connection, course_key=course_key)
            cls._insert_derived_chunks(connection, course_key=course_key)

        connection.execute("DELETE FROM search_dirty_course")
        connection.execute("DELETE FROM search_dirty_resource")
        connection.execute(
            "UPDATE search_index_state SET indexed_generation = ? WHERE singleton_key = 1",
            (generation,),
        )
        return cls._result(connection, generation)

    @staticmethod
    def _rebuild_documents(connection: sqlite3.Connection) -> IndexRebuildResult:
        # An external-content FTS index can lose all postings while its canonical
        # rows remain.  Repair those postings first so delete triggers can safely
        # replace the relational projection below.
        connection.execute(
            "INSERT INTO search_document_fts(search_document_fts) VALUES ('rebuild')"
        )
        connection.execute("DELETE FROM search_document")
        SearchIndex._insert_courses(connection)
        SearchIndex._insert_content(connection)
        SearchIndex._insert_announcements(connection)
        SearchIndex._insert_assessments(connection)
        SearchIndex._insert_events(connection)
        SearchIndex._insert_claims(connection)
        SearchIndex._insert_materials(connection)
        SearchIndex._insert_native_chunks(connection)
        SearchIndex._insert_derived_chunks(connection)
        state = connection.execute(
            "SELECT source_generation FROM search_index_state WHERE singleton_key = 1"
        ).fetchone()
        if state is None:
            raise SearchIndexError("full-text index state is unavailable")
        generation = int(state["source_generation"])
        connection.execute(
            """UPDATE search_index_state SET indexed_generation = ?
            WHERE singleton_key = 1""",
            (generation,),
        )
        connection.execute("DELETE FROM search_dirty_course")
        connection.execute("DELETE FROM search_dirty_resource")
        return SearchIndex._result(connection, generation)

    @staticmethod
    def _result(connection: sqlite3.Connection, generation: int) -> IndexRebuildResult:
        document_count = int(
            connection.execute("SELECT count(*) FROM search_document").fetchone()[0]
        )
        fts_count = int(
            connection.execute("SELECT count(*) FROM search_document_fts").fetchone()[0]
        )
        if document_count != fts_count:
            raise SearchIndexError("full-text index verification failed")
        connection.execute(
            """INSERT INTO search_document_fts(search_document_fts, rank)
            VALUES ('integrity-check', 1)"""
        )
        return IndexRebuildResult(document_count, fts_count, generation)

    @staticmethod
    def _insert_courses(connection: sqlite3.Connection, *, course_key: int | None = None) -> None:
        connection.execute(
            """
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, source_ref_kind, source_ref_key,
                course_code, course_title, content_title, title, filename, semantic_type,
                file_format, availability, text_origin, body
            )
            SELECT
                'course', c.course_key, c.course_key, 'source_object', c.source_object_key,
                c.code, c.title, '', c.title, '', '', '', c.availability, 'metadata', ''
            FROM course c
            WHERE (? IS NULL OR c.course_key = ?)
            ORDER BY c.course_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_content(connection: sqlite3.Connection, *, course_key: int | None = None) -> None:
        connection.execute(
            """
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, source_ref_kind, source_ref_key,
                course_code, course_title, content_title, title, filename, semantic_type,
                file_format, availability, text_origin, body
            )
            SELECT
                'content', n.content_key, c.course_key, 'source_object', n.source_object_key,
                c.code, c.title, n.title, n.title, '', '', '', n.availability, 'metadata', ''
            FROM content_node n
            JOIN course c ON c.course_key = n.course_key
            WHERE (? IS NULL OR c.course_key = ?)
            ORDER BY n.content_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_announcements(
        connection: sqlite3.Connection, *, course_key: int | None = None
    ) -> None:
        connection.execute(
            """
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, source_ref_kind, source_ref_key,
                course_code, course_title, content_title, title, filename, semantic_type,
                file_format, availability, text_origin, body
            )
            SELECT
                'announcement', announcement.announcement_key, course.course_key,
                'source_observation', announcement.current_observation_key,
                course.code, course.title, '', announcement.title, '', '', '',
                announcement.availability, 'metadata', announcement.body
            FROM announcement
            JOIN course ON course.course_key = announcement.course_key
            WHERE (? IS NULL OR course.course_key = ?)
            ORDER BY announcement.announcement_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_assessments(
        connection: sqlite3.Connection, *, course_key: int | None = None
    ) -> None:
        connection.execute(
            """
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, source_ref_kind, source_ref_key,
                course_code, course_title, content_title, title, filename, semantic_type,
                file_format, availability, text_origin, body
            )
            SELECT
                'assessment', assessment.assessment_key, course.course_key,
                'source_observation', assessment.current_observation_key,
                course.code, course.title, content.title, assessment.title, '',
                'assessment_information', '', assessment.availability, 'metadata',
                assessment.instructions
            FROM assessment
            JOIN course ON course.course_key = assessment.course_key
            JOIN content_node content ON content.content_key = assessment.content_key
            WHERE (? IS NULL OR course.course_key = ?)
            ORDER BY assessment.assessment_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_events(connection: sqlite3.Connection, *, course_key: int | None = None) -> None:
        """Index only the canonical title with the exact source that supports it."""

        connection.execute(
            """
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, resource_key, version_key,
                source_ref_kind, source_ref_key, course_code, course_title, content_title,
                title, filename, semantic_type, file_format, availability, text_origin, body
            )
            SELECT
                'event', event.event_key, course.course_key,
                version.resource_key, locator.version_key,
                CASE
                    WHEN field.source_observation_key IS NOT NULL THEN 'source_observation'
                    ELSE 'source_locator'
                END,
                COALESCE(field.source_observation_key, field.locator_key),
                course.code, course.title, '',
                CAST(json_extract(field.value_json, '$') AS TEXT), '', '',
                COALESCE(version.file_format, ''), 'UNKNOWN', 'metadata', ''
            FROM event
            JOIN course ON course.course_key = event.course_key
            JOIN event_field_projection projection
              ON projection.event_key = event.event_key AND projection.field_name = 'title'
            JOIN claim title_claim ON title_claim.claim_key = projection.selected_claim_key
            JOIN event_candidate_field field
              ON field.candidate_field_key = title_claim.candidate_field_key
            LEFT JOIN source_locator locator ON locator.locator_key = field.locator_key
            LEFT JOIN resource_version version ON version.version_key = locator.version_key
            WHERE title_claim.origin = 'SOURCE'
              AND title_claim.decision_state = 'ACCEPTED'
              AND json_type(field.value_json) = 'text'
              AND (? IS NULL OR course.course_key = ?)
            ORDER BY event.event_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_claims(connection: sqlite3.Connection, *, course_key: int | None = None) -> None:
        """Index each source claim against its own immutable evidence reference."""

        active_claim = active_claim_sql("claim")
        connection.execute(
            f"""
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, resource_key, version_key,
                source_ref_kind, source_ref_key, course_code, course_title, content_title,
                title, filename, semantic_type, file_format, availability, text_origin, body
            )
            SELECT
                'claim', claim.claim_key, source.course_key,
                version.resource_key, locator.version_key,
                CASE
                    WHEN field.source_observation_key IS NOT NULL THEN 'source_observation'
                    ELSE 'source_locator'
                END,
                COALESCE(field.source_observation_key, field.locator_key),
                course.code, course.title, '', field.original_text, '', '',
                COALESCE(version.file_format, ''), 'UNKNOWN', 'metadata',
                field.field_name || ' ' || claim.decision_state || ' '
                    || field.original_text || ' ' || field.value_json
            FROM claim
            JOIN event_source source ON source.event_source_key = claim.event_source_key
            JOIN course ON course.course_key = source.course_key
            JOIN event_candidate_field field
              ON field.candidate_field_key = claim.candidate_field_key
            LEFT JOIN source_locator locator ON locator.locator_key = field.locator_key
            LEFT JOIN resource_version version ON version.version_key = locator.version_key
            WHERE claim.origin = 'SOURCE'
              AND {active_claim}
              AND (? IS NULL OR course.course_key = ?)
            ORDER BY claim.claim_key
            """,
            (course_key, course_key),
        )

    @staticmethod
    def _insert_materials(
        connection: sqlite3.Connection,
        *,
        course_key: int | None = None,
        resource_key: int | None = None,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, resource_key, version_key,
                source_ref_kind, source_ref_key, course_code, course_title, content_title,
                title, filename, semantic_type, classification_confidence, file_format,
                availability, text_origin, body
            )
            SELECT
                'material', r.resource_key, c.course_key, r.resource_key, v.version_key,
                'source_object', r.source_object_key, c.code, c.title, n.title,
                r.display_title, {_FILENAME_SQL}, {_SEMANTIC_TYPE_SQL},
                {_SEMANTIC_CONFIDENCE_SQL}, COALESCE(v.file_format, ''),
                r.availability, 'metadata', ''
            FROM resource r
            JOIN content_node n ON n.content_key = r.content_key
            JOIN course c ON c.course_key = n.course_key
            LEFT JOIN resource_version v ON v.version_key = r.current_version_key
            WHERE (? IS NULL OR c.course_key = ?)
              AND (? IS NULL OR r.resource_key = ?)
            ORDER BY r.resource_key
            """,
            (course_key, course_key, resource_key, resource_key),
        )

    @staticmethod
    def _insert_native_chunks(
        connection: sqlite3.Connection,
        *,
        course_key: int | None = None,
        resource_key: int | None = None,
    ) -> None:
        connection.execute(
            f"""
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, resource_key, version_key, chunk_key,
                source_ref_kind, source_ref_key, course_code, course_title, content_title,
                title, filename, semantic_type, classification_confidence, file_format,
                availability, parse_coverage, text_origin, body
            )
            SELECT
                'chunk', chunk.chunk_key, c.course_key, r.resource_key, v.version_key,
                chunk.chunk_key, 'source_locator', locator.locator_key, c.code, c.title, n.title,
                r.display_title, {_FILENAME_SQL}, {_SEMANTIC_TYPE_SQL},
                {_SEMANTIC_CONFIDENCE_SQL}, v.file_format, r.availability, parsed.coverage,
                'native', chunk.native_text
            FROM document_chunk chunk
            JOIN source_locator locator ON locator.chunk_key = chunk.chunk_key
            JOIN parsed_document parsed ON parsed.parse_key = chunk.parse_key
            JOIN resource_version v ON v.version_key = parsed.version_key
            JOIN resource r ON r.resource_key = v.resource_key
            JOIN content_node n ON n.content_key = r.content_key
            JOIN course c ON c.course_key = n.course_key
            WHERE {_CURRENT_USABLE_PARSE_SQL}
              AND (? IS NULL OR c.course_key = ?)
              AND (? IS NULL OR r.resource_key = ?)
            ORDER BY chunk.chunk_key
            """,
            (course_key, course_key, resource_key, resource_key),
        )

    @staticmethod
    def _insert_derived_chunks(
        connection: sqlite3.Connection,
        *,
        course_key: int | None = None,
        resource_key: int | None = None,
    ) -> None:
        has_visual_metadata = (
            connection.execute(
                """SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'visual_evidence_metadata'"""
            ).fetchone()
            is not None
        )
        visual_join = (
            """LEFT JOIN visual_evidence_metadata metadata
              ON metadata.representation_key = representation.representation_key"""
            if has_visual_metadata
            else ""
        )
        visual_filter = (
            """AND (
                  representation.representation_kind <> 'vision_description'
                  OR NOT EXISTS (
                      SELECT 1
                      FROM chunk_representation newer
                      JOIN visual_evidence_metadata newer_metadata
                        ON newer_metadata.representation_key = newer.representation_key
                      WHERE newer.chunk_key = representation.chunk_key
                        AND newer.representation_kind = 'vision_description'
                        AND newer.method = representation.method
                        AND newer_metadata.version_key = metadata.version_key
                        AND newer_metadata.source_page_index = metadata.source_page_index
                        AND newer.representation_key > representation.representation_key
                  )
              )"""
            if has_visual_metadata
            else ""
        )
        connection.execute(
            f"""
            INSERT INTO search_document(
                entity_kind, entity_key, course_key, resource_key, version_key, chunk_key,
                representation_key, source_ref_kind, source_ref_key, course_code, course_title,
                content_title, title, filename, semantic_type, classification_confidence,
                file_format, availability, parse_coverage, text_origin, body
            )
            SELECT
                'chunk', representation.representation_key, c.course_key, r.resource_key,
                v.version_key, chunk.chunk_key, representation.representation_key,
                'source_locator', locator.locator_key, c.code, c.title, n.title,
                r.display_title, {_FILENAME_SQL}, {_SEMANTIC_TYPE_SQL},
                {_SEMANTIC_CONFIDENCE_SQL}, v.file_format, r.availability, parsed.coverage,
                'derived:' || representation.representation_kind, representation.text
            FROM chunk_representation representation
            {visual_join}
            JOIN document_chunk chunk ON chunk.chunk_key = representation.chunk_key
            JOIN source_locator locator ON locator.chunk_key = chunk.chunk_key
            JOIN parsed_document parsed ON parsed.parse_key = chunk.parse_key
            JOIN resource_version v ON v.version_key = parsed.version_key
            JOIN resource r ON r.resource_key = v.resource_key
            JOIN content_node n ON n.content_key = r.content_key
            JOIN course c ON c.course_key = n.course_key
            WHERE representation.text IS NOT NULL
              AND {_CURRENT_USABLE_PARSE_SQL}
              {visual_filter}
              AND (? IS NULL OR c.course_key = ?)
              AND (? IS NULL OR r.resource_key = ?)
            ORDER BY representation.representation_key
            """,
            (course_key, course_key, resource_key, resource_key),
        )
