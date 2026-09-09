"""Versioned parser contracts and immutable parsed-document values."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol, TypeAlias

from ntulearn_skill.core.models import Coverage
from ntulearn_skill.storage.resources import ResourceVersionRecord

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ParseStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNSUPPORTED = "UNSUPPORTED"


class ChunkKind(StrEnum):
    PAGE = "page"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    OTHER = "other"


class RepresentationKind(StrEnum):
    OCR_TEXT = "ocr_text"
    VISION_DESCRIPTION = "vision_description"
    RENDERED_DERIVATIVE = "rendered_derivative"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class ParserLimits:
    maximum_input_bytes: int = 64 * 1024 * 1024
    maximum_chunks: int = 2_000
    maximum_chunk_characters: int = 2 * 1024 * 1024
    maximum_total_characters: int = 32 * 1024 * 1024
    maximum_archive_entries: int = 4_096
    maximum_archive_uncompressed_bytes: int = 128 * 1024 * 1024
    maximum_content_stream_bytes: int = 16 * 1024 * 1024
    maximum_structured_elements: int = 100_000
    maximum_nesting_depth: int = 16

    def __post_init__(self) -> None:
        if any(
            value <= 0
            for value in (
                self.maximum_input_bytes,
                self.maximum_chunks,
                self.maximum_chunk_characters,
                self.maximum_total_characters,
                self.maximum_archive_entries,
                self.maximum_archive_uncompressed_bytes,
                self.maximum_content_stream_bytes,
                self.maximum_structured_elements,
                self.maximum_nesting_depth,
            )
        ):
            raise ValueError("parser limits must be positive")

    def as_settings(self) -> dict[str, int]:
        return {
            "maximum_archive_entries": self.maximum_archive_entries,
            "maximum_archive_uncompressed_bytes": self.maximum_archive_uncompressed_bytes,
            "maximum_chunk_characters": self.maximum_chunk_characters,
            "maximum_chunks": self.maximum_chunks,
            "maximum_content_stream_bytes": self.maximum_content_stream_bytes,
            "maximum_input_bytes": self.maximum_input_bytes,
            "maximum_nesting_depth": self.maximum_nesting_depth,
            "maximum_structured_elements": self.maximum_structured_elements,
            "maximum_total_characters": self.maximum_total_characters,
        }


@dataclass(frozen=True, slots=True)
class ParserOptions:
    limits: ParserLimits = field(default_factory=ParserLimits)
    low_text_character_threshold: int = 48
    drawing_operator_threshold: int = 12

    def __post_init__(self) -> None:
        if self.low_text_character_threshold < 0:
            raise ValueError("low-text threshold cannot be negative")
        if self.drawing_operator_threshold < 0:
            raise ValueError("drawing-operator threshold cannot be negative")

    def canonical_settings(self) -> dict[str, JsonValue]:
        limit_settings: dict[str, JsonValue] = {
            key: value for key, value in self.limits.as_settings().items()
        }
        return {
            "diagnostics": {
                "drawing_operator_threshold": self.drawing_operator_threshold,
                "low_text_character_threshold": self.low_text_character_threshold,
                "version": "stage-b-1",
            },
            "limits": limit_settings,
        }

    @property
    def settings_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_settings(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChunkDiagnostic:
    detector_version: str
    native_character_count: int
    content_stream_bytes: int | None
    image_count: int
    form_xobject_count: int
    drawing_operator_count: int
    reasons: tuple[str, ...]
    fallback_recommended: bool

    def as_json(self) -> dict[str, JsonValue]:
        return {
            "content_stream_bytes": self.content_stream_bytes,
            "detector_version": self.detector_version,
            "drawing_operator_count": self.drawing_operator_count,
            "fallback_recommended": self.fallback_recommended,
            "image_count": self.image_count,
            "form_xobject_count": self.form_xobject_count,
            "native_character_count": self.native_character_count,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True, slots=True)
class ParsedChunk:
    ordinal: int
    kind: ChunkKind
    native_text: str
    locator: dict[str, JsonValue]
    structured_elements: tuple[dict[str, JsonValue], ...] = ()
    diagnostic: ChunkDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class ParsedPayload:
    status: ParseStatus
    coverage: Coverage
    chunks: tuple[ParsedChunk, ...]
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ParserDescriptor:
    name: str
    version: str
    engine_version: str
    formats: frozenset[str]


class DocumentParser(Protocol):
    descriptor: ParserDescriptor

    def parse(
        self, path: Path, version: ResourceVersionRecord, options: ParserOptions
    ) -> ParsedPayload:
        """Parse exact immutable bytes into bounded chunks."""


@dataclass(frozen=True, slots=True)
class ParsedDocumentRecord:
    key: int
    version_key: int
    resource_sha256: str
    parser_name: str
    parser_version: str
    engine_version: str
    settings_hash: str
    status: ParseStatus
    coverage: Coverage
    warning_codes: tuple[str, ...]
    error_code: str | None


@dataclass(frozen=True, slots=True)
class DocumentChunkRecord:
    key: int
    locator_key: int
    parse_key: int
    ordinal: int
    kind: ChunkKind
    native_text: str
    locator: dict[str, JsonValue]
    structured_elements: tuple[dict[str, JsonValue], ...]
    diagnostic: ChunkDiagnostic | None


@dataclass(frozen=True, slots=True)
class ParseResult:
    document: ParsedDocumentRecord
    chunks: tuple[DocumentChunkRecord, ...]
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class FallbackRequest:
    source_path: Path
    version: ResourceVersionRecord
    chunk: DocumentChunkRecord


@dataclass(frozen=True, slots=True)
class FallbackOutput:
    representation_kind: RepresentationKind
    text: str | None
    artifact_relpath: str | None
    method: str
    provider: str | None
    engine_version: str
    settings_hash: str
    confidence: float | None

    def __post_init__(self) -> None:
        if self.text is None and self.artifact_relpath is None:
            raise ValueError("fallback output must contain text or an artifact")
        if self.confidence is not None and not 0.0 <= self.confidence <= 1.0:
            raise ValueError("fallback confidence must be between zero and one")


class SelectiveFallback(Protocol):
    def represent(self, request: FallbackRequest) -> FallbackOutput | None:
        """Return one derived representation for a flagged chunk, or skip it."""


@dataclass(frozen=True, slots=True)
class ChunkRepresentationRecord:
    key: int
    chunk_key: int
    representation_kind: RepresentationKind
    text: str | None
    artifact_relpath: str | None
    method: str
    provider: str | None
    engine_version: str
    settings_hash: str
    confidence: float | None
    diagnostic_reason: str


@dataclass(frozen=True, slots=True)
class HydratedChunkWindow:
    match_chunk_key: int
    chunks: tuple[DocumentChunkRecord, ...]


class ParserError(RuntimeError):
    """Privacy-safe parser error."""


class ParserInputRejected(ParserError):
    """The local input violates a parser safety bound."""
