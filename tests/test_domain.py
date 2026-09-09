from datetime import UTC, datetime, timedelta, timezone

import pytest

from ntulearn_skill.core import (
    AttachmentId,
    ContentId,
    CourseId,
    Coverage,
    SourceTime,
    TemporalPrecision,
)
from ntulearn_skill.core.models import from_storage_time, to_storage_time


def test_remote_identifiers_keep_namespaces_distinct() -> None:
    course = CourseId("synthetic", "same-opaque-value")
    content = ContentId("synthetic", "same-opaque-value")
    attachment = AttachmentId("synthetic", "same-opaque-value")

    assert course != content
    assert content != attachment
    assert len({course, content, attachment}) == 3
    with pytest.raises((AttributeError, TypeError)):
        course.extra = "mutable"  # type: ignore[attr-defined,misc]


@pytest.mark.parametrize(
    ("provider", "value"),
    [("", "opaque"), ("  ", "opaque"), ("synthetic", ""), ("synthetic", "  ")],
)
def test_remote_identifiers_reject_empty_components(provider: str, value: str) -> None:
    with pytest.raises(ValueError):
        CourseId(provider, value)


def test_source_time_normalizes_exact_instant_and_retains_source_fields() -> None:
    source_zone = timezone(timedelta(hours=8))
    value = SourceTime(
        datetime(2027, 2, 10, 10, 30, tzinfo=source_zone),
        "10 February 2027, 10:30",
        "Asia/Singapore",
        TemporalPrecision.EXACT_TIME,
    )

    assert value.instant == datetime(2027, 2, 10, 2, 30, tzinfo=UTC)
    assert value.source_timezone == "Asia/Singapore"
    assert value.source_text == "10 February 2027, 10:30"


def test_partial_source_time_cannot_fabricate_an_instant() -> None:
    with pytest.raises(ValueError, match="only exact source times"):
        SourceTime(
            datetime(2027, 2, 10, tzinfo=UTC),
            "Week 5",
            "Asia/Singapore",
            TemporalPrecision.WEEK_ONLY,
        )

    date_only = SourceTime(None, "10 February 2027", None, TemporalPrecision.DATE_ONLY)
    assert date_only.instant is None

    with pytest.raises(ValueError, match="source timezone"):
        SourceTime(
            datetime(2027, 2, 10, tzinfo=UTC),
            "10 February 2027, 00:00 UTC",
            None,
            TemporalPrecision.EXACT_TIME,
        )


def test_storage_timestamps_require_timezone_and_round_trip_as_utc() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        to_storage_time(datetime(2027, 2, 10))

    value = datetime(2027, 2, 10, 10, 30, tzinfo=timezone(timedelta(hours=8)))
    assert from_storage_time(to_storage_time(value)) == datetime(2027, 2, 10, 2, 30, tzinfo=UTC)


def test_coverage_values_are_explicit() -> None:
    assert {item.value for item in Coverage} == {
        "COMPLETE",
        "PARTIAL",
        "STALE",
        "UNKNOWN",
        "FAILED",
    }
