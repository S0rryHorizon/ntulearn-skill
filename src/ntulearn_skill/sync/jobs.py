"""Durable, bounded local work for derived resource processing."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import TypeAlias, cast

from ntulearn_skill.core import CourseId
from ntulearn_skill.core.models import from_storage_time, to_storage_time, utc_now
from ntulearn_skill.events.extraction import DeterministicEventExtractor
from ntulearn_skill.events.reconciliation import EventReconciler
from ntulearn_skill.index.fts import SearchIndex
from ntulearn_skill.parsers.models import ParserLimits, ParserOptions, ParseStatus
from ntulearn_skill.parsers.service import ParseService
from ntulearn_skill.storage.database import Database, StorageError


class LocalJobError(StorageError):
    """A privacy-safe local job persistence or execution failure."""


class LocalJobContractMismatch(LocalJobError):
    """The installed local service does not match a queued executable spec."""


JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class LocalJobKind(StrEnum):
    PARSE = "parse"
    INDEX = "index"
    EXTRACT = "extract"
    RECONCILE = "reconcile"


class LocalJobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class LocalJobRecord:
    key: int
    kind: LocalJobKind
    input_identity: str
    payload: dict[str, JsonValue]
    result: dict[str, int] | None
    depends_on_job_key: int | None
    status: LocalJobStatus
    attempt_count: int
    max_attempts: int
    available_at: datetime
    lease_expires_at: datetime | None
    last_error_category: str | None


@dataclass(frozen=True, slots=True)
class ResourceJobPlan:
    parse: LocalJobRecord
    index: LocalJobRecord
    extract: LocalJobRecord
    reconcile: LocalJobRecord

    @property
    def keys(self) -> tuple[int, ...]:
        return (self.parse.key, self.index.key, self.extract.key, self.reconcile.key)


@dataclass(frozen=True, slots=True)
class ObservationJobPlan:
    extract: LocalJobRecord
    reconcile: LocalJobRecord

    @property
    def keys(self) -> tuple[int, ...]:
        return (self.extract.key, self.reconcile.key)


@dataclass(frozen=True, slots=True)
class LocalJobRunResult:
    claimed: int
    succeeded: int
    failed: int
    recovered: int
    remaining: int
    blocked: int


def _identity(value: dict[str, object]) -> str:
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error_category(error: BaseException) -> str:
    value = re.sub(r"(?<!^)(?=[A-Z])", "_", type(error).__name__).lower()
    value = re.sub(r"[^a-z0-9_.-]", "_", value)[:120]
    return value or "local_job_error"


def _required_text(payload: dict[str, JsonValue], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("local job text spec is invalid")
    return value


def _required_key(payload: dict[str, JsonValue], key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("local job key spec is invalid")
    return value


def _required_digest(payload: dict[str, JsonValue], key: str) -> str:
    value = _required_text(payload, key)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("local job digest spec is invalid")
    return value


def _parser_options(payload: dict[str, JsonValue]) -> ParserOptions:
    settings = payload.get("parser_settings")
    if not isinstance(settings, dict) or set(settings) != {"diagnostics", "limits"}:
        raise ValueError("local parser settings spec is invalid")
    diagnostics = settings.get("diagnostics")
    limits = settings.get("limits")
    if not isinstance(diagnostics, dict) or not isinstance(limits, dict):
        raise ValueError("local parser settings spec is invalid")
    if set(diagnostics) != {
        "drawing_operator_threshold",
        "low_text_character_threshold",
        "version",
    } or diagnostics.get("version") not in {"stage-b-1", "stage-b-2"}:
        raise ValueError("local parser diagnostics spec is invalid")
    expected_limits = set(ParserLimits().as_settings())
    if set(limits) != expected_limits:
        raise ValueError("local parser limits spec is invalid")
    numeric = (
        *limits.values(),
        diagnostics.get("drawing_operator_threshold"),
        diagnostics.get("low_text_character_threshold"),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in numeric):
        raise ValueError("local parser numeric spec is invalid")
    options = ParserOptions(
        limits=ParserLimits(**cast(dict[str, int], limits)),
        drawing_operator_threshold=cast(int, diagnostics["drawing_operator_threshold"]),
        low_text_character_threshold=cast(int, diagnostics["low_text_character_threshold"]),
        diagnostic_version=cast(str, diagnostics["version"]),
    )
    if _required_text(payload, "settings_hash") != options.settings_hash:
        raise ValueError("local parser settings hash does not match its spec")
    return options


def _validate_payload(kind: LocalJobKind, payload: dict[str, JsonValue]) -> None:
    if kind is LocalJobKind.PARSE:
        expected = {
            "version_key",
            "parser_name",
            "parser_version",
            "engine_version",
            "settings_hash",
            "parser_settings",
        }
        if set(payload) != expected:
            raise ValueError("local parse job spec is invalid")
        _required_key(payload, "version_key")
        _required_text(payload, "parser_name")
        _required_text(payload, "parser_version")
        _required_text(payload, "engine_version")
        _parser_options(payload)
        return
    if kind is LocalJobKind.INDEX:
        if set(payload) != {"version_key", "indexer_version"}:
            raise ValueError("local index job spec is invalid")
        _required_key(payload, "version_key")
        _required_text(payload, "indexer_version")
        return
    if kind is LocalJobKind.EXTRACT:
        target = {"version_key"} if "version_key" in payload else {"observation_key"}
        legacy = target | {"extractor_name", "extractor_version"}
        current = legacy | {"extractor_settings_hash"}
        if set(payload) not in {frozenset(legacy), frozenset(current)}:
            raise ValueError("local extraction job spec is invalid")
        _required_key(payload, next(iter(target)))
        _required_text(payload, "extractor_name")
        _required_text(payload, "extractor_version")
        if "extractor_settings_hash" in payload:
            _required_digest(payload, "extractor_settings_hash")
        return
    if kind is LocalJobKind.RECONCILE:
        if set(payload) != {"course_key", "resolver_name", "resolver_version"}:
            raise ValueError("local reconciliation job spec is invalid")
        _required_key(payload, "course_key")
        _required_text(payload, "resolver_name")
        _required_text(payload, "resolver_version")
        return
    raise ValueError("local job kind is invalid")


class LocalJobQueue:
    """Persist idempotent local jobs without storing source text or remote routes."""

    def __init__(self, database: Database) -> None:
        self.database = database

    def schedule(
        self,
        kind: LocalJobKind,
        input_identity: str,
        payload: dict[str, JsonValue],
        *,
        depends_on_job_key: int | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> LocalJobRecord:
        if not isinstance(kind, LocalJobKind):
            raise TypeError("job kind must be typed")
        if len(input_identity) != 64 or any(c not in "0123456789abcdef" for c in input_identity):
            raise ValueError("job input identity must be a lowercase SHA-256")
        if not payload or any(not isinstance(key, str) or not key for key in payload):
            raise ValueError("local job payload is invalid")
        _validate_payload(kind, payload)
        if max_attempts <= 0:
            raise ValueError("maximum attempts must be positive")
        if depends_on_job_key is not None and depends_on_job_key <= 0:
            raise ValueError("dependency key must be positive")
        timestamp = to_storage_time(available_at or utc_now())
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded) > 16_384:
            raise ValueError("local job payload is too large")

        def write(active: sqlite3.Connection) -> LocalJobRecord:
            active.execute(
                """
                INSERT INTO local_job(
                    job_kind, input_identity, payload_json, depends_on_job_key, status,
                    max_attempts, available_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'PENDING', ?, ?, ?, ?)
                ON CONFLICT(job_kind, input_identity) DO NOTHING
                """,
                (
                    kind.value,
                    input_identity,
                    encoded,
                    depends_on_job_key,
                    max_attempts,
                    timestamp,
                    timestamp,
                    timestamp,
                ),
            )
            row = active.execute(
                "SELECT * FROM local_job WHERE job_kind = ? AND input_identity = ?",
                (kind.value, input_identity),
            ).fetchone()
            assert row is not None
            record = self._from_row(row)
            if record.payload != payload or record.depends_on_job_key != depends_on_job_key:
                raise LocalJobError("local job identity collision")
            return record

        try:
            if connection is not None:
                return write(connection)
            with self.database.transaction() as active:
                return write(active)
        except LocalJobError:
            raise
        except (sqlite3.Error, TypeError, ValueError):
            raise LocalJobError("local job could not be scheduled") from None

    def get(self, job_key: int) -> LocalJobRecord | None:
        if job_key <= 0:
            raise ValueError("job key must be positive")
        connection = self.database.connect()
        try:
            row = connection.execute(
                "SELECT * FROM local_job WHERE job_key = ?", (job_key,)
            ).fetchone()
            return None if row is None else self._from_row(row)
        except (sqlite3.Error, TypeError, ValueError):
            raise LocalJobError("local job could not be read") from None
        finally:
            connection.close()

    def list(self) -> tuple[LocalJobRecord, ...]:
        connection = self.database.connect()
        try:
            rows = connection.execute("SELECT * FROM local_job ORDER BY job_key").fetchall()
            return tuple(self._from_row(row) for row in rows)
        except (sqlite3.Error, TypeError, ValueError):
            raise LocalJobError("local jobs could not be read") from None
        finally:
            connection.close()

    def recover_interrupted(
        self, *, now: datetime | None = None, job_keys: Iterable[int] | None = None
    ) -> int:
        timestamp = to_storage_time(now or utc_now())
        selected = self._job_keys(job_keys)
        if selected == ():
            return 0
        selected_sql, selected_parameters = self._selection_sql(selected)
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    f"""
                    UPDATE local_job
                    SET status = CASE
                            WHEN attempt_count < max_attempts THEN 'PENDING'
                            ELSE 'FAILED'
                        END,
                        available_at = ?, lease_expires_at = NULL, worker_token = NULL,
                        last_error_category = 'interrupted', updated_at = ?
                    WHERE status = 'RUNNING' AND lease_expires_at <= ? {selected_sql}
                    """,
                    (timestamp, timestamp, timestamp, *selected_parameters),
                )
                return cursor.rowcount
        except sqlite3.Error:
            raise LocalJobError("interrupted local jobs could not be recovered") from None

    def retry_failed(self, job_key: int, *, now: datetime | None = None) -> bool:
        if job_key <= 0:
            raise ValueError("job key must be positive")
        timestamp = to_storage_time(now or utc_now())
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE local_job
                    SET status = 'PENDING', available_at = ?, updated_at = ?
                    WHERE job_key = ? AND status = 'FAILED' AND attempt_count < max_attempts
                    """,
                    (timestamp, timestamp, job_key),
                )
                return cursor.rowcount == 1
        except sqlite3.Error:
            raise LocalJobError("failed local job could not be retried") from None

    def _claim(
        self,
        *,
        now: datetime,
        lease: timedelta,
        job_keys: tuple[int, ...] | None = None,
    ) -> tuple[LocalJobRecord, str] | None:
        if job_keys == ():
            return None
        timestamp = to_storage_time(now)
        lease_expires = to_storage_time(now + lease)
        token = uuid.uuid4().hex
        selected_sql, selected_parameters = self._selection_sql(job_keys, alias="job")
        try:
            with self.database.transaction() as connection:
                row = connection.execute(
                    f"""
                    SELECT job.* FROM local_job job
                    LEFT JOIN local_job dependency
                      ON dependency.job_key = job.depends_on_job_key
                    WHERE job.status = 'PENDING'
                      AND job.available_at <= ?
                      AND job.attempt_count < job.max_attempts
                      AND (dependency.job_key IS NULL OR dependency.status = 'SUCCEEDED')
                      {selected_sql}
                    ORDER BY job.job_key LIMIT 1
                    """,
                    (timestamp, *selected_parameters),
                ).fetchone()
                if row is None:
                    return None
                cursor = connection.execute(
                    """
                    UPDATE local_job
                    SET status = 'RUNNING', attempt_count = attempt_count + 1,
                        lease_expires_at = ?, worker_token = ?,
                        started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE job_key = ? AND status = 'PENDING'
                    """,
                    (lease_expires, token, timestamp, timestamp, int(row["job_key"])),
                )
                if cursor.rowcount != 1:
                    return None
                claimed = connection.execute(
                    "SELECT * FROM local_job WHERE job_key = ?", (int(row["job_key"]),)
                ).fetchone()
                assert claimed is not None
                return self._from_row(claimed), token
        except (sqlite3.Error, TypeError, ValueError):
            raise LocalJobError("local job could not be claimed") from None

    def _finish(
        self,
        job_key: int,
        token: str,
        *,
        result: dict[str, int] | None,
        error_category: str | None,
        now: datetime,
    ) -> None:
        timestamp = to_storage_time(now)
        status = LocalJobStatus.SUCCEEDED if error_category is None else LocalJobStatus.FAILED
        if (result is None) != (status is LocalJobStatus.FAILED):
            raise ValueError("local job result does not match its status")
        result_json = None if result is None else json.dumps(result, sort_keys=True)
        try:
            with self.database.transaction() as connection:
                cursor = connection.execute(
                    """
                    UPDATE local_job
                    SET status = ?, lease_expires_at = NULL, worker_token = NULL,
                        last_error_category = ?, result_json = ?, completed_at = ?, updated_at = ?
                    WHERE job_key = ? AND status = 'RUNNING' AND worker_token = ?
                    """,
                    (
                        status.value,
                        error_category,
                        result_json,
                        timestamp if status is LocalJobStatus.SUCCEEDED else None,
                        timestamp,
                        job_key,
                        token,
                    ),
                )
                if cursor.rowcount != 1:
                    raise LocalJobError("local job lease was lost")
        except LocalJobError:
            raise
        except sqlite3.Error:
            raise LocalJobError("local job result could not be recorded") from None

    def counts(self, *, job_keys: Iterable[int] | None = None) -> tuple[int, int]:
        """Return selected not-succeeded jobs and the subset blocked from claiming."""

        selected = self._job_keys(job_keys)
        if selected == ():
            return 0, 0
        selected_sql, selected_parameters = self._selection_sql(selected, alias="job")
        connection = self.database.connect()
        try:
            row = connection.execute(
                f"""
                SELECT
                    SUM(CASE WHEN job.status <> 'SUCCEEDED' THEN 1 ELSE 0 END) AS remaining,
                    SUM(CASE
                        WHEN job.status = 'FAILED' THEN 1
                        WHEN job.status = 'PENDING' AND (
                            job.attempt_count >= job.max_attempts
                            OR (dependency.job_key IS NOT NULL
                                AND dependency.status <> 'SUCCEEDED')
                        ) THEN 1
                        ELSE 0
                    END) AS blocked
                FROM local_job job
                LEFT JOIN local_job dependency
                  ON dependency.job_key = job.depends_on_job_key
                WHERE 1 = 1 {selected_sql}
                """,
                selected_parameters,
            ).fetchone()
            assert row is not None
            return int(row["remaining"] or 0), int(row["blocked"] or 0)
        except sqlite3.Error:
            raise LocalJobError("local job counts could not be read") from None
        finally:
            connection.close()

    @staticmethod
    def _job_keys(job_keys: Iterable[int] | None) -> tuple[int, ...] | None:
        if job_keys is None:
            return None
        supplied = tuple(job_keys)
        if any(isinstance(key, bool) or not isinstance(key, int) or key <= 0 for key in supplied):
            raise ValueError("job keys must be positive integers")
        return tuple(sorted(set(supplied)))

    @staticmethod
    def _selection_sql(
        job_keys: tuple[int, ...] | None, *, alias: str = "local_job"
    ) -> tuple[str, tuple[int, ...]]:
        if job_keys is None:
            return "", ()
        placeholders = ",".join("?" for _ in job_keys)
        return f"AND {alias}.job_key IN ({placeholders})", job_keys

    @staticmethod
    def _from_row(row: sqlite3.Row) -> LocalJobRecord:
        payload = json.loads(str(row["payload_json"]))
        kind = LocalJobKind(str(row["job_kind"]))
        if not isinstance(payload, dict) or any(not isinstance(key, str) for key in payload):
            raise ValueError("stored local job payload is invalid")
        typed_payload = cast(dict[str, JsonValue], payload)
        _validate_payload(kind, typed_payload)
        raw_result = row["result_json"]
        result: dict[str, int] | None = None
        if raw_result is not None:
            loaded_result = json.loads(str(raw_result))
            if not isinstance(loaded_result, dict) or any(
                not isinstance(key, str)
                or isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
                for key, value in loaded_result.items()
            ):
                raise ValueError("stored local job result is invalid")
            result = cast(dict[str, int], loaded_result)
        return LocalJobRecord(
            key=int(row["job_key"]),
            kind=kind,
            input_identity=str(row["input_identity"]),
            payload=typed_payload,
            result=result,
            depends_on_job_key=None
            if row["depends_on_job_key"] is None
            else int(row["depends_on_job_key"]),
            status=LocalJobStatus(str(row["status"])),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            available_at=from_storage_time(str(row["available_at"])),
            lease_expires_at=None
            if row["lease_expires_at"] is None
            else from_storage_time(str(row["lease_expires_at"])),
            last_error_category=None
            if row["last_error_category"] is None
            else str(row["last_error_category"]),
        )


class LocalJobPlanner:
    """Create one dependency-aware pipeline per parser/extractor/resolver input identity."""

    def __init__(
        self,
        queue: LocalJobQueue,
        parse_service: ParseService,
        extractor: DeterministicEventExtractor,
        reconciler: EventReconciler,
        *,
        parser_options: ParserOptions | None = None,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts <= 0:
            raise ValueError("maximum attempts must be positive")
        self.queue = queue
        self.parse_service = parse_service
        self.extractor = extractor
        self.reconciler = reconciler
        self.parser_options = parser_options or ParserOptions()
        self.max_attempts = max_attempts

    def plan_resource(
        self, version_key: int, *, connection: sqlite3.Connection | None = None
    ) -> ResourceJobPlan:
        if version_key <= 0:
            raise ValueError("version key must be positive")

        def plan(active: sqlite3.Connection) -> ResourceJobPlan:
            row = active.execute(
                """
                SELECT version.sha256, version.file_format, node.course_key
                FROM resource_version version
                JOIN resource ON resource.resource_key = version.resource_key
                JOIN content_node node ON node.content_key = resource.content_key
                WHERE version.version_key = ? AND version.verification_status = 'VERIFIED'
                """,
                (version_key,),
            ).fetchone()
            if row is None:
                raise ValueError("verified resource version does not exist")
            parser = self.parse_service.registry.parser_for(str(row["file_format"]))
            parser_identity: dict[str, object]
            if parser is None:
                parser_identity = {
                    "name": "unsupported",
                    "version": "1",
                    "engine_version": "none",
                }
            else:
                descriptor = parser.descriptor
                parser_identity = {
                    "name": descriptor.name,
                    "version": descriptor.version,
                    "engine_version": descriptor.engine_version,
                }
            parse_identity = _identity(
                {
                    "kind": "parse",
                    "version_key": version_key,
                    "resource_sha256": str(row["sha256"]),
                    "parser": parser_identity,
                    "settings_hash": self.parser_options.settings_hash,
                }
            )
            parse_payload: dict[str, JsonValue] = {
                "version_key": version_key,
                "parser_name": cast(str, parser_identity["name"]),
                "parser_version": cast(str, parser_identity["version"]),
                "engine_version": cast(str, parser_identity["engine_version"]),
                "settings_hash": self.parser_options.settings_hash,
                "parser_settings": self.parser_options.canonical_settings(),
            }
            parse = self.queue.schedule(
                LocalJobKind.PARSE,
                parse_identity,
                parse_payload,
                max_attempts=self.max_attempts,
                connection=active,
            )
            index_identity = _identity(
                {
                    "kind": "index",
                    "parse_identity": parse_identity,
                    "indexer_version": SearchIndex.version,
                }
            )
            index = self.queue.schedule(
                LocalJobKind.INDEX,
                index_identity,
                {"version_key": version_key, "indexer_version": SearchIndex.version},
                depends_on_job_key=parse.key,
                max_attempts=self.max_attempts,
                connection=active,
            )
            extract_identity = _identity(
                {
                    "kind": "extract",
                    "parse_identity": parse_identity,
                    "extractor_name": self.extractor.name,
                    "extractor_version": self.extractor.version,
                    "extractor_settings_hash": self.extractor.settings_hash,
                }
            )
            extract = self.queue.schedule(
                LocalJobKind.EXTRACT,
                extract_identity,
                {
                    "version_key": version_key,
                    "extractor_name": self.extractor.name,
                    "extractor_version": self.extractor.version,
                    "extractor_settings_hash": self.extractor.settings_hash,
                },
                depends_on_job_key=parse.key,
                max_attempts=self.max_attempts,
                connection=active,
            )
            reconcile_identity = _identity(
                {
                    "kind": "reconcile",
                    "course_key": int(row["course_key"]),
                    "extract_identity": extract_identity,
                    "resolver_name": self.reconciler.name,
                    "resolver_version": self.reconciler.resolver_version,
                }
            )
            reconcile = self.queue.schedule(
                LocalJobKind.RECONCILE,
                reconcile_identity,
                {
                    "course_key": int(row["course_key"]),
                    "resolver_name": self.reconciler.name,
                    "resolver_version": self.reconciler.resolver_version,
                },
                depends_on_job_key=extract.key,
                max_attempts=self.max_attempts,
                connection=active,
            )
            return ResourceJobPlan(parse, index, extract, reconcile)

        try:
            if connection is not None:
                return plan(connection)
            with self.queue.database.transaction() as active:
                return plan(active)
        except (LocalJobError, ValueError):
            raise
        except sqlite3.Error:
            raise LocalJobError("resource local jobs could not be planned") from None

    def plan_observation(
        self, observation_key: int, *, connection: sqlite3.Connection | None = None
    ) -> ObservationJobPlan:
        """Queue deterministic extraction/reconciliation for one structured observation."""

        if observation_key <= 0:
            raise ValueError("observation key must be positive")

        def plan(active: sqlite3.Connection) -> ObservationJobPlan:
            row = active.execute(
                """
                SELECT observation.observation_hash, observation.source_object_key,
                       owner.course_key,
                       (
                           SELECT MIN(canonical.observation_key)
                           FROM source_observation canonical
                           WHERE canonical.source_object_key = observation.source_object_key
                             AND canonical.observation_hash = observation.observation_hash
                       ) AS canonical_observation_key
                FROM source_observation observation
                JOIN (
                    SELECT source_object_key, course_key FROM announcement
                    UNION ALL SELECT source_object_key, course_key FROM assessment
                    UNION ALL SELECT source_object_key, course_key FROM schedule_item
                    UNION ALL SELECT source_object_key, course_key FROM due_item
                ) owner ON owner.source_object_key = observation.source_object_key
                WHERE observation.observation_key = ?
                """,
                (observation_key,),
            ).fetchone()
            if row is None:
                raise ValueError("structured source observation does not exist")
            extract_identity = _identity(
                {
                    "kind": "extract_observation",
                    "observation_hash": str(row["observation_hash"]),
                    "source_object_key": int(row["source_object_key"]),
                    "extractor_name": self.extractor.name,
                    "extractor_version": self.extractor.version,
                    "extractor_settings_hash": self.extractor.settings_hash,
                }
            )
            canonical_observation_key = int(row["canonical_observation_key"])
            extract = self.queue.schedule(
                LocalJobKind.EXTRACT,
                extract_identity,
                {
                    "observation_key": canonical_observation_key,
                    "extractor_name": self.extractor.name,
                    "extractor_version": self.extractor.version,
                    "extractor_settings_hash": self.extractor.settings_hash,
                },
                max_attempts=self.max_attempts,
                connection=active,
            )
            reconcile_identity = _identity(
                {
                    "kind": "reconcile_observation",
                    "course_key": int(row["course_key"]),
                    "extract_identity": extract_identity,
                    "resolver_name": self.reconciler.name,
                    "resolver_version": self.reconciler.resolver_version,
                }
            )
            reconcile = self.queue.schedule(
                LocalJobKind.RECONCILE,
                reconcile_identity,
                {
                    "course_key": int(row["course_key"]),
                    "resolver_name": self.reconciler.name,
                    "resolver_version": self.reconciler.resolver_version,
                },
                depends_on_job_key=extract.key,
                max_attempts=self.max_attempts,
                connection=active,
            )
            return ObservationJobPlan(extract, reconcile)

        try:
            if connection is not None:
                return plan(connection)
            with self.queue.database.transaction() as active:
                return plan(active)
        except (LocalJobError, ValueError):
            raise
        except sqlite3.Error:
            raise LocalJobError("observation local jobs could not be planned") from None


class LocalJobRunner:
    """Run a bounded number of claimed local jobs through the real core services."""

    def __init__(
        self,
        queue: LocalJobQueue,
        parse_service: ParseService,
        search_index: SearchIndex,
        extractor: DeterministicEventExtractor,
        reconciler: EventReconciler,
    ) -> None:
        self.queue = queue
        self.parse_service = parse_service
        self.search_index = search_index
        self.extractor = extractor
        self.reconciler = reconciler

    def run(
        self,
        *,
        max_jobs: int = 32,
        lease: timedelta = timedelta(minutes=5),
        now: datetime | None = None,
        job_keys: Iterable[int] | None = None,
    ) -> LocalJobRunResult:
        if max_jobs <= 0:
            raise ValueError("maximum jobs must be positive")
        if lease <= timedelta(0):
            raise ValueError("job lease must be positive")
        clock = now or utc_now()
        selected = self.queue._job_keys(job_keys)
        recovered = self.queue.recover_interrupted(now=clock, job_keys=selected)
        claimed = succeeded = failed = 0
        for _ in range(max_jobs):
            item = self.queue._claim(now=clock, lease=lease, job_keys=selected)
            if item is None:
                break
            job, token = item
            claimed += 1
            try:
                execution_result = self._execute(job)
            except Exception as error:
                failed += 1
                self.queue._finish(
                    job.key,
                    token,
                    result=None,
                    error_category=_safe_error_category(error),
                    now=clock,
                )
            else:
                succeeded += 1
                self.queue._finish(
                    job.key,
                    token,
                    result=execution_result,
                    error_category=None,
                    now=clock,
                )
        remaining, blocked = self.queue.counts(job_keys=selected)
        return LocalJobRunResult(claimed, succeeded, failed, recovered, remaining, blocked)

    def _execute(self, job: LocalJobRecord) -> dict[str, int]:
        if job.kind is LocalJobKind.PARSE:
            version_key = _required_key(job.payload, "version_key")
            options = _parser_options(job.payload)
            version = self.parse_service.resources.get_version(version_key)
            if version is None:
                raise LocalJobError("queued parser input is unavailable")
            parser = self.parse_service.registry.parser_for(version.file_format)
            installed = (
                ("unsupported", "1", "none")
                if parser is None
                else (
                    parser.descriptor.name,
                    parser.descriptor.version,
                    parser.descriptor.engine_version,
                )
            )
            planned = (
                _required_text(job.payload, "parser_name"),
                _required_text(job.payload, "parser_version"),
                _required_text(job.payload, "engine_version"),
            )
            if installed != planned:
                raise LocalJobContractMismatch("queued parser contract is unavailable")
            parse_result = self.parse_service.parse_version(version_key, options=options)
            if parse_result.document.status is ParseStatus.FAILED:
                raise LocalJobError("parser returned a failed terminal result")
            if (
                parse_result.document.parser_name,
                parse_result.document.parser_version,
                parse_result.document.engine_version,
                parse_result.document.settings_hash,
            ) != (*planned, options.settings_hash):
                raise LocalJobContractMismatch("parser result does not match the queued contract")
            return {"parse_key": parse_result.document.key}
        if job.kind is LocalJobKind.INDEX:
            if _required_text(job.payload, "indexer_version") != self.search_index.version:
                raise LocalJobContractMismatch("queued indexer contract is unavailable")
            index_result = self.search_index.rebuild()
            return {"source_generation": index_result.source_generation}
        if job.kind is LocalJobKind.EXTRACT:
            planned_settings_hash = job.payload.get("extractor_settings_hash")
            if (
                _required_text(job.payload, "extractor_name") != self.extractor.name
                or _required_text(job.payload, "extractor_version") != self.extractor.version
                or not isinstance(planned_settings_hash, str)
                or planned_settings_hash != self.extractor.settings_hash
            ):
                raise LocalJobContractMismatch("queued extractor contract is unavailable")
            if "version_key" in job.payload:
                extraction_result = self.extractor.extract_resource_version(
                    _required_key(job.payload, "version_key"),
                    parse_key=self._dependency_parse_key(job),
                )
            elif "observation_key" in job.payload:
                extraction_result = self.extractor.extract_observation(
                    _required_key(job.payload, "observation_key")
                )
            else:
                raise LocalJobError("local extraction job payload is invalid")
            return {"extraction_record_key": extraction_result.extraction_record_key}
        if job.kind is LocalJobKind.RECONCILE:
            if (
                _required_text(job.payload, "resolver_name") != self.reconciler.name
                or _required_text(job.payload, "resolver_version")
                != self.reconciler.resolver_version
            ):
                raise LocalJobContractMismatch("queued resolver contract is unavailable")
            reconciliation_result = self.reconciler.reconcile_course(
                self._course(_required_key(job.payload, "course_key"))
            )
            return {"reconciliation_run_key": reconciliation_result.run_key}
        raise LocalJobError("unsupported local job kind")

    def _dependency_parse_key(self, job: LocalJobRecord) -> int:
        if job.depends_on_job_key is None:
            raise LocalJobContractMismatch("extraction job has no parser dependency")
        dependency = self.queue.get(job.depends_on_job_key)
        if (
            dependency is None
            or dependency.kind is not LocalJobKind.PARSE
            or dependency.status is not LocalJobStatus.SUCCEEDED
            or dependency.result is None
            or set(dependency.result) != {"parse_key"}
        ):
            raise LocalJobContractMismatch("parser dependency result is unavailable")
        parse_key = dependency.result["parse_key"]
        if parse_key <= 0:
            raise LocalJobContractMismatch("parser dependency result is invalid")
        return parse_key

    def _course(self, course_key: int) -> CourseId:
        connection = self.queue.database.connect()
        try:
            row = connection.execute(
                """
                SELECT provider.name AS provider, object.remote_key
                FROM course
                JOIN source_object object ON object.source_object_key = course.source_object_key
                JOIN source_provider provider ON provider.provider_key = object.provider_key
                WHERE course.course_key = ?
                """,
                (course_key,),
            ).fetchone()
            if row is None:
                raise LocalJobError("local job course no longer exists")
            return CourseId(str(row["provider"]), str(row["remote_key"]))
        except sqlite3.Error:
            raise LocalJobError("local job course could not be read") from None
        finally:
            connection.close()
