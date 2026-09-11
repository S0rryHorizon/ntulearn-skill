#!/usr/bin/env python3
"""Fail closed on private paths, likely credentials, and unsafe distributions."""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from collections.abc import Iterable
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024

PRIVATE_PARTS = frozenset(
    {
        ".git",
        ".local",
        ".mypy_cache",
        ".private",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "auth",
        "cache",
        "credentials",
        "courses",
        "data",
        "db",
        "downloads",
        "exports",
        "indexes",
        "logs",
        "objects",
        "recon",
        "request-dumps",
        "tmp",
        "venv",
    }
)
PRIVATE_SUFFIXES = (
    ".bin",
    ".cookie",
    ".cookies",
    ".docx",
    ".har",
    ".log",
    ".pdf",
    ".pptx",
    ".pyc",
    ".pyo",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".sqlite-wal",
    ".sqlite-shm",
    ".sqlite-journal",
    ".sqlite3-wal",
    ".sqlite3-shm",
    ".sqlite3-journal",
    ".db-wal",
    ".db-shm",
    ".db-journal",
)
PRIVATE_FILENAMES = frozenset(
    {
        ".env",
        "auth.json",
        "cookies.json",
        "credentials.json",
        "secrets.json",
        "session.json",
    }
)
SECRET_PATTERNS = (
    ("private key", re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("GitHub token", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b")),
    ("AWS access key", re.compile(rb"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    (
        "authorization bearer value",
        re.compile(rb"(?im)^\s*authorization\s*:\s*bearer\s+[A-Za-z0-9._~+/=-]{20,}\s*$"),
    ),
)


class AuditError(ValueError):
    """A public-file invariant was violated."""


def _safe_path(raw_name: str, *, strip_root: bool = False) -> PurePosixPath:
    if "\\" in raw_name or raw_name.startswith("/"):
        raise AuditError("unsafe archive path")
    path = PurePosixPath(raw_name)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise AuditError("unsafe archive path")
    if strip_root:
        if len(path.parts) < 2:
            raise AuditError("sdist member has no project root")
        path = PurePosixPath(*path.parts[1:])
    return path


def _check_public_path(path: PurePosixPath) -> None:
    lowered_parts = tuple(part.casefold() for part in path.parts)
    if any(part in PRIVATE_PARTS for part in lowered_parts):
        raise AuditError("private path is not public")
    lowered_name = path.name.casefold()
    if lowered_name in PRIVATE_FILENAMES or (
        lowered_name.startswith(".env.") and lowered_name != ".env.example"
    ):
        raise AuditError("private configuration filename is not public")
    if lowered_name.endswith(PRIVATE_SUFFIXES):
        raise AuditError("private artifact type is not public")


def _check_content(label: str, content: bytes) -> None:
    if len(content) > MAX_MEMBER_BYTES:
        raise AuditError("oversized public file")
    if b"\0" in content:
        raise AuditError("unexpected binary content")
    try:
        content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AuditError("public text is not valid UTF-8") from exc
    for pattern_name, pattern in SECRET_PATTERNS:
        if pattern.search(content):
            raise AuditError(f"possible {pattern_name} in public content")


def _validate_public_url(raw_url: object) -> None:
    if not isinstance(raw_url, str):
        raise AuditError("dependency URL must be a string")
    try:
        parsed = urlsplit(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise AuditError("malformed dependency URL in uv.lock") from exc
    permitted = (
        parsed.scheme == "https"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
        and not parsed.query
        and not parsed.fragment
        and (
            (
                parsed.hostname == "pypi.org"
                and (parsed.path == "/simple" or parsed.path.startswith("/simple/"))
            )
            or (
                parsed.hostname == "files.pythonhosted.org" and parsed.path.startswith("/packages/")
            )
        )
    )
    if not permitted:
        raise AuditError("non-public or credentialed dependency URL in uv.lock")


def _validate_locked_archive(value: object) -> None:
    if not isinstance(value, dict) or set(value) - {"url", "hash", "size", "upload-time"}:
        raise AuditError("unexpected dependency archive record in uv.lock")
    _validate_public_url(value.get("url"))
    digest = value.get("hash")
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise AuditError("dependency archive lacks a SHA-256 hash in uv.lock")


def _check_lock_sources(root: Path) -> None:
    lock_path = root / "uv.lock"
    if not lock_path.is_file():
        raise AuditError("uv.lock is required")
    document = tomllib.loads(lock_path.read_text(encoding="utf-8"))
    packages = document.get("package")
    if not isinstance(packages, list) or not packages:
        raise AuditError("uv.lock has no package records")
    for package in packages:
        if not isinstance(package, dict):
            raise AuditError("unexpected package record in uv.lock")
        name = package.get("name")
        source = package.get("source")
        if not isinstance(source, dict):
            raise AuditError("dependency source is missing from uv.lock")
        if set(source) == {"registry"}:
            _validate_public_url(source["registry"])
        elif name == "ntulearn-skill" and source == {"editable": "."}:
            pass
        else:
            raise AuditError("non-registry dependency source in uv.lock")
        if "sdist" in package:
            _validate_locked_archive(package["sdist"])
        wheels = package.get("wheels", [])
        if not isinstance(wheels, list):
            raise AuditError("unexpected dependency wheels record in uv.lock")
        for wheel in wheels:
            _validate_locked_archive(wheel)


def tracked_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(root / os.fsdecode(value) for value in result.stdout.split(b"\0") if value)


def _checked_tracked_path(root: Path, path: Path) -> tuple[Path, PurePosixPath]:
    candidate = path if path.is_absolute() else root / path
    try:
        lexical_relative = candidate.relative_to(root)
    except ValueError as exc:
        raise AuditError("tracked path escapes the repository") from exc
    current = root
    for part in lexical_relative.parts:
        current = current / part
        if current.is_symlink():
            raise AuditError("tracked symlink requires manual review")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError) as exc:
        raise AuditError("tracked path cannot be resolved inside the repository") from exc
    return resolved, PurePosixPath(lexical_relative.as_posix())


def audit_tracked(root: Path, paths: Iterable[Path] | None = None) -> int:
    selected = tuple(paths if paths is not None else tracked_paths(root))
    for path in selected:
        resolved, relative = _checked_tracked_path(root, path)
        _check_public_path(relative)
        _check_content(str(relative), resolved.read_bytes())
    _check_lock_sources(root)
    return len(selected)


def _check_wheel_member(path: PurePosixPath) -> None:
    if path.parts[0] == "ntulearn_skill":
        if path.suffix not in {".py", ".sql", ".typed"}:
            raise AuditError("unexpected wheel package member")
        return
    if len(path.parts) >= 2 and path.parts[0].startswith("ntulearn_skill-"):
        if ".dist-info" not in path.parts[0]:
            raise AuditError("unexpected wheel metadata directory")
        return
    raise AuditError("unexpected wheel member")


def _check_sdist_member(path: PurePosixPath) -> None:
    allowed_files = {
        ".env.example",
        ".gitignore",
        "AGENTS.md",
        "CONTRIBUTING.md",
        "CONTRIBUTING.zh-CN.md",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "README.zh-CN.md",
        "SECURITY.md",
        "SECURITY.zh-CN.md",
        "pyproject.toml",
        "uv.lock",
    }
    allowed_roots = {".github", "docs", "examples", "scripts", "skills", "src", "tests"}
    if len(path.parts) == 1 and path.name in allowed_files:
        return
    if path.parts[0] in allowed_roots:
        return
    raise AuditError("unexpected sdist member")


def audit_wheel(path: Path) -> int:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise AuditError("oversized wheel")
    required = {"METADATA", "WHEEL", "RECORD", "entry_points.txt"}
    found_metadata: set[str] = set()
    count = 0
    total_size = 0
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise AuditError("wheel link requires manual review")
            member = _safe_path(info.filename)
            _check_public_path(member)
            _check_wheel_member(member)
            if info.file_size > MAX_MEMBER_BYTES:
                raise AuditError("oversized wheel member")
            total_size += info.file_size
            if total_size > MAX_ARCHIVE_BYTES:
                raise AuditError("oversized expanded wheel")
            content = archive.read(info)
            _check_content(f"{path.name}:{member}", content)
            if ".dist-info" in member.parts[0]:
                found_metadata.add(member.name)
                if member.name == "METADATA":
                    if b"Requires-Python: >=3.11" not in content:
                        raise AuditError("wheel metadata has unexpected Requires-Python")
                elif member.name == "entry_points.txt":
                    if b"ntulearn = ntulearn_skill.cli:main" not in content:
                        raise AuditError("wheel is missing the ntulearn console entry point")
            count += 1
    missing = required - found_metadata
    if missing:
        raise AuditError(f"wheel metadata files missing: {', '.join(sorted(missing))}")
    return count


def audit_sdist(path: Path) -> int:
    if path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise AuditError("oversized sdist")
    count = 0
    total_size = 0
    roots: set[str] = set()
    with tarfile.open(path, mode="r:gz") as archive:
        for info in archive.getmembers():
            if info.issym() or info.islnk():
                raise AuditError("sdist link requires manual review")
            if not info.isfile():
                continue
            raw = _safe_path(info.name)
            roots.add(raw.parts[0])
            member = _safe_path(info.name, strip_root=True)
            _check_public_path(member)
            _check_sdist_member(member)
            if info.size > MAX_MEMBER_BYTES:
                raise AuditError("oversized sdist member")
            total_size += info.size
            if total_size > MAX_ARCHIVE_BYTES:
                raise AuditError("oversized expanded sdist")
            extracted = archive.extractfile(info)
            if extracted is None:
                raise AuditError("cannot inspect sdist member")
            _check_content(f"{path.name}:{member}", extracted.read())
            count += 1
    if len(roots) != 1:
        raise AuditError("sdist must have exactly one project root")
    return count


def audit_artifacts(dist: Path) -> tuple[int, int]:
    wheels = sorted(dist.glob("*.whl"))
    sdists = sorted(dist.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise AuditError("expected exactly one wheel and one .tar.gz sdist")
    return audit_wheel(wheels[0]), audit_sdist(sdists[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--tracked", action="store_true")
    parser.add_argument("--artifacts", type=Path)
    args = parser.parse_args(argv)
    if not args.tracked and args.artifacts is None:
        parser.error("select --tracked and/or --artifacts DIST")
    try:
        if args.tracked:
            count = audit_tracked(args.root.resolve())
            print(f"public tracked-file audit: PASS ({count} files)")
        if args.artifacts is not None:
            wheel_count, sdist_count = audit_artifacts(args.artifacts.resolve())
            print(
                "public distribution audit: PASS "
                f"({wheel_count} wheel files, {sdist_count} sdist files)"
            )
    except Exception:
        # Paths and parser errors may contain private bytes. Keep CLI diagnostics bounded.
        print("public artifact audit: FAIL (details withheld)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
