from __future__ import annotations

import os
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import ntulearn_skill.storage.database as database_module
from ntulearn_skill.storage.database import Database, StorageError
from ntulearn_skill.storage.migration import Migration, MigrationError, MigrationRunner
from ntulearn_skill.storage.paths import RuntimePathError, RuntimePaths, resolve_runtime_root


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _repository(path: Path) -> Path:
    path.mkdir()
    (path / ".git").mkdir()
    return path


def test_runtime_candidate_repository_is_checked_independently_of_cwd(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "public")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()

    with pytest.raises(RuntimePathError):
        resolve_runtime_root(repository / "tracked-runtime", cwd=cwd)


def test_explicit_real_local_directory_is_allowed(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "public")

    root = resolve_runtime_root(repository / ".local" / "runtime", cwd=tmp_path)

    assert root == repository / ".local" / "runtime"


def test_blank_runtime_environment_value_uses_home_default(tmp_path: Path) -> None:
    home = tmp_path / "home"

    root = resolve_runtime_root(environ={"NTULEARN_DATA_DIR": ""}, cwd=tmp_path, home=home)

    assert root == home / ".ntulearn-skill"


def test_local_symlink_cannot_alias_a_tracked_directory(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "public")
    tracked = repository / "tracked"
    tracked.mkdir()
    (repository / ".local").symlink_to(tracked, target_is_directory=True)

    with pytest.raises(RuntimePathError):
        resolve_runtime_root(repository / ".local" / "runtime", cwd=tmp_path)


def test_runtime_child_symlink_escape_is_rejected_without_chmod_target(tmp_path: Path) -> None:
    root = tmp_path / "private"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (root / "db").symlink_to(outside, target_is_directory=True)

    with pytest.raises(RuntimePathError):
        RuntimePaths(root).ensure()

    assert _mode(outside) == 0o755


def test_database_rejects_direct_tracked_path(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "public")

    with pytest.raises(StorageError, match="private metadata database path is unsafe"):
        Database(repository / "metadata.sqlite3")


def test_database_and_sidecar_symlinks_are_rejected(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "db" / "metadata.sqlite3"
    database_path.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("unchanged", encoding="utf-8")
    database_path.symlink_to(target)

    with pytest.raises(StorageError):
        Database(database_path)

    database_path.unlink()
    database = Database(database_path)
    sidecar_target = tmp_path / "sidecar-target"
    sidecar_target.write_text("unchanged", encoding="utf-8")
    Path(f"{database_path}-wal").symlink_to(sidecar_target)

    with pytest.raises(StorageError, match="could not open the private metadata database"):
        database.connect()

    assert sidecar_target.read_text(encoding="utf-8") == "unchanged"


def test_path_preparation_error_is_redacted(tmp_path: Path) -> None:
    blocked_parent = tmp_path / "private" / "db"
    database = Database(blocked_parent / "metadata.sqlite3")
    blocked_parent.parent.mkdir()
    blocked_parent.write_text("not a directory", encoding="utf-8")

    with pytest.raises(StorageError) as caught:
        database.connect()

    assert str(caught.value) == "could not open the private metadata database"
    assert str(tmp_path) not in str(caught.value)


def test_connection_is_closed_when_configuration_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenConnection:
        row_factory = None
        closed = False

        def execute(self, sql: str) -> None:
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("synthetic configuration failure")

        def close(self) -> None:
            self.closed = True

    connection = BrokenConnection()
    monkeypatch.setattr(database_module.sqlite3, "connect", lambda *args, **kwargs: connection)

    with pytest.raises(StorageError, match="could not open the private metadata database"):
        Database(tmp_path / "private" / "db" / "metadata.sqlite3").connect()

    assert connection.closed


FIRST = Migration(
    1,
    "first",
    """
    CREATE TABLE parent (
        parent_key INTEGER PRIMARY KEY,
        value TEXT NOT NULL
    ) STRICT;
    """,
)
SECOND = Migration(
    2,
    "second",
    """
    CREATE TABLE child (
        child_key INTEGER PRIMARY KEY,
        parent_key INTEGER NOT NULL REFERENCES parent(parent_key)
    ) STRICT;
    """,
)


def test_migration_forward_backup_permissions_foreign_keys_and_wal(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "db" / "metadata.sqlite3"
    database = Database(database_path)
    assert MigrationRunner(database, migrations=(FIRST,)).migrate() == 1
    with database.transaction() as connection:
        connection.execute("INSERT INTO parent(parent_key, value) VALUES (1, 'synthetic')")

    runner = MigrationRunner(database, migrations=(FIRST, SECOND))
    assert runner.migrate() == 2

    backups = list(runner.backup_directory.iterdir())
    assert len(backups) == 1
    assert _mode(database_path) == 0o600
    assert _mode(database_path.parent) == 0o700
    assert _mode(runner.backup_directory) == 0o700
    assert _mode(backups[0]) == 0o600
    with sqlite3.connect(backups[0]) as backup:
        assert backup.execute("SELECT value FROM parent").fetchone() == ("synthetic",)

    connection = database.connect()
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("INSERT INTO child(child_key, parent_key) VALUES (1, 999)")
    finally:
        connection.close()


def test_migration_splits_same_line_sql_and_keeps_trigger_body_together(tmp_path: Path) -> None:
    database = Database(tmp_path / "private" / "db" / "metadata.sqlite3")
    same_line = Migration(
        1,
        "same_line",
        "CREATE TABLE event(value TEXT); CREATE TABLE audit(value TEXT); "
        "CREATE TRIGGER record_event AFTER INSERT ON event BEGIN "
        "INSERT INTO audit VALUES ('first;value'); INSERT INTO audit VALUES (NEW.value); END;",
    )

    assert MigrationRunner(database, migrations=(same_line,)).migrate() == 1
    with database.transaction() as connection:
        connection.execute("INSERT INTO event VALUES ('second')")
    with database.connect() as connection:
        values = [row[0] for row in connection.execute("SELECT value FROM audit ORDER BY rowid")]
    assert values == ["first;value", "second"]


def test_migration_checksum_mismatch_is_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "private" / "db" / "metadata.sqlite3")
    MigrationRunner(database, migrations=(FIRST,)).migrate()
    changed = Migration(1, "first", "CREATE TABLE parent(parent_key INTEGER PRIMARY KEY);")

    with pytest.raises(MigrationError, match="checksum mismatch") as caught:
        MigrationRunner(database, migrations=(changed,)).migrate()

    assert caught.value.backup_reference is None


def test_migration_rejects_linked_backup_directory_without_chmod_target(tmp_path: Path) -> None:
    database = Database(tmp_path / "private" / "db" / "metadata.sqlite3")
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    backup_directory = tmp_path / "linked-backups"
    backup_directory.symlink_to(outside, target_is_directory=True)

    with pytest.raises(MigrationError, match="migration backup path is unsafe"):
        MigrationRunner(database, migrations=(FIRST,), backup_directory=backup_directory)

    assert _mode(outside) == 0o755


def test_failed_upgrade_rolls_back_and_exposes_safe_backup_reference(tmp_path: Path) -> None:
    traced_sql: list[str] = []

    class TracingDatabase(Database):
        def connect(self) -> sqlite3.Connection:
            connection = super().connect()
            connection.set_trace_callback(traced_sql.append)
            return connection

    database = TracingDatabase(tmp_path / "private" / "db" / "metadata.sqlite3")
    MigrationRunner(database, migrations=(FIRST,)).migrate()
    broken = Migration(
        2,
        "broken",
        "CREATE TABLE transient(value TEXT) STRICT; INSERT INTO missing(value) VALUES ('x');",
    )
    runner = MigrationRunner(database, migrations=(FIRST, broken))

    with pytest.raises(MigrationError) as caught:
        runner.migrate()

    reference = caught.value.backup_reference
    assert reference is not None
    assert Path(reference).name == reference
    assert str(tmp_path) not in str(caught.value)
    assert (runner.backup_directory / reference).is_file()
    with database.connect() as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migration")]
    assert "transient" not in tables
    assert versions == [1]
    assert any(statement.startswith("CREATE TABLE transient") for statement in traced_sql)


def test_concurrent_migration_runners_serialize_upgrade(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "db" / "metadata.sqlite3"
    database = Database(database_path)
    MigrationRunner(database, migrations=(FIRST,)).migrate()
    barrier = Barrier(2)

    def migrate() -> int:
        barrier.wait()
        return MigrationRunner(Database(database_path), migrations=(FIRST, SECOND)).migrate()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate(), range(2)))

    assert results == [2, 2]
    with database.connect() as connection:
        versions = [row[0] for row in connection.execute("SELECT version FROM schema_migration")]
    assert versions == [1, 2]
    assert len(list((database_path.parent / "backups").iterdir())) == 1


def test_database_files_are_owner_only_even_with_permissive_umask(tmp_path: Path) -> None:
    database_path = tmp_path / "private" / "db" / "metadata.sqlite3"
    previous_umask = os.umask(0)
    try:
        connection = Database(database_path).connect()
        connection.execute("CREATE TABLE synthetic(value TEXT)")
        sidecars = [
            path
            for suffix in ("-wal", "-shm")
            if (path := Path(f"{database_path}{suffix}")).exists()
        ]
        assert sidecars
        assert all(_mode(path) == 0o600 for path in sidecars)
        connection.close()
    finally:
        os.umask(previous_umask)

    assert _mode(database_path) == 0o600
    assert _mode(database_path.parent) == 0o700
