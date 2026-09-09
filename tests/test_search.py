from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId, Coverage
from ntulearn_skill.extractors import (
    ClassificationInput,
    ClassificationRepository,
    ClassificationService,
    RuleBasedMaterialClassifier,
    SemanticType,
)
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.parsers import (
    FallbackOutput,
    ParseRepository,
    ParserRegistry,
    ParseService,
    PdfParser,
    RepresentationKind,
)
from ntulearn_skill.search import (
    SearchEntityKind,
    SearchFilters,
    SearchQuery,
    SearchService,
    SearchTextOrigin,
    SourceReferenceKind,
)
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)
from ntulearn_skill.storage.migration import MigrationRunner, load_migrations


@dataclass(frozen=True)
class _Harness:
    paths: RuntimePaths
    database: Database
    domain: DomainRepository
    resources: ResourceRepository
    store: ResourceStore
    course_id: CourseId
    content_id: ContentId


def _pdf(*pages: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    for text in pages:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return output.getvalue()


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-synthetic-runtime")
    database = Database(paths.database)
    domain = DomainRepository(database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() == 7
    course_id = CourseId("synthetic", "m5-course")
    content_id = ContentId("synthetic", "m5-content")
    domain.put_course(course_id, code="PH0000", title="Invented Search Course")
    domain.put_content_node(
        content_id,
        course_id=course_id,
        handler_kind="document",
        title="Invented Week Five Materials",
        position=0,
    )
    return _Harness(paths, database, domain, resources, store, course_id, content_id)


def _run(database: Database, offset: int) -> int:
    timestamp = datetime(2027, 1, 1, 0, offset, tzinfo=UTC).isoformat()
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic', '{}', ?, 'SUCCEEDED')""",
            (timestamp,),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _ingest_and_parse(
    harness: _Harness,
    *,
    remote_key: str,
    title: str,
    filename: str,
    pages: tuple[str, ...],
    offset: int,
):
    written = harness.store.ingest(
        io.BytesIO(_pdf(*pages)),
        AttachmentId("synthetic", remote_key),
        content_id=harness.content_id,
        sync_run_key=_run(harness.database, offset),
        display_title=title,
        original_filename=filename,
        observed_at=datetime(2027, 1, 1, 0, offset, tzinfo=UTC),
    )
    service = ParseService(
        harness.paths,
        harness.resources,
        ParseRepository(harness.database),
        ParserRegistry((PdfParser(),)),
    )
    parsed = service.parse_version(written.version.key)
    return written, parsed


def _record_coverage(
    harness: _Harness,
    data_kind: str,
    coverage: Coverage,
    offset: int,
    *,
    course_id: CourseId | None = None,
) -> None:
    run_key = _run(harness.database, offset)
    course_id = course_id or harness.course_id
    with harness.database.transaction() as connection:
        context = connection.execute(
            """
            SELECT c.course_key, object.provider_key
            FROM course c JOIN source_object object
              ON object.source_object_key = c.source_object_key
            JOIN source_provider provider ON provider.provider_key = object.provider_key
            WHERE provider.name = ? AND object.remote_key = ?
            """,
            (course_id.provider, course_id.value),
        ).fetchone()
        assert context is not None
        connection.execute(
            """
            INSERT INTO sync_scope_result(
                sync_run_key, provider_key, course_key, data_kind, coverage,
                pages_seen, items_seen, pagination_complete, observed_at
            ) VALUES (?, ?, ?, ?, ?, 1, 1, ?, ?)
            """,
            (
                run_key,
                int(context["provider_key"]),
                int(context["course_key"]),
                data_kind,
                coverage.value,
                int(coverage is Coverage.COMPLETE),
                datetime(2027, 1, 1, 0, offset, tzinfo=UTC).isoformat(),
            ),
        )


def test_searches_actual_metadata_classification_and_parsed_body_with_exact_sources(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(
        harness,
        remote_key="slides",
        title="Quantum Lecture Slides",
        filename="wave-packet-notes.pdf",
        pages=("Neighbor introduction", "Unicode café evidence", "Closing page"),
        offset=1,
    )
    classification = ClassificationService(
        ClassificationRepository(harness.database), RuleBasedMaterialClassifier()
    ).classify(
        ClassificationInput(
            written.resource.key,
            written.version.key,
            "Quantum Lecture Slides",
            "wave-packet-notes.pdf",
        )
    )
    assert classification.run.candidates[0].semantic_type is SemanticType.LECTURE_SLIDES
    search = SearchService(harness.database)

    title_hit = search.search(
        SearchQuery(
            "Quantum",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.MATERIAL})),
        )
    ).items[0]
    filename_hit = search.search(
        SearchQuery(
            "packet",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.MATERIAL})),
        )
    ).items[0]
    semantic_hit = search.search(
        SearchQuery(
            "lecture slides",
            SearchFilters(
                entity_kinds=frozenset({SearchEntityKind.MATERIAL}),
                semantic_types=frozenset({SemanticType.LECTURE_SLIDES}),
            ),
        )
    ).items[0]
    body_hit = search.search(
        SearchQuery(
            "café evidence",
            SearchFilters(
                course=harness.course_id,
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                semantic_types=frozenset({SemanticType.LECTURE_SLIDES}),
                file_formats=frozenset({"pdf"}),
                availabilities=frozenset({Availability.ACTIVE}),
                text_origins=frozenset({SearchTextOrigin.NATIVE}),
                minimum_classification_confidence=0.5,
            ),
            neighbor_count=1,
        )
    ).items[0]

    assert title_hit.source.kind is SourceReferenceKind.SOURCE_OBJECT
    assert title_hit.locator is None
    assert filename_hit.filename == "wave-packet-notes.pdf"
    assert semantic_hit.semantic_type is SemanticType.LECTURE_SLIDES
    assert body_hit.chunk_key == parsed.chunks[1].key
    assert body_hit.locator == parsed.chunks[1].locator
    assert [chunk.ordinal for chunk in body_hit.neighbors] == [0, 1, 2]
    resolved = search.resolve_source(body_hit.source, context_window=0)
    assert resolved.version_key == written.version.key
    assert resolved.locator == parsed.chunks[1].locator
    assert [chunk.key for chunk in resolved.chunks] == [parsed.chunks[1].key]


def test_query_uses_the_same_unicode61_case_normalization_as_the_index(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _ingest_and_parse(
        harness,
        remote_key="unicode-sharp-s",
        title="Straße Notes",
        filename="strasse.pdf",
        pages=("Invented Straße evidence",),
        offset=1,
    )

    result = SearchService(harness.database).search(SearchQuery("Straße"))

    assert result.items
    assert any("Straße" in hit.title or "Straße" in hit.matching_text for hit in result.items)


def test_ranking_ties_are_deterministic_and_title_outweighs_body(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _ingest_and_parse(
        harness,
        remote_key="title-rank",
        title="Eigenvalue Beacon",
        filename="first.pdf",
        pages=("ordinary synthetic body",),
        offset=1,
    )
    _ingest_and_parse(
        harness,
        remote_key="body-rank",
        title="Ordinary Reading",
        filename="second.pdf",
        pages=("Eigenvalue Beacon",),
        offset=2,
    )
    service = SearchService(harness.database)
    query = SearchQuery("Eigenvalue Beacon", limit=20)

    first = service.search(query)
    second = service.search(query)

    assert [hit.search_document_key for hit in first.items] == [
        hit.search_document_key for hit in second.items
    ]
    assert first.items[0].title == "Eigenvalue Beacon"
    assert [(hit.rank, hit.search_document_key) for hit in first.items] == sorted(
        (hit.rank, hit.search_document_key) for hit in first.items
    )


def test_derived_text_is_separate_and_rebuild_is_lossless(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(
        harness,
        remote_key="derived",
        title="Diagram Notes",
        filename="diagram.pdf",
        pages=("Native evidence remains",),
        offset=1,
    )
    repository = ParseRepository(harness.database)
    representation = repository.append_representation(
        parsed.chunks[0].key,
        FallbackOutput(
            RepresentationKind.VISION_DESCRIPTION,
            "Derived synthetic circuit annotation",
            None,
            "synthetic-local",
            None,
            "1",
            hashlib.sha256(b"synthetic-settings").hexdigest(),
            0.8,
        ),
        diagnostic_reason="synthetic_test",
    )
    service = SearchService(harness.database)
    before = service.search(
        SearchQuery(
            "circuit annotation",
            SearchFilters(
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                text_origins=frozenset({SearchTextOrigin.DERIVED}),
            ),
        )
    )
    assert before.items[0].text_origin == "derived:vision_description"
    assert before.items[0].entity_key == representation.key
    assert "Derived" not in parsed.chunks[0].native_text

    with harness.database.transaction() as connection:
        canonical_before = tuple(
            tuple(row)
            for row in connection.execute(
                """SELECT entity_kind, entity_key, course_key, resource_key, version_key,
                          chunk_key, representation_key, source_ref_kind, source_ref_key,
                          course_code, course_title, content_title, title, filename,
                          semantic_type, classification_confidence, file_format, availability,
                          parse_coverage, text_origin, body
                   FROM search_document
                   ORDER BY entity_kind, entity_key, text_origin"""
            )
        )
    rebuilt = SearchIndex(harness.database).rebuild()
    with harness.database.transaction() as connection:
        canonical_after = tuple(
            tuple(row)
            for row in connection.execute(
                """SELECT entity_kind, entity_key, course_key, resource_key, version_key,
                          chunk_key, representation_key, source_ref_kind, source_ref_key,
                          course_code, course_title, content_title, title, filename,
                          semantic_type, classification_confidence, file_format, availability,
                          parse_coverage, text_origin, body
                   FROM search_document
                   ORDER BY entity_kind, entity_key, text_origin"""
            )
        )
    after = service.search(
        SearchQuery(
            "circuit annotation",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.CHUNK})),
        )
    )
    assert rebuilt.document_count == rebuilt.fts_count
    assert canonical_after == canonical_before
    assert [(hit.source, hit.locator) for hit in after.items] == [
        (hit.source, hit.locator) for hit in before.items
    ]


def test_rebuild_recovers_when_external_content_fts_postings_are_missing(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    _ingest_and_parse(
        harness,
        remote_key="missing-postings",
        title="Disposable Index Notes",
        filename="disposable-index.pdf",
        pages=("Missingpostingbeacon remains in canonical synthetic evidence",),
        offset=1,
    )
    service = SearchService(harness.database)
    query = SearchQuery("Missingpostingbeacon")
    assert service.search(query).items

    connection = harness.database.connect()
    try:
        connection.execute(
            "INSERT INTO search_document_fts(search_document_fts) VALUES ('delete-all')"
        )
        connection.commit()
        missing = connection.execute(
            "SELECT count(*) FROM search_document_fts WHERE search_document_fts MATCH ?",
            ('"Missingpostingbeacon"',),
        ).fetchone()
        assert missing is not None and int(missing[0]) == 0
    finally:
        connection.close()

    SearchIndex(harness.database).rebuild()

    assert service.search(query).items


@pytest.mark.parametrize("coverage", [Coverage.PARTIAL, Coverage.STALE, Coverage.FAILED])
def test_empty_result_wording_preserves_incomplete_coverage(
    tmp_path: Path, coverage: Coverage
) -> None:
    harness = _harness(tmp_path)
    _record_coverage(harness, "materials", coverage, 1)
    result = SearchService(harness.database).search(
        SearchQuery(
            "absent-token",
            SearchFilters(
                course=harness.course_id,
                entity_kinds=frozenset({SearchEntityKind.MATERIAL}),
            ),
        )
    )

    assert not result.items
    assert result.completeness is coverage
    assert "available local coverage" in result.message
    assert "complete local coverage" not in result.message


def test_no_scope_is_unknown_and_complete_scope_can_be_conclusive(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    service = SearchService(harness.database)
    query = SearchQuery(
        "absent-token",
        SearchFilters(
            course=harness.course_id,
            entity_kinds=frozenset({SearchEntityKind.MATERIAL}),
        ),
    )
    unknown = service.search(query)
    _record_coverage(harness, "materials", Coverage.COMPLETE, 1)
    complete = service.search(query)

    assert unknown.completeness is Coverage.UNKNOWN
    assert "available local coverage" in unknown.message
    assert complete.completeness is Coverage.COMPLETE
    assert complete.message == "No matches found in the complete local coverage."


def test_index_refresh_commits_and_rolls_back_with_source_metadata(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    service = SearchService(harness.database)
    assert service.search(SearchQuery("Search Course")).items

    harness.domain.put_course(
        harness.course_id,
        code="PH0000",
        title="Invented Renamed Course",
        observed_at=datetime(2027, 1, 2, tzinfo=UTC),
    )
    assert service.search(SearchQuery("Renamed Course")).items
    assert not service.search(SearchQuery("Search Course")).items

    with pytest.raises(RuntimeError, match="synthetic rollback"):
        with harness.database.transaction() as connection:
            connection.execute(
                "UPDATE course SET title = 'Invented Rolled Back Course' WHERE course_key = 1"
            )
            raise RuntimeError("synthetic rollback")

    assert service.search(SearchQuery("Renamed Course")).items
    assert not service.search(SearchQuery("Rolled Back Course")).items
    assert SearchIndex(harness.database).is_current()


def test_search_hydrates_historical_local_chunks_without_original_file(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    first, first_parse = _ingest_and_parse(
        harness,
        remote_key="history",
        title="Versioned Notes",
        filename="old.pdf",
        pages=("Retained historical quasar evidence",),
        offset=1,
    )
    second, _second_parse = _ingest_and_parse(
        harness,
        remote_key="history",
        title="Versioned Notes",
        filename="new.pdf",
        pages=("Current pulsar evidence",),
        offset=2,
    )
    (harness.paths.root / first.version.blob_relpath).unlink()
    service = SearchService(harness.database)

    historical = service.search(
        SearchQuery(
            "historical quasar",
            SearchFilters(
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                version_key=first.version.key,
            ),
            neighbor_count=0,
        )
    )
    current_only = service.search(
        SearchQuery(
            "historical quasar",
            SearchFilters(
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                include_historical_versions=False,
            ),
        )
    )

    assert historical.items[0].chunk_key == first_parse.chunks[0].key
    assert historical.items[0].neighbors[0].native_text.startswith("Retained historical")
    assert not current_only.items
    assert second.version.key != first.version.key


def test_migration_from_m4_rebuilds_existing_canonical_rows_on_first_search(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "private" / "metadata.sqlite3")
    assert MigrationRunner(database, migrations=load_migrations()[:4]).migrate() == 4
    domain = DomainRepository(database)
    course_id = CourseId("synthetic", "pre-m5-course")
    domain.put_course(course_id, code="CS0000", title="Preexisting Unicode 课程")

    assert MigrationRunner(database).migrate() == 7
    assert not SearchIndex(database).is_current()
    new_course_id = CourseId("synthetic", "post-m5-course")
    domain.put_course(new_course_id, code="PH0000", title="Post-migration Course")
    service = SearchService(database)
    legacy = service.search(
        SearchQuery(
            "课程",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.COURSE})),
        )
    )
    added = service.search(
        SearchQuery(
            "Post-migration",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.COURSE})),
        )
    )

    assert legacy.items[0].course == course_id
    assert added.items[0].course == new_course_id
    assert SearchIndex(database).is_current()


def test_unfetched_unavailable_resource_keeps_filtered_chunk_search_inconclusive(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    resource, _observation = harness.store.observe(
        AttachmentId("synthetic", "unavailable-unfetched"),
        content_id=harness.content_id,
        sync_run_key=_run(harness.database, 1),
        display_title="Unavailable Synthetic Material",
        original_filename="unavailable.pdf",
        availability=Availability.UNAVAILABLE,
    )
    assert resource.availability is Availability.UNAVAILABLE
    _record_coverage(harness, "materials", Coverage.COMPLETE, 2)

    result = SearchService(harness.database).search(
        SearchQuery(
            "absent-token",
            SearchFilters(
                course=harness.course_id,
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                availabilities=frozenset({Availability.UNAVAILABLE}),
            ),
        )
    )

    assert not result.items
    assert result.completeness is Coverage.UNKNOWN
    assert not result.conclusive_empty


def test_unfiltered_coverage_includes_courses_without_hits_and_uses_oldest_as_of(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    second_course = CourseId("synthetic", "m5-second-course")
    harness.domain.put_course(second_course, code="CS0000", title="Second Invented Course")
    _record_coverage(harness, "materials", Coverage.COMPLETE, 1)
    _record_coverage(
        harness,
        "materials",
        Coverage.PARTIAL,
        2,
        course_id=second_course,
    )

    result = SearchService(harness.database).search(
        SearchQuery(
            "absent-token",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.MATERIAL})),
        )
    )

    assert result.completeness is Coverage.PARTIAL
    assert {view.course for view in result.coverage} == {harness.course_id, second_course}
    assert result.as_of == datetime(2027, 1, 1, 0, 1, tzinfo=UTC)


def test_metadata_only_material_keeps_document_text_coverage_unknown(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    harness.store.observe(
        AttachmentId("synthetic", "metadata-only"),
        content_id=harness.content_id,
        sync_run_key=_run(harness.database, 1),
        display_title="Metadata Only Material",
        original_filename="metadata-only.pdf",
    )
    _record_coverage(harness, "materials", Coverage.COMPLETE, 2)

    result = SearchService(harness.database).search(
        SearchQuery(
            "absent-token",
            SearchFilters(
                course=harness.course_id,
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
            ),
        )
    )

    assert result.completeness is Coverage.UNKNOWN
    assert any(
        view.data_kind == "document_text" and view.coverage is Coverage.UNKNOWN
        for view in result.coverage
    )


def test_explicit_historical_version_coverage_ignores_unparsed_current_version(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    first, _parsed = _ingest_and_parse(
        harness,
        remote_key="coverage-history",
        title="Coverage History",
        filename="coverage-old.pdf",
        pages=("Historicalcoveragebeacon is parsed synthetic evidence",),
        offset=1,
    )
    second = harness.store.ingest(
        io.BytesIO(_pdf("Unparsed current synthetic evidence")),
        AttachmentId("synthetic", "coverage-history"),
        content_id=harness.content_id,
        sync_run_key=_run(harness.database, 2),
        display_title="Coverage History",
        original_filename="coverage-current.pdf",
        observed_at=datetime(2027, 1, 1, 0, 2, tzinfo=UTC),
    )
    assert second.version.key != first.version.key
    _record_coverage(harness, "materials", Coverage.COMPLETE, 3)

    result = SearchService(harness.database).search(
        SearchQuery(
            "Historicalcoveragebeacon",
            SearchFilters(
                course=harness.course_id,
                entity_kinds=frozenset({SearchEntityKind.CHUNK}),
                version_key=first.version.key,
            ),
        )
    )

    assert result.items[0].version_key == first.version.key
    assert result.completeness is Coverage.COMPLETE
