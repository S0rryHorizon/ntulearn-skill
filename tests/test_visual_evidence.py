from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfWriter
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from ntulearn_skill.cli import run
from ntulearn_skill.cli._main import EXIT_INCOMPLETE
from ntulearn_skill.core import AttachmentId, ContentId, CourseId, Coverage
from ntulearn_skill.core.api import CoreService, ResourceRef
from ntulearn_skill.core.results import ResultEnvelope
from ntulearn_skill.events import DeterministicEventExtractor, EventReconciler
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.parsers import (
    ParserOptions,
    ParserRegistry,
    PdfParser,
    RepresentationKind,
    VisualReviewStatus,
)
from ntulearn_skill.parsers.repository import ParseRepository
from ntulearn_skill.parsers.service import ParseService
from ntulearn_skill.parsers.visual import VisualEvidenceError, VisualEvidenceService
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    RuntimePaths,
)


@dataclass(frozen=True)
class _Harness:
    paths: RuntimePaths
    database: Database
    store: ResourceStore
    parser: ParseService
    visual: VisualEvidenceService
    course: CourseId
    content: ContentId


class _SyntheticRenderer:
    method = "synthetic-page-render"
    engine_version = "synthetic-renderer-1"

    def __init__(self) -> None:
        self.calls: list[int] = []

    def render(self, source: Path, page_index: int, destination: Path, *, dpi: int) -> None:
        assert source.is_file()
        assert dpi == 144
        self.calls.append(page_index)
        image = Image.new("RGB", (32, 32), "white")
        image.save(destination, "PNG")


class _ChangedSyntheticRenderer(_SyntheticRenderer):
    engine_version = "synthetic-renderer-2"


def _harness(tmp_path: Path) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-synthetic-runtime").ensure()
    database = Database(paths.database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() >= 10
    course = CourseId("synthetic", "visual-course")
    content = ContentId("synthetic", "visual-content")
    domain = DomainRepository(database)
    domain.put_course(course, code="PH0000", title="Invented Visual Course")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Invented visual material",
        position=0,
    )
    repository = ParseRepository(database)
    parser = ParseService(paths, resources, repository, ParserRegistry((PdfParser(),)))
    return _Harness(
        paths,
        database,
        store,
        parser,
        VisualEvidenceService(paths, parser, repository),
        course,
        content,
    )


def _sync_run(database: Database) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic-visual', '{}', ?, 'RUNNING')""",
            (datetime(2032, 1, 1, tzinfo=UTC).isoformat(),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _mixed_pdf() -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(
        72,
        720,
        "Invented native-only introduction with enough ordinary text to avoid fallback.",
    )
    document.showPage()
    document.drawString(
        72,
        720,
        "The following due dates are shown in the schedule image below.",
    )
    image = Image.new("RGB", (640, 240), "white")
    drawing = ImageDraw.Draw(image)
    drawing.text((20, 20), "Portfolio submission deadline: 1 April 2032", fill="black")
    drawing.text((20, 80), "Report submission deadline: 15 April 2032", fill="black")
    encoded = io.BytesIO()
    image.save(encoded, "PNG")
    encoded.seek(0)
    document.drawImage(ImageReader(encoded), 72, 430, width=420, height=160)
    document.showPage()
    document.save()
    return output.getvalue()


def _many_flagged_pages_pdf(page_count: int) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    image = Image.new("RGB", (8, 8), "white")
    encoded = io.BytesIO()
    image.save(encoded, "PNG")
    encoded.seek(0)
    image_reader = ImageReader(encoded)
    for _index in range(page_count):
        document.drawString(72, 720, "Due dates shown in the schedule image.")
        document.drawImage(image_reader, 72, 640, width=20, height=20)
        document.showPage()
    document.save()
    return output.getvalue()


def _ingest_and_parse(harness: _Harness):
    written = harness.store.ingest(
        io.BytesIO(_mixed_pdf()),
        AttachmentId("synthetic", "mixed-visual-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented mixed visual schedule",
        original_filename="invented-mixed.pdf",
    )
    return written, harness.parser.parse_version(written.version.key)


def _fill_bundle(item, *, status: str = "NEEDS_REVIEW") -> None:
    payload = json.loads(item.bundle_path.read_text(encoding="utf-8"))
    payload["results"] = [
        {
            "rendered_representation_key": item.rendered_representation_key,
            "text": (
                "Portfolio submission deadline: 1 April 2032.\n"
                "Report submission deadline: 15 April 2032.\n"
                "Project presentation deadline: date unclear."
            ),
            "engine_version": "synthetic-host-view-1",
            "settings": {"instruction_version": "event-transcription-1"},
            "confidence": 0.55,
            "review_status": status,
            "uncertainty": ["Project presentation date is illegible"],
        }
    ]
    item.bundle_path.write_text(json.dumps(payload), encoding="utf-8")


def _fill_bundle_text(item, text: str, *, confidence: float = 0.55) -> None:
    payload = json.loads(item.bundle_path.read_text(encoding="utf-8"))
    payload["results"] = [
        {
            "rendered_representation_key": item.rendered_representation_key,
            "text": text,
            "engine_version": "synthetic-host-view-1",
            "settings": {"instruction_version": "event-transcription-1"},
            "confidence": confidence,
            "review_status": "NEEDS_REVIEW",
            "uncertainty": [],
        }
    ]
    item.bundle_path.write_text(json.dumps(payload), encoding="utf-8")


def test_mixed_image_page_is_selected_import_is_idempotent_and_native_is_immutable(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(harness)
    original_blob = harness.paths.root / written.version.blob_relpath
    original_hash = hashlib.sha256(original_blob.read_bytes()).hexdigest()
    original_native = tuple(chunk.native_text for chunk in parsed.chunks)
    renderer = _SyntheticRenderer()
    native_extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=renderer)
    repeated_prepare = harness.visual.prepare(parsed.document.key, dpi=144, renderer=renderer)

    assert [item.source_page_index for item in prepared] == [1]
    assert renderer.calls == [1]
    assert (
        repeated_prepare[0].rendered_representation_key == prepared[0].rendered_representation_key
    )
    assert prepared[0].review_status is VisualReviewStatus.NEEDS_REVIEW
    assert (
        prepared[0].rendered_sha256
        == hashlib.sha256(prepared[0].local_path.read_bytes()).hexdigest()
    )

    _fill_bundle(prepared[0])
    core = CoreService(harness.database, runtime_paths=harness.paths)
    resource_result = core.get_resource(ResourceRef(local_key=written.resource.key))
    assert resource_result.items[0].parse_key == parsed.document.key
    import_result = core.import_visual_evidence(prepared[0].bundle_path)
    repeated_result = core.import_visual_evidence(prepared[0].bundle_path)
    assert import_result.ok and repeated_result.ok
    imported = import_result.items
    repeated_import = repeated_result.items

    assert imported[0].representation_key == repeated_import[0].representation_key
    assert imported[0].source_sha256 == written.version.sha256
    assert imported[0].rendered_sha256 == prepared[0].rendered_sha256
    assert imported[0].review_status is VisualReviewStatus.NEEDS_REVIEW
    assert imported[0].uncertainty == ("Project presentation date is illegible",)
    assert hashlib.sha256(original_blob.read_bytes()).hexdigest() == original_hash
    reparsed = harness.parser.repository.get(parsed.document.key)
    assert reparsed is not None
    assert tuple(chunk.native_text for chunk in reparsed.chunks) == original_native

    extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )
    cached_extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )
    assert extraction.extraction_record_key != native_extraction.extraction_record_key
    assert cached_extraction.extraction_record_key == extraction.extraction_record_key
    assert cached_extraction.cache_hit
    with harness.database.connect() as connection:
        extraction_row = connection.execute(
            """SELECT status, warning_codes_json FROM extraction_record
            WHERE extraction_record_key = ?""",
            (extraction.extraction_record_key,),
        ).fetchone()
    assert extraction_row is not None
    assert extraction_row["status"] == "PARTIAL"
    assert "visual_evidence_needs_review" in json.loads(extraction_row["warning_codes_json"])
    visual_candidates = [
        candidate
        for candidate in extraction.candidates
        if any(field.source_path.startswith("representation.") for field in candidate.fields)
    ]
    assert {candidate.fields[0].value for candidate in visual_candidates} == {
        "Portfolio submission",
        "Report submission",
    }
    assert all(candidate.confidence == 0.55 for candidate in visual_candidates)

    reconciled = EventReconciler(harness.database).reconcile_course(harness.course)
    visual_claims = [
        claim
        for event in reconciled.events
        for claim in event.claims
        if isinstance(claim.value, str)
        and claim.value in {"Portfolio submission", "Report submission"}
    ]
    assert visual_claims
    assert all(claim.confidence == 0.55 for claim in visual_claims)


def test_generic_submission_heading_uses_actionable_selection_title(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    text = document.beginText(72, 720)
    text.textLine("Assignment submission")
    text.textLine("Select a research topic from the catalogue by 12 March 2032.")
    document.drawText(text)
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "selection-title-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented selection instructions",
        original_filename="invented-selection.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    assert len(result.candidates) == 1
    title = next(field for field in result.candidates[0].fields if field.name.value == "title")
    assert title.value == "topic selection"
    assert title.original_text == "Select a research topic"


def test_visual_numeric_dates_use_unambiguous_same_page_order_and_keep_verbatim(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle_text(
        prepared[0],
        "Assignment Alpha due date: 4/9/32, 11:59 PM (UTC+8)\nAssignment Beta deadline: 10/23/32",
    )
    harness.visual.import_bundle(prepared[0].bundle_path)

    extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    due_fields = {
        next(field.value for field in candidate.fields if field.name.value == "title"): next(
            field for field in candidate.fields if field.name.value == "due_time"
        )
        for candidate in extraction.candidates
    }
    assert due_fields["Assignment Alpha"].original_text == ("4/9/32, 11:59 PM (UTC+8)")
    assert due_fields["Assignment Alpha"].value["instant"] == ("2032-04-09T15:59:00.000000+00:00")
    assert due_fields["Assignment Beta"].value["date"] == "2032-10-23"


def test_standalone_ambiguous_visual_numeric_date_remains_unknown(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle_text(prepared[0], "Assignment Gamma due date: 4/9/32, 11:59 PM (UTC+8)")
    harness.visual.import_bundle(prepared[0].bundle_path)

    extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    due = next(field for field in extraction.candidates[0].fields if field.name.value == "due_time")
    assert due.original_text == "4/9/32, 11:59 PM (UTC+8)"
    assert due.precision.value == "UNKNOWN"
    assert due.value["instant"] is None
    assert due.value["possible_dates"] == ["2032-04-09", "2032-09-04"]


def test_unambiguous_equal_numeric_human_meridiem_and_week_refinement(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    text = document.beginText(72, 720)
    text.textLine("Assignment Delta due 4/4/32")
    text.textLine("Assignment Epsilon due 15 April 2032, 11:30 PM (UTC+8)")
    text.textLine("Assignment Zeta (30%) due on Week 12 (by 18 April 2032)")
    document.drawText(text)
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "date-forms-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented date forms",
        original_filename="invented-date-forms.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    due_by_title = {
        next(field.value for field in candidate.fields if field.name.value == "title"): next(
            field for field in candidate.fields if field.name.value == "due_time"
        )
        for candidate in extraction.candidates
    }
    assert due_by_title["Assignment Delta"].value["date"] == "2032-04-04"
    assert due_by_title["Assignment Epsilon"].value["instant"] == (
        "2032-04-15T15:30:00.000000+00:00"
    )
    assert due_by_title["Assignment Zeta (30%)"].value["date"] == "2032-04-18"


def test_dense_pure_vector_layout_is_flagged_without_claiming_visible_text(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, "Invented assignment diagram overview")
    for index in range(260):
        document.rect(72 + index % 20, 400 + index % 40, 10, 10, stroke=1, fill=0)
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "dense-visual-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented dense diagram",
        original_filename="invented-dense.pdf",
    )

    parsed = harness.parser.parse_version(written.version.key)

    assert parsed.chunks[0].diagnostic is not None
    assert parsed.chunks[0].diagnostic.fallback_recommended
    assert "dense_visual_layout_with_limited_text" in parsed.chunks[0].diagnostic.reasons


def test_changed_renderer_identity_does_not_relabel_cached_bytes(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(harness)
    first_renderer = _SyntheticRenderer()
    second_renderer = _ChangedSyntheticRenderer()

    first = harness.visual.prepare(parsed.document.key, dpi=144, renderer=first_renderer)
    second = harness.visual.prepare(parsed.document.key, dpi=144, renderer=second_renderer)

    assert first_renderer.calls == [1]
    assert second_renderer.calls == [1]
    assert first[0].local_path != second[0].local_path
    assert first[0].rendered_representation_key != second[0].rendered_representation_key


def test_more_than_bundle_page_limit_is_rejected_before_rendering(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written = harness.store.ingest(
        io.BytesIO(_many_flagged_pages_pdf(65)),
        AttachmentId("synthetic", "many-flagged-pages-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented oversized visual schedule",
        original_filename="invented-many-pages.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)
    renderer = _SyntheticRenderer()

    with pytest.raises(VisualEvidenceError):
        harness.visual.prepare(parsed.document.key, dpi=144, renderer=renderer)

    assert renderer.calls == []


def test_bundle_cannot_switch_to_another_parse_of_the_same_version(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle(prepared[0])
    alternate = harness.parser.parse_version(
        written.version.key,
        options=ParserOptions(diagnostic_version="stage-b-1"),
    )
    payload = json.loads(prepared[0].bundle_path.read_text(encoding="utf-8"))
    payload["parse_key"] = alternate.document.key
    prepared[0].bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisualEvidenceError):
        harness.visual.import_bundle(prepared[0].bundle_path)


def test_corrected_visual_text_replaces_active_extraction_and_search_view(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    core = CoreService(harness.database, runtime_paths=harness.paths)
    _fill_bundle_text(prepared[0], "ObsoleteQuartz Assignment due 1 April 2032", confidence=0.6)
    assert core.import_visual_evidence(prepared[0].bundle_path).ok
    _fill_bundle_text(prepared[0], "CurrentTopaz Assignment due 2 April 2032", confidence=0.0)
    assert core.import_visual_evidence(prepared[0].bundle_path).ok

    extraction = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )
    visual_candidates = [
        candidate
        for candidate in extraction.candidates
        if any(field.source_path.startswith("representation.") for field in candidate.fields)
    ]
    assert len(visual_candidates) == 1
    assert visual_candidates[0].confidence == 0.0
    assert visual_candidates[0].raw_wording.startswith("CurrentTopaz")
    with harness.database.connect() as connection:
        indexed = connection.execute(
            """SELECT body FROM search_document
            WHERE text_origin = 'derived:vision_description'"""
        ).fetchall()
    assert [str(row["body"]) for row in indexed] == ["CurrentTopaz Assignment due 2 April 2032"]


def test_repeated_import_retries_a_pending_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle_text(prepared[0], "Retryable Assignment due 3 April 2032")
    core = CoreService(harness.database, runtime_paths=harness.paths)
    original_rebuild = SearchIndex.rebuild
    attempts = 0

    def flaky_rebuild(index: SearchIndex):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("synthetic projection interruption")
        return original_rebuild(index)

    monkeypatch.setattr(SearchIndex, "rebuild", flaky_rebuild)

    first = core.import_visual_evidence(prepared[0].bundle_path)
    second = core.import_visual_evidence(prepared[0].bundle_path)

    assert first.ok and second.ok
    assert [warning.code for warning in first.warnings][-1] == "visual_projection_pending"
    assert "visual_projection_pending" not in {warning.code for warning in second.warnings}
    assert first.items[0].representation_key == second.items[0].representation_key


def test_invalid_later_result_does_not_partially_import_bundle(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle(prepared[0])
    payload = json.loads(prepared[0].bundle_path.read_text(encoding="utf-8"))
    invalid = dict(payload["results"][0])
    invalid["text"] = ""
    payload["results"].append(invalid)
    prepared[0].bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisualEvidenceError):
        harness.visual.import_bundle(prepared[0].bundle_path)

    representations = harness.parser.repository.list_representations(prepared[0].chunk_key)
    assert all(
        item.representation_kind is not RepresentationKind.VISION_DESCRIPTION
        for item in representations
    )


def test_table_like_ordinal_boundary_does_not_attach_the_next_row_date(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    text = document.beginText(72, 720)
    for line in (
        "Invented Project Proposals (for Tutorial Presentations)",
        "Alex North",
        "Invented Room 2",
        "4",
        "24 August 2032",
    ):
        text.textLine(line)
    document.drawText(text)
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "row-boundary-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented row schedule",
        original_filename="invented-rows.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    assert result.candidates == ()


def test_leading_person_name_is_not_part_of_assignment_title(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, "Instructor: Alex North Assignment 1 due on Week 10")
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "clean-title-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented assignment table",
        original_filename="invented-assignment.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    title = next(field for field in result.candidates[0].fields if field.name.value == "title")
    assert title.value == "Assignment 1"
    assert title.original_text == "Assignment 1"


def test_assignment_title_modifier_is_not_mistaken_for_a_person_name(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, "Final Project Assignment 1 due on Week 10")
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "modified-title-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented modified assignment",
        original_filename="invented-modified-assignment.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    title = next(field for field in result.candidates[0].fields if field.name.value == "title")
    assert title.value == "Final Project Assignment 1"


def test_subject_words_are_not_mistaken_for_a_person_name(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    document = canvas.Canvas(payload, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, "Quantum Physics Assignment 2 due on Week 11")
    document.save()
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "subject-title-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented subject assignment",
        original_filename="invented-subject-assignment.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    title = next(field for field in result.candidates[0].fields if field.name.value == "title")
    assert title.value == "Quantum Physics Assignment 2"


def test_unsupported_parse_produces_auditable_partial_extraction(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    written = harness.store.ingest(
        io.BytesIO(b"synthetic unsupported bytes"),
        AttachmentId("synthetic", "unsupported-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented unsupported material",
        original_filename="invented.bin",
    )
    parsed = harness.parser.parse_version(written.version.key)

    extracted = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )
    repeated = DeterministicEventExtractor(harness.database).extract_resource_version(
        written.version.key, parse_key=parsed.document.key
    )

    assert extracted.candidates == ()
    assert repeated.cache_hit
    with harness.database.connect() as connection:
        row = connection.execute(
            """SELECT status, warning_codes_json FROM extraction_record
            WHERE extraction_record_key = ?""",
            (extracted.extraction_record_key,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "PARTIAL"
    assert json.loads(row["warning_codes_json"]) == ["parse_unsupported"]


def test_unsupported_pdf_parse_is_not_reported_as_complete_visual_empty(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path)
    payload = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.encrypt("synthetic-password")
    writer.write(payload)
    written = harness.store.ingest(
        io.BytesIO(payload.getvalue()),
        AttachmentId("synthetic", "encrypted-visual-resource"),
        content_id=harness.content,
        sync_run_key=_sync_run(harness.database),
        display_title="Invented encrypted material",
        original_filename="invented-encrypted.pdf",
    )
    parsed = harness.parser.parse_version(written.version.key)

    result = CoreService(harness.database, runtime_paths=harness.paths).prepare_visual_evidence(
        parsed.document.key
    )

    assert not result.ok
    assert result.completeness is Coverage.FAILED


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.update({"unexpected": "field"}),
        lambda payload: payload.update({"resource_sha256": "0" * 64}),
        lambda payload: payload["results"][0].update({"review_status": "COMPLETE"}),
        lambda payload: payload["results"][0].update({"confidence": True}),
        lambda payload: payload["results"][0].update({"text": ""}),
    ],
)
def test_hostile_or_ambiguous_bundle_is_rejected(tmp_path: Path, mutation) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle(prepared[0], status="PARTIAL")
    payload = json.loads(prepared[0].bundle_path.read_text(encoding="utf-8"))
    mutation(payload)
    prepared[0].bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisualEvidenceError):
        harness.visual.import_bundle(prepared[0].bundle_path)


def test_invalid_later_result_does_not_commit_an_earlier_result(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    _written, parsed = _ingest_and_parse(harness)
    prepared = harness.visual.prepare(parsed.document.key, dpi=144, renderer=_SyntheticRenderer())
    _fill_bundle(prepared[0])
    payload = json.loads(prepared[0].bundle_path.read_text(encoding="utf-8"))
    invalid = dict(payload["results"][0])
    invalid["rendered_representation_key"] = 999_999
    payload["results"].append(invalid)
    prepared[0].bundle_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(VisualEvidenceError):
        harness.visual.import_bundle(prepared[0].bundle_path)

    with harness.database.connect() as connection:
        stored = connection.execute(
            """SELECT COUNT(*) FROM chunk_representation
            WHERE representation_kind = 'vision_description'"""
        ).fetchone()[0]
    assert stored == 0


def test_bundle_outside_private_runtime_is_rejected(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")

    with pytest.raises(VisualEvidenceError):
        harness.visual.import_bundle(outside)


def test_visual_cli_delegates_prepare_and_private_bundle_import() -> None:
    class _Service:
        def __init__(self) -> None:
            self.calls: list[tuple[object, ...]] = []

        def prepare_visual_evidence(self, parse_key: int, *, dpi: int):
            self.calls.append(("prepare", parse_key, dpi))
            return ResultEnvelope("prepare_visual_evidence", completeness=Coverage.PARTIAL)

        def import_visual_evidence(self, bundle_path: str):
            self.calls.append(("import", bundle_path))
            return ResultEnvelope("import_visual_evidence", completeness=Coverage.PARTIAL)

    service = _Service()
    output = io.StringIO()

    assert (
        run(
            ["visual", "prepare", "7", "--dpi", "144", "--json"],
            service=service,  # type: ignore[arg-type]
            stdout=output,
        )
        == EXIT_INCOMPLETE
    )
    assert (
        run(
            ["visual", "import", "/private/runtime/result.json", "--json"],
            service=service,  # type: ignore[arg-type]
            stdout=output,
        )
        == EXIT_INCOMPLETE
    )
    assert service.calls == [
        ("prepare", 7, 144),
        ("import", "/private/runtime/result.json"),
    ]
