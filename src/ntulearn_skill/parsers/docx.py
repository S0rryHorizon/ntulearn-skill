"""DOCX body parsing that preserves paragraph/table order and structural locators."""

from __future__ import annotations

import xml.etree.ElementTree as element_tree
import zipfile
from dataclasses import dataclass
from importlib.metadata import version as package_version
from pathlib import Path

from docx import Document
from docx.table import Table, _Cell
from docx.text.paragraph import Paragraph

from ntulearn_skill.core.models import Coverage
from ntulearn_skill.parsers.models import (
    ChunkDiagnostic,
    ChunkKind,
    JsonValue,
    ParsedChunk,
    ParsedPayload,
    ParserDescriptor,
    ParserInputRejected,
    ParserOptions,
    ParseStatus,
)
from ntulearn_skill.storage.resources import ResourceVersionRecord

_UNSUPPORTED_XML_ELEMENTS = {
    "AlternateContent": "docx_alternate_content_partial",
    "altChunk": "docx_altchunk_partial",
    "del": "docx_revision_text_partial",
    "ins": "docx_revision_text_partial",
    "sdt": "docx_content_control_partial",
    "txbxContent": "docx_textbox_partial",
    "vMerge": "docx_vertical_merge_partial",
}


def _safe_archive(path: Path, options: ParserOptions) -> tuple[set[str], bytes]:
    limits = options.limits
    if path.stat().st_size > limits.maximum_input_bytes:
        raise ParserInputRejected("parser input exceeds configured size")
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            if len(entries) > limits.maximum_archive_entries:
                raise ParserInputRejected("document archive has too many entries")
            total = 0
            names: set[str] = set()
            for entry in entries:
                if entry.flag_bits & 0x1:
                    raise ParserInputRejected("encrypted document archives are unsupported")
                total += entry.file_size
                if entry.file_size > 0 and entry.compress_size == 0:
                    raise ParserInputRejected("document archive entry is invalid")
                if entry.compress_size > 0 and entry.file_size / entry.compress_size > 1_000:
                    raise ParserInputRejected("document archive compression ratio is unsafe")
                if total > limits.maximum_archive_uncompressed_bytes:
                    raise ParserInputRejected("document archive expands beyond configured size")
                names.add(entry.filename)
            if "word/document.xml" not in names:
                raise ParserInputRejected("document archive lacks its main body")
            info = archive.getinfo("word/document.xml")
            if info.file_size > limits.maximum_archive_uncompressed_bytes:
                raise ParserInputRejected("document body exceeds configured size")
            body = archive.read(info)
    except (OSError, zipfile.BadZipFile, KeyError):
        raise ParserInputRejected("document archive is invalid") from None
    return names, body


def _visual_count(element: Paragraph | Table) -> int:
    root = element._element
    return sum(
        1 for child in root.iter() if child.tag.rsplit("}", 1)[-1] in {"drawing", "object", "pict"}
    )


def _visual_diagnostic(text: str, visual_count: int, options: ParserOptions) -> ChunkDiagnostic:
    low_text = len(text.strip()) < options.low_text_character_threshold
    reasons: list[str] = []
    if low_text:
        reasons.append("low_native_text")
    if visual_count:
        reasons.append("embedded_visual_present")
    return ChunkDiagnostic(
        detector_version="stage-b-1",
        native_character_count=len(text),
        content_stream_bytes=None,
        image_count=visual_count,
        form_xobject_count=0,
        drawing_operator_count=0,
        reasons=tuple(reasons),
        fallback_recommended=low_text and visual_count > 0,
    )


@dataclass(slots=True)
class _StructureBudget:
    remaining_elements: int
    remaining_characters: int
    maximum_depth: int
    exhausted: bool = False

    def take_element(self) -> bool:
        if self.remaining_elements <= 0:
            self.exhausted = True
            return False
        self.remaining_elements -= 1
        return True

    def take_text(self, value: str) -> str:
        if len(value) > self.remaining_characters:
            value = value[: self.remaining_characters]
            self.exhausted = True
        self.remaining_characters -= len(value)
        return value


def _table_data(
    table: Table,
    *,
    table_path: tuple[int, ...],
    budget: _StructureBudget,
    warnings: set[str],
    depth: int,
) -> tuple[dict[str, JsonValue], str]:
    if depth > budget.maximum_depth:
        budget.exhausted = True
        return {"rows": [], "table_path": list(table_path)}, ""
    rows: list[JsonValue] = []
    text_rows: list[str] = []
    for row_index, row in enumerate(table.rows):
        if not budget.take_element():
            break
        if getattr(row, "grid_cols_before", 0) or getattr(row, "grid_cols_after", 0):
            warnings.add("docx_omitted_grid_cells_partial")
        cells: list[JsonValue] = []
        text_cells: list[str] = []
        visual_column = 0
        for enumerated_column, cell in enumerate(row.cells):
            if enumerated_column < visual_column:
                continue
            if not budget.take_element():
                break
            span = max(1, int(cell.grid_span))
            contents, text = _cell_data(
                cell,
                table_path=table_path,
                row_index=row_index,
                cell_index=visual_column,
                budget=budget,
                warnings=warnings,
                depth=depth,
            )
            cells.append(
                {
                    "cell_index": visual_column,
                    "contents": contents,
                    "grid_span": span,
                    "row_index": row_index,
                }
            )
            text_cells.append(text)
            visual_column += span
        rows.append({"cells": cells, "row_index": row_index})
        text_rows.append("\t".join(text_cells))
    return {"rows": rows, "table_path": list(table_path)}, "\n".join(text_rows)


def _cell_data(
    cell: _Cell,
    *,
    table_path: tuple[int, ...],
    row_index: int,
    cell_index: int,
    budget: _StructureBudget,
    warnings: set[str],
    depth: int,
) -> tuple[list[JsonValue], str]:
    contents: list[JsonValue] = []
    text_parts: list[str] = []
    nested_index = 0
    paragraph_index = 0
    for element in cell.iter_inner_content():
        if not budget.take_element():
            break
        if isinstance(element, Paragraph):
            text = budget.take_text(element.text)
            contents.append({"kind": "paragraph", "paragraph_index": paragraph_index, "text": text})
            text_parts.append(text)
            paragraph_index += 1
        elif isinstance(element, Table):
            nested_path = (*table_path, row_index, cell_index, nested_index)
            structured, text = _table_data(
                element,
                table_path=nested_path,
                budget=budget,
                warnings=warnings,
                depth=depth + 1,
            )
            contents.append({"kind": "table", "table": structured})
            text_parts.append(text)
            nested_index += 1
    return contents, "\n".join(text_parts)


class DocxParser:
    descriptor = ParserDescriptor(
        name="python-docx",
        version="1",
        engine_version=package_version("python-docx"),
        formats=frozenset({"docx"}),
    )

    def parse(
        self, path: Path, version: ResourceVersionRecord, options: ParserOptions
    ) -> ParsedPayload:
        if version.file_format != "docx":
            raise ParserInputRejected("parser received a mismatched detected format")
        archive_names, body_xml = _safe_archive(path, options)
        try:
            body_root = element_tree.fromstring(body_xml)
        except element_tree.ParseError:
            raise ParserInputRejected("document body XML is invalid") from None
        body_elements = {element.tag.rsplit("}", 1)[-1] for element in body_root.iter()}
        warnings = {
            warning
            for element_name, warning in _UNSUPPORTED_XML_ELEMENTS.items()
            if element_name in body_elements
        }
        if any(
            name.startswith(("word/header", "word/footer"))
            or name in {"word/footnotes.xml", "word/endnotes.xml", "word/comments.xml"}
            for name in archive_names
        ):
            warnings.add("docx_additional_story_partial")

        document = Document(str(path))
        chunks: list[ParsedChunk] = []
        paragraph_index = 0
        table_index = 0
        total_characters = 0
        truncated = False
        for element_index, element in enumerate(document.iter_inner_content()):
            if len(chunks) >= options.limits.maximum_chunks:
                warnings.add("document_chunk_limit_reached")
                truncated = True
                break
            if isinstance(element, Paragraph):
                text = element.text
                visual_count = _visual_count(element)
                kind = ChunkKind.PARAGRAPH
                locator: dict[str, JsonValue] = {
                    "element_index": element_index,
                    "format": "docx",
                    "paragraph_index": paragraph_index,
                }
                structured: tuple[dict[str, JsonValue], ...] = ()
                paragraph_index += 1
            elif isinstance(element, Table):
                budget = _StructureBudget(
                    options.limits.maximum_structured_elements,
                    min(
                        options.limits.maximum_chunk_characters,
                        max(0, options.limits.maximum_total_characters - total_characters),
                    ),
                    options.limits.maximum_nesting_depth,
                )
                table_data, text = _table_data(
                    element,
                    table_path=(table_index,),
                    budget=budget,
                    warnings=warnings,
                    depth=0,
                )
                if budget.exhausted:
                    warnings.add("docx_structure_limit_reached")
                    truncated = True
                kind = ChunkKind.TABLE
                locator = {
                    "element_index": element_index,
                    "format": "docx",
                    "table_index": table_index,
                }
                structured = (table_data,)
                visual_count = _visual_count(element)
                table_index += 1
            else:
                warnings.add("docx_body_element_partial")
                continue

            if len(text) > options.limits.maximum_chunk_characters:
                text = text[: options.limits.maximum_chunk_characters]
                warnings.add("chunk_text_truncated")
                truncated = True
            remaining = options.limits.maximum_total_characters - total_characters
            if remaining <= 0:
                text = ""
                warnings.add("document_text_limit_reached")
                truncated = True
            elif len(text) > remaining:
                text = text[:remaining]
                warnings.add("document_text_truncated")
                truncated = True
            total_characters += len(text)
            diagnostic = _visual_diagnostic(text, visual_count, options)
            if diagnostic.fallback_recommended:
                warnings.add("docx_visual_evidence_requires_fallback")
            chunks.append(
                ParsedChunk(
                    ordinal=len(chunks),
                    kind=kind,
                    native_text=text,
                    locator=locator,
                    structured_elements=structured,
                    diagnostic=diagnostic,
                )
            )

        partial = bool(warnings) or truncated
        return ParsedPayload(
            status=ParseStatus.PARTIAL if partial else ParseStatus.COMPLETE,
            coverage=Coverage.PARTIAL if partial else Coverage.COMPLETE,
            chunks=tuple(chunks),
            warning_codes=tuple(sorted(warnings)),
        )
