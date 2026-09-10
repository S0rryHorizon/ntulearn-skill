"""Parse exact verified versions and apply optional selective fallbacks."""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path, PurePosixPath

from ntulearn_skill.core.models import Coverage
from ntulearn_skill.parsers.models import (
    ChunkRepresentationRecord,
    FallbackOutput,
    FallbackRequest,
    ParsedPayload,
    ParserDescriptor,
    ParseResult,
    ParserInputRejected,
    ParserOptions,
    ParseStatus,
    SelectiveFallback,
)
from ntulearn_skill.parsers.registry import ParserRegistry
from ntulearn_skill.parsers.repository import ParseRepository, ParseStorageError, StoredParse
from ntulearn_skill.storage.paths import RuntimePaths
from ntulearn_skill.storage.resources import ResourceRepository, ResourceVersionRecord


class ParseOperationError(RuntimeError):
    """A privacy-safe failure resolving immutable parser evidence."""


_PARSER_OUTPUT_LOCK = threading.Lock()


@contextmanager
def _private_diagnostic_boundary() -> Iterator[None]:
    """Serialize untrusted parser callbacks and discard their raw diagnostics."""

    with _PARSER_OUTPUT_LOCK:
        previous_logging_threshold = logging.root.manager.disable
        logging.disable(sys.maxsize)
        try:
            with (
                open(os.devnull, "w", encoding="utf-8") as sink,
                redirect_stdout(sink),
                redirect_stderr(sink),
            ):
                yield
        finally:
            logging.disable(previous_logging_threshold)


class ParseService:
    def __init__(
        self,
        paths: RuntimePaths,
        resources: ResourceRepository,
        repository: ParseRepository,
        registry: ParserRegistry,
    ) -> None:
        self.paths = paths
        self.resources = resources
        self.repository = repository
        self.registry = registry

    def parse_version(
        self, version_key: int, *, options: ParserOptions | None = None
    ) -> ParseResult:
        options = options or ParserOptions()
        version = self.resources.get_version(version_key)
        if version is None:
            raise ParseOperationError("verified resource version was not found")
        parser = self.registry.parser_for(version.file_format)
        if parser is None:
            descriptor = ParserDescriptor(
                name="unsupported",
                version="1",
                engine_version="none",
                formats=frozenset({version.file_format}),
            )
            cached = self.repository.find_cached(version.key, descriptor, options.settings_hash)
            if cached is not None:
                return ParseResult(cached.document, cached.chunks, True)
            stored = self.repository.store_terminal(
                version,
                descriptor,
                options.settings_hash,
                options.canonical_settings(),
                ParsedPayload(ParseStatus.UNSUPPORTED, Coverage.UNKNOWN, ()),
                error_code="format_unsupported",
            )
            return ParseResult(stored.document, stored.chunks, False)

        cached = self.repository.find_cached(version.key, parser.descriptor, options.settings_hash)
        if cached is not None:
            return ParseResult(cached.document, cached.chunks, True)

        source_path = self._verified_blob(version)
        try:
            with _private_diagnostic_boundary():
                payload = parser.parse(source_path, version, options)
            error_code = None
        except ParserInputRejected:
            payload = ParsedPayload(ParseStatus.FAILED, Coverage.FAILED, ())
            error_code = "parser_input_rejected"
        except Exception:
            payload = ParsedPayload(ParseStatus.FAILED, Coverage.FAILED, ())
            error_code = "parser_failed"
        stored = self.repository.store_terminal(
            version,
            parser.descriptor,
            options.settings_hash,
            options.canonical_settings(),
            payload,
            error_code=error_code,
        )
        return ParseResult(stored.document, stored.chunks, False)

    def apply_selective_fallback(
        self, parse_key: int, fallback: SelectiveFallback
    ) -> tuple[ChunkRepresentationRecord, ...]:
        parsed = self.repository.get(parse_key)
        if parsed is None:
            raise ParseOperationError("parsed document was not found")
        version = self.resources.get_version(parsed.document.version_key)
        if version is None or version.sha256 != parsed.document.resource_sha256:
            raise ParseOperationError("parsed document evidence is unavailable")
        source_path = self._verified_blob(version)
        outputs: list[tuple[int, FallbackOutput, str]] = []
        try:
            with _private_diagnostic_boundary():
                for chunk in parsed.chunks:
                    diagnostic = chunk.diagnostic
                    if diagnostic is None or not diagnostic.fallback_recommended:
                        continue
                    output = fallback.represent(FallbackRequest(source_path, version, chunk))
                    if output is not None:
                        self._validate_fallback_output(output)
                        outputs.append((chunk.key, output, ";".join(diagnostic.reasons)))
        except Exception:
            raise ParseOperationError("selective fallback failed") from None

        stored: list[ChunkRepresentationRecord] = []
        for chunk_key, output, reason in outputs:
            try:
                stored.append(
                    self.repository.append_representation(
                        chunk_key, output, diagnostic_reason=reason or "diagnostic_flag"
                    )
                )
            except (ParseStorageError, TypeError, ValueError):
                raise ParseOperationError("selective fallback result could not be stored") from None
        return tuple(stored)

    def resolve_verified_parse(
        self, parse_key: int
    ) -> tuple[StoredParse, ResourceVersionRecord, Path]:
        """Resolve a parse to hash-verified immutable bytes for a local adapter."""

        if isinstance(parse_key, bool) or parse_key <= 0:
            raise ValueError("parse key must be positive")
        parsed = self.repository.get(parse_key)
        if parsed is None:
            raise ParseOperationError("parsed document was not found")
        version = self.resources.get_version(parsed.document.version_key)
        if version is None or version.sha256 != parsed.document.resource_sha256:
            raise ParseOperationError("parsed document evidence is unavailable")
        return parsed, version, self._verified_blob(version)

    def _validate_fallback_output(self, output: FallbackOutput) -> None:
        if output.text is not None and len(output.text) > 2 * 1024 * 1024:
            raise ValueError("fallback text exceeds configured size")
        if not output.method.strip() or len(output.method) > 120:
            raise ValueError("fallback method is invalid")
        if not output.engine_version.strip() or len(output.engine_version) > 120:
            raise ValueError("fallback engine version is invalid")
        if len(output.settings_hash) != 64 or any(
            character not in "0123456789abcdef" for character in output.settings_hash
        ):
            raise ValueError("fallback settings hash is invalid")
        if output.artifact_relpath is None:
            return
        relative = PurePosixPath(output.artifact_relpath)
        if (
            relative.is_absolute()
            or ".." in relative.parts
            or not relative.parts
            or relative.parts[0] != "cache"
        ):
            raise ValueError("fallback artifact must be inside the private cache")
        path = self.paths.root.joinpath(*relative.parts)
        self._reject_linked_ancestors(path)
        try:
            metadata = path.lstat()
        except OSError:
            raise ValueError("fallback artifact was not created") from None
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("fallback artifact is unsafe")

    def _verified_blob(self, version: ResourceVersionRecord) -> Path:
        relative = PurePosixPath(version.blob_relpath)
        expected = PurePosixPath(
            "objects", "sha256", version.sha256[:2], version.sha256[2:4], version.sha256
        )
        if relative != expected or relative.is_absolute() or ".." in relative.parts:
            raise ParseOperationError("resource version blob reference is invalid")
        path = self.paths.root.joinpath(*relative.parts)
        try:
            self._reject_linked_ancestors(path)
            metadata = path.lstat()
            if not stat.S_ISREG(metadata.st_mode) or path.is_symlink():
                raise ParseOperationError("resource version blob is unsafe")
            if metadata.st_size != version.byte_size:
                raise ParseOperationError("resource version blob failed verification")
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            if digest.hexdigest() != version.sha256:
                raise ParseOperationError("resource version blob failed verification")
        except ParseOperationError:
            raise
        except OSError:
            raise ParseOperationError("resource version blob could not be read") from None
        return path

    def _reject_linked_ancestors(self, path: Path) -> None:
        try:
            path.relative_to(self.paths.root)
        except ValueError:
            raise ParseOperationError("private parser path escapes its runtime root") from None
        current = path.parent
        while current != self.paths.root:
            if current.is_symlink():
                raise ParseOperationError("private parser path contains a symbolic link")
            current = current.parent
