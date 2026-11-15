"""Optional local semantic retrieval; shared filters and original source references.

No model is downloaded by this module. The core lexical dependency footprint is
unchanged. An optional private cache is refreshed when scoped text or the model changes.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import replace
from pathlib import Path
from threading import RLock
from typing import Any, Protocol

from ntulearn_skill.search.models import SearchQuery, SearchResult
from ntulearn_skill.search.service import SearchError, SearchService
from ntulearn_skill.storage import Database


class SentenceEncoder(Protocol):
    fingerprint: str

    def encode(self, texts: list[str], *, query: bool = False) -> Any: ...


class LocalBgeEncoder:
    """BGE v1.5 encoder loaded from an explicitly provisioned local directory."""

    def __init__(self, path: str | Path, *, device: str | None = None) -> None:
        path = Path(path).expanduser().resolve(strict=True)
        if not path.is_dir() or not (path / "config.json").is_file():
            raise ValueError("a local embedding model directory is required")
        digest = hashlib.sha256()
        for file in sorted(path.rglob("*")):
            if file.is_file() and file.suffix in {".safetensors", ".json", ".txt"}:
                digest.update(str(file.relative_to(path)).encode())
                with file.open("rb") as stream:
                    while block := stream.read(1024 * 1024):
                        digest.update(block)
        self.fingerprint = "bge-v1.5:" + digest.hexdigest()
        try:
            library = importlib.import_module("sentence_transformers")
            self.model = library.SentenceTransformer(
                str(path), device=device, local_files_only=True, trust_remote_code=False
            )
        except (ImportError, OSError):
            raise SearchError("local embedding model is unavailable") from None

    def encode(self, texts: list[str], *, query: bool = False) -> Any:
        if query:
            texts = ["Represent this sentence for searching relevant passages: " + t for t in texts]
        return self.model.encode(
            texts, batch_size=32, normalize_embeddings=True, show_progress_bar=False
        )


class HybridSearchService(SearchService):
    """Fuse 100 BM25 and 100 dense candidates using RRF(k=60).

    Exact (`all`) queries keep lexical semantics. `any` enables hybrid candidates;
    their scores are rankings, not probabilities that an answer exists. This
    bounded local implementation supports up to 50,000 documents per scope.
    """

    def __init__(
        self, database: Database, encoder: SentenceEncoder, *, cache_path: Path | None = None
    ) -> None:
        super().__init__(database)
        self.encoder = encoder
        self.ranking_profile = "hybrid-rrf60-depth100-v1:" + encoder.fingerprint
        self._lock = RLock()
        self._cached_signature = ""
        self._vectors: Any = None
        self._numpy = importlib.import_module("numpy")
        self._cache_path = cache_path

    def _document_vectors(self, signature: str, texts: list[str]) -> Any:
        if self._cache_path is not None and self._cache_path.is_file():
            try:
                with self._numpy.load(self._cache_path, allow_pickle=False) as saved:
                    if str(saved["signature"]) == signature:
                        vectors = saved["vectors"]
                        if vectors.ndim == 2 and vectors.shape[0] == len(texts):
                            if self._numpy.isfinite(vectors).all():
                                return vectors
            except (OSError, ValueError, KeyError):
                pass  # A missing/stale/corrupt cache is rebuilt from owning text.
        vectors = self._numpy.asarray(self.encoder.encode(texts), dtype="float32")
        if vectors.ndim != 2 or vectors.shape[0] != len(texts):
            raise SearchError("local encoder returned an invalid document matrix")
        if not self._numpy.isfinite(vectors).all():
            raise SearchError("local encoder returned non-finite document vectors")
        if self._cache_path is not None:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(dir=self._cache_path.parent, prefix=".vectors-")
            try:
                with os.fdopen(fd, "wb") as output:
                    self._numpy.savez_compressed(output, signature=signature, vectors=vectors)
                os.replace(name, self._cache_path)
            finally:
                Path(name).unlink(missing_ok=True)
        return vectors

    def search(self, query: SearchQuery, *, offset: int = 0) -> SearchResult:
        if query.match_mode == "all":
            return super().search(query, offset=offset)
        if query.cursor is not None or isinstance(offset, bool) or offset < 0:
            raise ValueError("hybrid search requires a non-negative offset and no cursor")
        # Lexical search also validates the query and obtains coverage/freshness.
        lexical = super().search(replace(query, limit=100, neighbor_count=0))
        with self._lock:
            with self.database.transaction() as connection:
                self.index.ensure_current(connection)
                course = self._course_key(connection, query.filters.course)
                rows = self._matched_rows(connection, query, None, course, 0, row_limit=50_001)
            if len(rows) > 50_000:
                raise SearchError("hybrid scope exceeds the local embedding bound")
            if not rows:
                return replace(lexical, items=(), truncated=False)
            # Stable keys tie-break both channels and preserve page ordering.
            rows = tuple(sorted(rows, key=lambda r: int(r["search_document_key"])))
            keys = [int(r["search_document_key"]) for r in rows]
            texts = [str(r["title"]) + ". " + str(r["body"]) for r in rows]
            signature = hashlib.sha256(
                (
                    self.encoder.fingerprint
                    + json.dumps(list(zip(keys, texts)), ensure_ascii=False)
                ).encode()
            ).hexdigest()
            if signature != self._cached_signature:
                self._vectors = self._document_vectors(signature, texts)
                self._cached_signature = signature
            vector = self._numpy.asarray(
                self.encoder.encode([query.text], query=True), dtype="float32"
            )
            if vector.shape != (1, self._vectors.shape[1]):
                raise SearchError("local encoder returned an invalid query vector")
            if not self._numpy.isfinite(vector).all():
                raise SearchError("local encoder returned a non-finite query vector")
            scores = self._vectors @ vector[0]
            dense = [keys[int(i)] for i in self._numpy.argsort(-scores, kind="stable")[:100]]
            fused: dict[int, float] = defaultdict(float)
            for ranking in ([h.search_document_key for h in lexical.items], dense):
                for position, key in enumerate(ranking, 1):
                    fused[key] += 1 / (60 + position)
            ranking = sorted(fused, key=lambda key: (-fused[key], key))[:100]
            selected = ranking[offset : offset + query.limit]
            by_key = {int(r["search_document_key"]): r for r in rows}
            chunk_keys = tuple(
                int(by_key[key]["chunk_key"])
                for key in selected
                if by_key[key]["chunk_key"] is not None
            )
            windows = {
                w.match_chunk_key: w.chunks
                for w in self.parses.hydrate_matches(
                    chunk_keys, neighbor_count=query.neighbor_count
                )
            }
            hits = tuple(
                replace(
                    self._hit(
                        by_key[key],
                        windows.get(int(by_key[key]["chunk_key"]), ())
                        if by_key[key]["chunk_key"] is not None
                        else (),
                    ),
                    rank=-fused[key],
                )
                for key in selected
            )
        return replace(
            lexical,
            items=hits,
            truncated=offset + query.limit < len(ranking),
            message=f"Found {len(hits)} hybrid candidate(s); verify relevance in the cited source.",
        )
