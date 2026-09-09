"""Typed remote identifiers used at public API boundaries."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RemoteId:
    """Provider-scoped opaque identity.

    Concrete subclasses deliberately remain distinct at runtime even when their
    provider and opaque values are identical.
    """

    provider: str
    value: str

    def __post_init__(self) -> None:
        if not self.provider or not self.provider.strip():
            raise ValueError("provider must be non-empty")
        if not self.value or not self.value.strip():
            raise ValueError("opaque identifier must be non-empty")


@dataclass(frozen=True, slots=True)
class CourseId(RemoteId):
    """Remote course identity."""


@dataclass(frozen=True, slots=True)
class ContentId(RemoteId):
    """Remote content-node identity."""


@dataclass(frozen=True, slots=True)
class AttachmentId(RemoteId):
    """Remote attachment identity."""


@dataclass(frozen=True, slots=True)
class AnnouncementId(RemoteId):
    """Remote announcement identity."""


@dataclass(frozen=True, slots=True)
class AssessmentId(RemoteId):
    """Remote assessment identity."""


@dataclass(frozen=True, slots=True)
class GradingColumnId(RemoteId):
    """Remote grading-column identity."""


@dataclass(frozen=True, slots=True)
class CalendarItemId(RemoteId):
    """Remote calendar-item identity."""


def require_identifier(value: object, expected: type[RemoteId]) -> RemoteId:
    """Reject namespace mistakes at runtime, including sibling ID wrappers."""

    if type(value) is not expected:
        raise TypeError(f"expected {expected.__name__}")
    return value
