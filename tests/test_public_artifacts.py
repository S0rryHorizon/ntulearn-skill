from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

import pytest

from scripts.check_public_artifacts import (
    AuditError,
    _check_content,
    _check_lock_sources,
    _check_public_path,
    audit_sdist,
    audit_tracked,
    audit_wheel,
)


@pytest.mark.parametrize(
    "value",
    [
        PurePosixPath(".git/config"),
        PurePosixPath("release/.private/export.json"),
        PurePosixPath("state.sqlite-wal"),
        PurePosixPath("captures/request.har"),
        PurePosixPath("tests/.env"),
        PurePosixPath("docs/cookies.json"),
        PurePosixPath("tests/.venv/config.json"),
        PurePosixPath("tests/__pycache__/cache.pyc"),
        PurePosixPath("src/payload.bin"),
    ],
)
def test_private_paths_are_rejected(value: PurePosixPath) -> None:
    with pytest.raises(AuditError):
        _check_public_path(value)


def test_likely_secret_is_rejected_without_storing_a_canary() -> None:
    secret = ("gh" + "p_" + "A" * 36).encode()
    with pytest.raises(AuditError, match="GitHub token"):
        _check_content("candidate.txt", secret)


def test_non_utf8_content_is_rejected() -> None:
    with pytest.raises(AuditError, match="not valid UTF-8"):
        _check_content("candidate.txt", b"\xff\xfe")


@pytest.mark.parametrize(
    "source",
    [
        '{ registry = "https://pypi.org/simple#synthetic-canary" }',
        '{ registry = "file:///private/wheelhouse" }',
        '{ git = "https://private.example.invalid/repository" }',
        "{ registry = 'https://pypi.org/simple.evil.invalid' }",
    ],
)
def test_non_public_dependency_source_is_rejected(tmp_path: Path, source: str) -> None:
    (tmp_path / "uv.lock").write_text(
        f'version = 1\n[[package]]\nname = "demo"\nsource = {source}\n',
        encoding="utf-8",
    )
    with pytest.raises(AuditError) as captured:
        _check_lock_sources(tmp_path)
    assert "synthetic-canary" not in str(captured.value)


def test_wheel_requires_expected_members_and_metadata(tmp_path: Path) -> None:
    wheel = tmp_path / "ntulearn_skill-0.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("ntulearn_skill/__init__.py", b"")
        archive.writestr("ntulearn_skill-0.0.0.dist-info/METADATA", b"Requires-Python: >=3.11\n")
        archive.writestr("ntulearn_skill-0.0.0.dist-info/WHEEL", b"Wheel-Version: 1.0\n")
        archive.writestr("ntulearn_skill-0.0.0.dist-info/RECORD", b"")
        archive.writestr(
            "ntulearn_skill-0.0.0.dist-info/entry_points.txt",
            b"[console_scripts]\nntulearn = ntulearn_skill.cli:main\n",
        )
    assert audit_wheel(wheel) == 5


def test_sdist_rejects_links(tmp_path: Path) -> None:
    sdist = tmp_path / "ntulearn_skill-0.0.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        content = b"[project]\nname='ntulearn-skill'\n"
        project = tarfile.TarInfo("ntulearn_skill-0.0.0/pyproject.toml")
        project.size = len(content)
        archive.addfile(project, io.BytesIO(content))
        link = tarfile.TarInfo("ntulearn_skill-0.0.0/docs/latest")
        link.type = tarfile.SYMTYPE
        link.linkname = "../../.local"
        archive.addfile(link)
    with pytest.raises(AuditError, match="link requires manual review"):
        audit_sdist(sdist)


@pytest.mark.parametrize("private", [False, True])
def test_sdist_skill_keeps_private_path_boundary(tmp_path: Path, private: bool) -> None:
    sdist = tmp_path / "ntulearn_skill-0.0.0.tar.gz"
    member = "skills/demo/.local/session.json" if private else "skills/demo/SKILL.md"
    with tarfile.open(sdist, "w:gz") as archive:
        content = b"Synthetic skill instructions.\n"
        entry = tarfile.TarInfo(f"ntulearn_skill-0.0.0/{member}")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    if private:
        with pytest.raises(AuditError):
            audit_sdist(sdist)
    else:
        assert audit_sdist(sdist) == 1


def test_tracked_parent_symlink_cannot_escape_repository(tmp_path: Path) -> None:
    root = tmp_path / "repository"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "payload.txt").write_text("private", encoding="utf-8")
    (root / "docs").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AuditError, match="symlink requires manual review"):
        audit_tracked(root, [root / "docs" / "payload.txt"])


@pytest.mark.parametrize("name", ["README.zh-CN.md", "CONTRIBUTING.zh-CN.md", "SECURITY.zh-CN.md"])
@pytest.mark.parametrize("secret", [False, True])
def test_translated_sdist_documents_keep_content_checks(
    tmp_path: Path, name: str, secret: bool
) -> None:
    sdist = tmp_path / "ntulearn_skill-0.0.0.tar.gz"
    content = ("gh" + "p_" + "A" * 36).encode() if secret else "虚构文档示例。\n".encode()
    with tarfile.open(sdist, "w:gz") as archive:
        entry = tarfile.TarInfo(f"ntulearn_skill-0.0.0/{name}")
        entry.size = len(content)
        archive.addfile(entry, io.BytesIO(content))
    if secret:
        with pytest.raises(AuditError, match="GitHub token"):
            audit_sdist(sdist)
    else:
        assert audit_sdist(sdist) == 1
