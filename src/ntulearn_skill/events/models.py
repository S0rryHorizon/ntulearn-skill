"""Typed, unresolved M6 event candidates and exact field evidence."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeAlias

from ntulearn_skill.core import CourseId, TemporalPrecision

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class EventType(StrEnum):
    ASSIGNMENT_DUE = "assignment_due"
    QUIZ = "quiz"
    TEST = "test"
    EXAM = "exam"
    PRESENTATION = "presentation"
    TUTORIAL = "tutorial"
    LAB = "lab"
    LECTURE = "lecture"
    PROJECT_MILESTONE = "project_milestone"
    SUBMISSION = "submission"
    COURSE_CHANGE = "course_change"
    CANCELLATION = "cancellation"
    VENUE_CHANGE = "venue_change"
    GENERIC_COURSE_EVENT = "generic_course_event"


class CandidateFieldName(StrEnum):
    TITLE = "title"
    EVENT_TYPE = "event_type"
    START_TIME = "start_time"
    END_TIME = "end_time"
    DUE_TIME = "due_time"
    OPEN_AT = "open_at"
    CLOSE_AT = "close_at"
    AVAILABLE_FROM = "available_from"
    AVAILABLE_UNTIL = "available_until"
    PUBLISHED_AT = "published_at"
    LOCATION = "location"
    STATUS = "status"


# M7 claims use the same bounded field vocabulary as their immutable M6 source fields.
ClaimFieldName = CandidateFieldName


class CandidateSourceKind(StrEnum):
    ANNOUNCEMENT = "announcement"
    ASSESSMENT = "assessment"
    SCHEDULE = "schedule"
    DUE_ITEM = "due_item"
    DOCUMENT = "document"


class EventStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    TENTATIVE = "TENTATIVE"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    UNKNOWN = "UNKNOWN"


class EventResolutionState(StrEnum):
    RESOLVED = "RESOLVED"
    UNRESOLVED = "UNRESOLVED"
    CONFLICTING = "CONFLICTING"


class EventSourceResolutionState(StrEnum):
    MATCHED = "MATCHED"
    UNRESOLVED = "UNRESOLVED"
    RELATED_CHANGE = "RELATED_CHANGE"


class ClaimDecisionState(StrEnum):
    ACCEPTED = "ACCEPTED"
    SUPERSEDED = "SUPERSEDED"
    CONFLICTING = "CONFLICTING"
    REJECTED = "REJECTED"
    UNRESOLVED = "UNRESOLVED"


class ClaimOrigin(StrEnum):
    SOURCE = "SOURCE"
    LOCAL_DECISION = "LOCAL_DECISION"


class ChangeKind(StrEnum):
    NONE = "NONE"
    MOVE = "MOVE"
    CANCELLATION = "CANCELLATION"
    VENUE_CHANGE = "VENUE_CHANGE"


class ConflictState(StrEnum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class ReconciliationDecisionResult(StrEnum):
    CREATED = "CREATED"
    MERGED = "MERGED"
    RETAINED = "RETAINED"
    UNRESOLVED = "UNRESOLVED"
    RELATED_CHANGE = "RELATED_CHANGE"
    MANUAL_RETAINED = "MANUAL_RETAINED"


@dataclass(frozen=True, slots=True)
class SourceObservationRecord:
    key: int
    source_object_key: int
    sync_run_key: int
    data_kind: CandidateSourceKind
    observed_at: datetime
    observation_hash: str
    snapshot: dict[str, JsonValue]
    raw_wording: str


@dataclass(frozen=True, slots=True)
class ObservedSourceRecord:
    entity_key: int
    observation: SourceObservationRecord


@dataclass(frozen=True, slots=True)
class CandidateField:
    key: int
    name: CandidateFieldName
    value: JsonValue
    original_text: str
    source_path: str
    precision: TemporalPrecision | None
    source_timezone: str | None
    source_observation_key: int | None
    locator_key: int | None


@dataclass(frozen=True, slots=True)
class EventCandidate:
    key: int
    extraction_record_key: int
    course: CourseId
    ordinal: int
    source_kind: CandidateSourceKind
    raw_wording: str
    confidence: float
    fields: tuple[CandidateField, ...]

    def field(self, name: CandidateFieldName) -> CandidateField | None:
        return next((field for field in self.fields if field.name is name), None)


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    extraction_record_key: int
    extractor_name: str
    extractor_version: str
    input_hash: str
    candidates: tuple[EventCandidate, ...]
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class EventSource:
    key: int
    candidate_key: int
    course: CourseId
    event_key: int | None
    source_kind: CandidateSourceKind
    evidence_kind: str
    evidence_key: int
    source_object_key: int | None
    source_timestamp: datetime | None
    source_timestamp_semantics: str | None
    observed_at: datetime | None
    change_kind: ChangeKind
    resolution_state: EventSourceResolutionState
    explanation: str


@dataclass(frozen=True, slots=True)
class Claim:
    key: int
    event_key: int | None
    event_source_key: int | None
    candidate_field_key: int | None
    origin: ClaimOrigin
    field_name: CandidateFieldName
    value: JsonValue
    original_text: str
    precision: TemporalPrecision | None
    source_timezone: str | None
    confidence: float
    decision_state: ClaimDecisionState
    decision_reason: str
    supersedes: tuple[int, ...] = ()
    superseded_by: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ClaimSupersession:
    successor_claim_key: int
    predecessor_claim_key: int
    reason: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class EventProjection:
    field_name: CandidateFieldName
    selected_claim_key: int
    value: JsonValue
    uncertain: bool


@dataclass(frozen=True, slots=True)
class EventConflict:
    key: int
    event_key: int
    field_name: CandidateFieldName
    state: ConflictState
    reason: str
    alternative_claim_keys: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class CanonicalEvent:
    key: int
    stable_id: str
    course: CourseId
    event_type: EventType | None
    title: str | None
    start_time: JsonValue
    end_time: JsonValue
    due_time: JsonValue
    location: str | None
    status: EventStatus | None
    resolution_state: EventResolutionState
    confidence: float
    source_count: int
    projections: tuple[EventProjection, ...]
    sources: tuple[EventSource, ...]
    claims: tuple[Claim, ...]
    conflicts: tuple[EventConflict, ...]

    def field(self, name: CandidateFieldName) -> EventProjection | None:
        return next((field for field in self.projections if field.field_name is name), None)


@dataclass(frozen=True, slots=True)
class ReconciliationDecision:
    key: int
    candidate_key: int
    event_key: int | None
    result: ReconciliationDecisionResult
    considered_event_keys: tuple[int, ...]
    accepted_signals: tuple[str, ...]
    rejected_signals: tuple[str, ...]
    confidence: float
    explanation: str


@dataclass(frozen=True, slots=True)
class ReconciliationResult:
    run_key: int
    resolver_name: str
    resolver_version: str
    input_hash: str
    events: tuple[CanonicalEvent, ...]
    unresolved_sources: tuple[EventSource, ...]
    decisions: tuple[ReconciliationDecision, ...]
    cache_hit: bool


@dataclass(frozen=True, slots=True)
class ManualFieldResolution:
    event_key: int
    field_name: CandidateFieldName
    reason: str
    selected_claim_key: int | None = None
    value: JsonValue = None
    original_text: str = "local user decision"
    precision: TemporalPrecision | None = None
    source_timezone: str | None = None
    confidence: float = 1.0

    def __post_init__(self) -> None:
        if self.event_key <= 0:
            raise ValueError("event key must be positive")
        if self.field_name not in {
            CandidateFieldName.TITLE,
            CandidateFieldName.EVENT_TYPE,
            CandidateFieldName.START_TIME,
            CandidateFieldName.END_TIME,
            CandidateFieldName.DUE_TIME,
            CandidateFieldName.LOCATION,
            CandidateFieldName.STATUS,
        }:
            raise ValueError("field is not part of the canonical event projection")
        if not self.reason.strip() or len(self.reason) > 4096:
            raise ValueError("manual resolution reason is invalid")
        if (self.selected_claim_key is None) == (self.value is None):
            raise ValueError("choose exactly one existing claim or local value")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("manual confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class ManualIdentityResolution:
    event_source_key: int
    event_key: int
    reason: str

    def __post_init__(self) -> None:
        if self.event_source_key <= 0 or self.event_key <= 0:
            raise ValueError("event and source keys must be positive")
        if not self.reason.strip() or len(self.reason) > 4096:
            raise ValueError("manual identity reason is invalid")
