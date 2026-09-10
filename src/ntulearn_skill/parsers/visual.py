"""Private, selective page rendering and bounded host visual-evidence import."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ntulearn_skill.parsers.models import (
    FallbackOutput,
    JsonValue,
    ParseStatus,
    RepresentationKind,
    VisualEvidenceMetadata,
    VisualReviewStatus,
)
from ntulearn_skill.parsers.repository import ParseRepository
from ntulearn_skill.parsers.service import ParseService
from ntulearn_skill.storage.paths import RuntimePaths, ensure_private_directory

_BUNDLE_SCHEMA = "ntulearn.visual-evidence.host-result.v1"
_RENDER_METHOD = "pdftoppm-page-render"
_IMPORT_METHOD = "host-view-image"
_IMPORT_METHOD_VERSION = "host-view-image-import-1"
_MAX_BUNDLE_BYTES = 512 * 1024
_MAX_RESULTS = 64
_MAX_TEXT_CHARACTERS = 32 * 1024
_MAX_RENDER_BYTES = 32 * 1024 * 1024


class VisualEvidenceError(RuntimeError):
    """A privacy-safe visual evidence workflow failure."""


class PageRenderer(Protocol):
    method: str
    engine_version: str

    def render(self, source: Path, page_index: int, destination: Path, *, dpi: int) -> None:
        """Render exactly one zero-based PDF page to a PNG destination."""


class PdftoppmRenderer:
    method = _RENDER_METHOD

    def __init__(self) -> None:
        executable = shutil.which("pdftoppm")
        if executable is None:
            raise VisualEvidenceError("local PDF page renderer is unavailable")
        self.executable = executable
        try:
            result = subprocess.run(
                [executable, "-v"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            raise VisualEvidenceError("local PDF page renderer is unavailable") from None
        first_line = result.stdout.decode("utf-8", errors="replace").splitlines()[:1]
        version = first_line[0].strip() if first_line else "pdftoppm-version-unknown"
        self.engine_version = version[:120]

    def render(self, source: Path, page_index: int, destination: Path, *, dpi: int) -> None:
        temporary_prefix = destination.parent / f".{destination.stem}-{uuid.uuid4().hex}"
        temporary_png = temporary_prefix.with_suffix(".png")
        try:
            result = subprocess.run(
                [
                    self.executable,
                    "-f",
                    str(page_index + 1),
                    "-l",
                    str(page_index + 1),
                    "-singlefile",
                    "-png",
                    "-r",
                    str(dpi),
                    os.fspath(source),
                    os.fspath(temporary_prefix),
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=60,
            )
            if result.returncode != 0:
                raise VisualEvidenceError("local PDF page rendering failed")
            metadata = temporary_png.lstat()
            if (
                temporary_png.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_RENDER_BYTES
            ):
                raise VisualEvidenceError("rendered page failed safety validation")
            with temporary_png.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise VisualEvidenceError("rendered page format is invalid")
            os.chmod(temporary_png, 0o600, follow_symlinks=False)
            os.replace(temporary_png, destination)
        except VisualEvidenceError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise VisualEvidenceError("local PDF page rendering failed") from None
        finally:
            temporary_png.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class VisualInspectionItem:
    parse_key: int
    chunk_key: int
    rendered_representation_key: int
    source_page_index: int
    source_page_number: int
    source_sha256: str
    rendered_sha256: str
    diagnostic_reasons: tuple[str, ...]
    review_status: VisualReviewStatus
    local_path: Path
    bundle_path: Path


@dataclass(frozen=True, slots=True)
class VisualImportItem:
    parse_key: int
    chunk_key: int
    representation_key: int
    source_page_index: int
    source_sha256: str
    rendered_sha256: str
    method: str
    engine_version: str
    settings_hash: str
    confidence: float
    review_status: VisualReviewStatus
    uncertainty: tuple[str, ...]


def _canonical(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _settings_hash(settings: object) -> str:
    return hashlib.sha256(_canonical(settings).encode("utf-8")).hexdigest()


class VisualEvidenceService:
    def __init__(
        self,
        paths: RuntimePaths,
        parse_service: ParseService,
        repository: ParseRepository,
    ) -> None:
        self.paths = paths
        self.parse_service = parse_service
        self.repository = repository

    def prepare(
        self,
        parse_key: int,
        *,
        dpi: int = 150,
        renderer: PageRenderer | None = None,
    ) -> tuple[VisualInspectionItem, ...]:
        if isinstance(dpi, bool) or not 72 <= dpi <= 300:
            raise ValueError("visual render DPI must be between 72 and 300")
        parsed, version, source = self.parse_service.resolve_verified_parse(parse_key)
        if parsed.document.status not in {ParseStatus.COMPLETE, ParseStatus.PARTIAL}:
            raise VisualEvidenceError("selective visual rendering requires a usable PDF parse")
        if version.file_format != "pdf":
            raise VisualEvidenceError("selective visual rendering currently supports PDF pages")
        renderer = renderer or PdftoppmRenderer()
        if (
            not renderer.method.strip()
            or len(renderer.method) > 120
            or not renderer.engine_version.strip()
            or len(renderer.engine_version) > 120
        ):
            raise VisualEvidenceError("local PDF page renderer identity is invalid")
        render_settings: dict[str, JsonValue] = {
            "dpi": dpi,
            "engine_version": renderer.engine_version,
            "format": "png",
            "method_version": "selective-page-render-1",
            "renderer_method": renderer.method,
        }
        render_hash = _settings_hash(render_settings)
        flagged_chunks = tuple(
            chunk
            for chunk in parsed.chunks
            if chunk.diagnostic is not None and chunk.diagnostic.fallback_recommended
        )
        if len(flagged_chunks) > _MAX_RESULTS:
            raise VisualEvidenceError("too many PDF pages require one visual inspection bundle")
        if not flagged_chunks:
            return ()
        relative_directory = Path(
            "cache",
            "visual-evidence",
            version.sha256,
            parsed.document.parser_name,
            parsed.document.parser_version,
            parsed.document.settings_hash,
            render_hash,
        )
        directory = self.paths.root / relative_directory
        ensure_private_directory(directory, boundary=self.paths.root)
        pending: list[
            tuple[
                int,
                FallbackOutput,
                VisualEvidenceMetadata,
                str,
                int,
                str,
                tuple[str, ...],
                Path,
            ]
        ] = []
        for chunk in flagged_chunks:
            diagnostic = chunk.diagnostic
            assert diagnostic is not None
            page_index = chunk.locator.get("physical_page_index")
            if isinstance(page_index, bool) or not isinstance(page_index, int) or page_index < 0:
                raise VisualEvidenceError("flagged PDF chunk has an invalid page locator")
            destination = directory / f"page-{page_index + 1:06d}.png"
            if not destination.exists():
                renderer.render(source, page_index, destination, dpi=dpi)
            self._validate_render(destination)
            rendered_sha256 = _sha256_file(destination)
            artifact_relpath = destination.relative_to(self.paths.root).as_posix()
            output = FallbackOutput(
                RepresentationKind.RENDERED_DERIVATIVE,
                None,
                artifact_relpath,
                renderer.method,
                None,
                renderer.engine_version,
                render_hash,
                1.0,
            )
            metadata = VisualEvidenceMetadata(
                version.key,
                version.sha256,
                page_index,
                rendered_sha256,
                "selective-page-render-1",
                render_settings,
                VisualReviewStatus.NEEDS_REVIEW,
                ("host_visual_inspection_pending",),
                chunk.locator,
            )
            pending.append(
                (
                    chunk.key,
                    output,
                    metadata,
                    ";".join(diagnostic.reasons) or "diagnostic_flag",
                    page_index,
                    rendered_sha256,
                    diagnostic.reasons,
                    destination,
                )
            )
        records = self.repository.append_visual_representations(
            tuple(
                (chunk_key, output, metadata, diagnostic_reason)
                for chunk_key, output, metadata, diagnostic_reason, *_rest in pending
            )
        )
        items = [
            VisualInspectionItem(
                parse_key,
                chunk_key,
                record.representation.key,
                page_index,
                page_index + 1,
                version.sha256,
                rendered_sha256,
                diagnostic_reasons,
                VisualReviewStatus.NEEDS_REVIEW,
                destination,
                Path(),
            )
            for (
                chunk_key,
                _output,
                _metadata,
                _diagnostic_reason,
                page_index,
                rendered_sha256,
                diagnostic_reasons,
                destination,
            ), record in zip(pending, records, strict=True)
        ]
        bundle_path = self._write_bundle_template(parsed.document.key, version.sha256, items)
        return tuple(
            VisualInspectionItem(
                item.parse_key,
                item.chunk_key,
                item.rendered_representation_key,
                item.source_page_index,
                item.source_page_number,
                item.source_sha256,
                item.rendered_sha256,
                item.diagnostic_reasons,
                item.review_status,
                item.local_path,
                bundle_path,
            )
            for item in items
        )

    def import_bundle(self, bundle_path: str | Path) -> tuple[VisualImportItem, ...]:
        path = self._safe_bundle_path(Path(bundle_path))
        payload = self._read_bundle(path)
        parse_key = self._positive_integer(payload.get("parse_key"), "bundle parse key")
        parsed, version, _source = self.parse_service.resolve_verified_parse(parse_key)
        if payload.get("resource_sha256") != version.sha256:
            raise VisualEvidenceError("visual evidence bundle source hash does not match")
        results = payload.get("results")
        if not isinstance(results, list) or not 1 <= len(results) <= _MAX_RESULTS:
            raise VisualEvidenceError("visual evidence bundle result count is invalid")
        inspection_items = payload["inspection_items"]
        assert isinstance(inspection_items, list)
        inspection_by_key = {
            int(item["rendered_representation_key"]): item for item in inspection_items
        }
        parsed_chunk_keys = {chunk.key for chunk in parsed.chunks}
        requests: list[tuple[int, FallbackOutput, VisualEvidenceMetadata, str]] = []
        seen: set[int] = set()
        for raw in results:
            result = self._validate_result(raw)
            text_value = result["text"]
            engine_version = result["engine_version"]
            confidence_value = result["confidence"]
            review_status_value = result["review_status"]
            uncertainty_value = result["uncertainty"]
            assert isinstance(text_value, str)
            assert isinstance(engine_version, str)
            assert isinstance(confidence_value, (int, float)) and not isinstance(
                confidence_value, bool
            )
            assert isinstance(review_status_value, str)
            assert isinstance(uncertainty_value, list)
            rendered_key = self._positive_integer(
                result["rendered_representation_key"], "rendered representation key"
            )
            if rendered_key in seen:
                raise VisualEvidenceError("visual evidence bundle repeats a rendered page")
            manifest_item = inspection_by_key.get(rendered_key)
            if manifest_item is None:
                raise VisualEvidenceError("visual evidence result was not prepared for inspection")
            seen.add(rendered_key)
            rendered = self.repository.get_visual_evidence(rendered_key)
            if (
                rendered is None
                or rendered.representation.representation_kind
                is not RepresentationKind.RENDERED_DERIVATIVE
                or rendered.representation.chunk_key not in parsed_chunk_keys
                or rendered.metadata.version_key != version.key
                or rendered.metadata.source_sha256 != parsed.document.resource_sha256
                or manifest_item["source_page_index"] != rendered.metadata.source_page_index
                or manifest_item["rendered_sha256"] != rendered.metadata.rendered_sha256
            ):
                raise VisualEvidenceError("visual evidence bundle references unrelated evidence")
            artifact = self._safe_artifact(rendered.representation.artifact_relpath)
            if _sha256_file(artifact) != rendered.metadata.rendered_sha256:
                raise VisualEvidenceError("rendered page integrity check failed")
            settings = result["settings"]
            assert isinstance(settings, dict)
            identity = {
                "confidence": confidence_value,
                "engine_version": engine_version,
                "import_method_version": _IMPORT_METHOD_VERSION,
                "rendered_sha256": rendered.metadata.rendered_sha256,
                "review_status": review_status_value,
                "settings": settings,
                "text_sha256": hashlib.sha256(text_value.encode("utf-8")).hexdigest(),
                "uncertainty": uncertainty_value,
            }
            settings_hash = _settings_hash(identity)
            review_status = VisualReviewStatus(review_status_value)
            uncertainty: tuple[str, ...] = tuple(str(item) for item in uncertainty_value)
            output = FallbackOutput(
                RepresentationKind.VISION_DESCRIPTION,
                text_value,
                rendered.representation.artifact_relpath,
                _IMPORT_METHOD,
                "local-host",
                engine_version,
                settings_hash,
                float(confidence_value),
            )
            metadata = VisualEvidenceMetadata(
                version.key,
                version.sha256,
                rendered.metadata.source_page_index,
                rendered.metadata.rendered_sha256,
                _IMPORT_METHOD_VERSION,
                settings,
                review_status,
                uncertainty,
                rendered.metadata.source_locator,
            )
            requests.append(
                (
                    rendered.representation.chunk_key,
                    output,
                    metadata,
                    "host_inspection_of_flagged_page",
                )
            )
        stored_records = self.repository.append_visual_representations(tuple(requests))
        return tuple(
            VisualImportItem(
                parse_key,
                stored.representation.chunk_key,
                stored.representation.key,
                stored.metadata.source_page_index,
                stored.metadata.source_sha256,
                stored.metadata.rendered_sha256,
                stored.representation.method,
                stored.representation.engine_version,
                stored.representation.settings_hash,
                float(stored.representation.confidence or 0.0),
                stored.metadata.review_status,
                stored.metadata.uncertainty,
            )
            for stored in stored_records
        )

    def _write_bundle_template(
        self, parse_key: int, source_sha256: str, items: list[VisualInspectionItem]
    ) -> Path:
        directory = self.paths.root / "exports" / "visual-evidence"
        ensure_private_directory(directory, boundary=self.paths.root)
        inspection_items = [
            {
                "rendered_representation_key": item.rendered_representation_key,
                "source_page_index": item.source_page_index,
                "source_page_number": item.source_page_number,
                "rendered_sha256": item.rendered_sha256,
            }
            for item in items
        ]
        bundle_identity = _settings_hash(inspection_items)[:12]
        path = directory / (f"parse-{parse_key}-{source_sha256[:12]}-{bundle_identity}.json")
        payload = {
            "schema_version": _BUNDLE_SCHEMA,
            "parse_key": parse_key,
            "resource_sha256": source_sha256,
            "results": [],
            "inspection_items": inspection_items,
        }
        if path.exists():
            existing = self._read_bundle(self._safe_bundle_path(path))
            if (
                existing["parse_key"] != parse_key
                or existing["resource_sha256"] != source_sha256
                or existing["inspection_items"] != inspection_items
            ):
                raise VisualEvidenceError("visual evidence bundle template identity changed")
            return path
        temporary = directory / f".{path.name}-{uuid.uuid4().hex}"
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(_canonical(payload) + "\n")
            os.replace(temporary, path)
        except OSError:
            temporary.unlink(missing_ok=True)
            raise VisualEvidenceError(
                "visual evidence bundle template could not be written"
            ) from None
        return path

    def _safe_bundle_path(self, path: Path) -> Path:
        lexical = Path(os.path.abspath(os.fspath(path.expanduser())))
        try:
            lexical.relative_to(self.paths.root)
        except ValueError:
            raise VisualEvidenceError(
                "visual evidence bundle must stay in the private runtime"
            ) from None
        if lexical.is_symlink():
            raise VisualEvidenceError("visual evidence bundle path is unsafe")
        try:
            resolved = lexical.resolve(strict=True)
            resolved.relative_to(self.paths.root.resolve())
            metadata = resolved.lstat()
        except (OSError, ValueError):
            raise VisualEvidenceError("visual evidence bundle path is unsafe") from None
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_BUNDLE_BYTES:
            raise VisualEvidenceError("visual evidence bundle file is invalid")
        return resolved

    @staticmethod
    def _read_bundle(path: Path) -> dict[str, object]:
        try:
            payload = json.loads(
                path.read_text(encoding="utf-8"),
                parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()),
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, RecursionError):
            raise VisualEvidenceError("visual evidence bundle is invalid") from None
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "parse_key",
            "resource_sha256",
            "results",
            "inspection_items",
        }:
            raise VisualEvidenceError("visual evidence bundle schema is invalid")
        if payload.get("schema_version") != _BUNDLE_SCHEMA:
            raise VisualEvidenceError("visual evidence bundle schema is unsupported")
        inspection_items = payload.get("inspection_items")
        if (
            not isinstance(inspection_items, list)
            or len(inspection_items) > _MAX_RESULTS
            or any(
                not VisualEvidenceService._valid_inspection_item(item) for item in inspection_items
            )
        ):
            raise VisualEvidenceError("visual evidence inspection manifest is invalid")
        rendered_keys = [item["rendered_representation_key"] for item in inspection_items]
        if len(rendered_keys) != len(set(rendered_keys)):
            raise VisualEvidenceError("visual evidence inspection manifest repeats a page")
        return payload

    @staticmethod
    def _valid_inspection_item(raw: object) -> bool:
        if not isinstance(raw, dict) or set(raw) != {
            "rendered_representation_key",
            "source_page_index",
            "source_page_number",
            "rendered_sha256",
        }:
            return False
        key = raw["rendered_representation_key"]
        page_index = raw["source_page_index"]
        page_number = raw["source_page_number"]
        digest = raw["rendered_sha256"]
        return (
            isinstance(key, int)
            and not isinstance(key, bool)
            and key > 0
            and isinstance(page_index, int)
            and not isinstance(page_index, bool)
            and page_index >= 0
            and page_number == page_index + 1
            and isinstance(digest, str)
            and len(digest) == 64
            and all(character in "0123456789abcdef" for character in digest)
        )

    @staticmethod
    def _validate_result(raw: object) -> dict[str, object]:
        expected = {
            "rendered_representation_key",
            "text",
            "engine_version",
            "settings",
            "confidence",
            "review_status",
            "uncertainty",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise VisualEvidenceError("visual evidence result schema is invalid")
        text = raw.get("text")
        engine_version = raw.get("engine_version")
        confidence = raw.get("confidence")
        uncertainty = raw.get("uncertainty")
        if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARACTERS:
            raise VisualEvidenceError("visual evidence text is invalid")
        if (
            not isinstance(engine_version, str)
            or not engine_version.strip()
            or len(engine_version) > 120
        ):
            raise VisualEvidenceError("visual evidence engine version is invalid")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise VisualEvidenceError("visual evidence confidence is invalid")
        if not 0.0 <= float(confidence) <= 1.0:
            raise VisualEvidenceError("visual evidence confidence is invalid")
        if raw.get("review_status") not in set(VisualReviewStatus):
            raise VisualEvidenceError("visual evidence review status is invalid")
        if (
            not isinstance(uncertainty, list)
            or len(uncertainty) > 20
            or any(
                not isinstance(item, str) or not item.strip() or len(item) > 200
                for item in uncertainty
            )
        ):
            raise VisualEvidenceError("visual evidence uncertainty is invalid")
        settings = raw.get("settings")
        try:
            settings_json = _canonical(settings)
        except (TypeError, ValueError, RecursionError):
            raise VisualEvidenceError("visual evidence settings are invalid") from None
        if not isinstance(settings, dict) or len(settings_json) > 8192:
            raise VisualEvidenceError("visual evidence settings are invalid")
        return raw

    @staticmethod
    def _positive_integer(value: object, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise VisualEvidenceError(f"{label} is invalid")
        return value

    def _safe_artifact(self, artifact_relpath: str | None) -> Path:
        if artifact_relpath is None:
            raise VisualEvidenceError("rendered page artifact is unavailable")
        path = self.paths.root / artifact_relpath
        try:
            path.resolve(strict=True).relative_to(self.paths.root.resolve())
            metadata = path.lstat()
        except (OSError, ValueError):
            raise VisualEvidenceError("rendered page artifact is unsafe") from None
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            raise VisualEvidenceError("rendered page artifact is unsafe")
        return path

    @staticmethod
    def _validate_render(path: Path) -> None:
        try:
            metadata = path.lstat()
            if (
                path.is_symlink()
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size <= 0
                or metadata.st_size > _MAX_RENDER_BYTES
            ):
                raise VisualEvidenceError("rendered page failed safety validation")
            with path.open("rb") as stream:
                if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                    raise VisualEvidenceError("rendered page format is invalid")
        except VisualEvidenceError:
            raise
        except OSError:
            raise VisualEvidenceError("rendered page failed safety validation") from None


__all__ = [
    "PageRenderer",
    "PdftoppmRenderer",
    "VisualEvidenceError",
    "VisualEvidenceService",
    "VisualImportItem",
    "VisualInspectionItem",
]
