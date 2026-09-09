"""Privacy-safe source wording normalization."""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_MARKUP = re.compile(r"<[A-Za-z!/][^>]*>")


class _VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._ignored_depth = 0

    def handle_data(self, data: str) -> None:
        if self._ignored_depth == 0:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._ignored_depth += 1
        elif self._ignored_depth == 0 and normalized in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.lower()
        if normalized in {"script", "style"}:
            self._ignored_depth = max(0, self._ignored_depth - 1)
        elif self._ignored_depth == 0 and normalized in {"p", "div", "li", "tr"}:
            self.parts.append("\n")


def _redact_url(match: re.Match[str]) -> str:
    try:
        parsed = urlsplit(match.group(0))
        port = parsed.port
    except ValueError:
        return "[redacted-url]"
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return "[redacted-url]"
    host = parsed.hostname
    if port is not None:
        host = f"{host}:{port}"
    return urlunsplit((parsed.scheme, host, parsed.path, "", ""))


def sanitize_source_text(value: str, *, maximum_characters: int = 256 * 1024) -> str:
    """Keep visible wording while removing HTML attributes and URL secrets."""

    if not isinstance(value, str):
        raise TypeError("source text must be a string")
    if _MARKUP.search(value):
        parser = _VisibleText()
        try:
            parser.feed(value)
            parser.close()
        except ValueError:
            raise ValueError("source text markup is invalid") from None
        value = "".join(parser.parts)
    else:
        value = html.unescape(value)
    value = _URL.sub(_redact_url, value)
    value = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", value)
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if len(value) > maximum_characters:
        raise ValueError("source text exceeds the configured bound")
    return value
