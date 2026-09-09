"""Format-independent, auditable material classification."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from ntulearn_skill.core.models import to_storage_time, utc_now
from ntulearn_skill.storage.database import Database, StorageError


class SemanticType(StrEnum):
    LECTURE_SLIDES = "lecture_slides"
    TUTORIAL = "tutorial"
    LAB_MANUAL = "lab_manual"
    ASSIGNMENT_BRIEF = "assignment_brief"
    SYLLABUS = "syllabus"
    ASSESSMENT_INFORMATION = "assessment_information"
    READING = "reading"
    REFERENCE = "reference"
    UNKNOWN = "unknown"


class ClassificationStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class HeadingSignal:
    chunk_key: int
    text: str


@dataclass(frozen=True, slots=True)
class ClassificationInput:
    resource_key: int
    version_key: int | None
    title: str
    filename: str
    ancestry: tuple[str, ...] = ()
    headings: tuple[HeadingSignal, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassificationEvidence:
    signal: str
    reference: str

    def as_json(self) -> dict[str, str]:
        return {"reference": self.reference, "signal": self.signal}


@dataclass(frozen=True, slots=True)
class ClassificationCandidate:
    semantic_type: SemanticType
    confidence: float
    evidence: tuple[ClassificationEvidence, ...]
    user_confirmed: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("classification confidence must be between zero and one")


@dataclass(frozen=True, slots=True)
class ClassificationPayload:
    status: ClassificationStatus
    candidates: tuple[ClassificationCandidate, ...]
    warning_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ClassifierDescriptor:
    name: str
    version: str


class MaterialClassifier(Protocol):
    descriptor: ClassifierDescriptor

    def classify(self, value: ClassificationInput) -> ClassificationPayload:
        """Classify instructional purpose without receiving physical format."""


_RULES: dict[SemanticType, tuple[str, ...]] = {
    SemanticType.LECTURE_SLIDES: ("lecture", "slides", "deck"),
    SemanticType.TUTORIAL: ("tutorial", "worksheet"),
    SemanticType.LAB_MANUAL: ("lab manual", "laboratory manual", "practical manual"),
    SemanticType.ASSIGNMENT_BRIEF: ("assignment", "coursework brief", "project brief"),
    SemanticType.SYLLABUS: ("syllabus", "course outline"),
    SemanticType.ASSESSMENT_INFORMATION: ("assessment", "exam information", "test information"),
    SemanticType.READING: ("reading", "article", "chapter"),
    SemanticType.REFERENCE: ("reference", "formula sheet", "handbook"),
}


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[^\W_]+", value.casefold(), flags=re.UNICODE))


def _contains_phrase(tokens: tuple[str, ...], phrase: str) -> bool:
    phrase_tokens = _tokens(phrase)
    width = len(phrase_tokens)
    return bool(width) and any(
        tokens[start : start + width] == phrase_tokens for start in range(len(tokens) - width + 1)
    )


class RuleBasedMaterialClassifier:
    descriptor = ClassifierDescriptor("bounded-keyword-rules", "1")

    def classify(self, value: ClassificationInput) -> ClassificationPayload:
        sources: list[tuple[str, str, float]] = [
            ("title", value.title, 0.82),
            ("filename", value.filename, 0.72),
        ]
        sources.extend(
            (f"ancestry:{index}", text, 0.62) for index, text in enumerate(value.ancestry)
        )
        sources.extend(
            (f"chunk:{heading.chunk_key}", heading.text, 0.58) for heading in value.headings
        )
        matches: list[ClassificationCandidate] = []
        for semantic_type, keywords in _RULES.items():
            evidence: list[ClassificationEvidence] = []
            weights: list[float] = []
            for reference, raw_text, weight in sources:
                normalized = _tokens(raw_text)
                matched_keyword = next(
                    (keyword for keyword in keywords if _contains_phrase(normalized, keyword)), None
                )
                if matched_keyword is not None:
                    evidence.append(ClassificationEvidence(f"keyword:{matched_keyword}", reference))
                    weights.append(weight)
            if evidence:
                confidence = min(0.98, max(weights) + 0.04 * (len(weights) - 1))
                matches.append(ClassificationCandidate(semantic_type, confidence, tuple(evidence)))
        if not matches:
            return ClassificationPayload(
                ClassificationStatus.COMPLETE,
                (ClassificationCandidate(SemanticType.UNKNOWN, 0.0, ()),),
            )
        return ClassificationPayload(
            ClassificationStatus.COMPLETE,
            tuple(
                sorted(
                    matches,
                    key=lambda candidate: (-candidate.confidence, candidate.semantic_type),
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class MaterialClassificationRecord:
    key: int
    run_key: int
    semantic_type: SemanticType
    confidence: float
    evidence: tuple[ClassificationEvidence, ...]
    user_confirmed: bool
    rank: int


@dataclass(frozen=True, slots=True)
class ClassificationRunRecord:
    key: int
    resource_key: int
    version_key: int | None
    classifier_name: str
    classifier_version: str
    input_hash: str
    settings_hash: str
    status: ClassificationStatus
    warning_codes: tuple[str, ...]
    error_code: str | None
    candidates: tuple[MaterialClassificationRecord, ...]


@dataclass(frozen=True, slots=True)
class ClassificationResult:
    run: ClassificationRunRecord
    cache_hit: bool


class ClassificationStorageError(StorageError):
    """Privacy-safe classification persistence failure."""


def _settings() -> tuple[dict[str, object], str]:
    settings: dict[str, object] = {"normalization": "unicode-word-casefold-1"}
    encoded = json.dumps(
        settings, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return settings, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _input_hash(value: ClassificationInput) -> str:
    encoded = json.dumps(
        {
            "ancestry": value.ancestry,
            "filename": value.filename,
            "headings": [
                {"chunk_key": heading.chunk_key, "text": heading.text} for heading in value.headings
            ],
            "title": value.title,
            "version_key": value.version_key,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ClassificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def find_cached(
        self,
        value: ClassificationInput,
        descriptor: ClassifierDescriptor,
        settings_hash: str,
    ) -> ClassificationRunRecord | None:
        input_hash = _input_hash(value)
        connection = self.database.connect()
        try:
            row = connection.execute(
                """
                SELECT * FROM classification_run
                WHERE resource_key = ? AND version_key IS ? AND classifier_name = ?
                  AND classifier_version = ? AND input_hash = ? AND settings_hash = ?
                """,
                (
                    value.resource_key,
                    value.version_key,
                    descriptor.name,
                    descriptor.version,
                    input_hash,
                    settings_hash,
                ),
            ).fetchone()
            return None if row is None else self._run(connection, row)
        except (sqlite3.Error, ValueError, TypeError):
            raise ClassificationStorageError("classification lookup failed") from None
        finally:
            connection.close()

    def store(
        self,
        value: ClassificationInput,
        descriptor: ClassifierDescriptor,
        settings: dict[str, object],
        settings_hash: str,
        payload: ClassificationPayload,
        *,
        error_code: str | None = None,
    ) -> ClassificationRunRecord:
        if payload.status is ClassificationStatus.FAILED and payload.candidates:
            raise ValueError("failed classifications cannot claim candidates")
        if len(payload.candidates) > 32 or len(payload.warning_codes) > 100:
            raise ValueError("classification report exceeds configured limits")
        input_hash = _input_hash(value)
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO classification_run(
                        resource_key, version_key, classifier_name, classifier_version, input_hash,
                        settings_hash, settings_json, status, warning_codes_json,
                        error_code, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        value.resource_key,
                        value.version_key,
                        descriptor.name,
                        descriptor.version,
                        input_hash,
                        settings_hash,
                        json.dumps(settings, sort_keys=True, separators=(",", ":")),
                        payload.status.value,
                        json.dumps(payload.warning_codes, separators=(",", ":")),
                        error_code,
                        to_storage_time(utc_now()),
                    ),
                )
                assert cursor.lastrowid is not None
                run_key = int(cursor.lastrowid)
                for rank, candidate in enumerate(payload.candidates):
                    connection.execute(
                        """
                        INSERT INTO material_classification(
                            classification_run_key, semantic_type, confidence,
                            evidence_json, user_confirmed, rank
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            run_key,
                            candidate.semantic_type.value,
                            candidate.confidence,
                            json.dumps(
                                [item.as_json() for item in candidate.evidence],
                                sort_keys=True,
                                separators=(",", ":"),
                                ensure_ascii=False,
                                allow_nan=False,
                            ),
                            int(candidate.user_confirmed),
                            rank,
                        ),
                    )
                row = connection.execute(
                    "SELECT * FROM classification_run WHERE classification_run_key = ?",
                    (run_key,),
                ).fetchone()
                assert row is not None
                return self._run(connection, row)
        except sqlite3.IntegrityError:
            cached = self.find_cached(value, descriptor, settings_hash)
            if cached is not None:
                return cached
            raise ClassificationStorageError("classification storage failed") from None
        except sqlite3.Error:
            raise ClassificationStorageError("classification storage failed") from None

    def select(self, classification_key: int, *, reason: str) -> int:
        if not reason.strip() or len(reason) > 120:
            raise ValueError("classification selection reason is invalid")
        try:
            with self.database.transaction() as connection:
                candidate = connection.execute(
                    """
                    SELECT r.resource_key
                    FROM material_classification c
                    JOIN classification_run r
                      ON r.classification_run_key = c.classification_run_key
                    WHERE c.classification_key = ?
                    """,
                    (classification_key,),
                ).fetchone()
                if candidate is None:
                    raise ValueError("classification candidate does not exist")
                resource_key = int(candidate["resource_key"])
                previous = connection.execute(
                    """
                    SELECT selection_key FROM material_classification_selection
                    WHERE resource_key = ? ORDER BY selection_key DESC LIMIT 1
                    """,
                    (resource_key,),
                ).fetchone()
                cursor = connection.execute(
                    """
                    INSERT INTO material_classification_selection(
                        resource_key, classification_key, supersedes_selection_key,
                        selected_at, reason
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        resource_key,
                        classification_key,
                        None if previous is None else int(previous["selection_key"]),
                        to_storage_time(utc_now()),
                        reason,
                    ),
                )
                assert cursor.lastrowid is not None
                return int(cursor.lastrowid)
        except sqlite3.Error:
            raise ClassificationStorageError("classification selection failed") from None

    @staticmethod
    def _run(connection: sqlite3.Connection, row: sqlite3.Row) -> ClassificationRunRecord:
        candidates = tuple(
            MaterialClassificationRecord(
                key=int(item["classification_key"]),
                run_key=int(item["classification_run_key"]),
                semantic_type=SemanticType(str(item["semantic_type"])),
                confidence=float(item["confidence"]),
                evidence=tuple(
                    ClassificationEvidence(str(evidence["signal"]), str(evidence["reference"]))
                    for evidence in json.loads(str(item["evidence_json"]))
                ),
                user_confirmed=bool(item["user_confirmed"]),
                rank=int(item["rank"]),
            )
            for item in connection.execute(
                """
                SELECT * FROM material_classification
                WHERE classification_run_key = ? ORDER BY rank
                """,
                (int(row["classification_run_key"]),),
            )
        )
        return ClassificationRunRecord(
            key=int(row["classification_run_key"]),
            resource_key=int(row["resource_key"]),
            version_key=None if row["version_key"] is None else int(row["version_key"]),
            classifier_name=str(row["classifier_name"]),
            classifier_version=str(row["classifier_version"]),
            input_hash=str(row["input_hash"]),
            settings_hash=str(row["settings_hash"]),
            status=ClassificationStatus(str(row["status"])),
            warning_codes=tuple(json.loads(str(row["warning_codes_json"]))),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            candidates=candidates,
        )


class ClassificationService:
    def __init__(
        self, repository: ClassificationRepository, classifier: MaterialClassifier
    ) -> None:
        self.repository = repository
        self.classifier = classifier

    def classify(self, value: ClassificationInput) -> ClassificationResult:
        settings, settings_hash = _settings()
        cached = self.repository.find_cached(value, self.classifier.descriptor, settings_hash)
        if cached is not None:
            return ClassificationResult(cached, True)
        try:
            payload = self.classifier.classify(value)
            error_code = None
        except Exception:
            payload = ClassificationPayload(ClassificationStatus.FAILED, ())
            error_code = "classification_failed"
        run = self.repository.store(
            value,
            self.classifier.descriptor,
            settings,
            settings_hash,
            payload,
            error_code=error_code,
        )
        return ClassificationResult(run, False)
