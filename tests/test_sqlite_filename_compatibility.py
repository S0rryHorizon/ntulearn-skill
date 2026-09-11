"""Filename selection uses portable SQLite correlation and preserves evidence scope."""

from __future__ import annotations

import sqlite3

import pytest

from ntulearn_skill.index.fts import _FILENAME_SQL


@pytest.mark.parametrize(
    ("version_key", "observations", "expected"),
    [
        (1, [], ""),
        (1, [(1, 1, 1, "exact.pdf"), (2, None, 2, "fallback.pdf")], "exact.pdf"),
        (1, [(1, 2, 3, "other.pdf"), (2, None, 2, "fallback.pdf")], "fallback.pdf"),
        (1, [(1, 2, 3, "other.pdf")], ""),
        (None, [(1, 1, 1, "versioned.pdf"), (2, None, 2, "latest.pdf")], "latest.pdf"),
        (None, [(1, None, 1, "fallback.pdf"), (2, 2, 2, "latest.pdf")], "latest.pdf"),
        (1, [(1, 1, 3, "latest.pdf"), (2, 1, 2, "older.pdf")], "latest.pdf"),
        (1, [(1, 1, 2, "first.pdf"), (2, 1, 2, "second.pdf")], "second.pdf"),
        (1, [(1, None, 2, "first.pdf"), (2, None, 2, "second.pdf")], "second.pdf"),
        (None, [(1, None, 2, "first.pdf"), (2, 2, 2, "second.pdf")], "second.pdf"),
        (1, [(1, 1, 1, ""), (2, None, 2, "fallback.pdf")], ""),
    ],
)
def test_filename_selection(
    version_key: int | None,
    observations: list[tuple[int, int | None, int, str]],
    expected: str,
) -> None:
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            """
            CREATE TABLE resource (resource_key INTEGER PRIMARY KEY);
            CREATE TABLE resource_version (version_key INTEGER PRIMARY KEY);
            CREATE TABLE resource_observation (
                observation_key INTEGER PRIMARY KEY,
                resource_key INTEGER,
                version_key INTEGER,
                observed_at INTEGER,
                original_filename TEXT NOT NULL
            );
            INSERT INTO resource VALUES (1);
            INSERT INTO resource_version VALUES (1), (2);
            """
        )
        connection.executemany(
            "INSERT INTO resource_observation VALUES (?, 1, ?, ?, ?)", observations
        )
        # An unrelated resource must never supply a filename, even if its evidence is newer.
        connection.execute(
            "INSERT INTO resource_observation VALUES (999, 2, ?, 999, 'unrelated.pdf')",
            (version_key,),
        )
        result = connection.execute(
            f"""SELECT {_FILENAME_SQL} FROM resource r
            LEFT JOIN resource_version v ON v.version_key = ?""",
            (version_key,),
        ).fetchone()
        assert result == (expected,)
