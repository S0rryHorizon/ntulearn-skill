"""Deterministic, LLM-independent extraction of unresolved event candidates."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone

from ntulearn_skill.core import CourseId, TemporalPrecision
from ntulearn_skill.core.models import to_storage_time, utc_now
from ntulearn_skill.events.models import (
    CandidateField,
    CandidateFieldName,
    CandidateSourceKind,
    ChangeKind,
    EventCandidate,
    EventType,
    ExtractionResult,
    JsonValue,
)
from ntulearn_skill.storage import Database, StorageError


class EventExtractionError(StorageError):
    """A privacy-safe deterministic extraction failure."""


_SETTINGS = {"maximum_candidates": 1000, "maximum_mentions_per_chunk": 100}
_SETTINGS_JSON = json.dumps(_SETTINGS, sort_keys=True, separators=(",", ":"))
_SETTINGS_HASH = hashlib.sha256(_SETTINGS_JSON.encode("utf-8")).hexdigest()
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_TEMPORAL_PATTERN = re.compile(
    rf"\b(?:"
    rf"\d{{4}}-\d{{2}}-\d{{2}}[T ]\d{{1,2}}:\d{{2}}(?::\d{{2}})?"
    rf"(?:Z|\s?(?:[+-]\d{{2}}:\d{{2}}|SGT|UTC))"
    rf"|(?:\d{{1,2}}\s+{_MONTH}|{_MONTH}\s+\d{{1,2}}),?\s+\d{{4}}"
    rf"\s+(?:at\s+)?\d{{1,2}}:\d{{2}}(?:\s?(?:[+-]\d{{2}}:\d{{2}}|SGT|UTC))"
    rf"|\d{{4}}-\d{{2}}-\d{{2}}"
    rf"|(?:\d{{1,2}}\s+{_MONTH}|{_MONTH}\s+\d{{1,2}}),?\s+\d{{4}}"
    rf"|Week\s+\d{{1,2}}"
    rf")\b",
    flags=re.IGNORECASE,
)
_SPLIT = re.compile(
    r"(?:\r?\n)+|;\s*|(?<=[.!?])\s+|"
    r",\s*(?=(?:Assignment|Homework|Quiz|Test|Exam|Presentation|Tutorial|Lab|Lecture)\b)",
    re.I,
)
_TYPE_PATTERNS: tuple[tuple[re.Pattern[str], EventType], ...] = (
    (re.compile(r"\b(?:assignment|homework)\b", re.I), EventType.ASSIGNMENT_DUE),
    (re.compile(r"\bquiz\b", re.I), EventType.QUIZ),
    (re.compile(r"\b(?:term\s+test|midterm|test)\b", re.I), EventType.TEST),
    (re.compile(r"\bexam(?:ination)?\b", re.I), EventType.EXAM),
    (re.compile(r"\bpresentation\b", re.I), EventType.PRESENTATION),
    (re.compile(r"\btutorial\b", re.I), EventType.TUTORIAL),
    (re.compile(r"\blab(?:oratory)?\b", re.I), EventType.LAB),
    (re.compile(r"\blecture\b", re.I), EventType.LECTURE),
    (re.compile(r"\b(?:project\s+milestone|milestone)\b", re.I), EventType.PROJECT_MILESTONE),
    (re.compile(r"\bsubmission\b", re.I), EventType.SUBMISSION),
)
_EXPLICIT_START_PATTERN = re.compile(r"\b(?:starts?|begins?|takes?\s+place)\b", re.I)
_NON_START_TEMPORAL_PATTERN = re.compile(
    r"\b(?:announced|published|opens?|available(?:\s+from)?)\b", re.I
)
_CHANGE_TERM = r"(?:moved|postponed|rescheduled|cancelled|canceled|changed|corrected|updated)"
_CHANGE_TERM_PATTERN = re.compile(rf"\b{_CHANGE_TERM}\b", re.I)
_CONDITIONAL_CHANGE_PATTERN = re.compile(r"\b(?:if|unless|whether)\b|\bin\s+case\b", re.I)
_ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
_EVENT_TARGET = (
    rf"(?:the\s+)?(?:{_ORDINAL}\s+)?"
    r"(?:assignment|homework|quiz|term\s+test|midterm|test|exam(?:ination)?|"
    r"presentation|tutorial|lab(?:oratory)?|lecture|submission)"
    r"(?:\s+#?\d+)?"
)
_PASSIVE_CHANGE_AUXILIARY = r"(?:(?:is|are|was|were)\s+|(?:has|have|had)\s+been\s+|will\s+be\s+)?"
_MOVE_CHANGE_AUXILIARY = r"(?:(?:is|are|was|were)\s+|(?:has|have|had)\s+(?:been\s+)?|will\s+be\s+)?"
_AFFIRMATIVE_CANCELLATION_PATTERN = re.compile(
    rf"^\s*{_EVENT_TARGET}\s+{_PASSIVE_CHANGE_AUXILIARY}(?:cancelled|canceled)\b",
    re.I,
)
_AFFIRMATIVE_MOVE_PATTERN = re.compile(
    rf"^\s*{_EVENT_TARGET}"
    r"(?:\s+(?:deadline|due\s+date|date|time|schedule|session))?\s+"
    rf"{_MOVE_CHANGE_AUXILIARY}(?:moved|postponed|rescheduled)\b",
    re.I,
)
_CANCELLATION_TOKEN_PATTERN = re.compile(r"\b(?:cancelled|canceled)\b", re.I)
_VENUE_CHANGE_PATTERN = re.compile(
    rf"^\s*{_EVENT_TARGET}\s+(?:venue|location|room)\s+{_MOVE_CHANGE_AUXILIARY}"
    r"(?:changed|moved|updated)\s+(?:to\s+)?(?P<location>[^.;\n]{1,200})",
    re.I,
)
_AFFIRMATIVE_TEMPORAL_REVISION_PATTERN = re.compile(
    rf"^\s*{_EVENT_TARGET}\s+(?:deadline|due\s+date|date|time|schedule|session)\s+"
    rf"{_MOVE_CHANGE_AUXILIARY}(?:changed|corrected|updated)\b",
    re.I,
)


def classify_change_language(text: str) -> ChangeKind:
    """Classify only bounded affirmative English change statements."""

    if _CHANGE_TERM_PATTERN.search(text) is None:
        return ChangeKind.NONE
    if text.rstrip().endswith("?") or _CONDITIONAL_CHANGE_PATTERN.search(text) is not None:
        return ChangeKind.NONE
    if _AFFIRMATIVE_CANCELLATION_PATTERN.search(text) is not None:
        return ChangeKind.CANCELLATION
    if _VENUE_CHANGE_PATTERN.search(text) is not None:
        return ChangeKind.VENUE_CHANGE
    if (
        _AFFIRMATIVE_MOVE_PATTERN.search(text) is not None
        or _AFFIRMATIVE_TEMPORAL_REVISION_PATTERN.search(text) is not None
    ):
        return ChangeKind.MOVE
    return ChangeKind.NONE


@dataclass(frozen=True, slots=True)
class _FieldDraft:
    name: CandidateFieldName
    value: JsonValue
    original_text: str
    precision: TemporalPrecision | None = None
    source_timezone: str | None = None
    source_path: str = "text"


@dataclass(frozen=True, slots=True)
class _CandidateDraft:
    source_kind: CandidateSourceKind
    raw_wording: str
    confidence: float
    evidence_key: int
    fields: tuple[_FieldDraft, ...]


class DeterministicEventExtractor:
    """Extract reproducible candidates from structured observations or immutable chunks."""

    name = "deterministic-event-rules"
    version = "3"

    def __init__(self, database: Database) -> None:
        self.database = database

    def extract_observation(self, observation_key: int) -> ExtractionResult:
        if observation_key <= 0:
            raise ValueError("observation key must be positive")
        try:
            with self.database.transaction() as connection:
                source = connection.execute(
                    """
                    SELECT observation.*, course.course_key, provider.name AS provider,
                           course_object.remote_key AS course_remote
                    FROM source_observation observation
                    JOIN source_object object
                      ON object.source_object_key = observation.source_object_key
                    JOIN source_provider provider ON provider.provider_key = object.provider_key
                    JOIN (
                        SELECT source_object_key, course_key FROM announcement
                        UNION ALL SELECT source_object_key, course_key FROM assessment
                        UNION ALL SELECT source_object_key, course_key FROM schedule_item
                        UNION ALL SELECT source_object_key, course_key FROM due_item
                    ) owner ON owner.source_object_key = observation.source_object_key
                    JOIN course ON course.course_key = owner.course_key
                    JOIN source_object course_object
                      ON course_object.source_object_key = course.source_object_key
                    WHERE observation.observation_key = ?
                    """,
                    (observation_key,),
                ).fetchone()
                if source is None:
                    raise ValueError("source observation does not exist")
                cached = self._cached(connection, "source_observation", observation_key, None, None)
                if cached is not None:
                    return self._result(connection, cached, cache_hit=True)
                snapshot = json.loads(str(source["snapshot_json"]))
                if not isinstance(snapshot, dict):
                    raise ValueError("source observation snapshot is invalid")
                source_kind = CandidateSourceKind(str(source["data_kind"]))
                drafts = self._source_drafts(source_kind, snapshot, observation_key)
                extraction_key = self._insert_extraction(
                    connection,
                    input_kind="source_observation",
                    source_observation_key=observation_key,
                    version_key=None,
                    parse_key=None,
                    input_hash=str(source["observation_hash"]),
                    status="COMPLETE",
                    warnings=(),
                )
                self._insert_drafts(
                    connection, extraction_key, int(source["course_key"]), drafts, document=False
                )
                return self._result(connection, extraction_key, cache_hit=False)
        except sqlite3.IntegrityError:
            return self._load_raced("source_observation", observation_key)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventExtractionError("structured event extraction failed") from None

    def extract_resource_version(self, version_key: int) -> ExtractionResult:
        if version_key <= 0:
            raise ValueError("version key must be positive")
        try:
            with self.database.transaction() as connection:
                version = connection.execute(
                    """
                    SELECT version.sha256, resource_node.course_key,
                           provider.name AS provider, course_object.remote_key AS course_remote
                    FROM resource_version version
                    JOIN resource ON resource.resource_key = version.resource_key
                    JOIN content_node resource_node
                      ON resource_node.content_key = resource.content_key
                    JOIN course ON course.course_key = resource_node.course_key
                    JOIN source_object course_object
                      ON course_object.source_object_key = course.source_object_key
                    JOIN source_provider provider
                      ON provider.provider_key = course_object.provider_key
                    WHERE version.version_key = ?
                      AND version.verification_status = 'VERIFIED'
                    """,
                    (version_key,),
                ).fetchone()
                if version is None:
                    raise ValueError("verified resource version does not exist")
                parsed = connection.execute(
                    """
                    SELECT * FROM parsed_document parsed
                    WHERE parsed.version_key = ? AND parsed.status IN ('COMPLETE', 'PARTIAL')
                    ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
                    """,
                    (version_key,),
                ).fetchone()
                if parsed is None:
                    raise ValueError("resource version has no reusable parse")
                parse_key = int(parsed["parse_key"])
                cached = self._cached(connection, "resource_version", None, version_key, parse_key)
                if cached is not None:
                    return self._result(connection, cached, cache_hit=True)
                chunks = connection.execute(
                    """
                    SELECT chunk.native_text, locator.locator_key, parsed.coverage
                    FROM document_chunk chunk
                    JOIN source_locator locator ON locator.chunk_key = chunk.chunk_key
                    JOIN parsed_document parsed ON parsed.parse_key = chunk.parse_key
                    WHERE chunk.parse_key = ? ORDER BY chunk.ordinal
                    """,
                    (parse_key,),
                ).fetchall()
                drafts: list[_CandidateDraft] = []
                warnings: list[str] = []
                for chunk in chunks:
                    chunk_drafts = self._text_drafts(
                        str(chunk["native_text"]), int(chunk["locator_key"])
                    )
                    if len(chunk_drafts) > _SETTINGS["maximum_mentions_per_chunk"]:
                        chunk_drafts = chunk_drafts[: _SETTINGS["maximum_mentions_per_chunk"]]
                        warnings.append("chunk_candidate_limit_reached")
                    drafts.extend(chunk_drafts)
                    if len(drafts) >= _SETTINGS["maximum_candidates"]:
                        drafts = drafts[: _SETTINGS["maximum_candidates"]]
                        warnings.append("candidate_limit_reached")
                        break
                parse_partial = any(str(chunk["coverage"]) != "COMPLETE" for chunk in chunks)
                if parse_partial:
                    warnings.append("parse_coverage_partial")
                extraction_key = self._insert_extraction(
                    connection,
                    input_kind="resource_version",
                    source_observation_key=None,
                    version_key=version_key,
                    parse_key=parse_key,
                    input_hash=self._parse_input_hash(str(version["sha256"]), parsed),
                    status="PARTIAL" if warnings else "COMPLETE",
                    warnings=tuple(dict.fromkeys(warnings)),
                )
                self._insert_drafts(
                    connection,
                    extraction_key,
                    int(version["course_key"]),
                    tuple(drafts),
                    document=True,
                )
                return self._result(connection, extraction_key, cache_hit=False)
        except sqlite3.IntegrityError:
            return self._load_raced("resource_version", version_key)
        except (sqlite3.Error, json.JSONDecodeError, TypeError):
            raise EventExtractionError("document event extraction failed") from None

    def _source_drafts(
        self,
        source_kind: CandidateSourceKind,
        snapshot: dict[str, JsonValue],
        evidence_key: int,
    ) -> tuple[_CandidateDraft, ...]:
        title = self._required_snapshot_text(snapshot, "title")
        body = snapshot.get("body") or snapshot.get("instructions") or title
        if not isinstance(body, str):
            raise ValueError("source text is invalid")
        if source_kind is CandidateSourceKind.ANNOUNCEMENT:
            drafts = list(self._text_drafts(f"{title}\n{body}", evidence_key, source_kind))
            for index, draft in enumerate(drafts):
                extra = self._supporting_times(
                    snapshot,
                    evidence_key,
                    ("published_at", "available_from", "available_until"),
                )
                drafts[index] = _CandidateDraft(
                    draft.source_kind,
                    draft.raw_wording,
                    draft.confidence,
                    draft.evidence_key,
                    (*draft.fields, *extra),
                )
            return tuple(drafts)
        event_type = self._structured_type(source_kind, snapshot)
        common = (
            _FieldDraft(CandidateFieldName.TITLE, title, title, source_path="title"),
            _FieldDraft(
                CandidateFieldName.EVENT_TYPE,
                event_type.value,
                str(snapshot.get("subtype") or source_kind.value),
                source_path="subtype" if source_kind is CandidateSourceKind.ASSESSMENT else "kind",
            ),
        )
        if source_kind is CandidateSourceKind.ASSESSMENT:
            supporting = self._supporting_times(
                snapshot,
                evidence_key,
                ("available_from", "available_until", "open_at", "close_at"),
            )
            due_values: list[tuple[str, dict[str, JsonValue]]] = []
            for name in ("due_at", "grading_due_at", "generic_due_at"):
                temporal = snapshot.get(name)
                if isinstance(temporal, dict):
                    due_values.append((name, temporal))
            if not due_values:
                return (
                    _CandidateDraft(
                        source_kind,
                        body[:4096],
                        0.9,
                        evidence_key,
                        (*common, *supporting),
                    ),
                )
            return tuple(
                _CandidateDraft(
                    source_kind,
                    body[:4096],
                    0.95,
                    evidence_key,
                    (
                        *common,
                        self._temporal_field(
                            CandidateFieldName.DUE_TIME,
                            due,
                            source_path={
                                "due_at": "assessment.dueDate",
                                "grading_due_at": "gradingColumn.dueDate",
                                "generic_due_at": "genericReadOnlyData.dueDate",
                            }[due_name],
                        ),
                        *supporting,
                    ),
                )
                for due_name, due in due_values
            )
        if source_kind is CandidateSourceKind.SCHEDULE:
            fields: list[_FieldDraft] = list(common)
            for key, field_name in (
                ("start_at", CandidateFieldName.START_TIME),
                ("end_at", CandidateFieldName.END_TIME),
            ):
                value = snapshot.get(key)
                if isinstance(value, dict):
                    fields.append(self._temporal_field(field_name, value, source_path=key))
            location = snapshot.get("location")
            if isinstance(location, str) and location:
                fields.append(
                    _FieldDraft(
                        CandidateFieldName.LOCATION,
                        location,
                        location,
                        source_path="location",
                    )
                )
            return (_CandidateDraft(source_kind, title, 0.98, evidence_key, tuple(fields)),)
        due = snapshot.get("due_at")
        if not isinstance(due, dict):
            # The historical due endpoint's startDate/endDate semantics are not established.
            return ()
        return (
            _CandidateDraft(
                source_kind,
                title,
                0.95,
                evidence_key,
                (
                    *common,
                    self._temporal_field(CandidateFieldName.DUE_TIME, due, source_path="due_at"),
                ),
            ),
        )

    def _text_drafts(
        self,
        text: str,
        evidence_key: int,
        source_kind: CandidateSourceKind = CandidateSourceKind.DOCUMENT,
    ) -> tuple[_CandidateDraft, ...]:
        drafts: list[_CandidateDraft] = []
        for mention in (part.strip() for part in _SPLIT.split(text)):
            if not mention:
                continue
            event_type, keyword = self._event_type(mention)
            temporals = list(_TEMPORAL_PATTERN.finditer(mention))
            change_kind = classify_change_language(mention)
            if change_kind is ChangeKind.NONE and _CHANGE_TERM_PATTERN.search(mention) is not None:
                # A negated, conditional, or modal mention is evidence about uncertainty,
                # not an affirmative source claim. Preserve it in the immutable observation
                # while abstaining from candidate generation.
                continue
            cancellation = (
                _CANCELLATION_TOKEN_PATTERN.search(mention)
                if change_kind is ChangeKind.CANCELLATION
                else None
            )
            venue_change = (
                _VENUE_CHANGE_PATTERN.search(mention)
                if change_kind is ChangeKind.VENUE_CHANGE
                else None
            )
            if event_type is None and cancellation is None and venue_change is None:
                continue
            if not temporals and cancellation is None and venue_change is None:
                continue
            if event_type is None:
                event_type = (
                    EventType.CANCELLATION if cancellation is not None else EventType.VENUE_CHANGE
                )
                keyword = "cancellation" if cancellation is not None else "venue change"
            boundary = temporals[0].start() if temporals else len(mention)
            title = self._candidate_title(mention, boundary)
            fields: list[_FieldDraft] = [
                _FieldDraft(CandidateFieldName.TITLE, title, title),
                _FieldDraft(CandidateFieldName.EVENT_TYPE, event_type.value, keyword),
            ]
            if cancellation is not None:
                fields.append(
                    _FieldDraft(
                        CandidateFieldName.STATUS,
                        "CANCELLED",
                        cancellation.group(0),
                        source_path="text.status",
                    )
                )
            if venue_change is not None:
                location = venue_change.group("location").strip(" :-–—\t")
                fields.append(
                    _FieldDraft(
                        CandidateFieldName.LOCATION,
                        location,
                        location,
                        source_path="text.location",
                    )
                )
            if not temporals:
                drafts.append(
                    _CandidateDraft(source_kind, mention[:4096], 0.8, evidence_key, tuple(fields))
                )
                continue
            due_marker = re.search(r"\b(?:due|deadline|submit(?:ted|sion)?)\b", mention, re.I)
            is_due = due_marker is not None
            if due_marker is not None:
                bound_temporals = [item for item in temporals if item.start() >= due_marker.end()]
                if not bound_temporals:
                    continue
                temporals = bound_temporals
            else:
                start_marker = _EXPLICIT_START_PATTERN.search(mention)
                if start_marker is not None:
                    bound_temporals = [
                        item for item in temporals if item.start() >= start_marker.end()
                    ]
                    if not bound_temporals:
                        continue
                    temporals = bound_temporals
                elif _NON_START_TEMPORAL_PATTERN.search(mention[: temporals[0].start()]):
                    continue
            first_name = CandidateFieldName.DUE_TIME if is_due else CandidateFieldName.START_TIME
            try:
                fields.append(self._temporal_from_text(first_name, temporals[0].group(0)))
            except ValueError:
                continue
            if len(temporals) > 1 and re.search(
                r"\b(?:to|until|ends?)\b",
                mention[temporals[0].end() : temporals[1].start()],
                re.I,
            ):
                try:
                    fields.append(
                        self._temporal_from_text(CandidateFieldName.END_TIME, temporals[1].group(0))
                    )
                except ValueError:
                    pass
            drafts.append(
                _CandidateDraft(source_kind, mention[:4096], 0.8, evidence_key, tuple(fields))
            )
        return tuple(drafts)

    @staticmethod
    def _event_type(text: str) -> tuple[EventType | None, str]:
        for pattern, event_type in _TYPE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return event_type, match.group(0)
        return None, ""

    @staticmethod
    def _candidate_title(text: str, temporal_start: int) -> str:
        prefix = re.sub(
            r"\b(?:is|will be|due|deadline|on|at|from)\s*$",
            "",
            text[:temporal_start],
            flags=re.I,
        )
        prefix = prefix.strip(" :-–—\t")
        prefix = re.sub(
            r"\s+(?:has\s+been\s+|is\s+|was\s+)?"
            r"(?:moved|postponed|rescheduled|cancelled|canceled)\s*(?:to)?\s*$",
            "",
            prefix,
            flags=re.I,
        )
        prefix = re.sub(
            r"\s+(?:venue|location|room)\s+(?:has\s+been\s+|is\s+)?"
            r"(?:changed|moved|updated)\s+(?:to\s+)?.*$",
            "",
            prefix,
            flags=re.I,
        )
        return (prefix or text).strip()[:500]

    @staticmethod
    def _structured_type(
        source_kind: CandidateSourceKind, snapshot: dict[str, JsonValue]
    ) -> EventType:
        if source_kind is CandidateSourceKind.ASSESSMENT:
            subtype = snapshot.get("subtype")
            return {
                "assignment": EventType.ASSIGNMENT_DUE,
                "quiz": EventType.QUIZ,
                "test": EventType.TEST,
                "exam": EventType.EXAM,
                "presentation": EventType.PRESENTATION,
            }.get(str(subtype), EventType.GENERIC_COURSE_EVENT)
        return EventType.GENERIC_COURSE_EVENT

    @classmethod
    def _supporting_times(
        cls,
        snapshot: dict[str, JsonValue],
        evidence_key: int,
        names: tuple[str, ...],
    ) -> tuple[_FieldDraft, ...]:
        del evidence_key
        mapping = {
            "published_at": CandidateFieldName.PUBLISHED_AT,
            "available_from": CandidateFieldName.AVAILABLE_FROM,
            "available_until": CandidateFieldName.AVAILABLE_UNTIL,
            "open_at": CandidateFieldName.OPEN_AT,
            "close_at": CandidateFieldName.CLOSE_AT,
        }
        return tuple(
            cls._temporal_field(mapping[name], value, source_path=name)
            for name in names
            if isinstance((value := snapshot.get(name)), dict)
        )

    @staticmethod
    def _temporal_field(
        name: CandidateFieldName,
        temporal: dict[str, JsonValue],
        *,
        source_path: str = "text",
    ) -> _FieldDraft:
        precision = TemporalPrecision(str(temporal["precision"]))
        source_text = str(temporal["source_text"])
        timezone_value = temporal.get("source_timezone")
        source_timezone = None if timezone_value is None else str(timezone_value)
        return _FieldDraft(name, temporal, source_text, precision, source_timezone, source_path)

    @staticmethod
    def _temporal_from_text(name: CandidateFieldName, source_text: str) -> _FieldDraft:
        normalized = source_text.strip()
        if re.fullmatch(r"Week\s+\d{1,2}", normalized, re.I):
            week_number = int(normalized.rsplit(maxsplit=1)[-1])
            if not 1 <= week_number <= 53:
                raise ValueError("week number is invalid")
            value: dict[str, JsonValue] = {
                "instant": None,
                "precision": TemporalPrecision.WEEK_ONLY.value,
                "source_text": source_text,
                "source_timezone": None,
                "week_number": week_number,
            }
            return _FieldDraft(name, value, source_text, TemporalPrecision.WEEK_ONLY)
        exact = bool(re.search(r"\d{1,2}:\d{2}", normalized))
        if not exact:
            normalized_date = DeterministicEventExtractor._parse_date_only(normalized)
            value = {
                "instant": None,
                "precision": TemporalPrecision.DATE_ONLY.value,
                "source_text": source_text,
                "source_timezone": None,
                "date": normalized_date,
            }
            return _FieldDraft(name, value, source_text, TemporalPrecision.DATE_ONLY)
        parsed, source_timezone = DeterministicEventExtractor._parse_exact(normalized)
        value = {
            "instant": to_storage_time(parsed),
            "precision": TemporalPrecision.EXACT_TIME.value,
            "source_text": source_text,
            "source_timezone": source_timezone,
        }
        return _FieldDraft(name, value, source_text, TemporalPrecision.EXACT_TIME, source_timezone)

    @staticmethod
    def _parse_date_only(value: str) -> str:
        cleaned = value.replace(",", "").strip()
        for pattern in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(cleaned, pattern).date().isoformat()
            except ValueError:
                continue
        raise ValueError("date-only source time is invalid")

    @staticmethod
    def _parse_exact(value: str) -> tuple[datetime, str]:
        if value.upper().endswith("SGT"):
            source_timezone = "SGT"
            cleaned = re.sub(r"\s*SGT$", "", value, flags=re.I)
            tz = timezone(timedelta(hours=8))
        elif value.upper().endswith("UTC"):
            source_timezone = "UTC"
            cleaned = re.sub(r"\s*UTC$", "", value, flags=re.I)
            tz = UTC
        else:
            offset = re.search(r"([+-]\d{2}:\d{2}|Z)$", value)
            if offset is None:
                raise ValueError("exact source time lacks a timezone")
            source_timezone = offset.group(1)
            cleaned = value[: offset.start()].rstrip()
            if source_timezone == "Z":
                tz = UTC
            else:
                sign = 1 if source_timezone.startswith("+") else -1
                hours, minutes = source_timezone[1:].split(":")
                tz = timezone(sign * timedelta(hours=int(hours), minutes=int(minutes)))
        if re.match(r"\d{4}-\d{2}-\d{2}", cleaned):
            parsed = datetime.fromisoformat(cleaned)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed.astimezone(UTC), source_timezone
        cleaned = re.sub(r"\s+at\s+", " ", cleaned, flags=re.I).replace(",", "")
        for pattern in ("%d %B %Y %H:%M", "%d %b %Y %H:%M", "%B %d %Y %H:%M", "%b %d %Y %H:%M"):
            try:
                parsed = datetime.strptime(cleaned, pattern)
            except ValueError:
                continue
            return parsed.replace(tzinfo=tz).astimezone(UTC), source_timezone
        raise ValueError("exact source time is unsupported")

    @staticmethod
    def _required_snapshot_text(snapshot: dict[str, JsonValue], name: str) -> str:
        value = snapshot.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("source observation is missing required text")
        return value

    @staticmethod
    def _parse_input_hash(resource_sha256: str, parsed: sqlite3.Row) -> str:
        payload = "\0".join(
            (
                resource_sha256,
                str(parsed["parser_name"]),
                str(parsed["parser_version"]),
                str(parsed["engine_version"]),
                str(parsed["settings_hash"]),
                str(parsed["parse_key"]),
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _insert_extraction(
        self,
        connection: sqlite3.Connection,
        *,
        input_kind: str,
        source_observation_key: int | None,
        version_key: int | None,
        parse_key: int | None,
        input_hash: str,
        status: str,
        warnings: tuple[str, ...],
    ) -> int:
        cursor = connection.execute(
            """
            INSERT INTO extraction_record(
                input_kind, source_observation_key, version_key, parse_key, extractor_name,
                extractor_version, settings_hash, input_hash, status,
                warning_codes_json, extracted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                input_kind,
                source_observation_key,
                version_key,
                parse_key,
                self.name,
                self.version,
                _SETTINGS_HASH,
                input_hash,
                status,
                json.dumps(warnings, separators=(",", ":")),
                to_storage_time(utc_now()),
            ),
        )
        assert cursor.lastrowid is not None
        return int(cursor.lastrowid)

    @staticmethod
    def _insert_drafts(
        connection: sqlite3.Connection,
        extraction_key: int,
        course_key: int,
        drafts: tuple[_CandidateDraft, ...],
        *,
        document: bool,
    ) -> None:
        for ordinal, draft in enumerate(drafts):
            cursor = connection.execute(
                """
                INSERT INTO event_candidate(
                    extraction_record_key, course_key, ordinal, source_kind,
                    raw_wording, confidence
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    extraction_key,
                    course_key,
                    ordinal,
                    draft.source_kind.value,
                    draft.raw_wording,
                    draft.confidence,
                ),
            )
            assert cursor.lastrowid is not None
            candidate_key = int(cursor.lastrowid)
            for field in draft.fields:
                connection.execute(
                    """
                    INSERT INTO event_candidate_field(
                        candidate_key, field_name, value_json, original_text, source_path,
                        temporal_precision, source_timezone,
                        source_observation_key, locator_key
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_key,
                        field.name.value,
                        json.dumps(
                            field.value,
                            sort_keys=True,
                            separators=(",", ":"),
                            ensure_ascii=False,
                            allow_nan=False,
                        ),
                        field.original_text[:4096],
                        field.source_path,
                        None if field.precision is None else field.precision.value,
                        field.source_timezone,
                        None if document else draft.evidence_key,
                        draft.evidence_key if document else None,
                    ),
                )

    def _cached(
        self,
        connection: sqlite3.Connection,
        input_kind: str,
        observation_key: int | None,
        version_key: int | None,
        parse_key: int | None,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT extraction_record_key FROM extraction_record
            WHERE input_kind = ? AND source_observation_key IS ? AND version_key IS ?
              AND parse_key IS ?
              AND extractor_name = ? AND extractor_version = ? AND settings_hash = ?
            """,
            (
                input_kind,
                observation_key,
                version_key,
                parse_key,
                self.name,
                self.version,
                _SETTINGS_HASH,
            ),
        ).fetchone()
        return None if row is None else int(row[0])

    def _load_raced(self, input_kind: str, key: int) -> ExtractionResult:
        connection = self.database.connect()
        try:
            key_column = (
                "source_observation_key" if input_kind == "source_observation" else "version_key"
            )
            row = connection.execute(
                f"""SELECT extraction_record_key FROM extraction_record
                WHERE input_kind = ? AND {key_column} = ?
                  AND extractor_name = ? AND extractor_version = ? AND settings_hash = ?
                ORDER BY extraction_record_key DESC LIMIT 1""",
                (input_kind, key, self.name, self.version, _SETTINGS_HASH),
            ).fetchone()
            if row is None:
                raise EventExtractionError("event extraction cache lookup failed")
            return self._result(connection, int(row[0]), cache_hit=True)
        finally:
            connection.close()

    def _result(
        self, connection: sqlite3.Connection, extraction_key: int, *, cache_hit: bool
    ) -> ExtractionResult:
        extraction = connection.execute(
            "SELECT * FROM extraction_record WHERE extraction_record_key = ?",
            (extraction_key,),
        ).fetchone()
        assert extraction is not None
        rows = connection.execute(
            """
            SELECT candidate.*, provider.name AS provider, course_object.remote_key AS course_remote
            FROM event_candidate candidate
            JOIN course ON course.course_key = candidate.course_key
            JOIN source_object course_object
              ON course_object.source_object_key = course.source_object_key
            JOIN source_provider provider ON provider.provider_key = course_object.provider_key
            WHERE candidate.extraction_record_key = ? ORDER BY candidate.ordinal
            """,
            (extraction_key,),
        ).fetchall()
        candidates: list[EventCandidate] = []
        for row in rows:
            field_rows = connection.execute(
                """SELECT * FROM event_candidate_field
                WHERE candidate_key = ? ORDER BY candidate_field_key""",
                (int(row["candidate_key"]),),
            ).fetchall()
            fields = tuple(
                CandidateField(
                    key=int(field["candidate_field_key"]),
                    name=CandidateFieldName(str(field["field_name"])),
                    value=json.loads(str(field["value_json"])),
                    original_text=str(field["original_text"]),
                    source_path=str(field["source_path"]),
                    precision=None
                    if field["temporal_precision"] is None
                    else TemporalPrecision(str(field["temporal_precision"])),
                    source_timezone=None
                    if field["source_timezone"] is None
                    else str(field["source_timezone"]),
                    source_observation_key=None
                    if field["source_observation_key"] is None
                    else int(field["source_observation_key"]),
                    locator_key=None if field["locator_key"] is None else int(field["locator_key"]),
                )
                for field in field_rows
            )
            candidates.append(
                EventCandidate(
                    key=int(row["candidate_key"]),
                    extraction_record_key=extraction_key,
                    course=CourseId(str(row["provider"]), str(row["course_remote"])),
                    ordinal=int(row["ordinal"]),
                    source_kind=CandidateSourceKind(str(row["source_kind"])),
                    raw_wording=str(row["raw_wording"]),
                    confidence=float(row["confidence"]),
                    fields=fields,
                )
            )
        return ExtractionResult(
            extraction_record_key=extraction_key,
            extractor_name=str(extraction["extractor_name"]),
            extractor_version=str(extraction["extractor_version"]),
            input_hash=str(extraction["input_hash"]),
            candidates=tuple(candidates),
            cache_hit=cache_hit,
        )
