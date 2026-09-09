"""Private runtime-root discovery and permission-safe directory creation."""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RUNTIME_ROOT_ENV = "NTULEARN_DATA_DIR"


class RuntimePathError(ValueError):
    """Raised when runtime data would cross the public repository boundary."""


def find_repository_root(start: Path) -> Path | None:
    """Find a Git checkout/worktree root without invoking Git."""

    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _absolute_without_resolving(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path.expanduser())))


def validate_private_path(path: Path, *, explicit: bool = True) -> Path:
    """Resolve a private path while rejecting tracked-repository aliases."""

    lexical_path = _absolute_without_resolving(path)
    resolved_path = lexical_path.resolve()
    repository = find_repository_root(resolved_path)
    if repository is None:
        return resolved_path

    local_root = repository / ".local"
    if not explicit or not _is_relative_to(lexical_path, local_root):
        raise RuntimePathError("runtime data may not use a tracked repository path")
    if local_root.is_symlink():
        raise RuntimePathError("runtime data may not use a linked repository path")
    if not _is_relative_to(resolved_path, local_root.resolve()):
        raise RuntimePathError("runtime data may not escape the private local directory")
    return resolved_path


def ensure_private_directory(directory: Path, *, boundary: Path | None = None) -> None:
    """Create an owner-only directory without following a final symlink."""

    if directory.is_symlink():
        raise RuntimePathError("private directory may not be a symbolic link")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    metadata = directory.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise RuntimePathError("private directory is not a real directory")
    resolved = directory.resolve()
    if boundary is not None and not _is_relative_to(resolved, boundary.resolve()):
        raise RuntimePathError("private directory escapes its runtime root")
    os.chmod(directory, 0o700, follow_symlinks=False)


def resolve_runtime_root(
    explicit: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    home: Path | None = None,
) -> Path:
    """Resolve the private root and prevent accidental tracked-path use.

    The default always points to ``~/.ntulearn-skill``. An explicit in-repository
    development root is accepted only below the ignored ``.local`` directory.
    """

    environment = os.environ if environ is None else environ
    working_directory = Path.cwd() if cwd is None else cwd
    home_directory = Path.home() if home is None else home
    environment_value = environment.get(RUNTIME_ROOT_ENV)
    configured = explicit if explicit is not None else environment_value or None
    is_explicit = configured is not None
    candidate = (
        Path(configured).expanduser()
        if configured is not None
        else home_directory / ".ntulearn-skill"
    )
    if not candidate.is_absolute():
        candidate = working_directory / candidate
    return validate_private_path(candidate, explicit=is_explicit)


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Resolved private filesystem layout."""

    root: Path

    def __post_init__(self) -> None:
        lexical_root = _absolute_without_resolving(self.root)
        if lexical_root.is_symlink():
            raise RuntimePathError("runtime root may not be a symbolic link")
        object.__setattr__(self, "root", validate_private_path(lexical_root))

    @classmethod
    def discover(
        cls,
        explicit: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        home: Path | None = None,
    ) -> RuntimePaths:
        return cls(resolve_runtime_root(explicit, environ=environ, cwd=cwd, home=home))

    @property
    def database(self) -> Path:
        return self.root / "db" / "metadata.sqlite3"

    @property
    def backups(self) -> Path:
        return self.root / "db" / "backups"

    def ensure(self) -> RuntimePaths:
        directories = (
            self.root,
            self.root / "config",
            self.root / "auth",
            self.root / "db",
            self.backups,
            self.root / "objects",
            self.root / "courses",
            self.root / "cache",
            self.root / "indexes",
            self.root / "exports",
            self.root / "logs",
            self.root / "tmp",
        )
        for directory in directories:
            ensure_private_directory(directory, boundary=self.root)
        return self
