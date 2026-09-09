"""Ordered, checksummed, transactional SQLite migrations."""

from __future__ import annotations

import hashlib
import importlib.resources
import os
import sqlite3
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ntulearn_skill.storage.database import Database, StorageError
from ntulearn_skill.storage.paths import (
    RuntimePathError,
    ensure_private_directory,
    validate_private_path,
)


class MigrationError(StorageError):
    """Migration failure with a deliberately redacted message."""

    def __init__(self, message: str, *, backup_reference: str | None = None) -> None:
        super().__init__(message)
        self.backup_reference = backup_reference


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def load_migrations() -> tuple[Migration, ...]:
    """Load packaged SQL migrations in version order."""

    root = importlib.resources.files("ntulearn_skill.storage.migrations")
    migrations: list[Migration] = []
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if entry.name.endswith(".sql"):
            prefix, _, name = entry.name.removesuffix(".sql").partition("_")
            migrations.append(Migration(int(prefix), name, entry.read_text(encoding="utf-8")))
    versions = [migration.version for migration in migrations]
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise MigrationError("packaged migration sequence is invalid")
    return tuple(migrations)


def _contains_only_complete_comments(sql: str) -> bool:
    position = 0
    while position < len(sql):
        if sql[position].isspace():
            position += 1
        elif sql.startswith("--", position):
            newline = sql.find("\n", position + 2)
            position = len(sql) if newline < 0 else newline + 1
        elif sql.startswith("/*", position):
            closing = sql.find("*/", position + 2)
            if closing < 0:
                return False
            position = closing + 2
        else:
            return False
    return True


def _statements(sql: str) -> Iterator[str]:
    buffer: list[str] = []
    for character in sql:
        buffer.append(character)
        if character != ";":
            continue
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            if candidate.strip():
                yield candidate.strip()
            buffer.clear()
    remainder = "".join(buffer)
    if remainder.strip() and not _contains_only_complete_comments(remainder):
        raise MigrationError("migration contains an incomplete statement")


class MigrationRunner:
    def __init__(
        self,
        database: Database,
        *,
        migrations: Iterable[Migration] | None = None,
        backup_directory: Path | None = None,
    ) -> None:
        self.database = database
        self.migrations = tuple(load_migrations() if migrations is None else migrations)
        lexical_backup_directory = Path(
            os.path.abspath(os.fspath(backup_directory or database.path.parent / "backups"))
        )
        if lexical_backup_directory.is_symlink():
            raise MigrationError("migration backup path is unsafe")
        try:
            self.backup_directory = validate_private_path(lexical_backup_directory)
        except RuntimePathError:
            raise MigrationError("migration backup path is unsafe") from None
        versions = [migration.version for migration in self.migrations]
        if versions != sorted(set(versions)):
            raise MigrationError("migration versions must be unique and ordered")

    def _applied(self, connection: sqlite3.Connection) -> dict[int, str]:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migration'"
        ).fetchone()
        if not exists:
            return {}
        return {
            int(row["version"]): str(row["checksum"])
            for row in connection.execute("SELECT version, checksum FROM schema_migration")
        }

    def _backup(self) -> Path:
        ensure_private_directory(self.backup_directory)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        filename = f"pre-migration-{stamp}-{uuid.uuid4().hex[:8]}.sqlite3"
        destination_path = self.backup_directory / filename
        descriptor = os.open(destination_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(descriptor)
        source: sqlite3.Connection | None = None
        destination: sqlite3.Connection | None = None
        try:
            source = sqlite3.connect(f"{self.database.path.as_uri()}?mode=ro", uri=True)
            destination = sqlite3.connect(destination_path)
            source.backup(destination)
            os.chmod(destination_path, 0o600, follow_symlinks=False)
            return destination_path
        except Exception:
            destination_path.unlink(missing_ok=True)
            raise
        finally:
            if destination is not None:
                destination.close()
            if source is not None:
                source.close()

    def migrate(self) -> int:
        """Validate history and apply every pending migration in one transaction."""

        connection = self.database.connect()
        backup_path: Path | None = None
        try:
            connection.execute("BEGIN EXCLUSIVE")
            applied = self._applied(connection)
            known = {migration.version: migration for migration in self.migrations}
            if any(version not in known for version in applied):
                raise MigrationError("database schema is newer than this application")
            for version, checksum in applied.items():
                if known[version].checksum != checksum:
                    raise MigrationError(f"migration checksum mismatch at version {version}")

            pending = [
                migration for migration in self.migrations if migration.version not in applied
            ]
            if applied and pending:
                backup_path = self._backup()
            if not pending:
                connection.commit()
                return max(applied, default=0)

            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    checksum TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                ) STRICT
                """
            )
            for migration in pending:
                for statement in _statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    """INSERT INTO schema_migration(version, name, checksum, applied_at)
                    VALUES (?, ?, ?, ?)""",
                    (
                        migration.version,
                        migration.name,
                        migration.checksum,
                        datetime.now(UTC).isoformat(timespec="microseconds"),
                    ),
                )
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if foreign_key_errors or integrity is None or integrity[0] != "ok":
                raise MigrationError("database integrity check failed after migration")
            connection.commit()
            return self.migrations[-1].version
        except MigrationError as error:
            connection.rollback()
            if backup_path is not None and error.backup_reference is None:
                raise MigrationError(str(error), backup_reference=backup_path.name) from None
            raise
        except (OSError, sqlite3.Error, ValueError):
            connection.rollback()
            raise MigrationError(
                "database migration failed",
                backup_reference=backup_path.name if backup_path is not None else None,
            ) from None
        finally:
            connection.close()
