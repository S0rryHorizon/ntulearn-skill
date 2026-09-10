"""R1 privacy boundary regressions use only disposable synthetic Git repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ntulearn_skill.storage.paths import RuntimePathError, validate_private_path


def _git(root: Path, *arguments: str) -> None:
    subprocess.run(["git", "-C", str(root), *arguments], check=True, capture_output=True)


def _repository(tmp_path: Path, rules: str) -> Path:
    root = tmp_path / "synthetic"
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text(rules, encoding="utf-8")
    return root


@pytest.mark.parametrize("rules", [".local/\n", ".local/runtime/\n", ".local/**\n"])
def test_effective_ignore_accepts_new_and_existing_runtime(tmp_path: Path, rules: str) -> None:
    root = _repository(tmp_path, rules)
    runtime = root / ".local" / "runtime"
    assert validate_private_path(runtime) == runtime
    runtime.mkdir(parents=True)
    (runtime / "synthetic.txt").write_text("synthetic", encoding="utf-8")
    assert validate_private_path(runtime) == runtime
    assert validate_private_path(runtime / "db" / "metadata.sqlite3") == (
        runtime / "db" / "metadata.sqlite3"
    )


@pytest.mark.parametrize(
    "rules",
    [
        "",
        ".local/*\n!.local/runtime/\n",
        ".local/runtime/*\n!.local/runtime/exposed.txt\n",
    ],
)
def test_absent_or_negated_directory_ignore_is_rejected(tmp_path: Path, rules: str) -> None:
    root = _repository(tmp_path, rules)
    runtime = root / ".local" / "runtime"
    for existing in (False, True):
        if existing:
            runtime.mkdir(parents=True)
            (runtime / "exposed.txt").write_text("synthetic", encoding="utf-8")
        with pytest.raises(RuntimePathError, match="not verifiably Git-ignored") as error:
            validate_private_path(runtime)
        assert str(root) not in str(error.value)


@pytest.mark.parametrize("deleted", [False, True])
def test_tracked_runtime_descendant_is_rejected_even_when_ignored(
    tmp_path: Path,
    deleted: bool,
) -> None:
    root = _repository(tmp_path, ".local/\n")
    runtime = root / ".local" / "runtime"
    runtime.mkdir(parents=True)
    data = runtime / "synthetic.txt"
    data.write_text("synthetic", encoding="utf-8")
    _git(root, "add", "-f", str(data))
    if deleted:
        data.unlink()
    with pytest.raises(RuntimePathError, match="Git-tracked content"):
        validate_private_path(runtime)


def test_git_environment_cannot_redirect_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repository(tmp_path, "")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.excludesFile")
    excludes = tmp_path / "injected-ignore"
    excludes.write_text(".local/\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", str(excludes))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "false-index"))
    with pytest.raises(RuntimePathError, match="not verifiably Git-ignored"):
        validate_private_path(root / ".local" / "runtime")


def test_git_failure_is_sanitized(tmp_path: Path) -> None:
    root = tmp_path / "synthetic"
    (root / ".git").mkdir(parents=True)
    with pytest.raises(RuntimePathError, match="could not be verified") as error:
        validate_private_path(root / ".local" / "runtime")
    assert str(root) not in str(error.value)


def test_worktree_git_file_is_supported(tmp_path: Path) -> None:
    root = _repository(tmp_path, ".local/\n")
    _git(root, "add", ".gitignore")
    _git(
        root,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-qm",
        "synthetic",
    )
    worktree = tmp_path / "synthetic-worktree"
    _git(root, "worktree", "add", "-q", "--detach", str(worktree))
    assert (worktree / ".git").is_file()
    assert validate_private_path(worktree / ".local" / "runtime") == worktree / ".local/runtime"


def test_local_symlink_to_external_directory_is_rejected(tmp_path: Path) -> None:
    root = _repository(tmp_path, ".local/\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".local").symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimePathError, match="linked repository path"):
        validate_private_path(root / ".local" / "runtime")


def test_nested_repository_cannot_hide_outer_tracked_runtime(tmp_path: Path) -> None:
    root = _repository(tmp_path, ".local/\n")
    nested = root / ".local" / "nested"
    nested.mkdir(parents=True)
    runtime = nested / ".local" / "runtime"
    runtime.mkdir(parents=True)
    data = runtime / "synthetic.txt"
    data.write_text("synthetic", encoding="utf-8")
    _git(root, "add", "-f", str(data))
    _git(nested, "init", "-q")
    (nested / ".gitignore").write_text(".local/\n", encoding="utf-8")
    with pytest.raises(RuntimePathError, match="Git-tracked content"):
        validate_private_path(runtime)


def test_database_file_only_ignore_does_not_protect_sidecars(tmp_path: Path) -> None:
    from ntulearn_skill.storage.database import Database, StorageError

    root = _repository(tmp_path, ".local/runtime/db/metadata.sqlite3\n")
    with pytest.raises(StorageError, match="database path is unsafe"):
        Database(root / ".local/runtime/db/metadata.sqlite3")


def test_database_ignored_directory_preserves_private_usage(tmp_path: Path) -> None:
    from ntulearn_skill.storage.database import Database

    root = _repository(tmp_path, ".local/runtime/\n")
    database = Database(root / ".local/runtime/db/metadata.sqlite3")
    with database.connect() as connection:
        assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_dangling_git_marker_is_not_treated_as_outside_repository(tmp_path: Path) -> None:
    root = tmp_path / "synthetic"
    root.mkdir()
    (root / ".git").symlink_to(root / "missing-git-directory", target_is_directory=True)
    with pytest.raises(RuntimePathError, match="could not be verified"):
        validate_private_path(root / ".local/runtime")
