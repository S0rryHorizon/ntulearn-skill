"""Bounded page-level PDF parsing with physical-page provenance."""

from __future__ import annotations

import logging
import re
from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any

from pypdf import PdfReader, apply_configuration

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

# pypdf robustness warnings can contain raw object values. Keep the library boundary silent;
# callers receive only the bounded warning codes returned below.
_PYPDF_LOGGER = logging.getLogger("pypdf")
_PYPDF_LOGGER.addHandler(logging.NullHandler())
_PYPDF_LOGGER.propagate = False

_DRAWING_OPERATOR = re.compile(rb"(?:^|\s)(?:m|l|c|v|y|h|re|S|s|f|F|f\*|B|B\*|b|b\*|n)(?=\s|$)")
_VISUAL_DATE_REFERENCE = re.compile(
    r"\b(?:"
    r"(?:mentioned|following|shown|listed)\s+(?:due\s+)?dates?"
    r"|due\s+dates?\s+(?:below|above|shown|listed)"
    r"|(?:see|refer\s+to)\s+(?:the\s+)?(?:table|figure|image|schedule)"
    r")\b",
    re.I,
)
_EVENTISH_VISUAL_CONTEXT = re.compile(
    r"\b(?:assignment|homework|quiz|test|exam(?:ination)?|presentation|tutorial|"
    r"lab(?:oratory)?|deadline|due|week|schedule)\b",
    re.I,
)


def _resolved(value: Any) -> Any:
    getter = getattr(value, "get_object", None)
    return getter() if callable(getter) else value


def _visual_xobjects(
    page: Any, *, maximum_depth: int, maximum_objects: int
) -> tuple[int, int, bool]:
    """Inspect images nested in Form XObjects without decoding image payloads."""

    stack: list[tuple[Any, int]] = [(page, 0)]
    visited: set[int] = set()
    image_count = 0
    form_count = 0
    inspected = 0
    incomplete = False
    while stack:
        container, depth = stack.pop()
        resources = _resolved(container.get("/Resources"))
        if not resources:
            if depth:
                incomplete = True
            continue
        xobjects = _resolved(resources.get("/XObject"))
        if not xobjects:
            continue
        for candidate in xobjects.values():
            item = _resolved(candidate)
            if item is None:
                continue
            marker = id(item)
            if marker in visited:
                continue
            visited.add(marker)
            inspected += 1
            if inspected > maximum_objects:
                incomplete = True
                return image_count, form_count, incomplete
            subtype = item.get("/Subtype")
            if subtype == "/Image":
                image_count += 1
            elif subtype == "/Form":
                form_count += 1
                if depth >= maximum_depth:
                    incomplete = True
                else:
                    stack.append((item, depth + 1))
    return image_count, form_count, incomplete


class PdfParser:
    descriptor = ParserDescriptor(
        name="pypdf",
        version="1",
        engine_version=package_version("pypdf"),
        formats=frozenset({"pdf"}),
    )

    def parse(
        self, path: Path, version: ResourceVersionRecord, options: ParserOptions
    ) -> ParsedPayload:
        stream_limit = options.limits.maximum_content_stream_bytes
        with apply_configuration(
            maximum_declared_stream_length=stream_limit,
            array_based_stream_maximum_output_length=stream_limit,
            jbig2_maximum_output_length=stream_limit,
            lzw_maximum_output_length=stream_limit,
            run_length_maximum_output_length=stream_limit,
            zlib_maximum_output_length=stream_limit,
            image_maximum_buffer_size=stream_limit,
            page_tree_maximum_entries=options.limits.maximum_chunks,
        ):
            return self._parse_configured(path, version, options)

    def _parse_configured(
        self, path: Path, version: ResourceVersionRecord, options: ParserOptions
    ) -> ParsedPayload:
        limits = options.limits
        if version.file_format != "pdf":
            raise ParserInputRejected("parser received a mismatched detected format")
        if path.stat().st_size > limits.maximum_input_bytes:
            raise ParserInputRejected("parser input exceeds configured size")

        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            return ParsedPayload(
                ParseStatus.UNSUPPORTED,
                Coverage.UNKNOWN,
                (),
                ("encrypted_pdf_unsupported",),
            )
        if len(reader.pages) > limits.maximum_chunks:
            raise ParserInputRejected("document exceeds configured page count")

        warning_codes: set[str] = set()
        try:
            labels = tuple(str(label) for label in reader.page_labels)
        except Exception:
            labels = ()
            warning_codes.add("logical_page_labels_unavailable")

        chunks: list[ParsedChunk] = []
        total_characters = 0
        partial = False
        for physical_index, page in enumerate(reader.pages):
            stream_size: int | None = None
            drawing_count = 0
            stream_limited = False
            try:
                contents = page.get_contents()
                stream = b"" if contents is None else contents.get_data()
                stream_size = len(stream)
                if stream_size <= limits.maximum_content_stream_bytes:
                    drawing_count = len(_DRAWING_OPERATOR.findall(stream))
                else:
                    stream_limited = True
                    partial = True
                    warning_codes.add("page_content_stream_limit_exceeded")
            except Exception:
                partial = True
                warning_codes.add("page_diagnostics_incomplete")

            xobject_inspection_incomplete = False
            try:
                image_count, form_count, xobject_inspection_incomplete = _visual_xobjects(
                    page,
                    maximum_depth=limits.maximum_nesting_depth,
                    maximum_objects=limits.maximum_structured_elements,
                )
                if xobject_inspection_incomplete:
                    partial = True
                    warning_codes.add("page_xobject_diagnostics_incomplete")
            except Exception:
                image_count = 0
                form_count = 0
                xobject_inspection_incomplete = True
                partial = True
                warning_codes.add("page_image_diagnostics_incomplete")

            extraction_failed = False
            if stream_limited:
                native_text = ""
                extraction_failed = True
                warning_codes.add("page_text_skipped_for_stream_limit")
            else:
                try:
                    native_text = page.extract_text() or ""
                except Exception:
                    native_text = ""
                    extraction_failed = True
                    partial = True
                    warning_codes.add("page_text_extraction_failed")

            if len(native_text) > limits.maximum_chunk_characters:
                native_text = native_text[: limits.maximum_chunk_characters]
                partial = True
                warning_codes.add("page_text_truncated")
            remaining = limits.maximum_total_characters - total_characters
            if remaining <= 0:
                native_text = ""
                partial = True
                warning_codes.add("document_text_limit_reached")
            elif len(native_text) > remaining:
                native_text = native_text[:remaining]
                partial = True
                warning_codes.add("document_text_truncated")
            total_characters += len(native_text)

            reasons: list[str] = []
            low_text = len(native_text.strip()) < options.low_text_character_threshold
            if low_text:
                reasons.append("low_native_text")
            if image_count:
                reasons.append("image_resources_present")
            if form_count:
                reasons.append("form_xobjects_present")
            if xobject_inspection_incomplete:
                reasons.append("xobject_inspection_incomplete")
            if drawing_count >= options.drawing_operator_threshold:
                reasons.append("drawing_operators_dominate")
            visual_date_reference = (
                options.diagnostic_version == "stage-b-2"
                and _VISUAL_DATE_REFERENCE.search(native_text) is not None
            )
            if visual_date_reference:
                reasons.append("native_text_references_visual_dates")
            dense_visual_layout = (
                options.diagnostic_version == "stage-b-2"
                and drawing_count >= options.drawing_operator_threshold
                and drawing_count >= 500
                and drawing_count >= max(1, len(native_text.strip())) * 2
                and _EVENTISH_VISUAL_CONTEXT.search(native_text) is not None
            )
            if dense_visual_layout:
                reasons.append("dense_visual_layout_with_limited_text")
            if extraction_failed:
                reasons.append("native_extraction_failed")
            if stream_limited:
                reasons.append("content_stream_too_large")

            supporting_visual_signal = (
                image_count > 0
                or form_count > 0
                or drawing_count >= options.drawing_operator_threshold
            )
            fallback_recommended = extraction_failed or (
                supporting_visual_signal
                and (low_text or visual_date_reference or dense_visual_layout)
            )
            if fallback_recommended:
                partial = True
                warning_codes.add("visual_evidence_requires_fallback")
            diagnostic = ChunkDiagnostic(
                detector_version=options.diagnostic_version,
                native_character_count=len(native_text),
                content_stream_bytes=stream_size,
                image_count=image_count,
                form_xobject_count=form_count,
                drawing_operator_count=drawing_count,
                reasons=tuple(reasons),
                fallback_recommended=fallback_recommended,
            )
            locator: dict[str, JsonValue] = {
                "format": "pdf",
                "physical_page_index": physical_index,
                "physical_page_number": physical_index + 1,
                "logical_page_label": labels[physical_index]
                if physical_index < len(labels)
                else None,
            }
            chunks.append(
                ParsedChunk(
                    ordinal=physical_index,
                    kind=ChunkKind.PAGE,
                    native_text=native_text,
                    locator=locator,
                    diagnostic=diagnostic,
                )
            )

        return ParsedPayload(
            status=ParseStatus.PARTIAL if partial else ParseStatus.COMPLETE,
            coverage=Coverage.PARTIAL if partial else Coverage.COMPLETE,
            chunks=tuple(chunks),
            warning_codes=tuple(sorted(warning_codes)),
        )
