"""Detected-format parser selection."""

from __future__ import annotations

from ntulearn_skill.parsers.models import DocumentParser


class ParserRegistry:
    def __init__(self, parsers: tuple[DocumentParser, ...] = ()) -> None:
        self._parsers: dict[str, DocumentParser] = {}
        for parser in parsers:
            self.register(parser)

    def register(self, parser: DocumentParser) -> None:
        for file_format in parser.descriptor.formats:
            if file_format in self._parsers:
                raise ValueError("a parser is already registered for this detected format")
            self._parsers[file_format] = parser

    def parser_for(self, detected_format: str) -> DocumentParser | None:
        return self._parsers.get(detected_format)

    @property
    def supported_formats(self) -> frozenset[str]:
        return frozenset(self._parsers)
