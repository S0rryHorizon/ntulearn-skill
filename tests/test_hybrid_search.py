from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from test_search import _harness, _ingest_and_parse

from ntulearn_skill.core import ContentId, CourseId
from ntulearn_skill.core.api import CoreService
from ntulearn_skill.search import SearchEntityKind, SearchFilters, SearchQuery, SearchService
from ntulearn_skill.search.hybrid import HybridSearchService


class Encoder:
    fingerprint = "synthetic-test-encoder-v1"

    def __init__(self):
        self.document_calls = 0

    def encode(self, texts, *, query=False):
        self.document_calls += not query
        vectors = []
        for text in texts:
            light = any(word in text.lower() for word in ("photon", "light packet"))
            vectors.append([1.0, 0.0] if light else [0.0, 1.0])
        return np.asarray(vectors)


def fixtures(tmp_path):
    harness = _harness(tmp_path)
    _ingest_and_parse(
        harness,
        remote_key="photons",
        title="Synthetic radiation",
        filename="radiation.pdf",
        pages=("A photon carries radiation energy.",),
        offset=1,
    )
    _ingest_and_parse(
        harness,
        remote_key="mineral",
        title="Synthetic geology",
        filename="geology.pdf",
        pages=("Granite contains several minerals.",),
        offset=2,
    )
    return harness


def query(harness, text="light packet"):
    return SearchQuery(
        text,
        SearchFilters(
            course=harness.course_id,
            entity_kinds=frozenset({SearchEntityKind.CHUNK}),
            include_historical_versions=False,
        ),
        limit=5,
        match_mode="any",
    )


def test_any_mode_does_not_change_default_exact_keyword_semantics(tmp_path):
    harness = fixtures(tmp_path)
    service = SearchService(harness.database)
    assert not service.search(SearchQuery("photon missingterm")).items
    assert service.search(SearchQuery("photon missingterm", match_mode="any")).items
    with pytest.raises(ValueError):
        SearchQuery("photon", match_mode="unsafe operator")


def test_hybrid_uses_filtered_sources_and_resolves_original_page(tmp_path):
    harness = fixtures(tmp_path)
    other_course = CourseId("synthetic", "other-course")
    other_content = ContentId("synthetic", "other-content")
    harness.domain.put_course(other_course, code="OTHER", title="Unrelated course")
    harness.domain.put_content_node(
        other_content,
        course_id=other_course,
        handler_kind="document",
        title="Other materials",
        position=0,
    )
    other = replace(harness, course_id=other_course, content_id=other_content)
    _ingest_and_parse(
        other,
        remote_key="other-photons",
        title="Other photons",
        filename="other.pdf",
        pages=("A photon is described in another course.",),
        offset=3,
    )
    service = HybridSearchService(harness.database, Encoder())
    result = service.search(query(harness))
    assert "photon" in result.items[0].matching_text
    assert all(hit.course == harness.course_id for hit in result.items)
    source = service.resolve_source(result.items[0].source)
    assert source.version_key == result.items[0].version_key
    assert source.locator == result.items[0].locator
    assert source.chunks and "photon" in source.chunks[0].native_text
    assert not service.search(replace(query(harness), match_mode="all")).items


def test_cache_reuses_vectors_and_invalidates_changed_version(tmp_path):
    harness = fixtures(tmp_path)
    encoder = Encoder()
    cache = tmp_path / "private-vectors.npz"
    service = HybridSearchService(harness.database, encoder, cache_path=cache)
    first = service.search(query(harness))
    old = first.items[0].version_key
    service.search(query(harness))
    assert encoder.document_calls == 1
    new_encoder = Encoder()
    new_service = HybridSearchService(harness.database, new_encoder, cache_path=cache)
    new_service.search(query(harness))
    assert new_encoder.document_calls == 0
    _ingest_and_parse(
        harness,
        remote_key="photons",
        title="Synthetic radiation",
        filename="radiation.pdf",
        pages=("A photon carries updated radiation evidence.",),
        offset=4,
    )
    current = new_service.search(query(harness))
    assert new_encoder.document_calls == 1
    assert all(hit.version_key != old for hit in current.items)
    historical = new_service.search(
        replace(
            query(harness),
            filters=replace(query(harness).filters, include_historical_versions=True),
        )
    )
    assert any(hit.version_key == old for hit in historical.items)
    cache.write_bytes(b"corrupt synthetic cache")
    recovered = HybridSearchService(harness.database, Encoder(), cache_path=cache)
    assert recovered.search(query(harness)).items


def test_cursor_binds_ranking_profile(tmp_path: Path):
    harness = fixtures(tmp_path)
    core = CoreService(harness.database)
    core.search_service = HybridSearchService(harness.database, Encoder())
    request = replace(query(harness), limit=1)
    first = core.search(request)
    assert first.next_cursor
    changed = replace(request, cursor=first.next_cursor, match_mode="all")
    assert core.search(changed).errors
    core.search_service = SearchService(harness.database)
    assert core.search(replace(request, cursor=first.next_cursor)).errors
