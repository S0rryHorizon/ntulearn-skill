"""Transactional manual event decisions over immutable M6 evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from typing import TYPE_CHECKING, NoReturn

from ntulearn_skill.core import TemporalPrecision
from ntulearn_skill.core.models import from_storage_time, to_storage_time, utc_now
from ntulearn_skill.events.models import (
    CandidateFieldName,
    CanonicalEvent,
    EventStatus,
    EventType,
    JsonValue,
    ManualFieldResolution,
    ManualIdentityResolution,
)

if TYPE_CHECKING:
    from ntulearn_skill.events.reconciliation import EventReconciler


_TEMPORAL_FIELDS = {
    CandidateFieldName.START_TIME,
    CandidateFieldName.END_TIME,
    CandidateFieldName.DUE_TIME,
}


def _failure(message: str) -> NoReturn:
    # Imported only on an exceptional runtime path to avoid a module cycle.
    from ntulearn_skill.events.reconciliation import EventReconciliationError

    raise EventReconciliationError(message) from None


def _bounded_text(value: object, label: str, *, maximum: int = 4096) -> str:
    if not isinstance(value, str):
        raise ValueError(f"manual {label} must be text")
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        raise ValueError(f"manual {label} is invalid")
    return normalized


def _temporal_value(
    request: ManualFieldResolution,
) -> tuple[JsonValue, TemporalPrecision, str | None]:
    if not isinstance(request.value, dict):
        raise ValueError("manual temporal value must be structured")
    supplied = request.value
    raw_precision = supplied.get("precision")
    if request.precision is not None and raw_precision is not None:
        try:
            embedded = TemporalPrecision(str(raw_precision))
        except ValueError:
            raise ValueError("manual temporal precision is invalid") from None
        if embedded is not request.precision:
            raise ValueError("manual temporal precision is inconsistent")
    precision = request.precision
    if precision is None and raw_precision is not None:
        try:
            precision = TemporalPrecision(str(raw_precision))
        except ValueError:
            raise ValueError("manual temporal precision is invalid") from None
    if precision is None:
        if isinstance(supplied.get("instant"), str):
            precision = TemporalPrecision.EXACT_TIME
        elif isinstance(supplied.get("date"), str):
            precision = TemporalPrecision.DATE_ONLY
        elif isinstance(supplied.get("week_number"), int):
            precision = TemporalPrecision.WEEK_ONLY
        else:
            raise ValueError("manual temporal precision is required")

    source_text = _bounded_text(supplied.get("source_text", request.original_text), "source text")
    embedded_timezone = supplied.get("source_timezone")
    if embedded_timezone is not None and not isinstance(embedded_timezone, str):
        raise ValueError("manual source timezone is invalid")
    if request.source_timezone is not None and embedded_timezone is not None:
        if request.source_timezone.strip() != embedded_timezone.strip():
            raise ValueError("manual source timezone is inconsistent")
    timezone = request.source_timezone if request.source_timezone is not None else embedded_timezone
    if timezone is not None:
        timezone = _bounded_text(timezone, "source timezone", maximum=200)

    if precision is TemporalPrecision.EXACT_TIME:
        instant = supplied.get("instant")
        if not isinstance(instant, str) or timezone is None:
            raise ValueError("manual exact time requires an instant and source timezone")
        try:
            normalized_instant = to_storage_time(from_storage_time(instant))
        except ValueError:
            raise ValueError("manual exact time is invalid") from None
        return (
            {
                "instant": normalized_instant,
                "precision": precision.value,
                "source_text": source_text,
                "source_timezone": timezone,
            },
            precision,
            timezone,
        )
    if timezone is not None:
        raise ValueError("manual partial time cannot carry a source timezone")
    if precision is TemporalPrecision.DATE_ONLY:
        raw_date = supplied.get("date")
        if not isinstance(raw_date, str):
            raise ValueError("manual date-only value requires a date")
        try:
            normalized_date = date.fromisoformat(raw_date).isoformat()
        except ValueError:
            raise ValueError("manual date-only value is invalid") from None
        return (
            {
                "instant": None,
                "precision": precision.value,
                "source_text": source_text,
                "source_timezone": None,
                "date": normalized_date,
            },
            precision,
            None,
        )
    if precision is TemporalPrecision.WEEK_ONLY:
        week = supplied.get("week_number")
        if isinstance(week, bool) or not isinstance(week, int) or not 1 <= week <= 53:
            raise ValueError("manual week-only value is invalid")
        return (
            {
                "instant": None,
                "precision": precision.value,
                "source_text": source_text,
                "source_timezone": None,
                "week_number": week,
            },
            precision,
            None,
        )
    raise ValueError("manual temporal precision is invalid")


def _local_assertion(request: ManualFieldResolution) -> tuple[str, str, str | None, str | None]:
    field = request.field_name
    if field in _TEMPORAL_FIELDS:
        temporal_value, precision, timezone = _temporal_value(request)
        original_text = _bounded_text(request.original_text, "original text")
        return _json(temporal_value), original_text, precision.value, timezone
    if request.precision is not None or request.source_timezone is not None:
        raise ValueError("manual non-temporal value cannot carry temporal metadata")
    if field is CandidateFieldName.EVENT_TYPE:
        try:
            value: JsonValue = EventType(str(request.value)).value
        except ValueError:
            raise ValueError("manual event type is invalid") from None
    elif field is CandidateFieldName.STATUS:
        try:
            value = EventStatus(str(request.value)).value
        except ValueError:
            raise ValueError("manual event status is invalid") from None
    elif field in {CandidateFieldName.TITLE, CandidateFieldName.LOCATION}:
        value = _bounded_text(request.value, field.value.replace("_", " "))
    else:
        raise ValueError("manual field is not part of the canonical event projection")
    return _json(value), _bounded_text(request.original_text, "original text"), None, None


def _json(value: JsonValue) -> str:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError):
        raise ValueError("manual value is invalid") from None


def _resolved_event(reconciler: EventReconciler, event_key: int) -> CanonicalEvent:
    event = reconciler.get_event(event_key)
    if event is None:
        _failure("manual resolution result is unavailable")
    return event


def resolve_field(reconciler: EventReconciler, request: ManualFieldResolution) -> CanonicalEvent:
    """Append one active field decision and rebuild the event atomically."""

    if not isinstance(request, ManualFieldResolution):
        raise TypeError("manual field resolution must be typed")
    local_assertion = None if request.selected_claim_key is not None else _local_assertion(request)
    now = to_storage_time(utc_now())
    try:
        with reconciler.database.transaction() as connection:
            event = connection.execute(
                "SELECT event_key FROM event WHERE event_key = ?", (request.event_key,)
            ).fetchone()
            if event is None:
                raise ValueError("manual resolution event does not exist")

            selected_claim_key = request.selected_claim_key
            local_claim_key: int | None = None
            if selected_claim_key is not None:
                claim = connection.execute(
                    """SELECT claim_key FROM claim
                    WHERE claim_key = ? AND event_key = ? AND field_name = ?""",
                    (selected_claim_key, request.event_key, request.field_name.value),
                ).fetchone()
                if claim is None:
                    raise ValueError("manual selected claim does not belong to this event field")
            else:
                assert local_assertion is not None
                value_json, original_text, precision, timezone = local_assertion
                cursor = connection.execute(
                    """INSERT INTO claim(
                        event_key, event_source_key, candidate_field_key, origin,
                        field_name, value_json, original_text, temporal_precision,
                        source_timezone, confidence, decision_state, decision_reason,
                        created_at, updated_at
                    ) VALUES (?, NULL, NULL, 'LOCAL_DECISION', ?, ?, ?, ?, ?, ?,
                              'UNRESOLVED', 'awaiting manual field resolution', ?, ?)""",
                    (
                        request.event_key,
                        request.field_name.value,
                        value_json,
                        original_text,
                        precision,
                        timezone,
                        request.confidence,
                        now,
                        now,
                    ),
                )
                assert cursor.lastrowid is not None
                local_claim_key = int(cursor.lastrowid)

            connection.execute(
                """UPDATE manual_field_resolution SET active = 0
                WHERE event_key = ? AND field_name = ? AND active = 1""",
                (request.event_key, request.field_name.value),
            )
            connection.execute(
                """INSERT INTO manual_field_resolution(
                    event_key, field_name, selected_claim_key, local_claim_key,
                    reason, decided_at, active
                ) VALUES (?, ?, ?, ?, ?, ?, 1)""",
                (
                    request.event_key,
                    request.field_name.value,
                    selected_claim_key,
                    local_claim_key,
                    request.reason,
                    now,
                ),
            )
            reconciler._recompute_event(connection, request.event_key, now)
    except ValueError:
        raise
    except (sqlite3.Error, TypeError):
        _failure("manual field resolution failed")
    return _resolved_event(reconciler, request.event_key)


def resolve_event_source(
    reconciler: EventReconciler, request: ManualIdentityResolution
) -> CanonicalEvent:
    """Append an identity decision and bind its source evidence atomically."""

    if not isinstance(request, ManualIdentityResolution):
        raise TypeError("manual identity resolution must be typed")
    now = to_storage_time(utc_now())
    try:
        with reconciler.database.transaction() as connection:
            target = connection.execute(
                "SELECT course_key FROM event WHERE event_key = ?", (request.event_key,)
            ).fetchone()
            source = connection.execute(
                """SELECT course_key, event_key FROM event_source
                WHERE event_source_key = ?""",
                (request.event_source_key,),
            ).fetchone()
            if target is None:
                raise ValueError("manual identity event does not exist")
            if source is None:
                raise ValueError("manual identity event source does not exist")
            if int(source["course_key"]) != int(target["course_key"]):
                raise ValueError("manual identity source and event must belong to the same course")
            previous_event_key = None if source["event_key"] is None else int(source["event_key"])
            if previous_event_key is not None and previous_event_key != request.event_key:
                raise ValueError("an assigned event source cannot be moved to another event")

            connection.execute(
                """UPDATE event_source SET event_key = ?, resolution_state = 'MATCHED',
                    explanation = 'manual identity resolution', updated_at = ?
                WHERE event_source_key = ?""",
                (request.event_key, now, request.event_source_key),
            )
            connection.execute(
                """UPDATE claim SET event_key = ?, decision_state = 'UNRESOLVED',
                    decision_reason = 'awaiting field reconciliation', updated_at = ?
                WHERE event_source_key = ?""",
                (request.event_key, now, request.event_source_key),
            )
            connection.execute(
                "DELETE FROM event_source_possible_match WHERE event_source_key = ?",
                (request.event_source_key,),
            )
            connection.execute(
                """UPDATE manual_identity_resolution SET active = 0
                WHERE event_source_key = ? AND active = 1""",
                (request.event_source_key,),
            )
            connection.execute(
                """INSERT INTO manual_identity_resolution(
                    event_source_key, event_key, reason, decided_at, active
                ) VALUES (?, ?, ?, ?, 1)""",
                (request.event_source_key, request.event_key, request.reason, now),
            )
            reconciler._recompute_event(connection, request.event_key, now)
    except ValueError:
        raise
    except (sqlite3.Error, TypeError):
        _failure("manual identity resolution failed")
    return _resolved_event(reconciler, request.event_key)
