"""Private runtime-root discovery and permission-safe directory creation."""

from __future__ import annotations

import json
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

RUNTIME_ROOT_ENV = "NTULEARN_DATA_DIR"
RUNTIME_CONFIG_NAME = "config.json"
_RUNTIME_CONFIG_MAX_BYTES = 65_536


class RuntimePathError(ValueError):
    """Raised when runtime data would cross the public repository boundary."""


def find_repository_root(start: Path) -> Path | None:
    """Find a Git checkout/worktree root without invoking Git."""

    current = start.expanduser().resolve()
    if current.is_file():
        current = current.parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / ".git").is_symlink():
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


def _git_boundary_output(repository: Path, *arguments: str, data: bytes | None = None) -> bytes:
    # Caller Git overrides must not redirect discovery, the index, or ignore configuration.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", os.fspath(repository), *arguments],
            input=data,
            capture_output=True,
            env=environment,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimePathError("runtime Git privacy boundary could not be verified") from None
    allowed_codes = (0, 1) if arguments[0] == "check-ignore" else (0,)
    if result.returncode not in allowed_codes:
        raise RuntimePathError("runtime Git privacy boundary could not be verified")
    return result.stdout


def _verify_git_boundary(repository: Path, path: Path) -> None:
    top_level = _git_boundary_output(repository, "rev-parse", "--show-toplevel")
    if top_level.rstrip(b"\n") != os.fsencode(repository):
        raise RuntimePathError("runtime Git privacy boundary could not be verified")
    relative = path.relative_to(repository).as_posix()
    tracked = _git_boundary_output(
        repository, "ls-files", "--cached", "-z", "--", f":(top,literal){relative}"
    )
    if tracked:
        raise RuntimePathError("runtime data location contains Git-tracked content")
    result = _git_boundary_output(
        repository,
        "check-ignore",
        "--no-index",
        "--verbose",
        "-z",
        "--stdin",
        data=os.fsencode(relative) + b"\0",
    )
    fields = result.split(b"\0")
    ignored = len(fields) == 5 and bool(fields[2]) and not fields[2].startswith(b"!")
    if path.exists() and ignored:
        return
    # Git cannot infer that a nonexistent final component is a directory. Accept
    # its slash form only for a literal directory rule, never a child wildcard
    # ("runtime/*" also matches "runtime/" and can reinclude individual children).
    if not path.exists():
        result = _git_boundary_output(
            repository,
            "check-ignore",
            "--no-index",
            "--verbose",
            "-z",
            "--stdin",
            data=os.fsencode(relative + "/") + b"\0",
        )
        fields = result.split(b"\0")
        if len(fields) == 5:
            pattern = fields[2]
            if (
                pattern
                and not pattern.startswith(b"!")
                and (
                    ignored
                    or (
                        pattern.endswith(b"/")
                        and not any(character in pattern for character in (b"*", b"?", b"[", b"\\"))
                    )
                )
            ):
                return
    raise RuntimePathError("runtime data location is not verifiably Git-ignored")


def validate_private_path(path: Path, *, explicit: bool = True) -> Path:
    """Resolve a private path while rejecting tracked-repository aliases."""

    lexical_path = _absolute_without_resolving(path)
    resolved_path = lexical_path.resolve()
    repositories = {
        parent
        for candidate in (lexical_path, resolved_path)
        for parent in (candidate, *candidate.parents)
        if (parent / ".git").exists() or (parent / ".git").is_symlink()
    }
    for repository in sorted(repositories):
        local_root = repository / ".local"
        if not explicit or not _is_relative_to(lexical_path, local_root):
            raise RuntimePathError("runtime data may not use a tracked repository path")
        if local_root.is_symlink():
            raise RuntimePathError("runtime data may not use a linked repository path")
        if not _is_relative_to(resolved_path, local_root.resolve()):
            raise RuntimePathError("runtime data may not escape the private local directory")
        _verify_git_boundary(repository, resolved_path)
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


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def _runtime_root_from_host_config(home: Path) -> str | None:
    config_path = home / ".ntulearn-skill" / RUNTIME_CONFIG_NAME
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(config_path, flags)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise RuntimePathError("runtime root configuration could not be read") from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimePathError("runtime root configuration must be a regular file")
        if metadata.st_size > _RUNTIME_CONFIG_MAX_BYTES:
            raise RuntimePathError("runtime root configuration is too large")
        if stat.S_IMODE(metadata.st_mode) & 0o077:
            raise RuntimePathError("runtime root configuration permissions are not private")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            raw = stream.read(_RUNTIME_CONFIG_MAX_BYTES + 1)
    except (OSError, UnicodeError) as error:
        raise RuntimePathError("runtime root configuration could not be read") from error
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if len(raw.encode("utf-8")) > _RUNTIME_CONFIG_MAX_BYTES:
        raise RuntimePathError("runtime root configuration is too large")
    try:
        payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys)
    except (json.JSONDecodeError, ValueError) as error:
        raise RuntimePathError("runtime root configuration is invalid") from error
    if not isinstance(payload, dict) or set(payload) != {"runtime_root"}:
        raise RuntimePathError("runtime root configuration is invalid")
    configured = payload.get("runtime_root")
    if not isinstance(configured, str) or not configured.strip():
        raise RuntimePathError("runtime root configuration is invalid")
    if not Path(configured).is_absolute():
        raise RuntimePathError("configured runtime root must be an absolute path")
    return configured


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
    if configured is None:
        configured = _runtime_root_from_host_config(home_directory)
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
