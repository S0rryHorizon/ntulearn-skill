"""Complete synthetic example of the private host-browser capture schema."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ntulearn_skill.client import (
    prepare_browser_capture_directory,
    write_browser_capture_manifest,
)


def _pdf(label: bytes = b"Synthetic browser capture") -> bytes:
    result = bytearray(b"%PDF-1.4\n% synthetic-only\n")
    object_offset = len(result)
    result.extend(b"1 0 obj\n<< /Type /Catalog /Label (" + label + b") >>\nendobj\n")
    xref_offset = len(result)
    result.extend(b"xref\n0 2\n0000000000 65535 f \n")
    result.extend(f"{object_offset:010d} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size 2 /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def synthetic_manifest(captured_at: datetime) -> dict[str, Any]:
    captured_at = captured_at.astimezone(UTC)
    started_at = captured_at - timedelta(minutes=8)
    return {
        "schema_version": 1,
        "source_kind": "host_browser_ui",
        "capture_id": "synthetic-capture-0001",
        "capture_start_at": started_at.isoformat(),
        "captured_at": captured_at.isoformat(),
        "expires_at": (captured_at + timedelta(hours=1)).isoformat(),
        "authentication_status": "READY",
        "course": {
            "coverage": "PARTIAL",
            "source_page_path": "/ultra/courses/_synthetic_course_1/outline",
            "item": {
                "remote_id": "_synthetic_course_1",
                "code": "PH0000",
                "title": "Example Physics Course",
                "term": "Synthetic Term",
                "availability": "ACTIVE",
            },
        },
        "content": {
            "coverage": "PARTIAL",
            "source_page_path": "/ultra/courses/_synthetic_course_1/outline",
            "items": [
                {
                    "remote_id": "_synthetic_parent_1",
                    "parent_remote_id": None,
                    "handler_kind": "folder",
                    "title": "Lecture Notes",
                    "position": 0,
                    "availability": "ACTIVE",
                    "is_container": True,
                    "sanitized_metadata": {"module_label": "Lecture Notes"},
                    "resources": [],
                },
                {
                    "remote_id": "_synthetic_file_1",
                    "parent_remote_id": "_synthetic_parent_1",
                    "handler_kind": "file",
                    "title": "Synthetic Course Overview",
                    "position": 0,
                    "availability": "ACTIVE",
                    "is_container": False,
                    "resources": [
                        {
                            "display_title": "Synthetic Course Overview",
                            "original_filename": "PH0000 Synthetic Course Overview.pdf",
                            "declared_mime": "application/pdf",
                            "file": {
                                "relative_path": "files/synthetic-course-overview.pdf",
                                "downloaded_at": (started_at + timedelta(minutes=3)).isoformat(),
                                "fresh_for_capture": True,
                            },
                        }
                    ],
                },
                {
                    "remote_id": "_synthetic_assessment_1",
                    "parent_remote_id": None,
                    "handler_kind": "Assignment",
                    "title": "Synthetic Assignment",
                    "position": 1,
                    "availability": "ACTIVE",
                    "is_container": False,
                    "resources": [],
                },
            ],
        },
        "announcements": {
            "coverage": "PARTIAL",
            "source_page_path": "/ultra/courses/_synthetic_course_1/announcements",
            "items": [
                {
                    "remote_id": "_synthetic_announcement_1",
                    "title": "Synthetic Welcome",
                    "body": "This announcement contains invented fixture text only.",
                    "availability": "ACTIVE",
                    "published_at": {"text": "Published on a synthetic course page"},
                }
            ],
        },
        "assessments": {
            "coverage": "PARTIAL",
            "source_page_path": "/ultra/courses/_synthetic_course_1/grades",
            "items": [
                {
                    "content_remote_id": "_synthetic_assessment_1",
                    "title": "Synthetic Assignment",
                    "kind_text": "Assignment",
                    "availability": "ACTIVE",
                    "due_at": {
                        "text": "36/9/4 23:59 (UTC+8)",
                        "source_timezone": "UTC+8",
                    },
                }
            ],
        },
    }


def write_synthetic_browser_bundle(root: Path, captured_at: datetime) -> Path:
    root = prepare_browser_capture_directory(root)
    files = root / "files"
    files.mkdir(mode=0o700)
    (files / "synthetic-course-overview.pdf").write_bytes(_pdf())
    manifest = root / "manifest.json"
    return write_browser_capture_manifest(manifest, synthetic_manifest(captured_at))
