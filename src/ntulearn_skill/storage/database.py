"""SQLite connection policy for the local metadata store."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from ntulearn_skill.storage.paths import (
    RuntimePathError,
    ensure_private_directory,
    validate_private_path,
)


class StorageError(RuntimeError):
    """Privacy-safe storage error without source data or SQL text."""


class Database:
    """Open consistently configured SQLite connections."""

    def __init__(self, path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        lexical_path = Path(os.path.abspath(os.fspath(path.expanduser())))
        if lexical_path.is_symlink() or lexical_path.parent.is_symlink():
            raise StorageError("private metadata database path is unsafe")
        try:
            self.path = validate_private_path(lexical_path)
        except RuntimePathError:
            raise StorageError("private metadata database path is unsafe") from None
        self.busy_timeout_ms = busy_timeout_ms

    def _prepare_path(self) -> None:
        validate_private_path(self.path)
        ensure_private_directory(self.path.parent)
        self._reject_sqlite_links()
        try:
            descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            metadata = self.path.lstat()
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise StorageError("private metadata database path is unsafe")
            os.chmod(self.path, 0o600, follow_symlinks=False)
        else:
            os.close(descriptor)

    def _sidecar_paths(self) -> tuple[Path, ...]:
        return tuple(Path(f"{self.path}{suffix}") for suffix in ("-wal", "-shm", "-journal"))

    def _reject_sqlite_links(self) -> None:
        for path in (self.path, *self._sidecar_paths()):
            if path.is_symlink():
                raise StorageError("private metadata database path is unsafe")

    def _secure_sqlite_files(self) -> None:
        self._reject_sqlite_links()
        for path in (self.path, *self._sidecar_paths()):
            if path.exists():
                metadata = path.lstat()
                if not stat.S_ISREG(metadata.st_mode):
                    raise StorageError("private metadata database path is unsafe")
                os.chmod(path, 0o600, follow_symlinks=False)

    def connect(self) -> sqlite3.Connection:
        """Return a connection with foreign keys, WAL, and bounded waits enabled."""

        connection: sqlite3.Connection | None = None
        try:
            self._prepare_path()
            connection = sqlite3.connect(
                self.path,
                timeout=self.busy_timeout_ms / 1_000,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
            connection.execute("PRAGMA journal_mode = WAL")
            self._secure_sqlite_files()
            return connection
        except (OSError, RuntimePathError, sqlite3.Error, StorageError):
            if connection is not None:
                connection.close()
            raise StorageError("could not open the private metadata database") from None

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        """Run one logical operation atomically, including any dirty search index."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            self._refresh_search_index(connection)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _refresh_search_index(connection: sqlite3.Connection) -> None:
        """Refresh derived FTS rows before committing a relational source write."""

        exists = connection.execute(
            """SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'search_index_state'"""
        ).fetchone()
        if exists is None:
            return
        state = connection.execute(
            """SELECT source_generation, indexed_generation
            FROM search_index_state WHERE singleton_key = 1"""
        ).fetchone()
        if state is None or int(state["source_generation"]) == int(state["indexed_generation"]):
            return
        # Imported lazily so the storage connection policy remains usable before migration 0005
        # and the index module can continue to depend on Database without an import cycle.
        from ntulearn_skill.index.fts import SearchIndex

        SearchIndex.refresh_dirty(connection)

    def integrity_check(self) -> bool:
        connection = self.connect()
        try:
            row = connection.execute("PRAGMA integrity_check").fetchone()
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            return row is not None and row[0] == "ok" and not foreign_key_errors
        finally:
            connection.close()
