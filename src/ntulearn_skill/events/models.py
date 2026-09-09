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


class CandidateSourceKind(StrEnum):
    ANNOUNCEMENT = "announcement"
    ASSESSMENT = "assessment"
    SCHEDULE = "schedule"
    DUE_ITEM = "due_item"
    DOCUMENT = "document"


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
