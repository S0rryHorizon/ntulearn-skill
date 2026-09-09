"""Course resource parser boundary."""

from ntulearn_skill.parsers.docx import DocxParser
from ntulearn_skill.parsers.models import (
    ChunkDiagnostic,
    ChunkKind,
    ChunkRepresentationRecord,
    DocumentChunkRecord,
    FallbackOutput,
    FallbackRequest,
    HydratedChunkWindow,
    JsonValue,
    ParsedChunk,
    ParsedDocumentRecord,
    ParsedPayload,
    ParserDescriptor,
    ParseResult,
    ParserInputRejected,
    ParserLimits,
    ParserOptions,
    ParseStatus,
    RepresentationKind,
    SelectiveFallback,
)
from ntulearn_skill.parsers.pdf import PdfParser
from ntulearn_skill.parsers.registry import ParserRegistry
from ntulearn_skill.parsers.repository import ParseRepository, ParseStorageError
from ntulearn_skill.parsers.service import ParseOperationError, ParseService

__all__ = [
    "ChunkDiagnostic",
    "ChunkKind",
    "ChunkRepresentationRecord",
    "DocumentChunkRecord",
    "DocxParser",
    "FallbackOutput",
    "FallbackRequest",
    "HydratedChunkWindow",
    "JsonValue",
    "ParsedChunk",
    "ParsedDocumentRecord",
    "ParsedPayload",
    "ParseOperationError",
    "ParseRepository",
    "ParseResult",
    "ParseService",
    "ParseStatus",
    "ParseStorageError",
    "ParserInputRejected",
    "ParserDescriptor",
    "ParserLimits",
    "ParserOptions",
    "ParserRegistry",
    "PdfParser",
    "RepresentationKind",
    "SelectiveFallback",
]
