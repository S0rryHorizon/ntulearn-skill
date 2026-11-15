"""Typed core query requests and result views."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TypeAlias

from ntulearn_skill.client import TimeWindow
from ntulearn_skill.core.identifiers import AttachmentId, CourseId, require_identifier
from ntulearn_skill.core.models import (
    AssessmentSubtype,
    Availability,
)
from ntulearn_skill.events import (
    EventType,
    ManualFieldResolution,
    ManualIdentityResolution,
)
from ntulearn_skill.search import SourceReference

DEFAULT_LIMIT = 100


def bounded_limit(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise ValueError("limit must be between 1 and 100")


def bounded_cursor(value: str | None) -> None:
    if value is not None and (
        not isinstance(value, str) or not value or not value.isascii() or len(value) > 2_048
    ):
        raise ValueError("cursor must be bounded ASCII text")


@dataclass(frozen=True, slots=True)
class CourseRef:
    local_key: int | None = None
    remote_id: CourseId | None = None

    def __post_init__(self) -> None:
        if (self.local_key is None) == (self.remote_id is None):
            raise ValueError("course reference requires exactly one local or remote identity")
        if self.local_key is not None and (isinstance(self.local_key, bool) or self.local_key <= 0):
            raise ValueError("course local key must be positive")
        if self.remote_id is not None:
            require_identifier(self.remote_id, CourseId)


@dataclass(frozen=True, slots=True)
class ResourceRef:
    local_key: int | None = None
    remote_id: AttachmentId | None = None

    def __post_init__(self) -> None:
        if (self.local_key is None) == (self.remote_id is None):
            raise ValueError("resource reference requires exactly one local or remote identity")
        if self.local_key is not None and (isinstance(self.local_key, bool) or self.local_key <= 0):
            raise ValueError("resource local key must be positive")
        if self.remote_id is not None:
            require_identifier(self.remote_id, AttachmentId)


@dataclass(frozen=True, slots=True)
class SourceLocatorRef:
    reference: SourceReference

    def __post_init__(self) -> None:
        if not isinstance(self.reference, SourceReference):
            raise TypeError("source locator reference must be typed")


@dataclass(frozen=True, slots=True)
class CourseFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        bounded_limit(self.limit)
        bounded_cursor(self.cursor)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("course availability filters must be typed")


@dataclass(frozen=True, slots=True)
class MaterialFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    file_formats: frozenset[str] = field(default_factory=frozenset)
    include_historical_versions: bool = False
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        bounded_limit(self.limit)
        bounded_cursor(self.cursor)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("material availability filters must be typed")
        if any(item not in {"pdf", "docx", "pptx", "unknown"} for item in self.file_formats):
            raise ValueError("material file format is invalid")


@dataclass(frozen=True, slots=True)
class AnnouncementFilter:
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        bounded_limit(self.limit)
        bounded_cursor(self.cursor)
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("announcement availability filters must be typed")


@dataclass(frozen=True, slots=True)
class AssessmentFilter:
    subtypes: frozenset[AssessmentSubtype] = field(default_factory=frozenset)
    availability: frozenset[Availability] = field(default_factory=frozenset)
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        bounded_limit(self.limit)
        bounded_cursor(self.cursor)
        if any(not isinstance(item, AssessmentSubtype) for item in self.subtypes):
            raise TypeError("assessment subtype filters must be typed")
        if any(not isinstance(item, Availability) for item in self.availability):
            raise TypeError("assessment availability filters must be typed")


@dataclass(frozen=True, slots=True)
class EventFilter:
    course: CourseRef | None = None
    event_types: frozenset[EventType] = field(default_factory=frozenset)
    window: TimeWindow | None = None
    include_cancelled: bool = True
    limit: int = DEFAULT_LIMIT
    cursor: str | None = None

    def __post_init__(self) -> None:
        bounded_limit(self.limit)
        bounded_cursor(self.cursor)
        if self.course is not None and not isinstance(self.course, CourseRef):
            raise TypeError("event course filter must be typed")
        if any(not isinstance(item, EventType) for item in self.event_types):
            raise TypeError("event type filters must be typed")
        if self.window is not None and not isinstance(self.window, TimeWindow):
            raise TypeError("event window must be typed")


@dataclass(frozen=True, slots=True)
class SyncPolicy:
    window: TimeWindow
    fetch_resources: bool = True
    verify_resources: bool = False
    page_size: int = 100
    max_pages_per_scope: int = 100
    max_content_nodes: int = 10_000
    max_jobs: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.window, TimeWindow):
            raise TypeError("sync policy window must be typed")
        if not 1 <= self.page_size <= 1_000:
            raise ValueError("page size must be between 1 and 1000")
        if min(self.max_pages_per_scope, self.max_content_nodes, self.max_jobs) <= 0:
            raise ValueError("sync bounds must be positive")


@dataclass(frozen=True, slots=True)
class CourseSummary:
    local_key: int
    course_id: CourseId
    code: str
    title: str
    term: str | None
    availability: Availability
    first_observed_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True, slots=True)
class MaterialSummary:
    local_key: int
    resource_id: AttachmentId
    course: CourseId
    content_key: int
    display_title: str
    availability: Availability
    version_key: int | None
    version_number: int | None
    file_format: str | None
    byte_size: int | None
    downloaded_at: datetime | None
    semantic_type: str | None
    classification_confidence: float | None


@dataclass(frozen=True, slots=True)
class LibraryStatus:
    course_key: int
    course: CourseId
    code: str
    title: str
    content_nodes: int
    discovered_materials: int
    downloaded_materials: int
    parsed_materials: int
    completely_parsed_materials: int
    indexed_chunks: int
    total_local_jobs: int
    succeeded_local_jobs: int
    pending_local_jobs: int
    running_local_jobs: int
    failed_local_jobs: int
    last_material_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class MaterialChangeView:
    resource_key: int
    observation_key: int
    course: CourseId
    display_title: str
    observed_at: datetime
    change_kinds: tuple[str, ...]
    version_key: int | None
    previous_version_key: int | None
    candidate_modified_at: str | None


@dataclass(frozen=True, slots=True)
class AnnouncementView:
    local_key: int
    announcement_id: str
    course: CourseId
    title: str
    body: str
    availability: Availability
    created: Mapping[str, object] | None
    modified: Mapping[str, object] | None
    published: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class AssessmentView:
    local_key: int
    assessment_id: str
    course: CourseId
    content_key: int
    title: str
    subtype: AssessmentSubtype
    instructions: str
    availability: Availability
    due: Mapping[str, object] | None
    grading_due: Mapping[str, object] | None
    generic_due: Mapping[str, object] | None
    open_at: Mapping[str, object] | None
    close_at: Mapping[str, object] | None


@dataclass(frozen=True, slots=True)
class ResourceView:
    local_key: int
    resource_id: AttachmentId
    display_title: str
    availability: Availability
    version_key: int | None
    version_number: int | None
    sha256: str | None
    byte_size: int | None
    file_format: str | None
    declared_mime: str | None
    downloaded_at: datetime | None
    parse_key: int | None = None
    local_path: Path | None = None


ManualResolution: TypeAlias = ManualFieldResolution | ManualIdentityResolution
