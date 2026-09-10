"""Transactional terminal parse results, locators, and local chunk hydration."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import PurePosixPath

from ntulearn_skill.core.models import Coverage, to_storage_time, utc_now
from ntulearn_skill.parsers.models import (
    ChunkDiagnostic,
    ChunkKind,
    ChunkRepresentationRecord,
    DocumentChunkRecord,
    FallbackOutput,
    HydratedChunkWindow,
    JsonValue,
    ParsedDocumentRecord,
    ParsedPayload,
    ParserDescriptor,
    ParseStatus,
    RepresentationKind,
    VisualEvidenceMetadata,
    VisualEvidenceRecord,
    VisualReviewStatus,
)
from ntulearn_skill.storage.database import Database, StorageError
from ntulearn_skill.storage.resources import ResourceVersionRecord


class ParseStorageError(StorageError):
    """A privacy-safe parser persistence error."""


def _json(value: JsonValue) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _warning_json(warnings: tuple[str, ...]) -> str:
    if len(warnings) > 100 or any(len(item) > 120 for item in warnings):
        raise ValueError("parser warning report exceeds configured limits")
    return _json(list(warnings))


@dataclass(frozen=True, slots=True)
class StoredParse:
    document: ParsedDocumentRecord
    chunks: tuple[DocumentChunkRecord, ...]


class ParseRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_cached(
        self,
        version_key: int,
        descriptor: ParserDescriptor,
        settings_hash: str,
    ) -> StoredParse | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM parsed_document
                WHERE version_key = ? AND parser_name = ? AND parser_version = ?
                  AND engine_version = ? AND settings_hash = ? AND status <> 'FAILED'
                """,
                (
                    version_key,
                    descriptor.name,
                    descriptor.version,
                    descriptor.engine_version,
                    settings_hash,
                ),
            ).fetchone()
            if row is None:
                return None
            document = self._document(row)
            return StoredParse(document, self._chunks(connection, document.key))
        except (sqlite3.Error, ValueError, TypeError):
            raise ParseStorageError("parsed document lookup failed") from None
        finally:
            connection.close()

    def store_terminal(
        self,
        version: ResourceVersionRecord,
        descriptor: ParserDescriptor,
        settings_hash: str,
        settings: dict[str, JsonValue],
        payload: ParsedPayload,
        *,
        error_code: str | None = None,
    ) -> StoredParse:
        if payload.status not in set(ParseStatus):
            raise ValueError("parse result must be terminal")
        if payload.status in {ParseStatus.FAILED, ParseStatus.UNSUPPORTED} and payload.chunks:
            raise ValueError("failed or unsupported parses cannot claim chunks")
        expected_ordinals = tuple(range(len(payload.chunks)))
        if tuple(chunk.ordinal for chunk in payload.chunks) != expected_ordinals:
            raise ValueError("parser chunk ordinals must be contiguous")
        if any(len(chunk.native_text) > 2 * 1024 * 1024 for chunk in payload.chunks):
            raise ValueError("parser chunk report exceeds storage limit")
        warning_json = _warning_json(payload.warning_codes)
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO parsed_document(
                        version_key, resource_sha256, parser_name, parser_version,
                        engine_version, settings_hash, settings_json, status, coverage,
                        warning_codes_json, error_code, parsed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version.key,
                        version.sha256,
                        descriptor.name,
                        descriptor.version,
                        descriptor.engine_version,
                        settings_hash,
                        _json(settings),
                        payload.status.value,
                        payload.coverage.value,
                        warning_json,
                        error_code,
                        to_storage_time(utc_now()),
                    ),
                )
                assert cursor.lastrowid is not None
                parse_key = int(cursor.lastrowid)
                for chunk in payload.chunks:
                    cursor = connection.execute(
                        """
                        INSERT INTO document_chunk(
                            parse_key, ordinal, kind, native_text, locator_json,
                            structured_elements_json, diagnostic_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            parse_key,
                            chunk.ordinal,
                            chunk.kind.value,
                            chunk.native_text,
                            _json(chunk.locator),
                            _json(list(chunk.structured_elements)),
                            None if chunk.diagnostic is None else _json(chunk.diagnostic.as_json()),
                        ),
                    )
                    assert cursor.lastrowid is not None
                    chunk_key = int(cursor.lastrowid)
                    self._insert_locator(connection, version.key, chunk_key, chunk.locator)
                document_row = connection.execute(
                    "SELECT * FROM parsed_document WHERE parse_key = ?", (parse_key,)
                ).fetchone()
                assert document_row is not None
                document = self._document(document_row)
                chunks = self._chunks(connection, parse_key)
            return StoredParse(document, chunks)
        except sqlite3.IntegrityError:
            cached = self.find_cached(version.key, descriptor, settings_hash)
            if cached is not None:
                return cached
            raise ParseStorageError("parsed document storage failed") from None
        except (sqlite3.Error, TypeError, ValueError):
            raise ParseStorageError("parsed document storage failed") from None

    @staticmethod
    def _insert_locator(
        connection: sqlite3.Connection,
        version_key: int,
        chunk_key: int,
        locator: dict[str, JsonValue],
    ) -> None:
        locator_format = locator.get("format")
        if locator_format not in {"pdf", "docx"}:
            raise ValueError("unsupported source locator format")
        connection.execute(
            """
            INSERT INTO source_locator(
                version_key, chunk_key, format, physical_page_index, logical_page_label,
                docx_element_index, docx_paragraph_index, docx_table_index, structured_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                version_key,
                chunk_key,
                locator_format,
                locator.get("physical_page_index"),
                locator.get("logical_page_label"),
                locator.get("element_index"),
                locator.get("paragraph_index"),
                locator.get("table_index"),
                _json(locator),
            ),
        )

    def get(self, parse_key: int) -> StoredParse | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM parsed_document WHERE parse_key = ?", (parse_key,)
            ).fetchone()
            if row is None:
                return None
            document = self._document(row)
            return StoredParse(document, self._chunks(connection, parse_key))
        except (sqlite3.Error, ValueError, TypeError):
            raise ParseStorageError("parsed document lookup failed") from None
        finally:
            connection.close()

    def hydrate_matches(
        self,
        chunk_keys: tuple[int, ...],
        *,
        neighbor_count: int = 1,
        maximum_neighbor_count: int = 5,
    ) -> tuple[HydratedChunkWindow, ...]:
        if neighbor_count < 0 or maximum_neighbor_count < 0:
            raise ValueError("neighbor counts cannot be negative")
        if neighbor_count > maximum_neighbor_count:
            raise ValueError("requested neighbor count exceeds the configured bound")
        if len(chunk_keys) > 500:
            raise ValueError("too many chunk matches requested")
        connection = self.database.connect()
        try:
            windows: list[HydratedChunkWindow] = []
            for chunk_key in chunk_keys:
                match = connection.execute(
                    "SELECT parse_key, ordinal FROM document_chunk WHERE chunk_key = ?",
                    (chunk_key,),
                ).fetchone()
                if match is None:
                    continue
                rows = connection.execute(
                    """
                    SELECT c.*, l.locator_key
                    FROM document_chunk c
                    JOIN source_locator l ON l.chunk_key = c.chunk_key
                    WHERE c.parse_key = ? AND c.ordinal BETWEEN ? AND ?
                    ORDER BY c.ordinal
                    """,
                    (
                        int(match["parse_key"]),
                        max(0, int(match["ordinal"]) - neighbor_count),
                        int(match["ordinal"]) + neighbor_count,
                    ),
                ).fetchall()
                windows.append(
                    HydratedChunkWindow(
                        match_chunk_key=chunk_key,
                        chunks=tuple(self._chunk(row) for row in rows),
                    )
                )
            return tuple(windows)
        except (sqlite3.Error, ValueError, TypeError):
            raise ParseStorageError("local chunk hydration failed") from None
        finally:
            connection.close()

    def append_representation(
        self,
        chunk_key: int,
        output: FallbackOutput,
        *,
        diagnostic_reason: str,
    ) -> ChunkRepresentationRecord:
        if output.artifact_relpath is not None:
            path = PurePosixPath(output.artifact_relpath)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("fallback artifact path must be private-root relative")
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO chunk_representation(
                        chunk_key, representation_kind, text, artifact_relpath, method,
                        provider, engine_version, settings_hash, confidence,
                        diagnostic_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        chunk_key, representation_kind, method, engine_version, settings_hash
                    ) DO NOTHING
                    """,
                    (
                        chunk_key,
                        output.representation_kind.value,
                        output.text,
                        output.artifact_relpath,
                        output.method,
                        output.provider,
                        output.engine_version,
                        output.settings_hash,
                        output.confidence,
                        diagnostic_reason,
                        to_storage_time(utc_now()),
                    ),
                )
                row = connection.execute(
                    """
                    SELECT * FROM chunk_representation
                    WHERE chunk_key = ? AND representation_kind = ? AND method = ?
                      AND engine_version = ? AND settings_hash = ?
                    """,
                    (
                        chunk_key,
                        output.representation_kind.value,
                        output.method,
                        output.engine_version,
                        output.settings_hash,
                    ),
                ).fetchone()
                assert row is not None
                return self._representation(row)
        except sqlite3.Error:
            raise ParseStorageError("chunk representation storage failed") from None

    def list_representations(self, chunk_key: int) -> tuple[ChunkRepresentationRecord, ...]:
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """SELECT * FROM chunk_representation
                WHERE chunk_key = ? ORDER BY representation_key""",
                (chunk_key,),
            ).fetchall()
            return tuple(self._representation(row) for row in rows)
        except sqlite3.Error:
            raise ParseStorageError("chunk representation lookup failed") from None
        finally:
            connection.close()

    def append_visual_representation(
        self,
        chunk_key: int,
        output: FallbackOutput,
        metadata: VisualEvidenceMetadata,
        *,
        diagnostic_reason: str,
    ) -> VisualEvidenceRecord:
        """Atomically append an immutable representation and its visual provenance."""

        return self.append_visual_representations(
            ((chunk_key, output, metadata, diagnostic_reason),)
        )[0]

    def append_visual_representations(
        self,
        requests: tuple[tuple[int, FallbackOutput, VisualEvidenceMetadata, str], ...],
    ) -> tuple[VisualEvidenceRecord, ...]:
        """Append a fully validated visual-evidence bundle in one transaction."""

        if not requests:
            return ()
        for _chunk_key, output, metadata, _diagnostic_reason in requests:
            if output.artifact_relpath is not None:
                path = PurePosixPath(output.artifact_relpath)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("fallback artifact path must be private-root relative")
            if len(metadata.uncertainty) > 20 or any(
                not item.strip() or len(item) > 200 for item in metadata.uncertainty
            ):
                raise ValueError("visual uncertainty report exceeds configured limits")
        try:
            with self.database.transaction() as connection:
                return tuple(
                    self._append_visual_representation(
                        connection,
                        chunk_key,
                        output,
                        metadata,
                        diagnostic_reason=diagnostic_reason,
                    )
                    for chunk_key, output, metadata, diagnostic_reason in requests
                )
        except sqlite3.Error:
            raise ParseStorageError("visual evidence storage failed") from None

    def _append_visual_representation(
        self,
        connection: sqlite3.Connection,
        chunk_key: int,
        output: FallbackOutput,
        metadata: VisualEvidenceMetadata,
        *,
        diagnostic_reason: str,
    ) -> VisualEvidenceRecord:
        """Insert one visual representation inside its caller's transaction."""

        connection.execute(
            """
                    INSERT INTO chunk_representation(
                        chunk_key, representation_kind, text, artifact_relpath, method,
                        provider, engine_version, settings_hash, confidence,
                        diagnostic_reason, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        chunk_key, representation_kind, method, engine_version, settings_hash
                    ) DO NOTHING
                    """,
            (
                chunk_key,
                output.representation_kind.value,
                output.text,
                output.artifact_relpath,
                output.method,
                output.provider,
                output.engine_version,
                output.settings_hash,
                output.confidence,
                diagnostic_reason,
                to_storage_time(utc_now()),
            ),
        )
        row = connection.execute(
            """
                    SELECT * FROM chunk_representation
                    WHERE chunk_key = ? AND representation_kind = ? AND method = ?
                      AND engine_version = ? AND settings_hash = ?
                    """,
            (
                chunk_key,
                output.representation_kind.value,
                output.method,
                output.engine_version,
                output.settings_hash,
            ),
        ).fetchone()
        assert row is not None
        representation = self._representation(row)
        if (
            representation.chunk_key != chunk_key
            or representation.representation_kind is not output.representation_kind
            or representation.text != output.text
            or representation.artifact_relpath != output.artifact_relpath
            or representation.method != output.method
            or representation.provider != output.provider
            or representation.engine_version != output.engine_version
            or representation.settings_hash != output.settings_hash
            or representation.confidence != output.confidence
            or representation.diagnostic_reason != diagnostic_reason
        ):
            raise ValueError("visual evidence cache identity collision")
        connection.execute(
            """
                    INSERT INTO visual_evidence_metadata(
                        representation_key, version_key, source_sha256, source_page_index,
                        rendered_sha256, method_version, settings_json, review_status,
                        uncertainty_json, source_locator_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(representation_key) DO NOTHING
                    """,
            (
                representation.key,
                metadata.version_key,
                metadata.source_sha256,
                metadata.source_page_index,
                metadata.rendered_sha256,
                metadata.method_version,
                _json(metadata.settings),
                metadata.review_status.value,
                _json(list(metadata.uncertainty)),
                _json(metadata.source_locator),
                to_storage_time(utc_now()),
            ),
        )
        metadata_row = connection.execute(
            "SELECT * FROM visual_evidence_metadata WHERE representation_key = ?",
            (representation.key,),
        ).fetchone()
        assert metadata_row is not None
        stored = self._visual_metadata(metadata_row)
        if stored != metadata:
            raise ValueError("visual evidence cache identity collision")
        return VisualEvidenceRecord(representation, stored)

    def get_visual_evidence(self, representation_key: int) -> VisualEvidenceRecord | None:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT representation.*, metadata.*
                FROM chunk_representation representation
                JOIN visual_evidence_metadata metadata USING(representation_key)
                WHERE representation.representation_key = ?
                """,
                (representation_key,),
            ).fetchone()
            if row is None:
                return None
            return VisualEvidenceRecord(self._representation(row), self._visual_metadata(row))
        except (sqlite3.Error, ValueError, TypeError, json.JSONDecodeError):
            raise ParseStorageError("visual evidence lookup failed") from None
        finally:
            connection.close()

    @staticmethod
    def _document(row: sqlite3.Row) -> ParsedDocumentRecord:
        warnings = json.loads(str(row["warning_codes_json"]))
        return ParsedDocumentRecord(
            key=int(row["parse_key"]),
            version_key=int(row["version_key"]),
            resource_sha256=str(row["resource_sha256"]),
            parser_name=str(row["parser_name"]),
            parser_version=str(row["parser_version"]),
            engine_version=str(row["engine_version"]),
            settings_hash=str(row["settings_hash"]),
            status=ParseStatus(str(row["status"])),
            coverage=Coverage(str(row["coverage"])),
            warning_codes=tuple(str(item) for item in warnings),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
        )

    @classmethod
    def _chunks(
        cls, connection: sqlite3.Connection, parse_key: int
    ) -> tuple[DocumentChunkRecord, ...]:
        rows = connection.execute(
            """
            SELECT c.*, l.locator_key
            FROM document_chunk c
            JOIN source_locator l ON l.chunk_key = c.chunk_key
            WHERE c.parse_key = ? ORDER BY c.ordinal
            """,
            (parse_key,),
        ).fetchall()
        return tuple(cls._chunk(row) for row in rows)

    @staticmethod
    def _chunk(row: sqlite3.Row) -> DocumentChunkRecord:
        locator = json.loads(str(row["locator_json"]))
        elements = json.loads(str(row["structured_elements_json"]))
        diagnostic_payload = (
            None if row["diagnostic_json"] is None else json.loads(str(row["diagnostic_json"]))
        )
        diagnostic = None
        if diagnostic_payload is not None:
            diagnostic = ChunkDiagnostic(
                detector_version=str(diagnostic_payload["detector_version"]),
                native_character_count=int(diagnostic_payload["native_character_count"]),
                content_stream_bytes=diagnostic_payload["content_stream_bytes"],
                image_count=int(diagnostic_payload["image_count"]),
                form_xobject_count=int(diagnostic_payload.get("form_xobject_count", 0)),
                drawing_operator_count=int(diagnostic_payload["drawing_operator_count"]),
                reasons=tuple(str(item) for item in diagnostic_payload["reasons"]),
                fallback_recommended=bool(diagnostic_payload["fallback_recommended"]),
            )
        return DocumentChunkRecord(
            key=int(row["chunk_key"]),
            locator_key=int(row["locator_key"]),
            parse_key=int(row["parse_key"]),
            ordinal=int(row["ordinal"]),
            kind=ChunkKind(str(row["kind"])),
            native_text=str(row["native_text"]),
            locator=locator,
            structured_elements=tuple(elements),
            diagnostic=diagnostic,
        )

    @staticmethod
    def _representation(row: sqlite3.Row) -> ChunkRepresentationRecord:
        return ChunkRepresentationRecord(
            key=int(row["representation_key"]),
            chunk_key=int(row["chunk_key"]),
            representation_kind=RepresentationKind(str(row["representation_kind"])),
            text=None if row["text"] is None else str(row["text"]),
            artifact_relpath=None
            if row["artifact_relpath"] is None
            else str(row["artifact_relpath"]),
            method=str(row["method"]),
            provider=None if row["provider"] is None else str(row["provider"]),
            engine_version=str(row["engine_version"]),
            settings_hash=str(row["settings_hash"]),
            confidence=None if row["confidence"] is None else float(row["confidence"]),
            diagnostic_reason=str(row["diagnostic_reason"]),
        )

    @staticmethod
    def _visual_metadata(row: sqlite3.Row) -> VisualEvidenceMetadata:
        return VisualEvidenceMetadata(
            version_key=int(row["version_key"]),
            source_sha256=str(row["source_sha256"]),
            source_page_index=int(row["source_page_index"]),
            rendered_sha256=str(row["rendered_sha256"]),
            method_version=str(row["method_version"]),
            settings=json.loads(str(row["settings_json"])),
            review_status=VisualReviewStatus(str(row["review_status"])),
            uncertainty=tuple(str(item) for item in json.loads(str(row["uncertainty_json"]))),
            source_locator=json.loads(str(row["source_locator_json"])),
        )
