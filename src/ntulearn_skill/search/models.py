"""Typed requests and provenance-bearing deterministic search results."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from ntulearn_skill.core import Availability, CourseId, Coverage
from ntulearn_skill.extractors import SemanticType
from ntulearn_skill.parsers import DocumentChunkRecord, JsonValue, VisualReviewStatus


class SearchEntityKind(StrEnum):
    COURSE = "course"
    CONTENT = "content"
    MATERIAL = "material"
    CHUNK = "chunk"
    ANNOUNCEMENT = "announcement"
    ASSESSMENT = "assessment"
    EVENT = "event"
    CLAIM = "claim"


class SearchTextOrigin(StrEnum):
    METADATA = "metadata"
    NATIVE = "native"
    DERIVED = "derived"


class SourceReferenceKind(StrEnum):
    SOURCE_OBJECT = "source_object"
    SOURCE_OBSERVATION = "source_observation"
    RESOURCE_OBSERVATION = "resource_observation"
    SOURCE_LOCATOR = "source_locator"


@dataclass(frozen=True, slots=True)
class SourceReference:
    kind: SourceReferenceKind
    key: int

    def __post_init__(self) -> None:
        if self.key <= 0:
            raise ValueError("source reference key must be positive")


@dataclass(frozen=True, slots=True)
class SearchFilters:
    course: CourseId | None = None
    entity_kinds: frozenset[SearchEntityKind] = field(
        default_factory=lambda: frozenset(SearchEntityKind)
    )
    semantic_types: frozenset[SemanticType] = field(default_factory=frozenset)
    file_formats: frozenset[str] = field(default_factory=frozenset)
    availabilities: frozenset[Availability] = field(default_factory=frozenset)
    text_origins: frozenset[SearchTextOrigin] = field(default_factory=frozenset)
    version_key: int | None = None
    minimum_classification_confidence: float | None = None
    include_historical_versions: bool = True

    def __post_init__(self) -> None:
        if self.course is not None and not isinstance(self.course, CourseId):
            raise TypeError("course filter must be a CourseId")
        if not self.entity_kinds:
            raise ValueError("at least one entity kind is required")
        if any(not isinstance(value, SearchEntityKind) for value in self.entity_kinds):
            raise TypeError("entity kind filters must be typed")
        if any(not isinstance(value, SemanticType) for value in self.semantic_types):
            raise TypeError("semantic type filters must be typed")
        if any(not isinstance(value, Availability) for value in self.availabilities):
            raise TypeError("availability filters must be typed")
        if any(not isinstance(value, SearchTextOrigin) for value in self.text_origins):
            raise TypeError("text origin filters must be typed")
        if any(value not in {"pdf", "docx", "pptx", "unknown"} for value in self.file_formats):
            raise ValueError("file format filter is invalid")
        if self.version_key is not None and self.version_key <= 0:
            raise ValueError("version key must be positive")
        confidence = self.minimum_classification_confidence
        if confidence is not None and not 0.0 <= confidence <= 1.0:
            raise ValueError("classification confidence filter must be between zero and one")


@dataclass(frozen=True, slots=True)
class SearchQuery:
    text: str
    filters: SearchFilters = field(default_factory=SearchFilters)
    limit: int = 20
    neighbor_count: int = 1
    cursor: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.filters, SearchFilters):
            raise TypeError("search filters must be typed")
        if not self.text.strip():
            raise ValueError("search text must be non-empty")
        if len(self.text) > 1_000:
            raise ValueError("search text exceeds the configured bound")
        if not 1 <= self.limit <= 100:
            raise ValueError("search limit must be between 1 and 100")
        if not 0 <= self.neighbor_count <= 5:
            raise ValueError("neighbor count must be between 0 and 5")
        if self.cursor is not None and (
            not isinstance(self.cursor, str)
            or not self.cursor
            or not self.cursor.isascii()
            or len(self.cursor) > 2_048
        ):
            raise ValueError("search cursor must be bounded ASCII text")


@dataclass(frozen=True, slots=True)
class SearchHit:
    search_document_key: int
    entity_kind: SearchEntityKind
    entity_key: int
    rank: float
    course: CourseId
    course_code: str
    course_title: str
    content_title: str
    title: str
    filename: str
    semantic_type: SemanticType | None
    classification_confidence: float | None
    file_format: str | None
    availability: Availability
    version_key: int | None
    chunk_key: int | None
    text_origin: str
    parse_coverage: Coverage | None
    source: SourceReference
    locator: dict[str, JsonValue] | None
    matching_text: str
    neighbors: tuple[DocumentChunkRecord, ...]


@dataclass(frozen=True, slots=True)
class CoverageView:
    course: CourseId
    data_kind: str
    coverage: Coverage
    observed_at: datetime | None
    evidence: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    items: tuple[SearchHit, ...]
    coverage: tuple[CoverageView, ...]
    completeness: Coverage
    as_of: datetime | None
    warnings: tuple[str, ...]
    message: str
    truncated: bool = False

    @property
    def conclusive_empty(self) -> bool:
        return not self.items and self.completeness is Coverage.COMPLETE


@dataclass(frozen=True, slots=True)
class SourceVisualEvidence:
    representation_key: int
    chunk_key: int
    text: str
    method: str
    provider: str | None
    engine_version: str
    settings_hash: str
    confidence: float | None
    diagnostic_reason: str
    method_version: str
    settings: dict[str, JsonValue]
    review_status: VisualReviewStatus
    uncertainty: tuple[str, ...]
    version_key: int
    source_sha256: str
    source_page_index: int
    rendered_sha256: str
    rendered_representation_key: int | None
    renderer_method: str | None
    renderer_engine_version: str | None
    render_settings_hash: str | None
    render_method_version: str | None
    render_settings: dict[str, JsonValue] | None
    source_locator: dict[str, JsonValue]
    is_current: bool


@dataclass(frozen=True, slots=True)
class ResolvedSource:
    reference: SourceReference
    provider: str
    object_kind: str
    remote_key: str
    version_key: int | None
    locator: dict[str, JsonValue] | None
    chunks: tuple[DocumentChunkRecord, ...]
    observation: dict[str, JsonValue] | None = None
    visual_evidence: tuple[SourceVisualEvidence, ...] = ()
