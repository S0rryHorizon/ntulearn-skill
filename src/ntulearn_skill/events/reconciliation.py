"""Conservative, deterministic M7 event reconciliation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import cast

from ntulearn_skill.core import CourseId, TemporalPrecision
from ntulearn_skill.core.identifiers import require_identifier
from ntulearn_skill.core.models import from_storage_time, to_storage_time, utc_now
from ntulearn_skill.events._activity import (
    active_claim_sql,
    active_event_source_sql,
    effective_extraction_sql,
)
from ntulearn_skill.events._matching import (
    change_applies,
    precision_rank,
    semantic_value,
    temporal_agrees,
    temporal_signature,
    title_identity,
)
from ntulearn_skill.events.extraction import classify_change_language
from ntulearn_skill.events.models import (
    CandidateFieldName,
    CandidateSourceKind,
    CanonicalEvent,
    ChangeKind,
    Claim,
    ClaimDecisionState,
    ClaimOrigin,
    ConflictState,
    EventConflict,
    EventProjection,
    EventResolutionState,
    EventSource,
    EventSourceResolutionState,
    EventStatus,
    EventType,
    JsonValue,
    ManualFieldResolution,
    ManualIdentityResolution,
    ReconciliationDecision,
    ReconciliationDecisionResult,
    ReconciliationResult,
)
from ntulearn_skill.storage import Database, DomainRepository, StorageError


class EventReconciliationError(StorageError):
    """A privacy-safe reconciliation persistence or hydration failure."""


_CANONICAL_FIELDS = (
    CandidateFieldName.TITLE,
    CandidateFieldName.EVENT_TYPE,
    CandidateFieldName.START_TIME,
    CandidateFieldName.END_TIME,
    CandidateFieldName.DUE_TIME,
    CandidateFieldName.LOCATION,
    CandidateFieldName.STATUS,
)
_TEMPORAL_FIELDS = {
    CandidateFieldName.START_TIME,
    CandidateFieldName.END_TIME,
    CandidateFieldName.DUE_TIME,
}
_ORDINALS = {
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
    "sixth": 6,
    "seventh": 7,
    "eighth": 8,
    "ninth": 9,
    "tenth": 10,
}


@dataclass(frozen=True, slots=True)
class _FieldData:
    key: int
    name: CandidateFieldName
    value: JsonValue
    value_json: str
    original_text: str
    precision: TemporalPrecision | None
    source_timezone: str | None
    observation_key: int | None
    locator_key: int | None


@dataclass(frozen=True, slots=True)
class _CandidateData:
    key: int
    course_key: int
    source_kind: CandidateSourceKind
    raw_wording: str
    confidence: float
    fields: tuple[_FieldData, ...]

    def field(self, name: CandidateFieldName) -> _FieldData | None:
        return next((field for field in self.fields if field.name is name), None)


@dataclass(frozen=True, slots=True)
class _Match:
    event_key: int
    confidence: float
    accepted: tuple[str, ...]
    rejected: tuple[str, ...]
    plausible: bool
    merge: bool


class EventReconciler:
    """Fuse M6 candidates without modifying or deleting their evidence."""

    name = "deterministic-event-reconciler"

    def __init__(self, database: Database, resolver_version: str = "2") -> None:
        if not resolver_version.strip() or len(resolver_version) > 200:
            raise ValueError("resolver version is invalid")
        self.database = database
        self.resolver_version = resolver_version

    def reconcile_course(self, course: CourseId) -> ReconciliationResult:
        require_identifier(course, CourseId)
        try:
            with self.database.transaction() as connection:
                course_key = DomainRepository._lookup_domain_key(
                    connection, course, "course", "course"
                )
                candidates = self._candidates(connection, course_key)
                input_hash = self._input_hash(connection, course_key, candidates)
                cached = connection.execute(
                    """SELECT reconciliation_run_key FROM reconciliation_run
                    WHERE course_key = ? AND resolver_name = ? AND resolver_version = ?
                      AND input_hash = ? AND status = 'COMPLETE'""",
                    (course_key, self.name, self.resolver_version, input_hash),
                ).fetchone()
                if cached is not None:
                    run_key = int(cached["reconciliation_run_key"])
                    cache_hit = True
                else:
                    now = to_storage_time(utc_now())
                    cursor = connection.execute(
                        """INSERT INTO reconciliation_run(
                            course_key, resolver_name, resolver_version, input_hash,
                            status, started_at, completed_at
                        ) VALUES (?, ?, ?, ?, 'COMPLETE', ?, ?)""",
                        (course_key, self.name, self.resolver_version, input_hash, now, now),
                    )
                    assert cursor.lastrowid is not None
                    run_key = int(cursor.lastrowid)
                    sources = {
                        candidate.key: self._ensure_source_and_claims(connection, candidate, now)
                        for candidate in candidates
                    }
                    ordinary = sorted(
                        (
                            candidate
                            for candidate in candidates
                            if self._change_kind(candidate) is ChangeKind.NONE
                        ),
                        key=lambda candidate: candidate.key,
                    )
                    changes = sorted(
                        (
                            candidate
                            for candidate in candidates
                            if self._change_kind(candidate) is not ChangeKind.NONE
                        ),
                        key=lambda candidate: candidate.key,
                    )
                    deferred = [
                        candidate
                        for candidate in ordinary
                        if not self._reconcile_candidate(
                            connection,
                            run_key,
                            candidate,
                            sources[candidate.key],
                            now,
                            defer_unresolved=True,
                        )
                    ]
                    for candidate in (*changes, *deferred):
                        self._reconcile_candidate(
                            connection, run_key, candidate, sources[candidate.key], now
                        )
                    for row in connection.execute(
                        "SELECT event_key FROM event WHERE course_key = ? ORDER BY event_key",
                        (course_key,),
                    ):
                        self._recompute_event(connection, int(row["event_key"]), now)
                    cache_hit = False
            return self._result(course, run_key, input_hash, cache_hit=cache_hit)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventReconciliationError("event reconciliation failed") from None

    def list_events(self, course: CourseId) -> tuple[CanonicalEvent, ...]:
        require_identifier(course, CourseId)
        connection = self.database.connect()
        try:
            course_key = DomainRepository._lookup_domain_key(connection, course, "course", "course")
            active_source = active_event_source_sql("source")
            rows = connection.execute(
                f"""SELECT event.* FROM event
                WHERE event.course_key = ? AND EXISTS (
                    SELECT 1 FROM event_source source
                    WHERE source.event_key = event.event_key AND {active_source}
                ) ORDER BY event.event_key""",
                (course_key,),
            ).fetchall()
            return tuple(self._event(connection, row) for row in rows)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventReconciliationError("event lookup failed") from None
        finally:
            connection.close()

    def get_event(self, event_key: int) -> CanonicalEvent | None:
        if event_key <= 0:
            raise ValueError("event key must be positive")
        connection = self.database.connect()
        try:
            active_source = active_event_source_sql("source")
            row = connection.execute(
                f"""SELECT event.* FROM event
                WHERE event.event_key = ? AND EXISTS (
                    SELECT 1 FROM event_source source
                    WHERE source.event_key = event.event_key AND {active_source}
                )""",
                (event_key,),
            ).fetchone()
            return None if row is None else self._event(connection, row)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventReconciliationError("event lookup failed") from None
        finally:
            connection.close()

    def resolve_field(self, request: ManualFieldResolution) -> CanonicalEvent:
        """Apply one explicit local field decision through the bounded manual module."""

        from ntulearn_skill.events._manual import resolve_field

        return resolve_field(self, request)

    def resolve_event_source(self, request: ManualIdentityResolution) -> CanonicalEvent:
        """Bind one unresolved source through an explicit local identity decision."""

        from ntulearn_skill.events._manual import resolve_event_source

        return resolve_event_source(self, request)

    def list_event_sources(
        self, event_key: int | None = None, *, include_unresolved: bool = True
    ) -> tuple[EventSource, ...]:
        if event_key is not None and event_key <= 0:
            raise ValueError("event key must be positive")
        connection = self.database.connect()
        try:
            clauses: list[str] = []
            parameters: list[object] = []
            if event_key is not None:
                clauses.append("source.event_key = ?")
                parameters.append(event_key)
            if not include_unresolved:
                clauses.append("source.resolution_state = 'MATCHED'")
            where = "" if not clauses else f"WHERE {' AND '.join(clauses)}"
            rows = connection.execute(
                f"""SELECT source.*, provider.name AS provider,
                           course_object.remote_key AS course_remote
                FROM event_source source
                JOIN course ON course.course_key = source.course_key
                JOIN source_object course_object
                  ON course_object.source_object_key = course.source_object_key
                JOIN source_provider provider
                  ON provider.provider_key = course_object.provider_key
                {where} ORDER BY source.event_source_key""",
                parameters,
            ).fetchall()
            return tuple(self._event_source(row) for row in rows)
        except sqlite3.Error:
            raise EventReconciliationError("event source lookup failed") from None
        finally:
            connection.close()

    def list_claims(
        self, event_key: int, field_name: CandidateFieldName | None = None
    ) -> tuple[Claim, ...]:
        if event_key <= 0:
            raise ValueError("event key must be positive")
        if field_name is not None and not isinstance(field_name, CandidateFieldName):
            raise TypeError("claim field must be typed")
        connection = self.database.connect()
        try:
            rows = connection.execute(
                """SELECT * FROM claim WHERE event_key = ? AND (? IS NULL OR field_name = ?)
                ORDER BY claim_key""",
                (
                    event_key,
                    None if field_name is None else field_name.value,
                    None if field_name is None else field_name.value,
                ),
            ).fetchall()
            return tuple(self._claim(connection, row) for row in rows)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventReconciliationError("claim lookup failed") from None
        finally:
            connection.close()

    def list_conflicts(self, event_key: int) -> tuple[EventConflict, ...]:
        if event_key <= 0:
            raise ValueError("event key must be positive")
        connection = self.database.connect()
        try:
            return self._conflicts(connection, event_key)
        except sqlite3.Error:
            raise EventReconciliationError("conflict lookup failed") from None
        finally:
            connection.close()

    def list_decisions(self, course: CourseId) -> tuple[ReconciliationDecision, ...]:
        require_identifier(course, CourseId)
        connection = self.database.connect()
        try:
            course_key = DomainRepository._lookup_domain_key(connection, course, "course", "course")
            rows = connection.execute(
                """SELECT decision.* FROM reconciliation_decision decision
                JOIN reconciliation_run run
                  ON run.reconciliation_run_key = decision.reconciliation_run_key
                WHERE run.course_key = ? ORDER BY decision.decision_key""",
                (course_key,),
            ).fetchall()
            return tuple(self._decision(row) for row in rows)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventReconciliationError("reconciliation audit lookup failed") from None
        finally:
            connection.close()

    def _result(
        self, course: CourseId, run_key: int, input_hash: str, *, cache_hit: bool
    ) -> ReconciliationResult:
        decisions = tuple(
            decision
            for decision in self.list_decisions(course)
            if self._decision_run_key(decision.key) == run_key
        )
        active_source_keys = self._active_source_keys(course)
        unresolved = tuple(
            source
            for source in self.list_event_sources()
            if source.key in active_source_keys
            and source.resolution_state is not EventSourceResolutionState.MATCHED
        )
        return ReconciliationResult(
            run_key,
            self.name,
            self.resolver_version,
            input_hash,
            self.list_events(course),
            unresolved,
            decisions,
            cache_hit,
        )

    def _active_source_keys(self, course: CourseId) -> frozenset[int]:
        connection = self.database.connect()
        try:
            course_key = DomainRepository._lookup_domain_key(connection, course, "course", "course")
            active_source = active_event_source_sql("source")
            return frozenset(
                int(row["event_source_key"])
                for row in connection.execute(
                    f"""SELECT source.event_source_key FROM event_source source
                    WHERE source.course_key = ? AND {active_source}""",
                    (course_key,),
                )
            )
        except sqlite3.Error:
            raise EventReconciliationError("event source lookup failed") from None
        finally:
            connection.close()

    def _decision_run_key(self, decision_key: int) -> int:
        connection = self.database.connect()
        try:
            row = connection.execute(
                """SELECT reconciliation_run_key FROM reconciliation_decision
                WHERE decision_key = ?""",
                (decision_key,),
            ).fetchone()
            if row is None:
                raise EventReconciliationError("reconciliation audit lookup failed")
            return int(row["reconciliation_run_key"])
        finally:
            connection.close()

    def _event(self, connection: sqlite3.Connection, row: sqlite3.Row) -> CanonicalEvent:
        course_row = connection.execute(
            """SELECT provider.name AS provider, object.remote_key
            FROM course JOIN source_object object
              ON object.source_object_key = course.source_object_key
            JOIN source_provider provider ON provider.provider_key = object.provider_key
            WHERE course.course_key = ?""",
            (int(row["course_key"]),),
        ).fetchone()
        if course_row is None:
            raise ValueError("event course is unavailable")
        event_key = int(row["event_key"])
        projection_rows = connection.execute(
            """SELECT * FROM event_field_projection
            WHERE event_key = ? ORDER BY field_name""",
            (event_key,),
        ).fetchall()
        active_source = active_event_source_sql("source")
        source_rows = connection.execute(
            f"""SELECT source.*, provider.name AS provider,
                      course_object.remote_key AS course_remote
            FROM event_source source
            JOIN course ON course.course_key = source.course_key
            JOIN source_object course_object
              ON course_object.source_object_key = course.source_object_key
            JOIN source_provider provider ON provider.provider_key = course_object.provider_key
            WHERE source.event_key = ? AND {active_source}
            ORDER BY source.event_source_key""",
            (event_key,),
        ).fetchall()
        active_claim = active_claim_sql("claim")
        claim_rows = connection.execute(
            f"""SELECT claim.* FROM claim
            WHERE claim.event_key = ? AND {active_claim}
            ORDER BY claim.claim_key""",
            (event_key,),
        ).fetchall()
        event_type = None if row["event_type"] is None else EventType(str(row["event_type"]))
        status = None if row["status"] is None else EventStatus(str(row["status"]))
        return CanonicalEvent(
            key=event_key,
            stable_id=str(row["stable_id"]),
            course=CourseId(str(course_row["provider"]), str(course_row["remote_key"])),
            event_type=event_type,
            title=None if row["title"] is None else str(row["title"]),
            start_time=self._json_or_none(row["start_time_json"]),
            end_time=self._json_or_none(row["end_time_json"]),
            due_time=self._json_or_none(row["due_time_json"]),
            location=None if row["location"] is None else str(row["location"]),
            status=status,
            resolution_state=EventResolutionState(str(row["resolution_state"])),
            confidence=float(row["confidence"]),
            source_count=int(row["source_count"]),
            projections=tuple(
                EventProjection(
                    CandidateFieldName(str(projection["field_name"])),
                    int(projection["selected_claim_key"]),
                    cast(JsonValue, json.loads(str(projection["value_json"]))),
                    bool(projection["uncertain"]),
                )
                for projection in projection_rows
            ),
            sources=tuple(self._event_source(source) for source in source_rows),
            claims=tuple(self._claim(connection, claim) for claim in claim_rows),
            conflicts=self._conflicts(connection, event_key),
        )

    @staticmethod
    def _event_source(row: sqlite3.Row) -> EventSource:
        return EventSource(
            key=int(row["event_source_key"]),
            candidate_key=int(row["candidate_key"]),
            course=CourseId(str(row["provider"]), str(row["course_remote"])),
            event_key=None if row["event_key"] is None else int(row["event_key"]),
            source_kind=CandidateSourceKind(str(row["source_kind"])),
            evidence_kind=str(row["evidence_ref_kind"]),
            evidence_key=int(row["evidence_ref_key"]),
            source_object_key=None
            if row["source_object_key"] is None
            else int(row["source_object_key"]),
            source_timestamp=None
            if row["source_timestamp"] is None
            else from_storage_time(str(row["source_timestamp"])),
            source_timestamp_semantics=None
            if row["source_timestamp_semantics"] is None
            else str(row["source_timestamp_semantics"]),
            observed_at=None
            if row["observed_at"] is None
            else from_storage_time(str(row["observed_at"])),
            change_kind=ChangeKind(str(row["change_kind"])),
            resolution_state=EventSourceResolutionState(str(row["resolution_state"])),
            explanation=str(row["explanation"]),
        )

    @staticmethod
    def _claim(connection: sqlite3.Connection, row: sqlite3.Row) -> Claim:
        supersedes = tuple(
            int(link["predecessor_claim_key"])
            for link in connection.execute(
                """SELECT predecessor_claim_key FROM claim_supersession
                WHERE successor_claim_key = ? ORDER BY predecessor_claim_key""",
                (int(row["claim_key"]),),
            )
        )
        superseded_by = tuple(
            int(link["successor_claim_key"])
            for link in connection.execute(
                """SELECT successor_claim_key FROM claim_supersession
                WHERE predecessor_claim_key = ? ORDER BY successor_claim_key""",
                (int(row["claim_key"]),),
            )
        )
        return Claim(
            key=int(row["claim_key"]),
            event_key=None if row["event_key"] is None else int(row["event_key"]),
            event_source_key=None
            if row["event_source_key"] is None
            else int(row["event_source_key"]),
            candidate_field_key=None
            if row["candidate_field_key"] is None
            else int(row["candidate_field_key"]),
            origin=ClaimOrigin(str(row["origin"])),
            field_name=CandidateFieldName(str(row["field_name"])),
            value=cast(JsonValue, json.loads(str(row["value_json"]))),
            original_text=str(row["original_text"]),
            precision=None
            if row["temporal_precision"] is None
            else TemporalPrecision(str(row["temporal_precision"])),
            source_timezone=None if row["source_timezone"] is None else str(row["source_timezone"]),
            confidence=float(row["confidence"]),
            decision_state=ClaimDecisionState(str(row["decision_state"])),
            decision_reason=str(row["decision_reason"]),
            supersedes=supersedes,
            superseded_by=superseded_by,
        )

    @staticmethod
    def _conflicts(connection: sqlite3.Connection, event_key: int) -> tuple[EventConflict, ...]:
        rows = connection.execute(
            """SELECT * FROM event_conflict
            WHERE event_key = ? ORDER BY conflict_key""",
            (event_key,),
        ).fetchall()
        return tuple(
            EventConflict(
                key=int(row["conflict_key"]),
                event_key=event_key,
                field_name=CandidateFieldName(str(row["field_name"])),
                state=ConflictState(str(row["state"])),
                reason=str(row["reason"]),
                alternative_claim_keys=tuple(
                    int(alternative["claim_key"])
                    for alternative in connection.execute(
                        """SELECT claim_key FROM event_conflict_alternative
                        WHERE conflict_key = ? ORDER BY claim_key""",
                        (int(row["conflict_key"]),),
                    )
                ),
            )
            for row in rows
        )

    @staticmethod
    def _decision(row: sqlite3.Row) -> ReconciliationDecision:
        return ReconciliationDecision(
            key=int(row["decision_key"]),
            candidate_key=int(row["candidate_key"]),
            event_key=None if row["event_key"] is None else int(row["event_key"]),
            result=ReconciliationDecisionResult(str(row["result"])),
            considered_event_keys=tuple(json.loads(str(row["considered_event_keys_json"]))),
            accepted_signals=tuple(json.loads(str(row["accepted_signals_json"]))),
            rejected_signals=tuple(json.loads(str(row["rejected_signals_json"]))),
            confidence=float(row["confidence"]),
            explanation=str(row["explanation"]),
        )

    @staticmethod
    def _json_or_none(raw: object) -> JsonValue:
        return None if raw is None else cast(JsonValue, json.loads(str(raw)))

    @staticmethod
    def _candidates(connection: sqlite3.Connection, course_key: int) -> tuple[_CandidateData, ...]:
        effective = effective_extraction_sql("extraction")
        rows = connection.execute(
            f"""SELECT candidate.* FROM event_candidate candidate
            JOIN extraction_record extraction
              ON extraction.extraction_record_key = candidate.extraction_record_key
            WHERE candidate.course_key = ? AND {effective}
            ORDER BY candidate.candidate_key""",
            (course_key,),
        ).fetchall()
        result: list[_CandidateData] = []
        for row in rows:
            field_rows = connection.execute(
                """SELECT * FROM event_candidate_field
                WHERE candidate_key = ? ORDER BY candidate_field_key""",
                (int(row["candidate_key"]),),
            ).fetchall()
            fields = tuple(
                _FieldData(
                    key=int(field["candidate_field_key"]),
                    name=CandidateFieldName(str(field["field_name"])),
                    value=cast(JsonValue, json.loads(str(field["value_json"]))),
                    value_json=str(field["value_json"]),
                    original_text=str(field["original_text"]),
                    precision=None
                    if field["temporal_precision"] is None
                    else TemporalPrecision(str(field["temporal_precision"])),
                    source_timezone=None
                    if field["source_timezone"] is None
                    else str(field["source_timezone"]),
                    observation_key=None
                    if field["source_observation_key"] is None
                    else int(field["source_observation_key"]),
                    locator_key=None if field["locator_key"] is None else int(field["locator_key"]),
                )
                for field in field_rows
            )
            result.append(
                _CandidateData(
                    int(row["candidate_key"]),
                    course_key,
                    CandidateSourceKind(str(row["source_kind"])),
                    str(row["raw_wording"]),
                    float(row["confidence"]),
                    fields,
                )
            )
        return tuple(result)

    def _input_hash(
        self,
        connection: sqlite3.Connection,
        course_key: int,
        candidates: tuple[_CandidateData, ...],
    ) -> str:
        manual_rows = connection.execute(
            """SELECT resolution.manual_resolution_key, resolution.event_key,
                      resolution.field_name, resolution.selected_claim_key,
                      resolution.local_claim_key
            FROM manual_field_resolution resolution
            JOIN event ON event.event_key = resolution.event_key
            WHERE event.course_key = ? AND resolution.active = 1
            ORDER BY resolution.manual_resolution_key""",
            (course_key,),
        ).fetchall()
        identity_rows = connection.execute(
            """SELECT resolution.manual_identity_resolution_key,
                      resolution.event_source_key, resolution.event_key
            FROM manual_identity_resolution resolution
            JOIN event ON event.event_key = resolution.event_key
            WHERE event.course_key = ? AND resolution.active = 1
            ORDER BY resolution.manual_identity_resolution_key""",
            (course_key,),
        ).fetchall()
        payload = {
            "effective_extractions": self._effective_extraction_fingerprint(connection, course_key),
            "candidates": [
                [candidate.key, [[field.key, field.value_json] for field in candidate.fields]]
                for candidate in candidates
            ],
            "manual_fields": [list(row) for row in manual_rows],
            "manual_identities": [list(row) for row in identity_rows],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _effective_extraction_fingerprint(
        connection: sqlite3.Connection, course_key: int
    ) -> list[list[object]]:
        effective = effective_extraction_sql("extraction")
        rows = connection.execute(
            f"""SELECT extraction.extraction_record_key, extraction.input_kind,
                extraction.status
            FROM extraction_record extraction
            WHERE {effective} AND (
                EXISTS (
                    SELECT 1 FROM source_observation observation
                    JOIN (
                        SELECT source_object_key, course_key FROM announcement
                        UNION ALL SELECT source_object_key, course_key FROM assessment
                        UNION ALL SELECT source_object_key, course_key FROM schedule_item
                        UNION ALL SELECT source_object_key, course_key FROM due_item
                    ) owner ON owner.source_object_key = observation.source_object_key
                    WHERE observation.observation_key = extraction.source_observation_key
                      AND owner.course_key = ?
                )
                OR EXISTS (
                    SELECT 1 FROM resource_version version
                    JOIN resource ON resource.resource_key = version.resource_key
                    JOIN content_node content ON content.content_key = resource.content_key
                    WHERE version.version_key = extraction.version_key
                      AND content.course_key = ?
                )
            ) ORDER BY extraction.extraction_record_key""",
            (course_key, course_key),
        ).fetchall()
        return [
            [int(row["extraction_record_key"]), str(row["input_kind"]), str(row["status"])]
            for row in rows
        ]

    def _ensure_source_and_claims(
        self, connection: sqlite3.Connection, candidate: _CandidateData, now: str
    ) -> int:
        existing = connection.execute(
            "SELECT event_source_key FROM event_source WHERE candidate_key = ?",
            (candidate.key,),
        ).fetchone()
        if existing is not None:
            return int(existing["event_source_key"])
        if not candidate.fields:
            raise ValueError("event candidate has no evidence fields")
        evidence = candidate.fields[0]
        if evidence.observation_key is not None:
            detail = connection.execute(
                """SELECT observation.source_object_key, observation.observed_at,
                          observation.snapshot_json
                FROM source_observation observation
                WHERE observation.observation_key = ?""",
                (evidence.observation_key,),
            ).fetchone()
            if detail is None:
                raise ValueError("candidate observation evidence is unavailable")
            snapshot = json.loads(str(detail["snapshot_json"]))
            if not isinstance(snapshot, dict):
                raise ValueError("candidate observation evidence is invalid")
            source_timestamp, semantics = self._source_timestamp(snapshot)
            source_object_key = int(detail["source_object_key"])
            observed_at = str(detail["observed_at"])
            evidence_kind = "source_observation"
            evidence_key = evidence.observation_key
        elif evidence.locator_key is not None:
            detail = connection.execute(
                """SELECT resource.source_object_key
                FROM source_locator locator
                JOIN resource_version version ON version.version_key = locator.version_key
                JOIN resource ON resource.resource_key = version.resource_key
                WHERE locator.locator_key = ?""",
                (evidence.locator_key,),
            ).fetchone()
            if detail is None:
                raise ValueError("candidate locator evidence is unavailable")
            source_timestamp = None
            semantics = None
            source_object_key = int(detail["source_object_key"])
            observed_at = None
            evidence_kind = "source_locator"
            evidence_key = evidence.locator_key
        else:
            raise ValueError("candidate lacks exact evidence")
        change_kind = self._change_kind(candidate)
        cursor = connection.execute(
            """INSERT INTO event_source(
                candidate_key, course_key, event_key, source_kind,
                evidence_ref_kind, evidence_ref_key, source_object_key,
                source_timestamp, source_timestamp_semantics, observed_at,
                change_kind, resolution_state, explanation, created_at, updated_at
            ) VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'UNRESOLVED',
                      'candidate has not been reconciled', ?, ?)""",
            (
                candidate.key,
                candidate.course_key,
                candidate.source_kind.value,
                evidence_kind,
                evidence_key,
                source_object_key,
                source_timestamp,
                semantics,
                observed_at,
                change_kind.value,
                now,
                now,
            ),
        )
        assert cursor.lastrowid is not None
        source_key = int(cursor.lastrowid)
        for field in candidate.fields:
            connection.execute(
                """INSERT INTO claim(
                    event_key, event_source_key, candidate_field_key, origin,
                    field_name, value_json, original_text, temporal_precision,
                    source_timezone, confidence, decision_state, decision_reason,
                    created_at, updated_at
                ) VALUES (NULL, ?, ?, 'SOURCE', ?, ?, ?, ?, ?, ?, 'UNRESOLVED',
                          'candidate identity is unresolved', ?, ?)""",
                (
                    source_key,
                    field.key,
                    field.name.value,
                    field.value_json,
                    field.original_text,
                    None if field.precision is None else field.precision.value,
                    field.source_timezone,
                    candidate.confidence,
                    now,
                    now,
                ),
            )
        return source_key

    @staticmethod
    def _source_timestamp(snapshot: dict[object, object]) -> tuple[str | None, str | None]:
        for name in ("modified_at", "published_at", "created_at"):
            value = snapshot.get(name)
            if not isinstance(value, dict):
                continue
            instant = value.get("instant")
            if isinstance(instant, str):
                # Parsing rejects naive or malformed instants before they influence precedence.
                normalized = to_storage_time(from_storage_time(instant))
                return normalized, name
        return None, None

    @staticmethod
    def _change_kind(candidate: _CandidateData) -> ChangeKind:
        return classify_change_language(candidate.raw_wording)

    def _reconcile_candidate(
        self,
        connection: sqlite3.Connection,
        run_key: int,
        candidate: _CandidateData,
        source_key: int,
        now: str,
        *,
        defer_unresolved: bool = False,
    ) -> bool:
        source = connection.execute(
            "SELECT * FROM event_source WHERE event_source_key = ?", (source_key,)
        ).fetchone()
        assert source is not None
        manual = connection.execute(
            """SELECT event_key FROM manual_identity_resolution
            WHERE event_source_key = ? AND active = 1""",
            (source_key,),
        ).fetchone()
        if manual is not None:
            event_key = int(manual["event_key"])
            self._bind_source(
                connection, source_key, event_key, "manual identity decision retained", now
            )
            self._audit(
                connection,
                run_key,
                candidate.key,
                event_key,
                ReconciliationDecisionResult.MANUAL_RETAINED,
                (event_key,),
                ("manual_identity",),
                (),
                1.0,
                "manual identity decision retained",
                now,
            )
            return True
        if source["event_key"] is not None:
            event_key = int(source["event_key"])
            self._audit(
                connection,
                run_key,
                candidate.key,
                event_key,
                ReconciliationDecisionResult.RETAINED,
                (event_key,),
                ("stable_existing_binding",),
                (),
                1.0,
                "existing deterministic binding retained",
                now,
            )
            return True
        matches = self._candidate_matches(connection, candidate)
        mergeable = [match for match in matches if match.merge]
        plausible = [match for match in matches if match.plausible]
        change_kind = ChangeKind(str(source["change_kind"]))
        if len(mergeable) == 1:
            match = mergeable[0]
            self._bind_source(connection, source_key, match.event_key, "candidate matched", now)
            self._audit(
                connection,
                run_key,
                candidate.key,
                match.event_key,
                ReconciliationDecisionResult.MERGED,
                tuple(item.event_key for item in matches),
                match.accepted,
                match.rejected,
                match.confidence,
                "course, type, title, and corroborating evidence support one event",
                now,
            )
            return True
        if plausible or change_kind is not ChangeKind.NONE:
            if defer_unresolved and change_kind is ChangeKind.NONE:
                return False
            state = (
                EventSourceResolutionState.RELATED_CHANGE
                if change_kind is not ChangeKind.NONE
                else EventSourceResolutionState.UNRESOLVED
            )
            explanation = (
                "change target is not uniquely defensible"
                if state is EventSourceResolutionState.RELATED_CHANGE
                else "candidate resembles an event but lacks corroborating identity evidence"
            )
            connection.execute(
                """UPDATE event_source SET resolution_state = ?, explanation = ?, updated_at = ?
                WHERE event_source_key = ?""",
                (state.value, explanation, now, source_key),
            )
            connection.execute(
                "DELETE FROM event_source_possible_match WHERE event_source_key = ?",
                (source_key,),
            )
            for match in plausible:
                connection.execute(
                    """INSERT INTO event_source_possible_match(
                        event_source_key, event_key, confidence, signals_json
                    ) VALUES (?, ?, ?, ?)""",
                    (
                        source_key,
                        match.event_key,
                        match.confidence,
                        json.dumps(match.accepted, separators=(",", ":")),
                    ),
                )
            self._audit(
                connection,
                run_key,
                candidate.key,
                None,
                ReconciliationDecisionResult.RELATED_CHANGE
                if state is EventSourceResolutionState.RELATED_CHANGE
                else ReconciliationDecisionResult.UNRESOLVED,
                tuple(item.event_key for item in matches),
                (),
                tuple(signal for item in matches for signal in item.rejected),
                max((item.confidence for item in plausible), default=0.0),
                explanation,
                now,
            )
            return True
        event_key = self._create_event(connection, candidate, source_key, now)
        self._bind_source(connection, source_key, event_key, "new logical event", now)
        self._audit(
            connection,
            run_key,
            candidate.key,
            event_key,
            ReconciliationDecisionResult.CREATED,
            (),
            ("no_plausible_existing_event",),
            (),
            candidate.confidence,
            "candidate anchors a new logical event",
            now,
        )
        return True

    def _candidate_matches(
        self, connection: sqlite3.Connection, candidate: _CandidateData
    ) -> tuple[_Match, ...]:
        title = candidate.field(CandidateFieldName.TITLE)
        event_type = candidate.field(CandidateFieldName.EVENT_TYPE)
        candidate_title, candidate_number = title_identity(None if title is None else title.value)
        candidate_type = None if event_type is None else str(event_type.value)
        source_row = connection.execute(
            "SELECT * FROM event_source WHERE candidate_key = ?", (candidate.key,)
        ).fetchone()
        assert source_row is not None
        candidate_context = self._source_context(connection, source_row)
        change_kind = ChangeKind(str(source_row["change_kind"]))
        matches: list[_Match] = []
        active_claim = active_claim_sql("claim")
        active_source = active_event_source_sql("source")
        for event_row in connection.execute(
            "SELECT event_key FROM event WHERE course_key = ? ORDER BY event_key",
            (candidate.course_key,),
        ):
            event_key = int(event_row["event_key"])
            rows = connection.execute(
                f"""SELECT claim.field_name, claim.value_json,
                          source.source_object_key, source.event_source_key,
                          source.source_kind, source.evidence_ref_key
                FROM claim JOIN event_source source
                  ON source.event_source_key = claim.event_source_key
                WHERE claim.event_key = ? AND claim.origin = 'SOURCE'
                  AND {active_claim}""",
                (event_key,),
            ).fetchall()
            event_titles = [
                cast(JsonValue, json.loads(str(row["value_json"])))
                for row in rows
                if str(row["field_name"]) == CandidateFieldName.TITLE.value
            ]
            identities = [title_identity(value) for value in event_titles]
            title_match = bool(candidate_title) and any(
                identity == candidate_title for identity, _ in identities
            )
            event_numbers = {number for _, number in identities if number is not None}
            number_conflict = candidate_number is not None and bool(
                {number for number in event_numbers if number != candidate_number}
            )
            event_types = {
                str(json.loads(str(row["value_json"])))
                for row in rows
                if str(row["field_name"]) == CandidateFieldName.EVENT_TYPE.value
            }
            exact_type = candidate_type is not None and candidate_type in event_types
            generic_compatible = candidate_type == EventType.GENERIC_COURSE_EVENT.value or (
                EventType.GENERIC_COURSE_EVENT.value in event_types
            )
            type_compatible = exact_type or generic_compatible
            event_source_rows = connection.execute(
                f"""SELECT source.* FROM event_source source
                WHERE source.event_key = ? AND {active_source}""",
                (event_key,),
            ).fetchall()
            same_source = any(
                row["source_object_key"] is not None
                and row["source_object_key"] == source_row["source_object_key"]
                for row in event_source_rows
            )
            event_context: set[str] = set()
            for row in event_source_rows:
                event_context.update(self._source_context(connection, row))
            shared_context = bool(candidate_context & event_context)
            matching_time = False
            conflicting_time = False
            for field in candidate.fields:
                if field.name not in _TEMPORAL_FIELDS:
                    continue
                comparable = [
                    cast(JsonValue, json.loads(str(row["value_json"])))
                    for row in rows
                    if str(row["field_name"]) == field.name.value
                ]
                if any(temporal_agrees(field.value, value) for value in comparable):
                    matching_time = True
                elif comparable:
                    conflicting_time = True
            accepted: list[str] = []
            rejected: list[str] = []
            if title_match:
                accepted.append("normalized_title")
            else:
                rejected.append("title_mismatch")
            if exact_type:
                accepted.append("compatible_type")
            elif generic_compatible:
                accepted.append("generic_type_requires_relation")
            else:
                rejected.append("incompatible_type")
            if candidate_number is not None:
                accepted.append(f"assessment_number:{candidate_number}")
            if number_conflict:
                rejected.append("assessment_number_contradiction")
            if matching_time:
                accepted.append("same_field_time")
            if conflicting_time:
                rejected.append("same_field_time_contradiction")
            if same_source:
                accepted.append("shared_typed_source")
            if shared_context:
                accepted.append("shared_source_context")
            explicit_change = change_kind is not ChangeKind.NONE
            if explicit_change:
                accepted.append(f"explicit_change:{change_kind.value.lower()}")
            plausible = title_match and type_compatible and not number_conflict
            relational = same_source or shared_context
            strong = matching_time or relational or explicit_change
            merge = plausible and strong and (exact_type or relational)
            if conflicting_time and not relational and not explicit_change:
                merge = False
            confidence = 0.95 if merge and relational else 0.9 if merge else 0.5
            if plausible:
                matches.append(
                    _Match(
                        event_key,
                        confidence,
                        tuple(accepted),
                        tuple(rejected),
                        plausible,
                        merge,
                    )
                )
        return tuple(matches)

    @staticmethod
    def _source_context(connection: sqlite3.Connection, source: sqlite3.Row) -> set[str]:
        result: set[str] = set()
        source_object_key = source["source_object_key"]
        if source_object_key is not None:
            result.add(f"source:{int(source_object_key)}")
        kind = str(source["source_kind"])
        if kind == CandidateSourceKind.ASSESSMENT.value and source_object_key is not None:
            row = connection.execute(
                """SELECT assessment.content_key, object.remote_key
                FROM assessment JOIN source_object object
                  ON object.source_object_key = assessment.source_object_key
                WHERE assessment.source_object_key = ?""",
                (int(source_object_key),),
            ).fetchone()
            if row is not None:
                result.update(
                    {f"content:{int(row['content_key'])}", f"remote:{str(row['remote_key'])}"}
                )
        elif kind == CandidateSourceKind.DUE_ITEM.value and source_object_key is not None:
            row = connection.execute(
                "SELECT item_source_id FROM due_item WHERE source_object_key = ?",
                (int(source_object_key),),
            ).fetchone()
            if row is not None and row["item_source_id"] is not None:
                result.add(f"remote:{str(row['item_source_id'])}")
        elif kind == CandidateSourceKind.DOCUMENT.value:
            row = connection.execute(
                """SELECT resource.content_key
                FROM source_locator locator
                JOIN resource_version version ON version.version_key = locator.version_key
                JOIN resource ON resource.resource_key = version.resource_key
                WHERE locator.locator_key = ?""",
                (int(source["evidence_ref_key"]),),
            ).fetchone()
            if row is not None:
                result.add(f"content:{int(row['content_key'])}")
        return result

    @staticmethod
    def _create_event(
        connection: sqlite3.Connection,
        candidate: _CandidateData,
        source_key: int,
        now: str,
    ) -> int:
        anchor = f"event-v1\0{candidate.course_key}\0{candidate.key}\0{source_key}"
        stable_id = hashlib.sha256(anchor.encode("utf-8")).hexdigest()
        cursor = connection.execute(
            """INSERT INTO event(
                stable_id, course_key, resolution_state, confidence,
                source_count, created_at, updated_at
            ) VALUES (?, ?, 'UNRESOLVED', ?, 0, ?, ?)""",
            (stable_id, candidate.course_key, candidate.confidence, now, now),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    @staticmethod
    def _bind_source(
        connection: sqlite3.Connection,
        source_key: int,
        event_key: int,
        explanation: str,
        now: str,
    ) -> None:
        connection.execute(
            """UPDATE event_source
            SET event_key = ?, resolution_state = 'MATCHED', explanation = ?, updated_at = ?
            WHERE event_source_key = ?""",
            (event_key, explanation, now, source_key),
        )
        connection.execute(
            """UPDATE claim SET event_key = ?, decision_state = 'UNRESOLVED',
                decision_reason = 'awaiting field reconciliation', updated_at = ?
            WHERE event_source_key = ?""",
            (event_key, now, source_key),
        )
        connection.execute(
            "DELETE FROM event_source_possible_match WHERE event_source_key = ?",
            (source_key,),
        )

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        run_key: int,
        candidate_key: int,
        event_key: int | None,
        result: ReconciliationDecisionResult,
        considered: tuple[int, ...],
        accepted: tuple[str, ...],
        rejected: tuple[str, ...],
        confidence: float,
        explanation: str,
        now: str,
    ) -> None:
        connection.execute(
            """INSERT INTO reconciliation_decision(
                reconciliation_run_key, candidate_key, event_key, result,
                considered_event_keys_json, accepted_signals_json,
                rejected_signals_json, confidence, explanation, decided_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_key,
                candidate_key,
                event_key,
                result.value,
                json.dumps(considered, separators=(",", ":")),
                json.dumps(accepted, separators=(",", ":")),
                json.dumps(rejected, separators=(",", ":")),
                confidence,
                explanation,
                now,
            ),
        )

    def _recompute_event(self, connection: sqlite3.Connection, event_key: int, now: str) -> None:
        active_claim = active_claim_sql("claim")
        connection.execute(
            f"""UPDATE claim SET
                decision_state = CASE
                    WHEN {active_claim} THEN 'UNRESOLVED'
                    ELSE 'REJECTED'
                END,
                decision_reason = CASE
                    WHEN {active_claim} THEN 'awaiting field reconciliation'
                    ELSE 'superseded deterministic extraction'
                END,
                updated_at = ?
            WHERE claim.event_key = ? AND claim.origin = 'SOURCE'""",
            (now, event_key),
        )
        connection.execute("DELETE FROM event_field_projection WHERE event_key = ?", (event_key,))
        connection.execute(
            """UPDATE event_conflict SET state = 'RESOLVED', resolution_kind = 'AUTOMATIC',
                resolved_at = ? WHERE event_key = ? AND state = 'OPEN'""",
            (now, event_key),
        )
        for field_name in _CANONICAL_FIELDS:
            rows = connection.execute(
                f"""SELECT claim.*, source.change_kind, source.source_timestamp
                FROM claim LEFT JOIN event_source source
                  ON source.event_source_key = claim.event_source_key
                WHERE claim.event_key = ? AND claim.field_name = ? AND {active_claim}
                ORDER BY claim.claim_key""",
                (event_key, field_name.value),
            ).fetchall()
            if not rows:
                continue
            manual = connection.execute(
                """SELECT selected_claim_key, local_claim_key
                FROM manual_field_resolution
                WHERE event_key = ? AND field_name = ? AND active = 1""",
                (event_key, field_name.value),
            ).fetchone()
            if manual is not None:
                selected_key = int(manual["selected_claim_key"] or manual["local_claim_key"])
                selected = next(
                    (row for row in rows if int(row["claim_key"]) == selected_key), None
                )
                if selected is None:
                    raise ValueError("manual resolution does not belong to this event field")
                self._select_claim_group(
                    connection,
                    event_key,
                    field_name,
                    rows,
                    selected,
                    "manual field resolution",
                    now,
                    supersede=False,
                )
                continue
            groups = self._claim_groups(field_name, rows)
            if field_name is CandidateFieldName.EVENT_TYPE:
                specific = [
                    group
                    for group in groups
                    if str(json.loads(str(group[0]["value_json"])))
                    != EventType.GENERIC_COURSE_EVENT.value
                ]
                if len(specific) == 1:
                    self._select_claim_group(
                        connection,
                        event_key,
                        field_name,
                        rows,
                        specific[0][0],
                        "one compatible specific type refines generic source wording",
                        now,
                        supersede=False,
                    )
                    continue
            if len(groups) == 1:
                selected = max(
                    groups[0],
                    key=lambda row: (
                        precision_rank(
                            None
                            if row["temporal_precision"] is None
                            else str(row["temporal_precision"])
                        ),
                        float(row["confidence"]),
                        -int(row["claim_key"]),
                    ),
                )
                self._select_claim_group(
                    connection,
                    event_key,
                    field_name,
                    rows,
                    selected,
                    "independent claims agree",
                    now,
                    supersede=False,
                )
                continue
            successor = self._explicit_successor(field_name, rows)
            if successor is not None:
                self._select_claim_group(
                    connection,
                    event_key,
                    field_name,
                    rows,
                    successor,
                    "newer explicit field-specific change",
                    now,
                    supersede=True,
                )
                continue
            conflict = connection.execute(
                """INSERT INTO event_conflict(
                    event_key, field_name, state, reason, created_at
                ) VALUES (?, ?, 'OPEN', 'incompatible claims have no defensible winner', ?)
                RETURNING conflict_key""",
                (event_key, field_name.value, now),
            ).fetchone()
            assert conflict is not None
            conflict_key = int(conflict["conflict_key"])
            for row in rows:
                claim_key = int(row["claim_key"])
                connection.execute(
                    """UPDATE claim SET decision_state = 'CONFLICTING',
                        decision_reason = 'incompatible claims have no defensible winner',
                        updated_at = ? WHERE claim_key = ?""",
                    (now, claim_key),
                )
                connection.execute(
                    """INSERT INTO event_conflict_alternative(conflict_key, claim_key)
                    VALUES (?, ?)""",
                    (conflict_key, claim_key),
                )
        projections = {
            str(row["field_name"]): str(row["value_json"])
            for row in connection.execute(
                "SELECT field_name, value_json FROM event_field_projection WHERE event_key = ?",
                (event_key,),
            )
        }
        has_conflict = connection.execute(
            "SELECT 1 FROM event_conflict WHERE event_key = ? AND state = 'OPEN' LIMIT 1",
            (event_key,),
        ).fetchone()
        active_source = active_event_source_sql("source")
        has_ambiguity = connection.execute(
            f"""SELECT 1 FROM event_source_possible_match possible
            JOIN event_source source ON source.event_source_key = possible.event_source_key
            WHERE possible.event_key = ? AND source.resolution_state <> 'MATCHED'
              AND {active_source} LIMIT 1""",
            (event_key,),
        ).fetchone()
        state = (
            EventResolutionState.CONFLICTING
            if has_conflict is not None
            else EventResolutionState.UNRESOLVED
            if has_ambiguity is not None
            else EventResolutionState.RESOLVED
        )
        confidence_row = connection.execute(
            f"""SELECT AVG(confidence) AS confidence,
                COUNT(DISTINCT event_source_key) AS sources
            FROM claim WHERE event_key = ? AND origin = 'SOURCE' AND {active_claim}""",
            (event_key,),
        ).fetchone()
        assert confidence_row is not None
        event_type = self._scalar_projection(projections, CandidateFieldName.EVENT_TYPE)
        title = self._scalar_projection(projections, CandidateFieldName.TITLE)
        location = self._scalar_projection(projections, CandidateFieldName.LOCATION)
        status = self._scalar_projection(projections, CandidateFieldName.STATUS)
        connection.execute(
            """UPDATE event SET event_type = ?, title = ?, start_time_json = ?,
                end_time_json = ?, due_time_json = ?, location = ?, status = ?,
                resolution_state = ?, confidence = ?, source_count = ?, updated_at = ?
            WHERE event_key = ?""",
            (
                event_type,
                title,
                projections.get(CandidateFieldName.START_TIME.value),
                projections.get(CandidateFieldName.END_TIME.value),
                projections.get(CandidateFieldName.DUE_TIME.value),
                location,
                status,
                state.value,
                float(confidence_row["confidence"] or 0.0),
                int(confidence_row["sources"]),
                now,
                event_key,
            ),
        )

    @staticmethod
    def _claim_groups(
        field_name: CandidateFieldName, rows: list[sqlite3.Row]
    ) -> list[list[sqlite3.Row]]:
        if field_name in _TEMPORAL_FIELDS:
            strict: dict[tuple[str, str] | None, list[sqlite3.Row]] = {}
            for row in rows:
                value = cast(JsonValue, json.loads(str(row["value_json"])))
                strict.setdefault(temporal_signature(value), []).append(row)
            temporal_groups = list(strict.values())
            partial_groups = [
                group
                for group in temporal_groups
                if precision_rank(
                    None
                    if group[0]["temporal_precision"] is None
                    else str(group[0]["temporal_precision"])
                )
                < precision_rank(TemporalPrecision.EXACT_TIME.value)
            ]
            for partial in partial_groups:
                partial_value = cast(JsonValue, json.loads(str(partial[0]["value_json"])))
                compatible = [
                    group
                    for group in temporal_groups
                    if group is not partial
                    and precision_rank(
                        None
                        if group[0]["temporal_precision"] is None
                        else str(group[0]["temporal_precision"])
                    )
                    > precision_rank(
                        None
                        if partial[0]["temporal_precision"] is None
                        else str(partial[0]["temporal_precision"])
                    )
                    and temporal_agrees(
                        partial_value,
                        cast(JsonValue, json.loads(str(group[0]["value_json"]))),
                    )
                ]
                if len(compatible) == 1 and partial in temporal_groups:
                    compatible[0].extend(partial)
                    temporal_groups.remove(partial)
            return temporal_groups
        groups: list[list[sqlite3.Row]] = []
        for row in rows:
            value = cast(JsonValue, json.loads(str(row["value_json"])))
            for group in groups:
                other = cast(JsonValue, json.loads(str(group[0]["value_json"])))
                agrees = semantic_value(field_name, value) == semantic_value(field_name, other)
                if agrees:
                    group.append(row)
                    break
            else:
                groups.append([row])
        return groups

    @staticmethod
    def _explicit_successor(
        field_name: CandidateFieldName, rows: list[sqlite3.Row]
    ) -> sqlite3.Row | None:
        groups = EventReconciler._claim_groups(field_name, rows)
        winners: list[sqlite3.Row] = []
        for group in groups:
            explicit = [
                row
                for row in group
                if row["change_kind"] is not None
                and change_applies(ChangeKind(str(row["change_kind"])), field_name)
                and row["source_timestamp"] is not None
            ]
            if not explicit:
                continue
            candidate = max(
                explicit,
                key=lambda row: (
                    str(row["source_timestamp"]),
                    precision_rank(
                        None
                        if row["temporal_precision"] is None
                        else str(row["temporal_precision"])
                    ),
                    float(row["confidence"]),
                    -int(row["claim_key"]),
                ),
            )
            others = [row for other in groups if other is not group for row in other]
            if not others or any(row["source_timestamp"] is None for row in others):
                continue
            if str(candidate["source_timestamp"]) <= max(
                str(row["source_timestamp"]) for row in others
            ):
                continue
            if field_name in _TEMPORAL_FIELDS:
                candidate_precision = precision_rank(
                    None
                    if candidate["temporal_precision"] is None
                    else str(candidate["temporal_precision"])
                )
                if candidate_precision < max(
                    precision_rank(
                        None
                        if row["temporal_precision"] is None
                        else str(row["temporal_precision"])
                    )
                    for row in others
                ):
                    continue
            winners.append(candidate)
        return winners[0] if len(winners) == 1 else None

    @staticmethod
    def _select_claim_group(
        connection: sqlite3.Connection,
        event_key: int,
        field_name: CandidateFieldName,
        rows: list[sqlite3.Row],
        selected: sqlite3.Row,
        reason: str,
        now: str,
        *,
        supersede: bool,
    ) -> None:
        selected_value = cast(JsonValue, json.loads(str(selected["value_json"])))
        selected_key = int(selected["claim_key"])
        for row in rows:
            claim_key = int(row["claim_key"])
            value = cast(JsonValue, json.loads(str(row["value_json"])))
            if claim_key == selected_key:
                accepted = True
            else:
                agrees = (
                    temporal_agrees(selected_value, value)
                    if field_name in _TEMPORAL_FIELDS
                    else semantic_value(field_name, selected_value)
                    == semantic_value(field_name, value)
                )
                same_precision = row["temporal_precision"] == selected["temporal_precision"]
                accepted = agrees and (field_name not in _TEMPORAL_FIELDS or same_precision)
            state = (
                ClaimDecisionState.ACCEPTED
                if accepted
                else ClaimDecisionState.SUPERSEDED
                if supersede
                else ClaimDecisionState.REJECTED
            )
            connection.execute(
                """UPDATE claim SET decision_state = ?, decision_reason = ?, updated_at = ?
                WHERE claim_key = ?""",
                (state.value, reason, now, claim_key),
            )
            if state is ClaimDecisionState.SUPERSEDED:
                connection.execute(
                    """INSERT OR IGNORE INTO claim_supersession(
                        successor_claim_key, predecessor_claim_key, reason, created_at
                    ) VALUES (?, ?, ?, ?)""",
                    (selected_key, claim_key, reason, now),
                )
        connection.execute(
            """INSERT INTO event_field_projection(
                event_key, field_name, selected_claim_key, value_json, uncertain, updated_at
            ) VALUES (?, ?, ?, ?, 0, ?)""",
            (event_key, field_name.value, selected_key, str(selected["value_json"]), now),
        )

    @staticmethod
    def _scalar_projection(
        projections: dict[str, str], field_name: CandidateFieldName
    ) -> str | None:
        raw = projections.get(field_name.value)
        if raw is None:
            return None
        value = json.loads(raw)
        return value if isinstance(value, str) else None
