"""Incremental, read-only resource fetching with explicit byte-verification receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import BinaryIO, Literal, cast

from ntulearn_skill.client import (
    AuthorizedReadSession,
    ContentSourceRecord,
    CourseSourceRecord,
    ReadPurpose,
    ResourceMetadataRecord,
    SessionExpired,
    SessionProvider,
    SourceCapability,
    SourceError,
    SourceProtocolError,
    SourceProvider,
)
from ntulearn_skill.core import AttachmentId, Availability, ContentId, CourseId
from ntulearn_skill.core.identifiers import require_identifier
from ntulearn_skill.core.models import (
    FetchDecision,
    ObservationStatus,
    from_storage_time,
    to_storage_time,
    utc_now,
)
from ntulearn_skill.storage.resources import (
    ResourceObservationRecord,
    ResourceRecord,
    ResourceStorageError,
    ResourceStore,
    ResourceVersionRecord,
)
from ntulearn_skill.sync.jobs import LocalJobPlanner, ResourceJobPlan

FetchOutcome = Literal[
    "NEW_VERSION",
    "UNCHANGED_ASSUMED",
    "UNCHANGED_HASH_VERIFIED",
    "BINARY_CHANGED",
    "FAILED",
]


@dataclass(frozen=True, slots=True)
class ResourceFetchResult:
    resource: ResourceRecord | None
    version: ResourceVersionRecord | None
    observation: ResourceObservationRecord | None
    fetch_decision: FetchDecision
    binary_changed: bool | None
    receipt_key: int | None
    job_plan: ResourceJobPlan | None
    error_category: str | None
    warning_codes: tuple[str, ...] = ()

    @property
    def outcome(self) -> FetchOutcome:
        if self.fetch_decision is FetchDecision.FAILED:
            return "FAILED"
        if self.fetch_decision is FetchDecision.NOT_NEEDED:
            return "UNCHANGED_ASSUMED"
        if self.binary_changed:
            return "BINARY_CHANGED"
        if self.fetch_decision is FetchDecision.REUSED_VERIFIED:
            return "UNCHANGED_HASH_VERIFIED"
        return "NEW_VERSION"


@dataclass(frozen=True, slots=True)
class _ComparisonBasis:
    metadata_fingerprint: str
    candidate_modified_at: str | None
    candidate_revision: str | None
    verified_at: datetime | None


@dataclass(frozen=True, slots=True)
class _ResolvedEvidence:
    metadata: ResourceMetadataRecord
    content_id: ContentId
    course_id: CourseId
    availability: Availability
    sanitized_metadata: dict[str, object]
    candidate_modified_at: str | None
    candidate_revision: str | None
    metadata_fingerprint: str


class ResourceFetchService:
    """Fetch one authorized resource without persisting ephemeral transport state."""

    def __init__(
        self,
        sessions: SessionProvider,
        source: SourceProvider,
        store: ResourceStore,
        jobs: LocalJobPlanner,
        *,
        verification_interval: timedelta = timedelta(days=7),
    ) -> None:
        if verification_interval <= timedelta(0):
            raise ValueError("verification interval must be positive")
        self.sessions = sessions
        self.source = source
        self.store = store
        self.jobs = jobs
        self.verification_interval = verification_interval

    def fetch(
        self,
        resource: ResourceMetadataRecord | AttachmentId,
        *,
        sync_run_key: int,
        content: ContentSourceRecord | ContentId | None = None,
        course: CourseSourceRecord | CourseId | None = None,
        verify: bool = False,
        observed_at: datetime | None = None,
    ) -> ResourceFetchResult:
        if sync_run_key <= 0:
            raise ValueError("sync run key must be positive")
        clock = observed_at or utc_now()
        # Validate the supplied clock before any source or local state change.
        to_storage_time(clock)
        attachment_id: AttachmentId = (
            resource.remote_id if isinstance(resource, ResourceMetadataRecord) else resource
        )
        require_identifier(attachment_id, AttachmentId)
        if attachment_id.provider != self.source.provider_name:
            raise ValueError("resource provider does not match the source provider")

        evidence: _ResolvedEvidence | None = None
        try:
            metadata = (
                resource
                if isinstance(resource, ResourceMetadataRecord)
                else self._metadata(resource)
            )
            evidence = self._resolve_evidence(metadata, content, course)
            current = self.store.repository.get_current_version(attachment_id)
            basis = self._comparison_basis(attachment_id, current)
            reason = self._policy_reason(evidence, current, basis, verify=verify, observed_at=clock)
            if reason == "within_verification_interval":
                assert current is not None and basis is not None and basis.verified_at is not None
                stored_resource, observation = self.store.observe(
                    attachment_id,
                    content_id=evidence.content_id,
                    sync_run_key=sync_run_key,
                    display_title=evidence.metadata.display_title,
                    original_filename=evidence.metadata.original_filename,
                    availability=evidence.availability,
                    observation_status=ObservationStatus.OBSERVED,
                    fetch_decision=FetchDecision.NOT_NEEDED,
                    sanitized_metadata=evidence.sanitized_metadata,
                    candidate_modified_at=evidence.candidate_modified_at,
                    candidate_revision=evidence.candidate_revision,
                    observed_at=clock,
                )
                receipt_key, plan, receipt_warnings = self._persist_receipt_and_jobs(
                    stored_resource,
                    observation,
                    current,
                    policy_reason=reason,
                    binary_changed=None,
                    verified_at=basis.verified_at,
                )
                return ResourceFetchResult(
                    stored_resource,
                    current,
                    observation,
                    FetchDecision.NOT_NEEDED,
                    None,
                    receipt_key,
                    plan,
                    None,
                    receipt_warnings,
                )

            previous_hash = None if current is None else current.sha256
            self.source.capabilities().require(SourceCapability.RESOURCE_STREAM)
            session = self._session(ReadPurpose.RESOURCE_STREAM)
            with self.source.open_resource_stream(session, attachment_id) as stream:
                write = self.store.ingest(
                    cast(BinaryIO, stream),
                    attachment_id,
                    content_id=evidence.content_id,
                    sync_run_key=sync_run_key,
                    display_title=evidence.metadata.display_title,
                    original_filename=evidence.metadata.original_filename,
                    declared_mime=evidence.metadata.declared_mime,
                    availability=evidence.availability,
                    observation_status=ObservationStatus.OBSERVED,
                    sanitized_metadata=evidence.sanitized_metadata,
                    candidate_modified_at=evidence.candidate_modified_at,
                    candidate_revision=evidence.candidate_revision,
                    observed_at=clock,
                )
            changed = None if previous_hash is None else previous_hash != write.version.sha256
            receipt_key, plan, receipt_warnings = self._persist_receipt_and_jobs(
                write.resource,
                write.observation,
                write.version,
                policy_reason=reason,
                binary_changed=changed,
                verified_at=clock,
            )
            final_warnings = list(receipt_warnings)
            if not write.view_ready:
                final_warnings.append("browse_view_requires_repair")
            return ResourceFetchResult(
                write.resource,
                write.version,
                write.observation,
                write.observation.fetch_decision,
                changed,
                receipt_key,
                plan,
                None,
                tuple(final_warnings),
            )
        except SessionExpired as error:
            try:
                self.sessions.invalidate(error.category)
            except Exception:
                # Invalidation is best-effort cleanup owned by the session provider.  Its
                # diagnostics may contain private auth state, so retain only the original safe
                # source category and continue through the normal durable failure path.
                pass
            return self._failed(attachment_id, evidence, sync_run_key, clock, error.category)
        except SourceError as error:
            return self._failed(attachment_id, evidence, sync_run_key, clock, error.category)
        except (ResourceStorageError, sqlite3.Error, OSError, TypeError, ValueError):
            return self._failed(
                attachment_id, evidence, sync_run_key, clock, "resource_storage_error"
            )
        except Exception:
            return self._failed(attachment_id, evidence, sync_run_key, clock, "source_unavailable")

    def _metadata(self, attachment_id: AttachmentId) -> ResourceMetadataRecord:
        local = self._local_metadata(attachment_id)
        if local is not None:
            return local
        self.source.capabilities().require(SourceCapability.RESOURCE_METADATA)
        session = self._session(ReadPurpose.RESOURCE_METADATA)
        result = self.source.get_resource_metadata(session, attachment_id)
        if type(result) is not ResourceMetadataRecord or result.remote_id != attachment_id:
            raise SourceProtocolError()
        return result

    def _local_metadata(self, attachment_id: AttachmentId) -> ResourceMetadataRecord | None:
        connection = self.store.repository.database.connect()
        try:
            row = connection.execute(
                """
                SELECT resource.display_title, observation.original_filename,
                       observation.sanitized_metadata_json,
                       observation.candidate_modified_at,
                       content_provider.name AS content_provider,
                       content_object.remote_key AS content_remote
                FROM resource
                JOIN source_object resource_object
                  ON resource_object.source_object_key = resource.source_object_key
                JOIN source_provider resource_provider
                  ON resource_provider.provider_key = resource_object.provider_key
                JOIN content_node ON content_node.content_key = resource.content_key
                JOIN source_object content_object
                  ON content_object.source_object_key = content_node.source_object_key
                JOIN source_provider content_provider
                  ON content_provider.provider_key = content_object.provider_key
                JOIN resource_observation observation
                  ON observation.resource_key = resource.resource_key
                WHERE resource_provider.name = ?
                  AND resource_object.object_kind = 'attachment'
                  AND resource_object.remote_key = ?
                ORDER BY observation.observed_at DESC, observation.observation_key DESC LIMIT 1
                """,
                (attachment_id.provider, attachment_id.value),
            ).fetchone()
            if row is None:
                return None
            metadata = json.loads(str(row["sanitized_metadata_json"]))
            declared_mime = metadata.get("content_type") if isinstance(metadata, dict) else None
            if declared_mime is not None and not isinstance(declared_mime, str):
                raise ValueError("stored resource content type is invalid")
            modified = (
                None
                if row["candidate_modified_at"] is None
                else from_storage_time(str(row["candidate_modified_at"]))
            )
            return ResourceMetadataRecord(
                attachment_id,
                ContentId(str(row["content_provider"]), str(row["content_remote"])),
                str(row["display_title"]),
                str(row["original_filename"]),
                declared_mime,
                modified,
            )
        finally:
            connection.close()

    def _session(self, purpose: ReadPurpose) -> AuthorizedReadSession:
        session = self.sessions.acquire(purpose)
        if (
            type(session) is not AuthorizedReadSession
            or session.provider != self.source.provider_name
            or not session.permits(purpose)
        ):
            raise SourceProtocolError()
        return session

    def _resolve_evidence(
        self,
        metadata: ResourceMetadataRecord,
        content: ContentSourceRecord | ContentId | None,
        course: CourseSourceRecord | CourseId | None,
    ) -> _ResolvedEvidence:
        if (
            type(metadata) is not ResourceMetadataRecord
            or type(metadata.remote_id) is not AttachmentId
            or type(metadata.content_id) is not ContentId
            or metadata.remote_id.provider != self.source.provider_name
            or metadata.content_id.provider != self.source.provider_name
            or not metadata.display_title.strip()
            or not metadata.original_filename
        ):
            raise SourceProtocolError()
        if type(content) is ContentSourceRecord:
            if content.remote_id != metadata.content_id:
                raise ValueError("resource and content evidence do not match")
            content_id = content.remote_id
            content_course = content.course_id
            availability = content.availability
        elif type(content) is ContentId:
            if content != metadata.content_id:
                raise ValueError("resource and content evidence do not match")
            content_id = content
            content_course = self._local_course_id(content_id)
            node = self.store.repository.database.connect()
            try:
                row = node.execute(
                    """
                    SELECT availability FROM content_node
                    JOIN source_object object
                      ON object.source_object_key = content_node.source_object_key
                    JOIN source_provider provider ON provider.provider_key = object.provider_key
                    WHERE provider.name = ? AND object.object_kind = 'content'
                      AND object.remote_key = ?
                    """,
                    (content_id.provider, content_id.value),
                ).fetchone()
                if row is None:
                    raise ValueError("resource content does not exist locally")
                availability = Availability(str(row["availability"]))
            finally:
                node.close()
        elif content is None:
            content_id = metadata.content_id
            content_course, availability = self._local_content_context(content_id)
        else:
            raise TypeError("content evidence must be typed")

        if type(course) is CourseSourceRecord:
            course_id = course.remote_id
        elif type(course) is CourseId:
            course_id = course
        elif course is None:
            course_id = content_course
        else:
            raise TypeError("course evidence must be typed")
        if course_id != content_course or course_id.provider != metadata.remote_id.provider:
            raise ValueError("resource, content, and course evidence do not match")

        candidate_modified = (
            None
            if metadata.candidate_modified_at is None
            else to_storage_time(metadata.candidate_modified_at)
        )
        sanitized: dict[str, object] = {
            "content_type": metadata.declared_mime,
            "display_name": metadata.display_title,
        }
        encoded = json.dumps(sanitized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return _ResolvedEvidence(
            metadata,
            content_id,
            course_id,
            availability,
            sanitized,
            candidate_modified,
            None,
            hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
        )

    def _local_course_id(self, content_id: ContentId) -> CourseId:
        course, _availability = self._local_content_context(content_id)
        return course

    def _local_content_context(self, content_id: ContentId) -> tuple[CourseId, Availability]:
        connection = self.store.repository.database.connect()
        try:
            row = connection.execute(
                """
                SELECT course_provider.name AS course_provider,
                       course_object.remote_key AS course_remote,
                       node.availability
                FROM content_node node
                JOIN source_object content_object
                  ON content_object.source_object_key = node.source_object_key
                JOIN source_provider content_provider
                  ON content_provider.provider_key = content_object.provider_key
                JOIN course ON course.course_key = node.course_key
                JOIN source_object course_object
                  ON course_object.source_object_key = course.source_object_key
                JOIN source_provider course_provider
                  ON course_provider.provider_key = course_object.provider_key
                WHERE content_provider.name = ? AND content_object.object_kind = 'content'
                  AND content_object.remote_key = ?
                """,
                (content_id.provider, content_id.value),
            ).fetchone()
            if row is None:
                raise ValueError("resource content does not exist locally")
            return (
                CourseId(str(row["course_provider"]), str(row["course_remote"])),
                Availability(str(row["availability"])),
            )
        finally:
            connection.close()

    def _comparison_basis(
        self,
        attachment_id: AttachmentId,
        current: ResourceVersionRecord | None,
    ) -> _ComparisonBasis | None:
        if current is None:
            return None
        connection = self.store.repository.database.connect()
        try:
            resource = connection.execute(
                """
                SELECT resource.resource_key FROM resource
                JOIN source_object object ON object.source_object_key = resource.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE provider.name = ? AND object.object_kind = 'attachment'
                  AND object.remote_key = ?
                """,
                (attachment_id.provider, attachment_id.value),
            ).fetchone()
            if resource is None:
                return None
            resource_key = int(resource["resource_key"])
            # The comparison evidence must describe the authoritative current version.  A
            # delayed observation can create or reuse another immutable version without becoming
            # current; using that observation would allow metadata to suppress the bytes that are
            # actually current.
            receipt = connection.execute(
                """
                SELECT observation.metadata_fingerprint,
                       observation.candidate_modified_at,
                       observation.candidate_revision,
                       receipt.verified_at
                FROM resource_fetch_receipt receipt
                JOIN resource_observation observation
                  ON observation.observation_key = receipt.observation_key
                WHERE receipt.resource_key = ? AND receipt.version_key = ?
                  AND receipt.fetch_decision IN ('FETCHED', 'REUSED_VERIFIED', 'NOT_NEEDED')
                ORDER BY receipt.observed_at DESC, receipt.fetch_receipt_key DESC LIMIT 1
                """,
                (resource_key, current.key),
            ).fetchone()
            if receipt is not None:
                return _ComparisonBasis(
                    str(receipt["metadata_fingerprint"]),
                    None
                    if receipt["candidate_modified_at"] is None
                    else str(receipt["candidate_modified_at"]),
                    None
                    if receipt["candidate_revision"] is None
                    else str(receipt["candidate_revision"]),
                    from_storage_time(str(receipt["verified_at"])),
                )
            observation = connection.execute(
                """
                SELECT metadata_fingerprint, candidate_modified_at, candidate_revision,
                       observed_at AS verified_at
                FROM resource_observation
                WHERE resource_key = ? AND version_key = ?
                  AND fetch_decision IN ('FETCHED', 'REUSED_VERIFIED')
                ORDER BY observed_at DESC, observation_key DESC LIMIT 1
                """,
                (resource_key, current.key),
            ).fetchone()
            if observation is None:
                return None
            return _ComparisonBasis(
                str(observation["metadata_fingerprint"]),
                None
                if observation["candidate_modified_at"] is None
                else str(observation["candidate_modified_at"]),
                None
                if observation["candidate_revision"] is None
                else str(observation["candidate_revision"]),
                from_storage_time(str(observation["verified_at"])),
            )
        finally:
            connection.close()

    def _policy_reason(
        self,
        evidence: _ResolvedEvidence,
        current: ResourceVersionRecord | None,
        basis: _ComparisonBasis | None,
        *,
        verify: bool,
        observed_at: datetime,
    ) -> str:
        if current is None:
            return "new_resource"
        if verify:
            return "verification_requested"
        if basis is None:
            return "metadata_unknown"
        if (
            evidence.metadata_fingerprint != basis.metadata_fingerprint
            or evidence.candidate_modified_at != basis.candidate_modified_at
            or evidence.candidate_revision != basis.candidate_revision
        ):
            return "metadata_changed"
        if basis.verified_at is None:
            return "metadata_unknown"
        if observed_at - basis.verified_at >= self.verification_interval:
            return "verification_expired"
        return "within_verification_interval"

    def _persist_receipt_and_jobs(
        self,
        resource: ResourceRecord,
        observation: ResourceObservationRecord,
        version: ResourceVersionRecord,
        *,
        policy_reason: str,
        binary_changed: bool | None,
        verified_at: datetime,
    ) -> tuple[int | None, ResourceJobPlan | None, tuple[str, ...]]:
        try:
            with self.store.repository.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO resource_fetch_receipt(
                        resource_key, observation_key, version_key, metadata_fingerprint,
                        fetch_decision, policy_reason, binary_changed, verified_at, observed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        resource.key,
                        observation.key,
                        version.key,
                        observation.metadata_fingerprint,
                        observation.fetch_decision.value,
                        policy_reason,
                        None if binary_changed is None else int(binary_changed),
                        to_storage_time(verified_at),
                        to_storage_time(observation.observed_at),
                    ),
                )
                assert cursor.lastrowid is not None
                plan = self.jobs.plan_resource(version.key, connection=connection)
                return int(cursor.lastrowid), plan, ()
        except Exception:
            # Binary ingestion and its immutable observation are already committed.  Report the
            # durable gap without pretending the fetch rolled back; a later call replans by input
            # identity and can safely repair the missing receipt/jobs.
            return None, None, ("receipt_and_jobs_pending_recovery",)

    def _failed(
        self,
        attachment_id: AttachmentId,
        evidence: _ResolvedEvidence | None,
        sync_run_key: int,
        observed_at: datetime,
        error_category: str,
    ) -> ResourceFetchResult:
        current = self.store.repository.get_current_version(attachment_id)
        resource = self.store.repository.get_resource(attachment_id)
        observation: ResourceObservationRecord | None = None
        receipt_key: int | None = None
        warnings: list[str] = []
        if evidence is not None:
            try:
                resource, observation = self.store.observe(
                    attachment_id,
                    content_id=evidence.content_id,
                    sync_run_key=sync_run_key,
                    display_title=evidence.metadata.display_title,
                    original_filename=evidence.metadata.original_filename,
                    availability=evidence.availability,
                    observation_status=ObservationStatus.UNKNOWN,
                    fetch_decision=FetchDecision.FAILED,
                    sanitized_metadata=evidence.sanitized_metadata,
                    candidate_modified_at=evidence.candidate_modified_at,
                    candidate_revision=evidence.candidate_revision,
                    observed_at=observed_at,
                )
                with self.store.repository.database.transaction() as connection:
                    cursor = connection.execute(
                        """
                        INSERT INTO resource_fetch_receipt(
                            resource_key, observation_key, metadata_fingerprint, fetch_decision,
                            policy_reason, observed_at, error_category
                        ) VALUES (?, ?, ?, 'FAILED', ?, ?, ?)
                        """,
                        (
                            resource.key,
                            observation.key,
                            observation.metadata_fingerprint,
                            "source_failure"
                            if error_category != "resource_storage_error"
                            else "storage_failure",
                            to_storage_time(observation.observed_at),
                            error_category[:120],
                        ),
                    )
                    assert cursor.lastrowid is not None
                    receipt_key = int(cursor.lastrowid)
            except Exception:
                warnings.append("failure_observation_not_recorded")
        return ResourceFetchResult(
            resource,
            current,
            observation,
            FetchDecision.FAILED,
            None,
            receipt_key,
            None,
            error_category,
            tuple(warnings),
        )


# Concise use-case name retained for callers that model this operation as a command service.
FetchResource = ResourceFetchService
