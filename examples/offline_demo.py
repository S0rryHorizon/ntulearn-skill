"""Run the real local sync/search path against an invented, disposable course.

Install the package and the optional ``reportlab`` development dependency first.
This example has no NTULearn login, network transport, or persistent runtime root.
"""

from __future__ import annotations

import io
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from reportlab.pdfgen import canvas  # type: ignore[import-untyped]

from ntulearn_skill.client import (
    BrowserCaptureProvider,
    prepare_browser_capture_directory,
    write_browser_capture_manifest,
)
from ntulearn_skill.core import CourseId, Coverage
from ntulearn_skill.core.api import CoreService, CourseRef, SyncPolicy, TimeWindow
from ntulearn_skill.integrations.codex import CodexToolDispatcher
from ntulearn_skill.search import SearchEntityKind, SearchFilters, SearchQuery
from ntulearn_skill.storage import ResourceRepository

SEARCH_TERM = "spectralneedle"
MISSING_TERM = "absentneedle"
COURSE_REMOTE_ID = "invented-course"


def _write_synthetic_capture(root: Path, captured_at: datetime) -> Path:
    """Build invented browser-capture input, including a three-page PDF."""

    capture_root = prepare_browser_capture_directory(root)
    files = capture_root / "files"
    files.mkdir(mode=0o700)
    pdf_path = files / "invented-optics.pdf"
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    for text in (
        "Invented optics course: introduction.",
        f"The {SEARCH_TERM} marks physical page two of this invented handout.",
        "Invented optics course: final page.",
    ):
        document.drawString(72, 720, text)
        document.showPage()
    document.save()
    pdf_path.write_bytes(output.getvalue())
    pdf_path.chmod(0o600)

    course_path = "/synthetic/course/outline"
    capture_start = captured_at - timedelta(minutes=5)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "source_kind": "host_browser_ui",
        "capture_id": "offline-synthetic-demo",
        "capture_start_at": capture_start.isoformat(),
        "captured_at": captured_at.isoformat(),
        "expires_at": (captured_at + timedelta(hours=1)).isoformat(),
        "authentication_status": "READY",
        "course": {
            "coverage": "PARTIAL",
            "source_page_path": course_path,
            "item": {
                "remote_id": COURSE_REMOTE_ID,
                "code": "PH0000",
                "title": "Invented Optics Course",
                "term": "Synthetic term",
                "availability": "ACTIVE",
            },
        },
        "content": {
            "coverage": "PARTIAL",
            "source_page_path": course_path,
            "items": [
                {
                    "remote_id": "invented-handout",
                    "parent_remote_id": None,
                    "handler_kind": "file",
                    "title": "Invented optics handout",
                    "position": 0,
                    "availability": "ACTIVE",
                    "is_container": False,
                    "resources": [
                        {
                            "display_title": "Invented optics handout",
                            "original_filename": "invented-optics.pdf",
                            "declared_mime": "application/pdf",
                            "file": {
                                "relative_path": "files/invented-optics.pdf",
                                "downloaded_at": (captured_at - timedelta(minutes=1)).isoformat(),
                                "fresh_for_capture": True,
                            },
                        }
                    ],
                }
            ],
        },
        "announcements": {
            "coverage": "PARTIAL",
            "source_page_path": "/synthetic/course/announcements",
            "items": [],
        },
        "assessments": {
            "coverage": "PARTIAL",
            "source_page_path": "/synthetic/course/assessments",
            "items": [],
        },
    }
    return write_browser_capture_manifest(capture_root / "manifest.json", manifest)


def run_demo() -> None:
    """Print a compact walkthrough; every invocation gets a fresh temporary root."""

    with tempfile.TemporaryDirectory(prefix="ntulearn-offline-demo-") as temporary:
        private_root = Path(temporary)
        captured_at = datetime.now(UTC) - timedelta(seconds=1)
        manifest = _write_synthetic_capture(private_root / "capture", captured_at)
        service = CoreService.from_runtime(private_root / "runtime", browser_capture=manifest)
        course = CourseRef(
            remote_id=CourseId(BrowserCaptureProvider.provider_name, COURSE_REMOTE_ID)
        )
        policy = SyncPolicy(TimeWindow(captured_at - timedelta(days=1), captured_at))

        first_sync = service.sync_course(course, policy)
        if not first_sync.ok:
            raise RuntimeError("synthetic sync failed")
        materials = service.list_materials(course)
        if len(materials.items) != 1 or materials.items[0].version_key is None:
            raise RuntimeError("synthetic material was not versioned")
        material = materials.items[0]
        versions = ResourceRepository(service.database).list_versions(material.resource_id)
        if len(versions) != 1:
            raise RuntimeError("expected one synthetic resource version")

        dispatcher = CodexToolDispatcher(CoreService.from_runtime(private_root / "runtime"))
        search = dispatcher.call("search", {"query": SEARCH_TERM, "neighbor_count": 0})
        hits = search["items"]
        if not isinstance(hits, list) or len(hits) != 1:
            raise RuntimeError("synthetic PDF text was not indexed")
        hit = hits[0]
        if not isinstance(hit, dict) or not isinstance(hit.get("locator"), dict):
            raise RuntimeError("synthetic search hit has no source locator")
        locator = hit["locator"]
        if (
            locator.get("physical_page_index") != 1
            or hit.get("version_key") != material.version_key
        ):
            raise RuntimeError("synthetic hit points to the wrong page or version")
        provenance = search["provenance"]
        if not isinstance(provenance, list) or len(provenance) != 1:
            raise RuntimeError("synthetic search hit has no provenance")
        source = provenance[0]
        if not isinstance(source, dict):
            raise RuntimeError("synthetic source reference is invalid")
        resolved = dispatcher.call(
            "resolve_source",
            {"kind": source["source_kind"], "key": source["source_key"], "context_window": 0},
        )
        resolved_items = resolved["items"]
        if not isinstance(resolved_items, list) or not resolved_items or resolved["errors"]:
            raise RuntimeError("synthetic original-source resolution failed")
        resolved_source = resolved_items[0]
        if (
            not isinstance(resolved_source, dict)
            or resolved_source.get("version_key") != hit["version_key"]
        ):
            raise RuntimeError("resolved source points to the wrong version")
        chunks = resolved_source.get("chunks")
        if not isinstance(chunks, list) or len(chunks) != 1:
            raise RuntimeError("resolved source has no page text")
        chunk = chunks[0]
        if not isinstance(chunk, dict) or not isinstance(chunk.get("native_text"), str):
            raise RuntimeError("resolved source chunk is invalid")
        source_text = chunk["native_text"]
        if SEARCH_TERM not in source_text:
            raise RuntimeError("resolved source does not contain the searched text")

        second_sync = service.sync_course(course, policy)
        if not second_sync.ok:
            raise RuntimeError("repeat synthetic sync failed")
        repeated_versions = ResourceRepository(service.database).list_versions(material.resource_id)
        if len(repeated_versions) != 1 or repeated_versions[0].key != versions[0].key:
            raise RuntimeError("repeat sync created a duplicate version")

        missing = service.search_course(
            course,
            SearchQuery(
                MISSING_TERM,
                filters=SearchFilters(entity_kinds=frozenset({SearchEntityKind.CHUNK})),
            ),
        )
        if missing.items or missing.source_completeness is not Coverage.PARTIAL:
            raise RuntimeError("incomplete source coverage was lost")

        print("Offline synthetic NTULearn demo (temporary data deleted after this run)")
        print(f"Sync: one invented PDF, version {material.version_number}")
        print(
            f"Search: {SEARCH_TERM} -> version {hit['version_key']}, "
            f"physical page {locator['physical_page_index'] + 1}, "
            f"source locator {source['source_key']}"
        )
        print(f"Source text: {source_text.strip()}")
        print(f"Repeat sync: version count {len(versions)} -> {len(repeated_versions)}")
        print(
            f"Missing term: 0 local matches; source coverage="
            f"{missing.source_completeness.value}, overall={missing.completeness.value}. "
            "Incomplete coverage cannot prove remote absence."
        )


if __name__ == "__main__":
    run_demo()
