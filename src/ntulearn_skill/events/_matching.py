"""Small pure helpers for conservative event identity and claim comparison."""

from __future__ import annotations

import re
from typing import Any

from ntulearn_skill.events.models import CandidateFieldName, ChangeKind

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
_CHANGE_SUFFIX = re.compile(
    r"\b(?:is|was|has\s+been)?\s*"
    r"(?:moved|postponed|rescheduled|cancelled|canceled|changed|corrected|updated)\b.*$",
    re.I,
)


def title_identity(value: object) -> tuple[str, int | None]:
    if not isinstance(value, str):
        return "", None
    text = _CHANGE_SUFFIX.sub("", value.casefold()).replace("#", " ")
    for word, ordinal in _ORDINALS.items():
        text = re.sub(rf"\b{word}\b", str(ordinal), text)
    tokens = re.findall(r"[a-z]+|\d+", text)
    number = next((int(token) for token in tokens if token.isdigit()), None)
    event_word = next(
        (
            token
            for token in tokens
            if token
            in {
                "assignment",
                "homework",
                "quiz",
                "test",
                "exam",
                "presentation",
                "tutorial",
                "lab",
                "lecture",
                "submission",
            }
        ),
        None,
    )
    if event_word is not None and number is not None:
        event_word = "assignment" if event_word == "homework" else event_word
        return f"{event_word} {number}", number
    return " ".join(tokens), number


def temporal_signature(value: object) -> tuple[str, str] | None:
    if not isinstance(value, dict):
        return None
    instant = value.get("instant")
    if isinstance(instant, str):
        return "instant", instant
    date = value.get("date")
    if isinstance(date, str):
        return "date", date
    week = value.get("week_number")
    if isinstance(week, int):
        return "week", str(week)
    return None


def temporal_agrees(left: object, right: object) -> bool:
    left_signature = temporal_signature(left)
    right_signature = temporal_signature(right)
    if left_signature is None or right_signature is None:
        return False
    if left_signature == right_signature:
        return True
    if left_signature[0] == "instant" and right_signature[0] == "date":
        return left_signature[1][:10] == right_signature[1]
    if left_signature[0] == "date" and right_signature[0] == "instant":
        return left_signature[1] == right_signature[1][:10]
    return False


def semantic_value(field: CandidateFieldName, value: Any) -> object:
    if field is CandidateFieldName.TITLE:
        return title_identity(value)[0]
    if field in {
        CandidateFieldName.START_TIME,
        CandidateFieldName.END_TIME,
        CandidateFieldName.DUE_TIME,
    }:
        return temporal_signature(value)
    if isinstance(value, str):
        return value.strip().casefold()
    return value


def change_applies(change: ChangeKind, field: CandidateFieldName) -> bool:
    if change is ChangeKind.MOVE:
        return field in {
            CandidateFieldName.START_TIME,
            CandidateFieldName.END_TIME,
            CandidateFieldName.DUE_TIME,
        }
    if change is ChangeKind.CANCELLATION:
        return field is CandidateFieldName.STATUS
    if change is ChangeKind.VENUE_CHANGE:
        return field is CandidateFieldName.LOCATION
    return False


def precision_rank(value: str | None) -> int:
    if value is None:
        return 0
    return {"EXACT_TIME": 3, "DATE_ONLY": 2, "WEEK_ONLY": 1, "UNKNOWN": 0}.get(value, 0)
