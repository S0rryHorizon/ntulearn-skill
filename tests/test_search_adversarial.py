from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from ntulearn_skill.core import AttachmentId, ContentId, CourseId
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.parsers import ParseRepository, ParserRegistry, ParseService, PdfParser
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


@dataclass(frozen=True)
class _Harness:
    paths: RuntimePaths
    database: Database
    domain: DomainRepository
    resources: ResourceRepository
    store: ResourceStore
    search: SearchService


def _pdf(*pages: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    for text in pages:
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    return output.getvalue()


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "synthetic-private-runtime")
    database = Database(paths.database)
    domain = DomainRepository(database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() == 6
    return _Harness(paths, database, domain, resources, store, SearchService(database))


def _course(
    harness: _Harness,
    key: str,
    *,
    code: str,
    title: str,
) -> tuple[CourseId, ContentId]:
    course_id = CourseId("synthetic", f"course-{key}")
    content_id = ContentId("synthetic", f"content-{key}")
    harness.domain.put_course(course_id, code=code, title=title)
    harness.domain.put_content_node(
        content_id,
        course_id=course_id,
        handler_kind="document",
        title=f"Invented {key} materials",
        position=0,
    )
    return course_id, content_id


def _sync_run(database: Database, minute: int) -> int:
    timestamp = datetime(2027, 2, 1, 0, minute, tzinfo=UTC).isoformat()
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
    content_id: ContentId,
    *,
    attachment_key: str,
    pages: tuple[str, ...],
    minute: int,
    title: str = "Invented Search Notes",
    filename: str = "invented-search-notes.pdf",
):
    observed_at = datetime(2027, 2, 1, 0, minute, tzinfo=UTC)
    written = harness.store.ingest(
        io.BytesIO(_pdf(*pages)),
        AttachmentId("synthetic", attachment_key),
        content_id=content_id,
        sync_run_key=_sync_run(harness.database, minute),
        display_title=title,
        original_filename=filename,
        declared_mime="application/pdf",
        observed_at=observed_at,
    )
    parsed = ParseService(
        harness.paths,
        harness.resources,
        ParseRepository(harness.database),
        ParserRegistry((PdfParser(),)),
    ).parse_version(written.version.key)
    assert parsed.chunks
    return written, parsed


def _chunk_filters(
    *,
    course: CourseId | None = None,
    version_key: int | None = None,
    include_historical_versions: bool = True,
) -> SearchFilters:
    return SearchFilters(
        course=course,
        entity_kinds=frozenset({SearchEntityKind.CHUNK}),
        text_origins=frozenset({SearchTextOrigin.NATIVE}),
        version_key=version_key,
        include_historical_versions=include_historical_versions,
    )


def test_relational_and_fts_refresh_roll_back_in_the_same_transaction(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    course_id, content_id = _course(
        harness,
        "rollback",
        code="RB1000",
        title="Stable Rollback Course",
    )
    _ingest_and_parse(
        harness,
        content_id,
        attachment_key="rollback-document",
        pages=("Stable parsed evidence remains available after a synthetic rollback.",),
        minute=1,
    )
    course = harness.domain.get_course(course_id)
    assert course is not None
    assert harness.search.search(SearchQuery("Stable Rollback")).items

    with pytest.raises(RuntimeError, match="abort synthetic transaction"):
        with harness.database.transaction() as connection:
            connection.execute(
                "UPDATE course SET title = ? WHERE course_key = ?",
                ("Transient Rollback Course", course.key),
            )
            SearchIndex.refresh_dirty(connection)
            in_transaction = connection.execute(
                "SELECT count(*) FROM search_document_fts WHERE search_document_fts MATCH ?",
                ('"transient"',),
            ).fetchone()
            assert in_transaction is not None and int(in_transaction[0]) > 0
            raise RuntimeError("abort synthetic transaction")

    stable = harness.search.search(SearchQuery("Stable Rollback"))
    transient = harness.search.search(SearchQuery("Transient Rollback"))
    assert stable.items
    assert not transient.items
    assert harness.domain.get_course(course_id).title == "Stable Rollback Course"  # type: ignore[union-attr]
    assert SearchIndex(harness.database).is_current()


def test_rebuild_preserves_search_evidence_and_exact_source_locator(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    course_id, content_id = _course(
        harness,
        "rebuild",
        code="RE2000",
        title="Invented Rebuild Course",
    )
    written, parsed = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="rebuild-document",
        pages=(
            "Neighbor before the exact locator with enough synthetic searchable text.",
            "Rebuildlocatorbeacon marks the exact middle page for stable provenance.",
            "Neighbor after the exact locator with enough synthetic searchable text.",
        ),
        minute=2,
    )
    query = SearchQuery(
        "Rebuildlocatorbeacon",
        _chunk_filters(course=course_id),
        neighbor_count=1,
    )
    before = harness.search.search(query)
    assert len(before.items) == 1
    before_hit = before.items[0]
    before_source = harness.search.resolve_source(before_hit.source, context_window=1)

    rebuilt = SearchIndex(harness.database).rebuild()
    after = harness.search.search(query)
    assert len(after.items) == 1
    after_hit = after.items[0]
    after_source = harness.search.resolve_source(after_hit.source, context_window=1)

    assert rebuilt.document_count == rebuilt.fts_count
    assert after_hit.entity_kind is before_hit.entity_kind
    assert after_hit.entity_key == before_hit.entity_key == parsed.chunks[1].key
    assert after_hit.version_key == before_hit.version_key == written.version.key
    assert after_hit.chunk_key == before_hit.chunk_key == parsed.chunks[1].key
    assert after_hit.source == before_hit.source
    assert after_hit.source.kind is SourceReferenceKind.SOURCE_LOCATOR
    assert after_hit.locator == before_hit.locator == parsed.chunks[1].locator
    assert after_hit.matching_text == before_hit.matching_text
    assert after_source == before_source


def test_historical_version_queries_never_mix_current_version_evidence(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    course_id, content_id = _course(
        harness,
        "history",
        code="HI3000",
        title="Invented History Course",
    )
    first, first_parse = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="versioned-document",
        pages=(
            "Legacyversionbeacon belongs only to immutable synthetic version one evidence.",
            "Legacy neighbor belongs only to immutable synthetic version one evidence.",
        ),
        minute=3,
        filename="version-one.pdf",
    )
    second, second_parse = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="versioned-document",
        pages=(
            "Currentversionbeacon belongs only to immutable synthetic version two evidence.",
            "Current neighbor belongs only to immutable synthetic version two evidence.",
        ),
        minute=4,
        filename="version-two.pdf",
    )

    historical = harness.search.search(
        SearchQuery(
            "Legacyversionbeacon",
            _chunk_filters(course=course_id, version_key=first.version.key),
            neighbor_count=1,
        )
    )
    old_excluded = harness.search.search(
        SearchQuery(
            "Legacyversionbeacon",
            _chunk_filters(course=course_id, include_historical_versions=False),
        )
    )
    current = harness.search.search(
        SearchQuery(
            "Currentversionbeacon",
            _chunk_filters(course=course_id, include_historical_versions=False),
            neighbor_count=1,
        )
    )

    assert [hit.version_key for hit in historical.items] == [first.version.key]
    assert [chunk.key for chunk in historical.items[0].neighbors] == [
        chunk.key for chunk in first_parse.chunks
    ]
    assert not (
        {chunk.key for chunk in historical.items[0].neighbors}
        & {chunk.key for chunk in second_parse.chunks}
    )
    assert not old_excluded.items
    assert [hit.version_key for hit in current.items] == [second.version.key]
    assert [chunk.key for chunk in current.items[0].neighbors] == [
        chunk.key for chunk in second_parse.chunks
    ]
    resolved_old = harness.search.resolve_source(historical.items[0].source, context_window=1)
    assert resolved_old.version_key == first.version.key
    assert [chunk.key for chunk in resolved_old.chunks] == [
        chunk.key for chunk in first_parse.chunks
    ]


def test_search_hydrates_only_bounded_neighbors_after_originals_are_removed(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    course_id, content_id = _course(
        harness,
        "offline",
        code="OF4000",
        title="Invented Offline Course",
    )
    written, parsed = _ingest_and_parse(
        harness,
        content_id,
        attachment_key="offline-document",
        pages=(
            "Page zero contains only invented boundary context and sufficient native text.",
            "Page one contains only invented preceding context and sufficient native text.",
            "Centralhydrationtoken appears only on invented page two for exact retrieval.",
            "Page three contains only invented following context and sufficient native text.",
            "Page four contains only invented boundary context and sufficient native text.",
        ),
        minute=5,
    )
    blob_path = harness.paths.root / written.version.blob_relpath
    version_path = harness.paths.root / written.version.browse_relpath
    current_path = version_path.parent.parent / "current"
    blob_path.unlink()
    version_path.unlink()
    current_path.unlink()
    assert not blob_path.exists()
    assert not version_path.exists()
    assert not current_path.exists()

    result = harness.search.search(
        SearchQuery(
            "Centralhydrationtoken",
            _chunk_filters(course=course_id),
            neighbor_count=1,
        )
    )
    assert len(result.items) == 1
    hit = result.items[0]
    assert hit.chunk_key == parsed.chunks[2].key
    assert [chunk.ordinal for chunk in hit.neighbors] == [1, 2, 3]
    assert len(hit.neighbors) == 3
    resolved = harness.search.resolve_source(hit.source, context_window=1)
    assert [chunk.ordinal for chunk in resolved.chunks] == [1, 2, 3]
    exact = harness.search.resolve_source(hit.source, context_window=0)
    assert [chunk.ordinal for chunk in exact.chunks] == [2]


def test_unicode_punctuation_and_cross_course_filters_are_literal_and_bounded(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    first_course, first_content = _course(
        harness,
        "unicode",
        code="UC5000",
        title="量子计算",
    )
    second_course, second_content = _course(
        harness,
        "other",
        code="OC6000",
        title="Invented Other Course",
    )
    _ingest_and_parse(
        harness,
        first_content,
        attachment_key="unicode-document",
        pages=("A résumé naïve punctuation example carries sharedcoursetoken in course one.",),
        minute=6,
    )
    _ingest_and_parse(
        harness,
        second_content,
        attachment_key="other-document",
        pages=("Sharedcoursetoken appears in course two with enough synthetic text.",),
        minute=7,
    )

    unicode_result = harness.search.search(
        SearchQuery(
            "量子计算",
            SearchFilters(entity_kinds=frozenset({SearchEntityKind.COURSE})),
            limit=1,
            neighbor_count=0,
        )
    )
    punctuated = harness.search.search(
        SearchQuery(
            "résumé / naïve",
            _chunk_filters(course=first_course),
            limit=1,
            neighbor_count=0,
        )
    )
    cross_course = harness.search.search(
        SearchQuery(
            "sharedcoursetoken",
            _chunk_filters(course=second_course),
            limit=1,
            neighbor_count=0,
        )
    )

    assert [hit.course for hit in unicode_result.items] == [first_course]
    assert len(punctuated.items) == 1
    assert punctuated.items[0].course == first_course
    assert [chunk.ordinal for chunk in punctuated.items[0].neighbors] == [0]
    assert len(cross_course.items) == 1
    assert cross_course.items[0].course == second_course
    assert cross_course.items[0].course != first_course
