from __future__ import annotations

import io
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from reportlab.pdfgen import canvas

from ntulearn_skill.client import (
    AnnouncementSourceRecord,
    AuthorizedReadSession,
    CapabilityState,
    EphemeralByteStream,
    ReadPurpose,
    ResourceMetadataRecord,
    SessionExpired,
    SessionStatus,
    SourceCapabilities,
    SourceCapability,
    SourceUnavailable,
)
from ntulearn_skill.core import AnnouncementId, AttachmentId, Availability, ContentId, CourseId
from ntulearn_skill.events import DeterministicEventExtractor, EventReconciler, EventRepository
from ntulearn_skill.index import SearchIndex
from ntulearn_skill.parsers import (
    ParsedPayload,
    ParserDescriptor,
    ParserOptions,
    ParserRegistry,
    ParseService,
    PdfParser,
)
from ntulearn_skill.parsers.repository import ParseRepository
from ntulearn_skill.storage import (
    Database,
    DomainRepository,
    ResourceRepository,
    ResourceStore,
    ResourceVersionRecord,
    RuntimePaths,
)
from ntulearn_skill.sync.jobs import (
    LocalJobPlanner,
    LocalJobQueue,
    LocalJobRunner,
    LocalJobStatus,
)
from ntulearn_skill.sync.resources import ResourceFetchService


@dataclass
class _Sessions:
    acquired: list[ReadPurpose] = field(default_factory=list)
    invalidated: list[str] = field(default_factory=list)
    marker: object = field(default_factory=object)

    def acquire(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        self.acquired.append(purpose)
        return AuthorizedReadSession("synthetic", frozenset({purpose}), self.marker)

    def status(self) -> SessionStatus:
        return SessionStatus.READY

    def invalidate(self, reason: str) -> None:
        self.invalidated.append(reason)


class _Source:
    provider_name = "synthetic"

    def __init__(self, metadata: ResourceMetadataRecord, payloads: list[bytes | Exception]) -> None:
        self.metadata = metadata
        self.payloads = payloads
        self.stream_calls = 0
        self.metadata_calls = 0

    def capabilities(self) -> SourceCapabilities:
        return SourceCapabilities(
            {
                SourceCapability.RESOURCE_METADATA: CapabilityState.SUPPORTED,
                SourceCapability.RESOURCE_STREAM: CapabilityState.SUPPORTED,
            }
        )

    def get_resource_metadata(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> ResourceMetadataRecord:
        self.metadata_calls += 1
        assert resource == self.metadata.remote_id
        return self.metadata

    def open_resource_stream(
        self, session: AuthorizedReadSession, resource: AttachmentId
    ) -> EphemeralByteStream:
        self.stream_calls += 1
        assert resource == self.metadata.remote_id
        result = self.payloads.pop(0)
        if isinstance(result, Exception):
            raise result
        return EphemeralByteStream((result,))


class _VersionedPdfParser:
    def __init__(self, version: str) -> None:
        self.descriptor = ParserDescriptor(
            name="pypdf",
            version=version,
            engine_version=PdfParser.descriptor.engine_version,
            formats=frozenset({"pdf"}),
        )
        self.delegate = PdfParser()

    def parse(
        self, path: Path, version: ResourceVersionRecord, options: ParserOptions
    ) -> ParsedPayload:
        return self.delegate.parse(path, version, options)


@dataclass(frozen=True)
class _Harness:
    database: Database
    source: _Source
    sessions: _Sessions
    service: ResourceFetchService
    queue: LocalJobQueue
    planner: LocalJobPlanner
    runner: LocalJobRunner
    metadata: ResourceMetadataRecord


def _pdf(text: str) -> bytes:
    output = io.BytesIO()
    document = canvas.Canvas(output, pagesize=(612, 792), invariant=1)
    document.drawString(72, 720, text)
    document.save()
    return output.getvalue()


def _run(database: Database, when: datetime) -> int:
    with database.transaction() as connection:
        cursor = connection.execute(
            """
            INSERT INTO sync_run(mode, requested_scope_json, started_at, status)
            VALUES ('synthetic', '{}', ?, 'RUNNING')
            """,
            (when.isoformat(timespec="microseconds"),),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)


def _harness(tmp_path: Path, payloads: list[bytes | Exception]) -> _Harness:
    paths = RuntimePaths(tmp_path / "private-runtime")
    database = Database(paths.database)
    resources = ResourceRepository(database)
    store = ResourceStore(paths, resources)
    assert store.initialize() >= 9
    domain = DomainRepository(database)
    course = CourseId("synthetic", "course-one")
    content = ContentId("synthetic", "content-one")
    attachment = AttachmentId("synthetic", "attachment-one")
    domain.put_course(course, code="PH0000", title="Invented Course")
    domain.put_content_node(
        content,
        course_id=course,
        handler_kind="document",
        title="Invented Materials",
        position=0,
    )
    metadata = ResourceMetadataRecord(
        attachment,
        content,
        "Invented schedule",
        "invented-schedule.pdf",
        "application/pdf",
    )
    source = _Source(metadata, payloads)
    sessions = _Sessions()
    parser = ParseService(
        paths,
        resources,
        ParseRepository(database),
        ParserRegistry((PdfParser(),)),
    )
    extractor = DeterministicEventExtractor(database)
    reconciler = EventReconciler(database)
    queue = LocalJobQueue(database)
    planner = LocalJobPlanner(queue, parser, extractor, reconciler)
    runner = LocalJobRunner(queue, parser, SearchIndex(database), extractor, reconciler)
    service = ResourceFetchService(
        sessions,
        source,
        store,
        planner,
        verification_interval=timedelta(days=1),
    )
    return _Harness(database, source, sessions, service, queue, planner, runner, metadata)


def test_verification_interval_uses_last_verified_observation_clock(tmp_path: Path) -> None:
    first_bytes = _pdf("Assignment 1 due 2027-01-20")
    harness = _harness(tmp_path, [first_bytes, first_bytes])
    first_at = datetime(2027, 1, 10, tzinfo=UTC)

    first = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, first_at), observed_at=first_at
    )
    assumed_at = first_at + timedelta(hours=12)
    assumed = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, assumed_at),
        observed_at=assumed_at,
    )
    verified_at = first_at + timedelta(days=2)
    verified = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, verified_at),
        observed_at=verified_at,
    )

    assert first.outcome == "NEW_VERSION"
    assert assumed.outcome == "UNCHANGED_ASSUMED"
    assert verified.outcome == "UNCHANGED_HASH_VERIFIED"
    assert harness.source.stream_calls == 2
    assert first.version == assumed.version == verified.version
    connection = harness.database.connect()
    try:
        receipts = connection.execute(
            """SELECT fetch_decision, verified_at
            FROM resource_fetch_receipt ORDER BY fetch_receipt_key"""
        ).fetchall()
    finally:
        connection.close()
    assert [row["fetch_decision"] for row in receipts] == [
        "FETCHED",
        "NOT_NEEDED",
        "REUSED_VERIFIED",
    ]
    assert receipts[0]["verified_at"] == receipts[1]["verified_at"]
    assert receipts[2]["verified_at"] == verified_at.isoformat(timespec="microseconds")


def test_resource_policy_compares_the_same_sanitized_metadata_that_is_persisted(
    tmp_path: Path,
) -> None:
    payload = _pdf("Invented stable bytes")
    harness = _harness(tmp_path, [payload])
    first_at = datetime(2027, 1, 10, tzinfo=UTC)
    token_one = replace(
        harness.metadata,
        display_title=(
            "Synthetic Slides https://download.example.invalid/file?token=SYNTHETIC-TOKEN-ONE"
        ),
    )
    whitespace_variant = replace(
        harness.metadata,
        display_title=(
            "  Synthetic   Slides   https://download.example.invalid/file?token=SYNTHETIC-TOKEN-ONE"
        ),
    )
    token_two = replace(
        harness.metadata,
        display_title=(
            "Synthetic Slides https://download.example.invalid/file?token=SYNTHETIC-TOKEN-TWO"
        ),
    )

    first = harness.service.fetch(
        token_one,
        sync_run_key=_run(harness.database, first_at),
        observed_at=first_at,
    )
    repeated = harness.service.fetch(
        whitespace_variant,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=5)),
        observed_at=first_at + timedelta(minutes=5),
    )
    rotated = harness.service.fetch(
        token_two,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=10)),
        observed_at=first_at + timedelta(minutes=10),
    )

    assert first.outcome == "NEW_VERSION"
    assert repeated.outcome == rotated.outcome == "UNCHANGED_ASSUMED"
    assert first.version == repeated.version == rotated.version
    assert harness.source.stream_calls == 1
    assert len(harness.service.store.repository.list_versions(harness.metadata.remote_id)) == 1


def test_canonical_resource_metadata_is_reused_for_id_only_fetches(tmp_path: Path) -> None:
    payload = _pdf("Invented stable bytes")
    harness = _harness(tmp_path, [payload])
    first_at = datetime(2027, 1, 10, tzinfo=UTC)
    encoded_title = replace(
        harness.metadata,
        display_title="Synthetic &lt;Slides&gt;",
    )

    first = harness.service.fetch(
        encoded_title,
        sync_run_key=_run(harness.database, first_at),
        observed_at=first_at,
    )
    repeated = harness.service.fetch(
        encoded_title.remote_id,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=5)),
        observed_at=first_at + timedelta(minutes=5),
    )
    third = harness.service.fetch(
        encoded_title.remote_id,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=10)),
        observed_at=first_at + timedelta(minutes=10),
    )

    assert first.outcome == "NEW_VERSION"
    assert repeated.outcome == third.outcome == "UNCHANGED_ASSUMED"
    assert first.version == repeated.version == third.version
    assert harness.source.stream_calls == 1
    assert harness.source.metadata_calls == 0
    stored = harness.service.store.repository.get_resource(encoded_title.remote_id)
    assert stored is not None and stored.display_title == "Synthetic <Slides>"


def test_same_metadata_can_produce_binary_changed_after_forced_verification(
    tmp_path: Path,
) -> None:
    harness = _harness(
        tmp_path,
        [_pdf("Invented bytes one"), _pdf("Invented bytes two")],
    )
    first_at = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, first_at), observed_at=first_at
    )
    second = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=5)),
        verify=True,
        observed_at=first_at + timedelta(minutes=5),
    )

    assert first.outcome == "NEW_VERSION"
    assert second.outcome == "BINARY_CHANGED"
    assert first.version is not None and second.version is not None
    assert first.version.sha256 != second.version.sha256
    assert second.version.version_number == 2


def test_delayed_fetch_basis_cannot_describe_a_noncurrent_version(tmp_path: Path) -> None:
    bytes_a = _pdf("Invented bytes A")
    bytes_b = _pdf("Invented bytes B")
    harness = _harness(tmp_path, [bytes_a, bytes_b, bytes_b])
    time_zero = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, time_zero),
        observed_at=time_zero,
    )
    assert first.version is not None

    metadata_b = ResourceMetadataRecord(
        harness.metadata.remote_id,
        harness.metadata.content_id,
        "Invented renamed schedule",
        harness.metadata.original_filename,
        harness.metadata.declared_mime,
    )
    future_discovery = time_zero + timedelta(hours=2)
    discovery_run = _run(harness.database, future_discovery)
    harness.service.store.observe(
        metadata_b.remote_id,
        content_id=metadata_b.content_id,
        sync_run_key=discovery_run,
        display_title=metadata_b.display_title,
        original_filename=metadata_b.original_filename,
        sanitized_metadata={
            "content_type": metadata_b.declared_mime,
            "display_name": metadata_b.display_title,
        },
        observed_at=future_discovery,
    )
    delayed = harness.service.fetch(
        metadata_b,
        sync_run_key=_run(harness.database, time_zero + timedelta(hours=1)),
        observed_at=time_zero + timedelta(hours=1),
    )
    assert delayed.version is not None
    current_after_delayed = harness.service.store.repository.get_current_version(
        harness.metadata.remote_id
    )
    assert current_after_delayed == first.version

    repaired = harness.service.fetch(
        metadata_b,
        sync_run_key=_run(harness.database, time_zero + timedelta(hours=3)),
        observed_at=time_zero + timedelta(hours=3),
    )
    assert repaired.outcome == "BINARY_CHANGED"
    assert repaired.version == delayed.version
    assert harness.source.stream_calls == 3
    current = harness.service.store.repository.get_current_version(harness.metadata.remote_id)
    assert current == delayed.version


def test_reverting_to_an_older_immutable_version_is_still_binary_changed(tmp_path: Path) -> None:
    bytes_a = _pdf("Invented bytes A")
    bytes_b = _pdf("Invented bytes B")
    harness = _harness(tmp_path, [bytes_a, bytes_b, bytes_a])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    second = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=1)),
        verify=True,
        observed_at=timestamp + timedelta(minutes=1),
    )
    reverted = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=2)),
        verify=True,
        observed_at=timestamp + timedelta(minutes=2),
    )

    assert first.version is not None and second.version is not None
    assert reverted.version == first.version
    assert reverted.fetch_decision.value == "REUSED_VERIFIED"
    assert reverted.binary_changed is True
    assert reverted.outcome == "BINARY_CHANGED"
    assert len(harness.service.store.repository.list_versions(harness.metadata.remote_id)) == 2


def test_deferred_discovery_observation_cannot_compare_against_itself(tmp_path: Path) -> None:
    payload = _pdf("Invented stable bytes")
    harness = _harness(tmp_path, [payload, payload])
    first_at = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata.remote_id,
        sync_run_key=_run(harness.database, first_at),
        observed_at=first_at,
    )
    assert harness.source.metadata_calls == 1

    changed_metadata = ResourceMetadataRecord(
        harness.metadata.remote_id,
        harness.metadata.content_id,
        "Renamed invented schedule",
        harness.metadata.original_filename,
        harness.metadata.declared_mime,
    )
    second_at = first_at + timedelta(minutes=5)
    second_run = _run(harness.database, second_at)
    harness.service.store.observe(
        changed_metadata.remote_id,
        content_id=changed_metadata.content_id,
        sync_run_key=second_run,
        display_title=changed_metadata.display_title,
        original_filename=changed_metadata.original_filename,
        sanitized_metadata={
            "content_type": changed_metadata.declared_mime,
            "display_name": changed_metadata.display_title,
        },
        observed_at=second_at,
    )
    second = harness.service.fetch(
        changed_metadata,
        sync_run_key=second_run,
        observed_at=second_at + timedelta(seconds=1),
    )

    assert first.outcome == "NEW_VERSION"
    assert second.outcome == "UNCHANGED_HASH_VERIFIED"
    assert harness.source.stream_calls == 2


def test_failed_verification_preserves_current_version_and_records_safe_failure(
    tmp_path: Path,
) -> None:
    payload = _pdf("Invented retained bytes")
    harness = _harness(tmp_path, [payload, SourceUnavailable()])
    first_at = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, first_at), observed_at=first_at
    )
    failed = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, first_at + timedelta(minutes=10)),
        verify=True,
        observed_at=first_at + timedelta(minutes=10),
    )

    assert failed.outcome == "FAILED"
    assert failed.error_category == "source_unavailable"
    assert failed.version == first.version
    assert failed.observation is not None
    assert failed.observation.fetch_decision.value == "FAILED"
    current = harness.service.store.repository.get_current_version(harness.metadata.remote_id)
    assert current == first.version


def test_session_expiry_contains_a_failing_invalidation_hook(tmp_path: Path) -> None:
    private_canary = "synthetic-secret-invalidation-canary"

    class FailingInvalidationSessions(_Sessions):
        def invalidate(self, reason: str) -> None:
            self.invalidated.append(reason)
            raise RuntimeError(private_canary)

    payload = _pdf("Invented retained bytes")
    harness = _harness(tmp_path, [payload, SessionExpired()])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    first = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    sessions = FailingInvalidationSessions()
    harness.service.sessions = sessions
    failed = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=1)),
        verify=True,
        observed_at=timestamp + timedelta(minutes=1),
    )

    assert failed.outcome == "FAILED"
    assert failed.error_category == "session_expired"
    assert private_canary not in repr(failed)
    assert sessions.invalidated == ["session_expired"]
    assert harness.source.stream_calls == 2
    current = harness.service.store.repository.get_current_version(harness.metadata.remote_id)
    assert current == first.version


def test_job_plan_is_idempotent_and_runner_uses_real_local_services(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [_pdf("Assignment 1 due 2027-01-20")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, timestamp), observed_at=timestamp
    )
    assert fetched.version is not None and fetched.job_plan is not None

    duplicate = harness.planner.plan_resource(fetched.version.key)
    assert duplicate.keys == fetched.job_plan.keys
    assert len(harness.queue.list()) == 4

    result = harness.runner.run(max_jobs=4, now=timestamp + timedelta(minutes=1))
    assert result.claimed == result.succeeded == 4
    assert result.failed == 0
    assert {job.status for job in harness.queue.list()} == {LocalJobStatus.SUCCEEDED}


def test_scoped_runner_reports_truncated_work_without_claiming_other_jobs(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [_pdf("Invented resource backlog")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    assert fetched.job_plan is not None
    observed = EventRepository(harness.database).observe_announcement(
        AnnouncementSourceRecord(
            remote_id=AnnouncementId("synthetic", "announcement-scoped"),
            course_id=CourseId("synthetic", "course-one"),
            title="Invented scoped deadline",
            body="Assignment 4 due 2027-01-24",
            availability=Availability.ACTIVE,
        ),
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=1)),
        observed_at=timestamp + timedelta(minutes=1),
    )
    observation_plan = harness.planner.plan_observation(observed.observation.key)

    truncated = harness.runner.run(
        max_jobs=1,
        job_keys=observation_plan.keys,
        now=timestamp + timedelta(minutes=2),
    )
    assert truncated.succeeded == 1
    assert truncated.remaining == 1
    assert truncated.blocked == 0
    assert all(
        harness.queue.get(key).status is LocalJobStatus.PENDING  # type: ignore[union-attr]
        for key in fetched.job_plan.keys
    )
    completed = harness.runner.run(
        max_jobs=1,
        job_keys=observation_plan.keys,
        now=timestamp + timedelta(minutes=3),
    )
    assert completed.succeeded == 1
    assert completed.remaining == completed.blocked == 0


def test_runner_rejects_parser_drift_and_a_new_planner_replans(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [_pdf("Invented parser drift")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    assert fetched.version is not None and fetched.job_plan is not None
    drift_service = ParseService(
        harness.service.store.paths,
        harness.service.store.repository,
        ParseRepository(harness.database),
        ParserRegistry((_VersionedPdfParser("999"),)),
    )
    extractor = DeterministicEventExtractor(harness.database)
    reconciler = EventReconciler(harness.database)
    drift_runner = LocalJobRunner(
        harness.queue,
        drift_service,
        SearchIndex(harness.database),
        extractor,
        reconciler,
    )

    rejected = drift_runner.run(max_jobs=1, now=timestamp + timedelta(minutes=1))
    original = harness.queue.get(fetched.job_plan.parse.key)
    assert rejected.failed == 1
    assert original is not None
    assert original.last_error_category == "local_job_contract_mismatch"
    connection = harness.database.connect()
    try:
        assert connection.execute("SELECT COUNT(*) FROM parsed_document").fetchone()[0] == 0
    finally:
        connection.close()

    replacement = LocalJobPlanner(
        harness.queue, drift_service, extractor, reconciler
    ).plan_resource(fetched.version.key)
    assert replacement.parse.key != fetched.job_plan.parse.key
    completed = drift_runner.run(max_jobs=1, now=timestamp + timedelta(minutes=2))
    assert completed.succeeded == 1


def test_planned_parser_options_are_executed_from_the_durable_spec(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [_pdf("Invented parser settings")])
    options = ParserOptions(low_text_character_threshold=7, drawing_operator_threshold=19)
    planner = LocalJobPlanner(
        harness.queue,
        harness.runner.parse_service,
        harness.runner.extractor,
        harness.runner.reconciler,
        parser_options=options,
    )
    service = ResourceFetchService(
        harness.sessions,
        harness.source,
        harness.service.store,
        planner,
        verification_interval=timedelta(days=1),
    )
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    assert fetched.job_plan is not None

    result = harness.runner.run(max_jobs=1, now=timestamp + timedelta(minutes=1))
    assert result.succeeded == 1
    stored = harness.queue.get(fetched.job_plan.parse.key)
    assert stored is not None and stored.result is not None
    connection = harness.database.connect()
    try:
        parsed = connection.execute(
            "SELECT settings_hash FROM parsed_document WHERE parse_key = ?",
            (stored.result["parse_key"],),
        ).fetchone()
    finally:
        connection.close()
    assert parsed is not None and parsed["settings_hash"] == options.settings_hash


def test_document_extraction_is_pinned_to_its_parser_dependency(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [_pdf("Assignment 1 due 2027-01-20")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    assert fetched.version is not None and fetched.job_plan is not None
    assert harness.runner.run(max_jobs=1, now=timestamp + timedelta(minutes=1)).succeeded == 1
    parse_job = harness.queue.get(fetched.job_plan.parse.key)
    assert parse_job is not None and parse_job.result is not None
    planned_parse_key = parse_job.result["parse_key"]

    later_service = ParseService(
        harness.service.store.paths,
        harness.service.store.repository,
        ParseRepository(harness.database),
        ParserRegistry((_VersionedPdfParser("later"),)),
    )
    later_parse = later_service.parse_version(fetched.version.key)
    assert later_parse.document.key != planned_parse_key

    assert harness.runner.run(max_jobs=2, now=timestamp + timedelta(minutes=2)).succeeded == 2
    extract_job = harness.queue.get(fetched.job_plan.extract.key)
    assert extract_job is not None and extract_job.result is not None
    connection = harness.database.connect()
    try:
        extraction = connection.execute(
            "SELECT parse_key FROM extraction_record WHERE extraction_record_key = ?",
            (extract_job.result["extraction_record_key"],),
        ).fetchone()
    finally:
        connection.close()
    assert extraction is not None and extraction["parse_key"] == planned_parse_key


def test_structured_observation_uses_the_same_durable_extraction_queue(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    repository = EventRepository(harness.database)
    record = AnnouncementSourceRecord(
        remote_id=AnnouncementId("synthetic", "announcement-one"),
        course_id=CourseId("synthetic", "course-one"),
        title="Invented deadline",
        body="Assignment 2 due 2027-01-22",
        availability=Availability.ACTIVE,
    )
    observed = repository.observe_announcement(
        record,
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    repeated = repository.observe_announcement(
        record,
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=1)),
        observed_at=timestamp + timedelta(minutes=1),
    )

    first = harness.planner.plan_observation(observed.observation.key)
    duplicate = harness.planner.plan_observation(observed.observation.key)
    repeated_plan = harness.planner.plan_observation(repeated.observation.key)
    assert first.keys == duplicate.keys == repeated_plan.keys
    assert first.extract.payload["observation_key"] == observed.observation.key
    assert len(harness.queue.list()) == 2
    result = harness.runner.run(max_jobs=2, now=timestamp + timedelta(minutes=1))
    assert result.succeeded == 2
    assert {job.status for job in harness.queue.list()} == {LocalJobStatus.SUCCEEDED}


def test_runner_rejects_extractor_and_resolver_version_drift(tmp_path: Path) -> None:
    harness = _harness(tmp_path, [])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    observed = EventRepository(harness.database).observe_announcement(
        AnnouncementSourceRecord(
            remote_id=AnnouncementId("synthetic", "announcement-drift"),
            course_id=CourseId("synthetic", "course-one"),
            title="Invented drift deadline",
            body="Assignment 3 due 2027-01-23",
            availability=Availability.ACTIVE,
        ),
        sync_run_key=_run(harness.database, timestamp),
        observed_at=timestamp,
    )
    plan = harness.planner.plan_observation(observed.observation.key)

    class DriftExtractor(DeterministicEventExtractor):
        version = "999"

    drift_extractor = DriftExtractor(harness.database)
    extractor_runner = LocalJobRunner(
        harness.queue,
        harness.runner.parse_service,
        SearchIndex(harness.database),
        drift_extractor,
        harness.runner.reconciler,
    )
    assert extractor_runner.run(max_jobs=1, now=timestamp + timedelta(minutes=1)).failed == 1
    extract_job = harness.queue.get(plan.extract.key)
    assert extract_job is not None
    assert extract_job.last_error_category == "local_job_contract_mismatch"

    assert harness.queue.retry_failed(plan.extract.key, now=timestamp + timedelta(minutes=2))
    assert harness.runner.run(max_jobs=1, now=timestamp + timedelta(minutes=2)).succeeded == 1
    resolver_runner = LocalJobRunner(
        harness.queue,
        harness.runner.parse_service,
        SearchIndex(harness.database),
        harness.runner.extractor,
        EventReconciler(harness.database, resolver_version="999"),
    )
    assert resolver_runner.run(max_jobs=1, now=timestamp + timedelta(minutes=3)).failed == 1
    reconcile_job = harness.queue.get(plan.reconcile.key)
    assert reconcile_job is not None
    assert reconcile_job.last_error_category == "local_job_contract_mismatch"


def test_failed_and_interrupted_jobs_have_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, [_pdf("Assignment 1 due 2027-01-20")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    fetched = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, timestamp), observed_at=timestamp
    )
    assert fetched.job_plan is not None
    real_parse = harness.runner.parse_service.parse_version
    calls = 0

    def fail_once(*args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic private-looking parser detail")
        return real_parse(*args, **kwargs)

    monkeypatch.setattr(harness.runner.parse_service, "parse_version", fail_once)
    failed = harness.runner.run(max_jobs=1, now=timestamp + timedelta(minutes=1))
    parse_key = fetched.job_plan.parse.key
    assert failed.failed == 1
    assert harness.queue.get(parse_key).last_error_category == "runtime_error"  # type: ignore[union-attr]
    assert harness.queue.retry_failed(parse_key, now=timestamp + timedelta(minutes=2))
    retried = harness.runner.run(max_jobs=4, now=timestamp + timedelta(minutes=2))
    assert retried.succeeded == 4

    later_plan = harness.planner.plan_resource(fetched.version.key)
    assert later_plan.keys == fetched.job_plan.keys
    connection = harness.database.connect()
    try:
        connection.execute(
            """
                UPDATE local_job
                SET status = 'RUNNING', lease_expires_at = ?, worker_token = 'synthetic-worker',
                    completed_at = NULL, result_json = NULL
            WHERE job_key = ?
            """,
            ((timestamp - timedelta(minutes=1)).isoformat(timespec="microseconds"), parse_key),
        )
    finally:
        connection.close()
    assert harness.queue.recover_interrupted(now=timestamp) == 1
    assert harness.queue.get(parse_key).status is LocalJobStatus.PENDING  # type: ignore[union-attr]


def test_receipt_and_job_transaction_gap_is_reported_and_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = _harness(tmp_path, [_pdf("Invented recovery bytes")])
    timestamp = datetime(2027, 1, 10, tzinfo=UTC)
    real_plan = harness.planner.plan_resource

    def fail_plan(*args: object, **kwargs: object):
        raise ValueError("synthetic planning failure")

    monkeypatch.setattr(harness.planner, "plan_resource", fail_plan)
    fetched = harness.service.fetch(
        harness.metadata, sync_run_key=_run(harness.database, timestamp), observed_at=timestamp
    )
    assert fetched.outcome == "NEW_VERSION"
    assert fetched.receipt_key is None
    assert fetched.warning_codes == ("receipt_and_jobs_pending_recovery",)
    assert harness.queue.list() == ()

    monkeypatch.setattr(harness.planner, "plan_resource", real_plan)
    recovered = harness.service.fetch(
        harness.metadata,
        sync_run_key=_run(harness.database, timestamp + timedelta(minutes=5)),
        observed_at=timestamp + timedelta(minutes=5),
    )
    assert recovered.outcome == "UNCHANGED_ASSUMED"
    assert recovered.receipt_key is not None
    assert recovered.job_plan is not None
    assert len(harness.queue.list()) == 4
