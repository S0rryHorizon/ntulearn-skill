"""Deterministic, LLM-independent extraction of unresolved event candidates."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta, timezone

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


_SETTINGS = {
    "context_maximum_characters": 1200,
    "context_maximum_segments": 6,
    "maximum_candidates": 1000,
    "maximum_mentions_per_chunk": 100,
    "rule_revision": 5,
}
_SETTINGS_JSON = json.dumps(_SETTINGS, sort_keys=True, separators=(",", ":"))
_SETTINGS_HASH = hashlib.sha256(_SETTINGS_JSON.encode("utf-8")).hexdigest()
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_WEEKDAY = (
    r"(?:Mon(?:day)?|Tue(?:sday)?|Wed(?:nesday)?|Thu(?:rsday)?|"
    r"Fri(?:day)?|Sat(?:urday)?|Sun(?:day)?)"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_HUMAN_DATE = rf"(?:(?:{_WEEKDAY})\s*,?\s*)?(?:{_DAY}\s+{_MONTH}|{_MONTH}\s+{_DAY}),?\s+\d{{4}}"
_SLASH_DATE = r"(?:0?[1-9]|[12]\d|3[01])/(?:0?[1-9]|[12]\d|3[01])/\d{2,4}"
_DATE_TOKEN = rf"(?:\d{{4}}-\d{{2}}-\d{{2}}|{_HUMAN_DATE}|{_SLASH_DATE})"
_ZONE = r"(?:[+-]\d{2}:\d{2}|SGT|UTC(?:[+-]\d{1,2})?|Z)"
_ZONE_WRAPPED = rf"\(?{_ZONE}\)?"
_CLOCK = r"(?:[01]?\d|2[0-3])[:.]\d{2}(?:\s*(?:a\.?m\.?|p\.?m\.?))?"
_TEMPORAL_PATTERN = re.compile(
    rf"\b(?:"
    rf"\d{{4}}-\d{{2}}-\d{{2}}[T ]\d{{1,2}}:\d{{2}}(?::\d{{2}})?"
    rf"(?:Z|\s?(?:[+-]\d{{2}}:\d{{2}}|SGT|UTC))"
    rf"|{_DATE_TOKEN},?\s+(?:at\s+)?{_CLOCK}(?:\s?{_ZONE_WRAPPED})"
    rf"|{_DATE_TOKEN}"
    rf"|Week\s+\d{{1,2}}"
    rf")(?!\w)",
    flags=re.IGNORECASE,
)
_DATE_PATTERN = re.compile(rf"\b{_DATE_TOKEN}\b", re.I)
_SLASH_DATE_PATTERN = re.compile(rf"\b{_SLASH_DATE}\b")
_TIME_RANGE_PATTERN = re.compile(
    rf"\b(?P<start>{_CLOCK})\s*(?:to|until|[-–—])\s*(?P<end>{_CLOCK})"
    rf"(?:\s*(?P<zone>{_ZONE_WRAPPED}))?(?!\w)",
    re.I,
)
_TIME_PATTERN = re.compile(rf"\b(?P<clock>{_CLOCK})(?:\s*(?P<zone>{_ZONE_WRAPPED}))?(?!\w)", re.I)
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
_NEGATED_EVENT_PATTERN = re.compile(
    r"\b(?:not|never)\s+(?:be\s+)?(?:held|take\s+place|due|scheduled|submitted)\b|"
    r"\bno\s+(?:assignment|homework|quiz|test|exam|presentation|tutorial|lab|lecture)\b",
    re.I,
)
_CHANGE_TERM = r"(?:moved|postponed|rescheduled|cancelled|canceled|changed|corrected|updated)"
_CHANGE_TERM_PATTERN = re.compile(rf"\b{_CHANGE_TERM}\b", re.I)
_CONDITIONAL_CHANGE_PATTERN = re.compile(r"\b(?:if|unless|whether)\b|\bin\s+case\b", re.I)
_ORDINAL = r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth)"
_LOCATION_PATTERN = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:venue|location|room)\s*[:–—-]\s*(?P<location>\S.{0,199})$",
    re.I,
)
_FIELD_CONTINUATION_PATTERN = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:date|time|venue|location|room)\s*(?:[:–—-]|$)",
    re.I,
)
_FIELD_LABEL_ONLY_PATTERN = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:date|time|venue|location|room)\s*[:–—-]?\s*$",
    re.I,
)
_ORDINAL_ONLY_PATTERN = re.compile(
    rf"^\s*(?:[-*•]\s*)?(?:{_ORDINAL}|\d{{1,2}}(?:st|nd|rd|th)|\d{{1,2}}[.)])\s*$",
    re.I,
)
_ROW_BOUNDARY_PATTERN = re.compile(r"^\s*(?:[-*•]\s*)?\d{1,2}(?:st|nd|rd|th|[.)])?\s*$", re.I)
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


@dataclass(frozen=True, slots=True)
class _TextSegment:
    text: str
    source_path: str


class DeterministicEventExtractor:
    """Extract reproducible candidates from structured observations or immutable chunks."""

    name = "deterministic-event-rules"
    version = "5"

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
                cached = self._cached(
                    connection,
                    "source_observation",
                    observation_key,
                    None,
                    None,
                    str(source["observation_hash"]),
                )
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

    def extract_resource_version(
        self, version_key: int, *, parse_key: int | None = None
    ) -> ExtractionResult:
        if version_key <= 0:
            raise ValueError("version key must be positive")
        if parse_key is not None and parse_key <= 0:
            raise ValueError("parse key must be positive")
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
                if parse_key is None:
                    parsed = connection.execute(
                        """
                        SELECT * FROM parsed_document parsed
                        WHERE parsed.version_key = ?
                          AND parsed.status IN ('COMPLETE', 'PARTIAL', 'UNSUPPORTED')
                        ORDER BY parsed.parsed_at DESC, parsed.parse_key DESC LIMIT 1
                        """,
                        (version_key,),
                    ).fetchone()
                else:
                    parsed = connection.execute(
                        """
                        SELECT * FROM parsed_document parsed
                        WHERE parsed.parse_key = ? AND parsed.version_key = ?
                          AND parsed.status IN ('COMPLETE', 'PARTIAL', 'UNSUPPORTED')
                        """,
                        (parse_key, version_key),
                    ).fetchone()
                if parsed is None:
                    raise ValueError("resource version has no reusable parse")
                selected_parse_key = int(parsed["parse_key"])
                chunks = connection.execute(
                    """
                    SELECT chunk.chunk_key, chunk.native_text, chunk.diagnostic_json,
                           locator.locator_key, parsed.coverage
                    FROM document_chunk chunk
                    JOIN source_locator locator ON locator.chunk_key = chunk.chunk_key
                    JOIN parsed_document parsed ON parsed.parse_key = chunk.parse_key
                    WHERE chunk.parse_key = ? ORDER BY chunk.ordinal
                    """,
                    (selected_parse_key,),
                ).fetchall()
                representations = connection.execute(
                    """
                    SELECT representation.representation_key, representation.chunk_key,
                           representation.text, representation.settings_hash,
                           representation.confidence, metadata.review_status,
                           metadata.rendered_sha256
                    FROM chunk_representation representation
                    JOIN visual_evidence_metadata metadata USING(representation_key)
                    JOIN document_chunk chunk ON chunk.chunk_key = representation.chunk_key
                    WHERE chunk.parse_key = ?
                      AND representation.representation_kind = 'vision_description'
                      AND representation.text IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1
                          FROM chunk_representation newer
                          JOIN visual_evidence_metadata newer_metadata
                            USING(representation_key)
                          WHERE newer.chunk_key = representation.chunk_key
                            AND newer.representation_kind = 'vision_description'
                            AND newer.method = representation.method
                            AND newer_metadata.version_key = metadata.version_key
                            AND newer_metadata.source_page_index
                              = metadata.source_page_index
                            AND newer.representation_key
                              > representation.representation_key
                      )
                    ORDER BY representation.representation_key
                    """,
                    (selected_parse_key,),
                ).fetchall()
                input_hash = self._parse_input_hash(str(version["sha256"]), parsed, representations)
                cached = self._cached(
                    connection,
                    "resource_version",
                    None,
                    version_key,
                    selected_parse_key,
                    input_hash,
                )
                if cached is not None:
                    return self._result(connection, cached, cache_hit=True)
                visual_by_chunk: dict[int, list[sqlite3.Row]] = {}
                for representation in representations:
                    visual_by_chunk.setdefault(int(representation["chunk_key"]), []).append(
                        representation
                    )
                drafts: list[_CandidateDraft] = []
                warnings: list[str] = (
                    ["parse_unsupported"] if str(parsed["status"]) == "UNSUPPORTED" else []
                )
                for chunk in chunks:
                    chunk_drafts = self._text_drafts(
                        str(chunk["native_text"]), int(chunk["locator_key"])
                    )
                    if len(chunk_drafts) > _SETTINGS["maximum_mentions_per_chunk"]:
                        chunk_drafts = chunk_drafts[: _SETTINGS["maximum_mentions_per_chunk"]]
                        warnings.append("chunk_candidate_limit_reached")
                    drafts.extend(chunk_drafts)
                    visual_rows = visual_by_chunk.get(int(chunk["chunk_key"]), [])
                    for visual in visual_rows:
                        visual_drafts = self._text_drafts(
                            str(visual["text"]),
                            int(chunk["locator_key"]),
                            source_path_prefix=f"representation.{int(visual['representation_key'])}",
                        )
                        stored_confidence = visual["confidence"]
                        confidence = 0.5 if stored_confidence is None else float(stored_confidence)
                        drafts.extend(
                            replace(draft, confidence=min(draft.confidence, confidence))
                            for draft in visual_drafts[: _SETTINGS["maximum_mentions_per_chunk"]]
                        )
                        if str(visual["review_status"]) == "PARTIAL":
                            warnings.append("visual_evidence_partial")
                        else:
                            warnings.append("visual_evidence_needs_review")
                    diagnostic_json = chunk["diagnostic_json"]
                    if diagnostic_json is not None:
                        diagnostic = json.loads(str(diagnostic_json))
                        if int(diagnostic.get("image_count", 0)) > 0 and not visual_rows:
                            warnings.append("embedded_visual_content_not_extracted")
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
                    parse_key=selected_parse_key,
                    input_hash=input_hash,
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
        *,
        source_path_prefix: str | None = None,
    ) -> tuple[_CandidateDraft, ...]:
        segments = self._text_segments(text, source_path_prefix=source_path_prefix)
        numeric_date_order = self._numeric_date_order(text)
        drafts: list[_CandidateDraft] = []
        for index, anchor in enumerate(segments):
            event_type, keyword = self._event_type(anchor.text)
            if event_type is None:
                continue
            block = [anchor]
            for following in segments[index + 1 : index + _SETTINGS["context_maximum_segments"]]:
                if _ROW_BOUNDARY_PATTERN.fullmatch(following.text) is not None:
                    break
                following_type, _ = self._event_type(following.text)
                if following_type is not None:
                    break
                has_temporal_assertion = _TEMPORAL_PATTERN.search(
                    "\n".join(segment.text for segment in block)
                )
                if (
                    has_temporal_assertion is not None
                    and _FIELD_CONTINUATION_PATTERN.match(following.text) is None
                    and _FIELD_LABEL_ONLY_PATTERN.fullmatch(block[-1].text) is None
                ):
                    break
                if (
                    sum(len(segment.text) for segment in block) + len(following.text)
                    > _SETTINGS["context_maximum_characters"]
                ):
                    break
                block.append(following)
            ordinal_prefix = (
                segments[index - 1]
                if index > 0 and _ORDINAL_ONLY_PATTERN.fullmatch(segments[index - 1].text)
                else None
            )
            draft = self._draft_from_block(
                tuple(block),
                event_type,
                keyword,
                evidence_key,
                source_kind,
                ordinal_prefix=ordinal_prefix,
                numeric_date_order=numeric_date_order,
            )
            if draft is not None:
                drafts.append(draft)
        return tuple(drafts)

    @staticmethod
    def _text_segments(
        text: str, *, source_path_prefix: str | None = None
    ) -> tuple[_TextSegment, ...]:
        parts = [part.strip() for part in _SPLIT.split(text) if part.strip()]
        prefix = "text" if source_path_prefix is None else f"{source_path_prefix}.text"
        if len(parts) == 1:
            return (_TextSegment(parts[0], prefix),)
        return tuple(
            _TextSegment(part, f"{prefix}.segment.{index}") for index, part in enumerate(parts)
        )

    def _draft_from_block(
        self,
        block: tuple[_TextSegment, ...],
        event_type: EventType,
        keyword: str,
        evidence_key: int,
        source_kind: CandidateSourceKind,
        *,
        ordinal_prefix: _TextSegment | None,
        numeric_date_order: str | None,
    ) -> _CandidateDraft | None:
        if len(block) == 1 and ordinal_prefix is None:
            path = (
                block[0].source_path
                if block[0].source_path.startswith("representation.")
                else "text"
            )
            block = (_TextSegment(block[0].text, path),)
        mention = "\n".join(segment.text for segment in block)
        if _NEGATED_EVENT_PATTERN.search(block[0].text) is not None:
            return None
        temporals = list(_TEMPORAL_PATTERN.finditer(mention))
        change_kind = classify_change_language(block[0].text)
        if (
            change_kind is ChangeKind.NONE
            and _CHANGE_TERM_PATTERN.search(block[0].text) is not None
        ):
            return None
        cancellation = (
            _CANCELLATION_TOKEN_PATTERN.search(block[0].text)
            if change_kind is ChangeKind.CANCELLATION
            else None
        )
        venue_change = (
            _VENUE_CHANGE_PATTERN.search(block[0].text)
            if change_kind is ChangeKind.VENUE_CHANGE
            else None
        )
        if not temporals and cancellation is None and venue_change is None:
            return None

        anchor = block[0]
        anchor_temporal = _TEMPORAL_PATTERN.search(anchor.text)
        boundary = anchor_temporal.start() if anchor_temporal is not None else len(anchor.text)
        title = self._candidate_title(anchor.text, boundary)
        title_text = anchor.text[:boundary].strip(" :-–—\t") or title
        title_path = anchor.source_path
        title_offset = title_text.casefold().find(title.casefold())
        if title_offset > 0:
            title_text = title_text[title_offset : title_offset + len(title)]
        if re.fullmatch(r"(?:assignment\s+)?submission", title, re.I) and len(block) > 1:
            selection = re.search(
                r"\b(?:choose|select)\b(?P<wording>[^.;\n]{0,300}?)"
                r"\b(?P<object>topic|subject|project|option|group)\b",
                block[1].text,
                re.I,
            )
            if selection is not None:
                title = f"{selection.group('object').lower()} selection"
                title_text = selection.group(0).strip()
                title_path = block[1].source_path
        if ordinal_prefix is not None:
            ordinal = ordinal_prefix.text.strip(" -*•\t")
            title = f"{ordinal} {title}"
            title_text = f"{ordinal_prefix.text}\n{title_text}"
            title_path = f"{ordinal_prefix.source_path}+{anchor.source_path}"
        fields: list[_FieldDraft] = [
            _FieldDraft(
                CandidateFieldName.TITLE,
                title,
                title_text,
                source_path=title_path,
            ),
            _FieldDraft(
                CandidateFieldName.EVENT_TYPE,
                event_type.value,
                keyword,
                source_path=anchor.source_path,
            ),
        ]
        if cancellation is not None:
            fields.append(
                _FieldDraft(
                    CandidateFieldName.STATUS,
                    "CANCELLED",
                    cancellation.group(0),
                    source_path=self._path_for_offset(block, cancellation.start()),
                )
            )
        if venue_change is not None:
            location = venue_change.group("location").strip(" :-–—\t")
            fields.append(
                _FieldDraft(
                    CandidateFieldName.LOCATION,
                    location,
                    location,
                    source_path=self._path_for_offset(block, venue_change.start("location")),
                )
            )
        else:
            location_field = self._location_from_block(block)
            if location_field is not None:
                fields.append(location_field)
        if not temporals:
            return _CandidateDraft(source_kind, mention[:4096], 0.8, evidence_key, tuple(fields))

        due_marker = self._due_marker(block, mention, event_type)
        is_due = due_marker is not None
        if due_marker is not None:
            temporals = [item for item in temporals if item.start() >= due_marker.end()]
            if not temporals:
                return None
            if _NON_START_TEMPORAL_PATTERN.search(mention, due_marker.end(), temporals[0].start()):
                return None
        else:
            start_marker = _EXPLICIT_START_PATTERN.search(block[0].text)
            if start_marker is not None:
                temporals = [item for item in temporals if item.start() >= start_marker.end()]
                if not temporals:
                    return None
            elif _NON_START_TEMPORAL_PATTERN.search(
                self._segment_prefix_for_offset(block, temporals[0].start())
            ):
                return None
        if self._selected_temporal_is_negated(block, temporals[0].start()):
            return None

        first_name = CandidateFieldName.DUE_TIME if is_due else CandidateFieldName.START_TIME
        temporal_fields = self._temporal_fields_from_block(
            block,
            mention,
            temporals,
            first_name,
            numeric_date_order=numeric_date_order,
        )
        if not temporal_fields:
            return None
        fields.extend(temporal_fields)
        return _CandidateDraft(source_kind, mention[:4096], 0.8, evidence_key, tuple(fields))

    @staticmethod
    def _due_marker(
        block: tuple[_TextSegment, ...], mention: str, event_type: EventType
    ) -> re.Match[str] | None:
        anchor_pattern = r"\b(?:due|deadline)\b"
        if event_type in {EventType.ASSIGNMENT_DUE, EventType.SUBMISSION}:
            anchor_pattern += r"|\bsubmit(?:ted)?\s+(?:by|before)\b|\bby\b"
        marker = re.search(anchor_pattern, block[0].text, re.I)
        if marker is not None:
            return marker
        if event_type not in {EventType.ASSIGNMENT_DUE, EventType.SUBMISSION}:
            return None
        return re.search(
            rf"(?im)(?:^\s*(?:[-*•]\s*)?"
            rf"(?:due(?:\s+date)?|deadline|submission\s+deadline)\s*:?(?=\s*(?:{_DATE_TOKEN})|\s*$)"
            rf"|\bby(?=\s+(?:{_DATE_TOKEN})))",
            mention,
        )

    def _temporal_fields_from_block(
        self,
        block: tuple[_TextSegment, ...],
        mention: str,
        temporals: list[re.Match[str]],
        first_name: CandidateFieldName,
        *,
        numeric_date_order: str | None,
    ) -> tuple[_FieldDraft, ...]:
        first = temporals[0]
        if len(temporals) > 1 and re.fullmatch(r"Week\s+\d{1,2}", first.group(0), re.I):
            refinement = temporals[1]
            same_segment = self._path_for_span(
                block, first.start(), first.end()
            ) == self._path_for_span(block, refinement.start(), refinement.end())
            if same_segment and re.search(
                r"\(\s*(?:by|due(?:\s+on)?)\s*$",
                mention[first.end() : refinement.start()],
                re.I,
            ):
                temporals = temporals[1:]
                first = refinement
        date_match = _DATE_PATTERN.search(first.group(0))
        range_match = _TIME_RANGE_PATTERN.search(mention, first.end())
        if range_match is not None and (
            _DATE_PATTERN.search(mention, first.end(), range_match.start()) is not None
            or not self._time_range_is_bounded(range_match)
        ):
            range_match = None
        if date_match is not None and range_match is not None:
            date_text = date_match.group(0)
            source_path = self._combined_source_path(
                self._path_for_span(block, first.start(), first.end()),
                self._path_for_span(block, range_match.start(), range_match.end()),
            )
            original = range_match.group(0)
            try:
                return (
                    self._local_temporal_from_text(
                        first_name,
                        date_text,
                        range_match.group("start"),
                        original,
                        source_path,
                        zone=range_match.group("zone"),
                        shared_meridiem=self._meridiem(range_match.group("end")),
                        numeric_date_order=numeric_date_order,
                    ),
                    self._local_temporal_from_text(
                        CandidateFieldName.END_TIME,
                        date_text,
                        range_match.group("end"),
                        original,
                        source_path,
                        zone=range_match.group("zone"),
                        numeric_date_order=numeric_date_order,
                    ),
                )
            except ValueError:
                return ()

        first_text = first.group(0)
        first_path = self._path_for_span(block, first.start(), first.end())
        first_is_date_only = (
            date_match is not None
            and re.search(rf"{_CLOCK}(?:\s*{_ZONE_WRAPPED})?\s*$", first_text, re.I) is None
        )
        if first_is_date_only:
            assert date_match is not None
            clock_match = _TIME_PATTERN.search(mention, first.end())
            if clock_match is not None and _DATE_PATTERN.search(
                mention, first.end(), clock_match.start()
            ):
                clock_match = None
            if clock_match is not None and self._clock_is_bounded(clock_match.group("clock")):
                original = clock_match.group(0)
                source_path = self._combined_source_path(
                    first_path,
                    self._path_for_span(block, clock_match.start(), clock_match.end()),
                )
                try:
                    return (
                        self._local_temporal_from_text(
                            first_name,
                            date_match.group(0),
                            clock_match.group("clock"),
                            original,
                            source_path,
                            zone=clock_match.group("zone"),
                            numeric_date_order=numeric_date_order,
                        ),
                    )
                except ValueError:
                    return ()
        try:
            fields = [
                self._temporal_from_text(
                    first_name,
                    first_text,
                    source_path=first_path,
                    numeric_date_order=numeric_date_order,
                )
            ]
        except ValueError:
            return ()
        if len(temporals) > 1 and re.search(
            r"\b(?:to|until|ends?)\b",
            mention[first.end() : temporals[1].start()],
            re.I,
        ):
            second = temporals[1]
            try:
                fields.append(
                    self._temporal_from_text(
                        CandidateFieldName.END_TIME,
                        second.group(0),
                        source_path=self._path_for_span(block, second.start(), second.end()),
                        numeric_date_order=numeric_date_order,
                    )
                )
            except ValueError:
                pass
        return tuple(fields)

    @classmethod
    def _time_range_is_bounded(cls, match: re.Match[str]) -> bool:
        start_text = match.group("start")
        end_text = match.group("end")
        shared_meridiem = cls._meridiem(end_text)
        if not cls._clock_is_bounded(end_text):
            return False
        if not cls._clock_is_bounded(start_text) and shared_meridiem is None:
            return False
        try:
            start = cls._parse_clock(start_text, shared_meridiem=shared_meridiem)
            end = cls._parse_clock(end_text)
        except ValueError:
            return False
        return start < end

    @staticmethod
    def _location_from_block(block: tuple[_TextSegment, ...]) -> _FieldDraft | None:
        for index, segment in enumerate(block[1:], start=1):
            match = _LOCATION_PATTERN.fullmatch(segment.text)
            if match is not None:
                location = match.group("location").strip()
                original_text = segment.text
                source_path = segment.source_path
            elif re.fullmatch(
                r"\s*(?:[-*•]\s*)?(?:venue|location|room)\s*[:–—-]?\s*",
                segment.text,
                re.I,
            ) and index + 1 < len(block):
                value_segment = block[index + 1]
                location = value_segment.text.strip()
                original_text = f"{segment.text}\n{value_segment.text}"
                source_path = DeterministicEventExtractor._combined_source_path(
                    segment.source_path,
                    value_segment.source_path,
                )
            else:
                continue
            if re.search(r"\b(?:office|staff|instructor|lecturer|teacher)\b", location, re.I):
                continue
            if (
                len(location) > 200
                or _TEMPORAL_PATTERN.search(location) is not None
                or DeterministicEventExtractor._event_type(location)[0] is not None
            ):
                continue
            return _FieldDraft(
                CandidateFieldName.LOCATION,
                location,
                original_text,
                source_path=source_path,
            )
        return None

    @staticmethod
    def _path_for_offset(block: tuple[_TextSegment, ...], offset: int) -> str:
        consumed = 0
        for segment in block:
            end = consumed + len(segment.text)
            if offset <= end:
                return segment.source_path
            consumed = end + 1
        return block[-1].source_path

    @classmethod
    def _path_for_span(cls, block: tuple[_TextSegment, ...], start: int, end: int) -> str:
        return cls._combined_source_path(
            cls._path_for_offset(block, start),
            cls._path_for_offset(block, max(start, end - 1)),
        )

    @staticmethod
    def _segment_prefix_for_offset(block: tuple[_TextSegment, ...], offset: int) -> str:
        consumed = 0
        for segment in block:
            end = consumed + len(segment.text)
            if offset <= end:
                return segment.text[: max(0, offset - consumed)]
            consumed = end + 1
        return block[-1].text

    @staticmethod
    def _selected_temporal_is_negated(block: tuple[_TextSegment, ...], offset: int) -> bool:
        consumed = 0
        for index, segment in enumerate(block):
            end = consumed + len(segment.text)
            if offset <= end:
                if _NEGATED_EVENT_PATTERN.search(segment.text) is not None:
                    return True
                if index == 0:
                    return False
                previous = block[index - 1].text
                if _FIELD_LABEL_ONLY_PATTERN.fullmatch(previous) is not None:
                    return _NEGATED_EVENT_PATTERN.search(previous) is not None
                return (
                    re.search(
                        r"\bnot\s+(?:be\s+)?(?:held|scheduled|due|submitted|take\s+place)"
                        r"(?:\s+(?:on|for|until|by))?\s*$",
                        previous,
                        re.I,
                    )
                    is not None
                )
            consumed = end + 1
        return False

    @staticmethod
    def _combined_source_path(first: str, second: str) -> str:
        return first if first == second else f"{first}+{second}"

    @staticmethod
    def _meridiem(value: str) -> str | None:
        match = re.search(r"([ap])\.?m\.?\s*$", value, re.I)
        return None if match is None else match.group(1).lower()

    @classmethod
    def _clock_is_bounded(cls, value: str) -> bool:
        if cls._meridiem(value) is not None or ":" in value:
            return True
        hour = int(value.split(".", maxsplit=1)[0])
        return hour > 12

    @classmethod
    def _local_temporal_from_text(
        cls,
        name: CandidateFieldName,
        date_text: str,
        clock_text: str,
        original_text: str,
        source_path: str,
        *,
        zone: str | None,
        shared_meridiem: str | None = None,
        numeric_date_order: str | None = None,
    ) -> _FieldDraft:
        hour, minute = cls._parse_clock(clock_text, shared_meridiem=shared_meridiem)
        local_time = f"{hour:02d}:{minute:02d}"
        if cls._ambiguous_slash_dates(date_text, numeric_date_order):
            return cls._ambiguous_numeric_temporal(
                name,
                date_text,
                original_text,
                source_path,
                local_time=local_time,
                source_timezone=zone,
            )
        date = cls._parse_date_only(date_text, numeric_date_order=numeric_date_order)
        if zone is None:
            value: dict[str, JsonValue] = {
                "date": date,
                "date_source_text": date_text,
                "instant": None,
                "local_time": local_time,
                "precision": TemporalPrecision.UNKNOWN.value,
                "source_text": original_text,
                "source_timezone": None,
            }
            return _FieldDraft(
                name,
                value,
                original_text,
                TemporalPrecision.UNKNOWN,
                source_path=source_path,
            )
        parsed, source_timezone = cls._parse_exact(
            f"{date} {local_time} {zone}", numeric_date_order=numeric_date_order
        )
        value = {
            "date_source_text": date_text,
            "instant": to_storage_time(parsed),
            "precision": TemporalPrecision.EXACT_TIME.value,
            "source_text": original_text,
            "source_timezone": source_timezone,
        }
        return _FieldDraft(
            name,
            value,
            original_text,
            TemporalPrecision.EXACT_TIME,
            source_timezone,
            source_path,
        )

    @classmethod
    def _parse_clock(cls, value: str, *, shared_meridiem: str | None = None) -> tuple[int, int]:
        normalized = re.sub(r"\.?(a|p)\.?m\.?$", r" \1m", value.strip().lower())
        normalized = normalized.replace(".", ":")
        match = re.fullmatch(
            r"(?P<hour>\d{1,2}):(?P<minute>\d{2})(?:\s*(?P<meridiem>[ap])m)?",
            normalized,
        )
        if match is None:
            raise ValueError("clock source time is invalid")
        hour = int(match.group("hour"))
        minute = int(match.group("minute"))
        meridiem = match.group("meridiem") or shared_meridiem
        if minute > 59 or hour > 23:
            raise ValueError("clock source time is invalid")
        if meridiem is not None:
            if not 1 <= hour <= 12:
                raise ValueError("12-hour clock source time is invalid")
            hour = hour % 12 + (12 if meridiem == "p" else 0)
        return hour, minute

    @staticmethod
    def _slash_date_possibilities(value: str) -> dict[str, str]:
        match = _SLASH_DATE_PATTERN.fullmatch(value.strip())
        if match is None:
            return {}
        first_text, second_text, year_text = match.group(0).split("/")
        first = int(first_text)
        second = int(second_text)
        year = int(year_text)
        if len(year_text) == 2:
            year += 2000
        possibilities: dict[str, str] = {}
        for order, month, day in (("MDY", first, second), ("DMY", second, first)):
            try:
                possibilities[order] = date(year, month, day).isoformat()
            except ValueError:
                continue
        return possibilities

    @classmethod
    def _numeric_date_order(cls, text: str) -> str | None:
        orders: set[str] = set()
        for match in _SLASH_DATE_PATTERN.finditer(text):
            possibilities = cls._slash_date_possibilities(match.group(0))
            if len(possibilities) == 1:
                orders.update(possibilities)
        return next(iter(orders)) if len(orders) == 1 else None

    @classmethod
    def _ambiguous_slash_dates(cls, value: str, numeric_date_order: str | None) -> bool:
        match = _SLASH_DATE_PATTERN.search(value)
        if match is None:
            return False
        possibilities = cls._slash_date_possibilities(match.group(0))
        if not possibilities:
            raise ValueError("numeric source date is invalid")
        return numeric_date_order is None and len(set(possibilities.values())) > 1

    @classmethod
    def _ambiguous_numeric_temporal(
        cls,
        name: CandidateFieldName,
        date_text: str,
        source_text: str,
        source_path: str,
        *,
        local_time: str | None = None,
        source_timezone: str | None = None,
    ) -> _FieldDraft:
        match = _SLASH_DATE_PATTERN.search(date_text)
        if match is None:
            raise ValueError("numeric source date is unavailable")
        possibilities = cls._slash_date_possibilities(match.group(0))
        possible_dates: list[JsonValue] = list(sorted(set(possibilities.values())))
        value: dict[str, JsonValue] = {
            "date": None,
            "date_order": None,
            "instant": None,
            "possible_dates": possible_dates,
            "precision": TemporalPrecision.UNKNOWN.value,
            "source_text": source_text,
            "source_timezone": source_timezone,
        }
        if local_time is not None:
            value["local_time"] = local_time
        return _FieldDraft(
            name,
            value,
            source_text,
            TemporalPrecision.UNKNOWN,
            source_timezone,
            source_path,
        )

    @staticmethod
    def _event_type(text: str) -> tuple[EventType | None, str]:
        for pattern, event_type in _TYPE_PATTERNS:
            match = pattern.search(text)
            if match is not None:
                return event_type, match.group(0)
        return None, ""

    @staticmethod
    def _candidate_title(text: str, temporal_start: int) -> str:
        prefix = text[:temporal_start].strip(" :-–—\t")
        prefix = re.sub(
            r"\b(?:is|will be|due(?:\s+(?:on|date))?|deadline|on|at|from)\s*$",
            "",
            prefix,
            flags=re.I,
        )
        prefix = prefix.strip(" :-–—\t")
        prefix = re.sub(
            r"^(?:(?:Instructor|Lecturer|Tutor|Professor|Prof|Dr)\.?\s*[:–—-]?\s+)"
            r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+"
            r"(?=(?:Assignment|Homework|Quiz|Test|Exam(?:ination)?)\b)",
            "",
            prefix,
        )
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
    def _temporal_from_text(
        name: CandidateFieldName,
        source_text: str,
        *,
        source_path: str = "text",
        numeric_date_order: str | None = None,
    ) -> _FieldDraft:
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
            return _FieldDraft(
                name,
                value,
                source_text,
                TemporalPrecision.WEEK_ONLY,
                source_path=source_path,
            )
        if DeterministicEventExtractor._ambiguous_slash_dates(normalized, numeric_date_order):
            return DeterministicEventExtractor._ambiguous_numeric_temporal(
                name,
                normalized,
                source_text,
                source_path,
            )
        exact = bool(re.search(r"\d{1,2}[:.]\d{2}", normalized))
        if not exact:
            normalized_date = DeterministicEventExtractor._parse_date_only(
                normalized, numeric_date_order=numeric_date_order
            )
            value = {
                "instant": None,
                "precision": TemporalPrecision.DATE_ONLY.value,
                "source_text": source_text,
                "source_timezone": None,
                "date": normalized_date,
            }
            return _FieldDraft(
                name,
                value,
                source_text,
                TemporalPrecision.DATE_ONLY,
                source_path=source_path,
            )
        parsed, source_timezone = DeterministicEventExtractor._parse_exact(
            normalized, numeric_date_order=numeric_date_order
        )
        value = {
            "instant": to_storage_time(parsed),
            "precision": TemporalPrecision.EXACT_TIME.value,
            "source_text": source_text,
            "source_timezone": source_timezone,
        }
        return _FieldDraft(
            name,
            value,
            source_text,
            TemporalPrecision.EXACT_TIME,
            source_timezone,
            source_path,
        )

    @staticmethod
    def _parse_date_only(value: str, *, numeric_date_order: str | None = None) -> str:
        cleaned = value.replace(",", "").strip()
        slash = _SLASH_DATE_PATTERN.fullmatch(cleaned)
        if slash is not None:
            possibilities = DeterministicEventExtractor._slash_date_possibilities(cleaned)
            if numeric_date_order is None:
                distinct_dates = set(possibilities.values())
                if len(distinct_dates) != 1:
                    raise ValueError("numeric source date order is ambiguous")
                return next(iter(distinct_dates))
            try:
                return possibilities[numeric_date_order]
            except KeyError:
                raise ValueError("numeric source date does not match its page context") from None
        cleaned = re.sub(rf"^(?:{_WEEKDAY})\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
        for pattern in ("%Y-%m-%d", "%d %B %Y", "%d %b %Y", "%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(cleaned, pattern).date().isoformat()
            except ValueError:
                continue
        raise ValueError("date-only source time is invalid")

    @staticmethod
    def _parse_exact(value: str, *, numeric_date_order: str | None = None) -> tuple[datetime, str]:
        utc_offset = re.search(r"\(?UTC(?P<sign>[+-])(?P<hours>\d{1,2})\)?$", value, re.I)
        if utc_offset is not None:
            hours = int(utc_offset.group("hours"))
            if hours > 14:
                raise ValueError("UTC source offset is invalid")
            source_timezone = utc_offset.group(0).strip("()")
            cleaned = value[: utc_offset.start()].rstrip()
            sign = 1 if utc_offset.group("sign") == "+" else -1
            tz = timezone(sign * timedelta(hours=hours))
        elif value.upper().endswith("SGT"):
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
                offset_hours, offset_minutes = source_timezone[1:].split(":")
                tz = timezone(
                    sign * timedelta(hours=int(offset_hours), minutes=int(offset_minutes))
                )
        slash = _SLASH_DATE_PATTERN.match(cleaned)
        if slash is not None:
            normalized_date = DeterministicEventExtractor._parse_date_only(
                slash.group(0), numeric_date_order=numeric_date_order
            )
            cleaned = normalized_date + cleaned[slash.end() :]
        if re.match(r"\d{4}-\d{2}-\d{2}", cleaned):
            cleaned = re.sub(r"\s+at\s+", " ", cleaned, flags=re.I).replace(",", "")
            cleaned = re.sub(r"\s*([ap])\.?m\.?$", r" \1m", cleaned, flags=re.I)
            for pattern in (None, "%Y-%m-%d %I:%M %p"):
                try:
                    parsed = (
                        datetime.fromisoformat(cleaned)
                        if pattern is None
                        else datetime.strptime(cleaned, pattern)
                    )
                except ValueError:
                    continue
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=tz)
                return parsed.astimezone(UTC), source_timezone
            raise ValueError("exact source time is unsupported")
        cleaned = re.sub(r"\s+at\s+", " ", cleaned, flags=re.I).replace(",", "")
        cleaned = re.sub(rf"^(?:{_WEEKDAY})\s+", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\b(\d{1,2})(?:st|nd|rd|th)\b", r"\1", cleaned, flags=re.I)
        cleaned = re.sub(r"(?<=\d)\.(?=\d{2}$)", ":", cleaned)
        cleaned = re.sub(r"\s*([ap])\.?m\.?$", r" \1m", cleaned, flags=re.I)
        for pattern in (
            "%d %B %Y %H:%M",
            "%d %b %Y %H:%M",
            "%B %d %Y %H:%M",
            "%b %d %Y %H:%M",
            "%d %B %Y %I:%M %p",
            "%d %b %Y %I:%M %p",
            "%B %d %Y %I:%M %p",
            "%b %d %Y %I:%M %p",
        ):
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
    def _parse_input_hash(
        resource_sha256: str,
        parsed: sqlite3.Row,
        representations: list[sqlite3.Row] | tuple[sqlite3.Row, ...] = (),
    ) -> str:
        components = [
            resource_sha256,
            str(parsed["parser_name"]),
            str(parsed["parser_version"]),
            str(parsed["engine_version"]),
            str(parsed["settings_hash"]),
            str(parsed["parse_key"]),
        ]
        for representation in representations:
            text_hash = hashlib.sha256(str(representation["text"]).encode("utf-8")).hexdigest()
            components.extend(
                (
                    str(representation["representation_key"]),
                    str(representation["settings_hash"]),
                    str(representation["review_status"]),
                    str(representation["rendered_sha256"]),
                    text_hash,
                )
            )
        payload = "\0".join(components)
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
        input_hash: str,
    ) -> int | None:
        row = connection.execute(
            """
            SELECT extraction_record_key FROM extraction_record
            WHERE input_kind = ? AND source_observation_key IS ? AND version_key IS ?
              AND parse_key IS ?
              AND extractor_name = ? AND extractor_version = ? AND settings_hash = ?
              AND input_hash = ?
            """,
            (
                input_kind,
                observation_key,
                version_key,
                parse_key,
                self.name,
                self.version,
                _SETTINGS_HASH,
                input_hash,
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
