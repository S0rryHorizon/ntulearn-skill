"""Offline deterministic search with exact local provenance and coverage."""

from __future__ import annotations

import json
import re
import sqlite3

from ntulearn_skill.core import Availability, CourseId, Coverage
from ntulearn_skill.core.identifiers import require_identifier
from ntulearn_skill.core.models import from_storage_time
from ntulearn_skill.extractors import SemanticType
from ntulearn_skill.index import SearchIndex, SearchIndexError
from ntulearn_skill.parsers import (
    DocumentChunkRecord,
    JsonValue,
    ParseRepository,
    ParseStorageError,
)
from ntulearn_skill.search.models import (
    CoverageView,
    ResolvedSource,
    SearchEntityKind,
    SearchFilters,
    SearchHit,
    SearchQuery,
    SearchResult,
    SearchTextOrigin,
    SourceReference,
    SourceReferenceKind,
)
from ntulearn_skill.storage import Database, StorageError


class SearchError(StorageError):
    """Privacy-safe deterministic search failure."""


class SourceResolutionError(SearchError):
    """A typed local source reference cannot be resolved."""


_WORD = re.compile(r"[^\W_]+", flags=re.UNICODE)
_COVERAGE_PRIORITY = {
    Coverage.COMPLETE: 0,
    Coverage.STALE: 1,
    Coverage.PARTIAL: 2,
    Coverage.UNKNOWN: 3,
    Coverage.FAILED: 4,
}
_SCOPE_ALIASES = {
    "course": "courses",
    "courses": "courses",
    "course_discovery": "courses",
    "content": "content",
    "content_tree": "content",
    "material": "materials",
    "materials": "materials",
    "resource": "materials",
    "resources": "materials",
    "attachments": "materials",
    "documents": "materials",
    "announcement": "announcements",
    "announcements": "announcements",
    "assessment": "assessments",
    "assessments": "assessments",
}


def _literal_match_query(value: str) -> str:
    # Let SQLite's unicode61 tokenizer perform the same case normalization for
    # indexed text and queries.  Python casefolding expands some characters
    # (for example, German sharp s) that unicode61 deliberately keeps intact.
    words = _WORD.findall(value)
    if not words:
        raise ValueError("search text must contain a Unicode word")
    if len(words) > 64:
        raise ValueError("search text contains too many terms")
    return " AND ".join(f'"{word}"' for word in words)


def _classification_value_sql(value_column: str, fallback: str) -> str:
    return f"""
        COALESCE(
            (
                SELECT classification.{value_column}
                FROM material_classification_selection selection
                JOIN material_classification classification
                  ON classification.classification_key = selection.classification_key
                JOIN classification_run run
                  ON run.classification_run_key = classification.classification_run_key
                WHERE selection.resource_key = resource.resource_key
                  AND (run.version_key IS NULL OR run.version_key IS version.version_key)
                ORDER BY selection.selection_key DESC
                LIMIT 1
            ),
            (
                SELECT classification.{value_column}
                FROM classification_run run
                JOIN material_classification classification
                  ON classification.classification_run_key = run.classification_run_key
                WHERE run.resource_key = resource.resource_key
                  AND (run.version_key IS NULL OR run.version_key IS version.version_key)
                  AND run.status <> 'FAILED'
                ORDER BY run.created_at DESC, run.classification_run_key DESC,
                         classification.rank
                LIMIT 1
            ),
            {fallback}
        )
    """


class SearchService:
    """Search only local relational text; never opens originals or uses a network."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self.index = SearchIndex(database)
        self.parses = ParseRepository(database)

    def search(self, query: SearchQuery) -> SearchResult:
        match_query = _literal_match_query(query.text)
        try:
            with self.database.transaction() as connection:
                self.index.ensure_current(connection)
                course_key = self._course_key(connection, query.filters.course)
                rows = self._matched_rows(connection, query, match_query, course_key)
                coverage = self._coverage(connection, query, course_key)
        except (sqlite3.Error, SearchIndexError, ValueError):
            raise SearchError("local full-text search failed") from None

        chunk_keys = tuple(int(row["chunk_key"]) for row in rows if row["chunk_key"] is not None)
        try:
            windows = {
                window.match_chunk_key: window.chunks
                for window in self.parses.hydrate_matches(
                    chunk_keys, neighbor_count=query.neighbor_count
                )
            }
        except ParseStorageError:
            raise SearchError("local search evidence hydration failed") from None

        items = tuple(
            self._hit(
                row,
                () if row["chunk_key"] is None else windows.get(int(row["chunk_key"]), ()),
            )
            for row in rows
        )
        completeness = self._aggregate_coverage(coverage)
        source_coverage = tuple(item for item in coverage if item.evidence != "parsed_document")
        as_of = (
            None
            if not source_coverage or any(item.observed_at is None for item in source_coverage)
            else min(item.observed_at for item in source_coverage if item.observed_at is not None)
        )
        warnings = self._warnings(coverage)
        if items:
            message = f"Found {len(items)} result(s) in the available local coverage."
        elif completeness is Coverage.COMPLETE:
            message = "No matches found in the complete local coverage."
        else:
            message = (
                "No matches found in the available local coverage; "
                f"coverage is {completeness.value.lower()}."
            )
        return SearchResult(items, coverage, completeness, as_of, warnings, message)

    def resolve_source(
        self, reference: SourceReference, *, context_window: int = 1
    ) -> ResolvedSource:
        if not 0 <= context_window <= 5:
            raise ValueError("context window must be between 0 and 5")
        connection = self.database.connect()
        try:
            if reference.kind is SourceReferenceKind.SOURCE_OBJECT:
                row = connection.execute(
                    """
                    SELECT provider.name AS provider, object.object_kind, object.remote_key
                    FROM source_object object
                    JOIN source_provider provider
                      ON provider.provider_key = object.provider_key
                    WHERE object.source_object_key = ?
                    """,
                    (reference.key,),
                ).fetchone()
                if row is None:
                    raise SourceResolutionError("local source object was not found")
                return ResolvedSource(
                    reference,
                    str(row["provider"]),
                    str(row["object_kind"]),
                    str(row["remote_key"]),
                    None,
                    None,
                    (),
                )

            if reference.kind is SourceReferenceKind.SOURCE_OBSERVATION:
                row = connection.execute(
                    """
                    SELECT provider.name AS provider, object.object_kind, object.remote_key,
                           observation.snapshot_json
                    FROM source_observation observation
                    JOIN source_object object
                      ON object.source_object_key = observation.source_object_key
                    JOIN source_provider provider ON provider.provider_key = object.provider_key
                    WHERE observation.observation_key = ?
                    """,
                    (reference.key,),
                ).fetchone()
                if row is None:
                    raise SourceResolutionError("local source observation was not found")
                return ResolvedSource(
                    reference,
                    str(row["provider"]),
                    str(row["object_kind"]),
                    str(row["remote_key"]),
                    None,
                    None,
                    (),
                    json.loads(str(row["snapshot_json"])),
                )

            if reference.kind is SourceReferenceKind.RESOURCE_OBSERVATION:
                row = connection.execute(
                    """SELECT provider.name AS provider, object.object_kind, object.remote_key,
                              observation.version_key, observation.observation_status,
                              observation.original_filename,
                              observation.sanitized_metadata_json,
                              observation.candidate_modified_at,
                              observation.candidate_revision,
                              observation.availability, observation.observed_at,
                              observation.fetch_decision
                    FROM resource_observation observation
                    JOIN resource ON resource.resource_key = observation.resource_key
                    JOIN source_object object
                      ON object.source_object_key = resource.source_object_key
                    JOIN source_provider provider ON provider.provider_key = object.provider_key
                    WHERE observation.observation_key = ?""",
                    (reference.key,),
                ).fetchone()
                if row is None:
                    raise SourceResolutionError("local resource observation was not found")
                observation = {
                    "observation_status": str(row["observation_status"]),
                    "original_filename": str(row["original_filename"]),
                    "sanitized_metadata": json.loads(str(row["sanitized_metadata_json"])),
                    "candidate_modified_at": row["candidate_modified_at"],
                    "candidate_revision": row["candidate_revision"],
                    "availability": str(row["availability"]),
                    "observed_at": str(row["observed_at"]),
                    "fetch_decision": str(row["fetch_decision"]),
                }
                return ResolvedSource(
                    reference,
                    str(row["provider"]),
                    str(row["object_kind"]),
                    str(row["remote_key"]),
                    None if row["version_key"] is None else int(row["version_key"]),
                    None,
                    (),
                    observation,
                )

            row = connection.execute(
                """
                SELECT locator.version_key, locator.chunk_key, locator.structured_json,
                       provider.name AS provider, object.object_kind, object.remote_key
                FROM source_locator locator
                JOIN resource_version version ON version.version_key = locator.version_key
                JOIN resource ON resource.resource_key = version.resource_key
                JOIN source_object object
                  ON object.source_object_key = resource.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE locator.locator_key = ?
                """,
                (reference.key,),
            ).fetchone()
            if row is None:
                raise SourceResolutionError("local source locator was not found")
            chunk_key = int(row["chunk_key"])
        except sqlite3.Error:
            raise SourceResolutionError("local source resolution failed") from None
        finally:
            connection.close()
        try:
            hydrated = self.parses.hydrate_matches((chunk_key,), neighbor_count=context_window)
        except ParseStorageError:
            raise SourceResolutionError("local source resolution failed") from None
        chunks = () if not hydrated else hydrated[0].chunks
        return ResolvedSource(
            reference,
            str(row["provider"]),
            str(row["object_kind"]),
            str(row["remote_key"]),
            int(row["version_key"]),
            json.loads(str(row["structured_json"])),
            chunks,
        )

    @staticmethod
    def _course_key(connection: sqlite3.Connection, course: CourseId | None) -> int | None:
        if course is None:
            return None
        require_identifier(course, CourseId)
        row = connection.execute(
            """
            SELECT c.course_key
            FROM course c
            JOIN source_object object ON object.source_object_key = c.source_object_key
            JOIN source_provider provider ON provider.provider_key = object.provider_key
            WHERE provider.name = ? AND object.object_kind = 'course' AND object.remote_key = ?
            """,
            (course.provider, course.value),
        ).fetchone()
        if row is None:
            raise ValueError("course filter does not resolve to a local course")
        return int(row["course_key"])

    @staticmethod
    def _matched_rows(
        connection: sqlite3.Connection,
        query: SearchQuery,
        match_query: str,
        course_key: int | None,
    ) -> tuple[sqlite3.Row, ...]:
        clauses = ["search_document_fts MATCH ?"]
        parameters: list[object] = [match_query]

        def add_values(column: str, values: tuple[str, ...]) -> None:
            if not values:
                return
            clauses.append(f"document.{column} IN ({','.join('?' for _ in values)})")
            parameters.extend(values)

        filters = query.filters
        if course_key is not None:
            clauses.append("document.course_key = ?")
            parameters.append(course_key)
        add_values("entity_kind", tuple(sorted(item.value for item in filters.entity_kinds)))
        add_values("semantic_type", tuple(sorted(item.value for item in filters.semantic_types)))
        add_values("file_format", tuple(sorted(filters.file_formats)))
        add_values("availability", tuple(sorted(item.value for item in filters.availabilities)))
        if filters.text_origins:
            origin_parts: list[str] = []
            for origin in sorted(filters.text_origins, key=lambda item: item.value):
                if origin is SearchTextOrigin.DERIVED:
                    origin_parts.append("document.text_origin GLOB 'derived:*'")
                else:
                    origin_parts.append("document.text_origin = ?")
                    parameters.append(origin.value)
            clauses.append(f"({' OR '.join(origin_parts)})")
        if filters.version_key is not None:
            clauses.append("document.version_key = ?")
            parameters.append(filters.version_key)
        if filters.minimum_classification_confidence is not None:
            clauses.append("document.classification_confidence >= ?")
            parameters.append(filters.minimum_classification_confidence)
        if not filters.include_historical_versions:
            clauses.append(
                """(document.version_key IS NULL OR document.version_key = (
                    SELECT current_version_key FROM resource
                    WHERE resource_key = document.resource_key
                ))"""
            )
        parameters.append(query.limit)
        sql = f"""
            SELECT document.*, provider.name AS provider, course_object.remote_key AS course_remote,
                   bm25(search_document_fts, 12.0, 5.0, 7.0, 10.0, 9.0, 8.0, 1.0)
                       AS rank_value,
                   locator.structured_json AS locator_json
            FROM search_document_fts
            JOIN search_document document
              ON document.search_document_key = search_document_fts.rowid
            JOIN course course_row ON course_row.course_key = document.course_key
            JOIN source_object course_object
              ON course_object.source_object_key = course_row.source_object_key
            JOIN source_provider provider ON provider.provider_key = course_object.provider_key
            LEFT JOIN source_locator locator
              ON document.source_ref_kind = 'source_locator'
             AND locator.locator_key = document.source_ref_key
            WHERE {" AND ".join(clauses)}
            ORDER BY rank_value ASC,
                     CASE document.entity_kind
                         WHEN 'course' THEN 0 WHEN 'content' THEN 1
                         WHEN 'material' THEN 2 ELSE 3 END,
                     document.course_key ASC,
                     COALESCE(document.resource_key, 0) ASC,
                     COALESCE(document.version_key, 0) ASC,
                     COALESCE(document.chunk_key, 0) ASC,
                     document.text_origin ASC,
                     document.entity_key ASC,
                     document.source_ref_key ASC,
                     document.search_document_key ASC
            LIMIT ?
        """
        return tuple(connection.execute(sql, parameters))

    @staticmethod
    def _hit(row: sqlite3.Row, neighbors: tuple[DocumentChunkRecord, ...]) -> SearchHit:
        semantic = str(row["semantic_type"])
        file_format = str(row["file_format"])
        locator_payload = row["locator_json"]
        locator: dict[str, JsonValue] | None = None
        if locator_payload is not None:
            locator = json.loads(str(locator_payload))
        return SearchHit(
            search_document_key=int(row["search_document_key"]),
            entity_kind=SearchEntityKind(str(row["entity_kind"])),
            entity_key=int(row["entity_key"]),
            rank=float(row["rank_value"]),
            course=CourseId(str(row["provider"]), str(row["course_remote"])),
            course_code=str(row["course_code"]),
            course_title=str(row["course_title"]),
            content_title=str(row["content_title"]),
            title=str(row["title"]),
            filename=str(row["filename"]),
            semantic_type=None if not semantic else SemanticType(semantic),
            classification_confidence=None
            if row["classification_confidence"] is None
            else float(row["classification_confidence"]),
            file_format=None if not file_format else file_format,
            availability=Availability(str(row["availability"])),
            version_key=None if row["version_key"] is None else int(row["version_key"]),
            chunk_key=None if row["chunk_key"] is None else int(row["chunk_key"]),
            text_origin=str(row["text_origin"]),
            parse_coverage=None
            if row["parse_coverage"] is None
            else Coverage(str(row["parse_coverage"])),
            source=SourceReference(
                SourceReferenceKind(str(row["source_ref_kind"])), int(row["source_ref_key"])
            ),
            locator=locator,
            matching_text=str(row["body"]),
            neighbors=neighbors,
        )

    def _coverage(
        self,
        connection: sqlite3.Connection,
        query: SearchQuery,
        course_key: int | None,
    ) -> tuple[CoverageView, ...]:
        course_keys = (
            {course_key}
            if course_key is not None
            else {int(row[0]) for row in connection.execute("SELECT course_key FROM course")}
        )
        expected: set[str] = set()
        kinds = query.filters.entity_kinds
        if SearchEntityKind.COURSE in kinds:
            expected.add("courses")
        if SearchEntityKind.CONTENT in kinds:
            expected.add("content")
        if SearchEntityKind.MATERIAL in kinds or SearchEntityKind.CHUNK in kinds:
            expected.add("materials")
        if SearchEntityKind.ANNOUNCEMENT in kinds:
            expected.add("announcements")
        if SearchEntityKind.ASSESSMENT in kinds:
            expected.add("assessments")

        views: list[CoverageView] = []
        for key in sorted(course_keys):
            identity = connection.execute(
                """
                SELECT provider.name AS provider, object.remote_key, provider.provider_key
                FROM course c
                JOIN source_object object ON object.source_object_key = c.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE c.course_key = ?
                """,
                (key,),
            ).fetchone()
            if identity is None:
                continue
            course = CourseId(str(identity["provider"]), str(identity["remote_key"]))
            scope_rows = connection.execute(
                """
                SELECT data_kind, coverage, observed_at, course_key
                FROM sync_scope_result
                WHERE provider_key = ? AND (course_key = ? OR course_key IS NULL)
                ORDER BY (course_key IS NOT NULL) DESC, observed_at DESC, scope_result_key DESC
                """,
                (int(identity["provider_key"]), key),
            ).fetchall()
            latest: dict[str, sqlite3.Row] = {}
            for scope in scope_rows:
                normalized = _SCOPE_ALIASES.get(str(scope["data_kind"]).casefold())
                if normalized is not None and normalized not in latest:
                    latest[normalized] = scope
            # The M3 content traversal discovers resource metadata in the same bounded
            # pagination scope. A later dedicated material scope remains more specific.
            if "materials" not in latest and "content" in latest:
                latest["materials"] = latest["content"]
            for data_kind in sorted(expected):
                scope = latest.get(data_kind)
                if scope is None:
                    views.append(
                        CoverageView(course, data_kind, Coverage.UNKNOWN, None, "unrecorded")
                    )
                else:
                    views.append(
                        CoverageView(
                            course,
                            data_kind,
                            Coverage(str(scope["coverage"])),
                            from_storage_time(str(scope["observed_at"])),
                            "sync_scope_result",
                        )
                    )
            if SearchEntityKind.CHUNK in kinds:
                views.append(
                    self._parse_coverage(
                        connection,
                        key,
                        course,
                        filters=query.filters,
                    )
                )
        return tuple(views)

    @staticmethod
    def _parse_coverage(
        connection: sqlite3.Connection,
        course_key: int,
        course: CourseId,
        *,
        filters: SearchFilters,
    ) -> CoverageView:
        if filters.text_origins and not filters.text_origins.intersection(
            {SearchTextOrigin.NATIVE, SearchTextOrigin.DERIVED}
        ):
            return CoverageView(course, "document_text", Coverage.COMPLETE, None, "parsed_document")

        parameters: list[object] = [course_key]
        clauses = ["content.course_key = ?"]
        if filters.version_key is not None:
            relation = """
            FROM resource_version version
            JOIN resource ON resource.resource_key = version.resource_key
            JOIN content_node content ON content.content_key = resource.content_key
            """
            clauses.append("version.version_key = ?")
            parameters.append(filters.version_key)
        elif filters.include_historical_versions:
            relation = """
            FROM resource
            JOIN content_node content ON content.content_key = resource.content_key
            LEFT JOIN resource_version version ON version.resource_key = resource.resource_key
            """
        else:
            relation = """
            FROM resource
            JOIN content_node content ON content.content_key = resource.content_key
            LEFT JOIN resource_version version
              ON version.version_key = resource.current_version_key
            """

        if filters.availabilities:
            availability_values = tuple(sorted(item.value for item in filters.availabilities))
            clauses.append(
                f"resource.availability IN ({','.join('?' for _ in availability_values)})"
            )
            parameters.extend(availability_values)
        if filters.file_formats:
            format_values = tuple(sorted(filters.file_formats))
            clauses.append(
                "(version.version_key IS NULL OR "
                f"version.file_format IN ({','.join('?' for _ in format_values)}))"
            )
            parameters.extend(format_values)

        semantic_type_sql = _classification_value_sql("semantic_type", "''")
        confidence_sql = _classification_value_sql("confidence", "NULL")
        if filters.semantic_types:
            semantic_values = tuple(sorted(item.value for item in filters.semantic_types))
            clauses.append(
                f"(({semantic_type_sql}) = '' OR "
                f"({semantic_type_sql}) IN ({','.join('?' for _ in semantic_values)}))"
            )
            parameters.extend(semantic_values)
        if filters.minimum_classification_confidence is not None:
            clauses.append(f"(({confidence_sql}) IS NULL OR ({confidence_sql}) >= ?)")
            parameters.append(filters.minimum_classification_confidence)

        rows = connection.execute(
            f"""
            SELECT (
                SELECT parsed.coverage
                FROM parsed_document parsed
                WHERE parsed.version_key = version.version_key
                ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
            ) AS coverage,
            (
                SELECT parsed.status
                FROM parsed_document parsed
                WHERE parsed.version_key = version.version_key
                ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
            ) AS status
            {relation}
            WHERE {" AND ".join(clauses)}
            """,
            parameters,
        ).fetchall()
        if not rows:
            coverage = Coverage.COMPLETE
        else:
            row_coverage = (
                Coverage.UNKNOWN
                if row["coverage"] is None or row["status"] == "UNSUPPORTED"
                else Coverage(str(row["coverage"]))
                for row in rows
            )
            coverage = max(row_coverage, key=_COVERAGE_PRIORITY.__getitem__)
        return CoverageView(course, "document_text", coverage, None, "parsed_document")

    @staticmethod
    def _aggregate_coverage(views: tuple[CoverageView, ...]) -> Coverage:
        if not views:
            return Coverage.UNKNOWN
        return max((view.coverage for view in views), key=_COVERAGE_PRIORITY.__getitem__)

    @staticmethod
    def _warnings(views: tuple[CoverageView, ...]) -> tuple[str, ...]:
        return tuple(
            f"coverage_{coverage.value.lower()}"
            for coverage in sorted(
                {view.coverage for view in views if view.coverage is not Coverage.COMPLETE},
                key=lambda item: (_COVERAGE_PRIORITY[item], item.value),
            )
        )
