"""Opaque continuation cursors and SQLite snapshot fingerprints."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Generic, TypeVar

from ntulearn_skill.core.results import ProvenanceView
from ntulearn_skill.storage import Database

R = TypeVar("R")


@dataclass(frozen=True, slots=True)
class PageLoad(Generic[R]):
    items: tuple[R, ...]
    provenance: tuple[ProvenanceView, ...]
    truncated: bool = False


@dataclass(frozen=True, slots=True)
class Pagination:
    operation: str
    binding: str
    offset: int
    limit: int
    revision: str


def prepare_pagination(
    database: Database,
    operation: str,
    cursor: str | None,
    filters: Mapping[str, object],
    limit: int,
) -> Pagination:
    binding = hashlib.sha256(
        json.dumps(
            {
                "database": hashlib.sha256(os.fsencode(os.path.abspath(database.path))).hexdigest(),
                "filters": filters,
                "limit": limit,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("ascii")
    ).hexdigest()
    revision = database_revision(database)
    if cursor is None:
        return Pagination(operation, binding, 0, limit, revision)
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode((cursor + padding).encode("ascii"))
        payload = json.loads(raw.decode("ascii"))
        if (
            not isinstance(payload, dict)
            or set(payload) != {"v", "operation", "binding", "offset", "revision"}
            or payload["v"] != 1
            or payload["operation"] != operation
            or payload["binding"] != binding
            or payload["revision"] != revision
            or isinstance(payload["offset"], bool)
            or not isinstance(payload["offset"], int)
            or payload["offset"] <= 0
        ):
            raise ValueError
    except Exception:
        raise ValueError(
            "pagination cursor is invalid or local data changed; restart from the first page"
        ) from None
    return Pagination(operation, binding, int(payload["offset"]), limit, revision)


def encode_cursor(operation: str, binding: str, offset: int, limit: int, revision: str) -> str:
    if offset <= 0 or not 1 <= limit <= 100:
        raise ValueError("pagination state is invalid")
    payload = json.dumps(
        {
            "v": 1,
            "operation": operation,
            "binding": binding,
            "offset": offset,
            "revision": revision,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")


def database_revision(database: Database) -> str:
    """Fingerprint one logical SQLite snapshot for continuation validation.

    Hashing SQLite's serialized view includes committed WAL content but remains
    stable when an unchanged WAL is checkpointed or removed as connections close.
    The caller compares snapshots before and after every page; this is an
    optimistic change guard, not a cross-call database transaction.
    """
    connection = database.connect()
    try:
        connection.execute("BEGIN")
        # Establish a read snapshot before serializing the logical database.
        connection.execute("SELECT count(*) FROM sqlite_schema").fetchone()
        serialized = connection.serialize()
    finally:
        connection.close()
    return hashlib.sha256(serialized).hexdigest()
