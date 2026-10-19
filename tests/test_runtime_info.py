"""Installation diagnostics must work without opening a private course library."""

from __future__ import annotations

import io
import json
import platform
import sys
from importlib import metadata
from pathlib import Path
from typing import NoReturn

import pytest

import ntulearn_skill
from ntulearn_skill import client
from ntulearn_skill.cli import run
from ntulearn_skill.cli._main import EXIT_COMPLETE, EXIT_USAGE
from ntulearn_skill.core.api import CoreService
from ntulearn_skill.storage.paths import RuntimePaths


@pytest.fixture(autouse=True)
def forbid_private_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        raise AssertionError("runtime-info must not access private runtime state")

    monkeypatch.setattr(CoreService, "from_runtime", forbidden)
    monkeypatch.setattr(RuntimePaths, "discover", forbidden)
    monkeypatch.setattr(client.BrowserCaptureProvider, "from_manifest", forbidden)
    monkeypatch.setattr(client, "prepare_browser_capture_directory", forbidden)
    monkeypatch.setattr(client, "write_browser_capture_manifest", forbidden)


@pytest.mark.parametrize("arguments", [["runtime-info", "--json"], ["--json", "runtime-info"]])
def test_runtime_info_json_reports_the_active_installation(arguments: list[str]) -> None:
    output = io.StringIO()

    assert run(arguments, stdout=output) == EXIT_COMPLETE
    assert json.loads(output.getvalue()) == {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "module_path": ntulearn_skill.__file__,
        "package_version": metadata.version("ntulearn-skill"),
        "browser_capture_helpers": {
            "prepare_browser_capture_directory": True,
            "write_browser_capture_manifest": True,
        },
    }


def test_runtime_info_does_not_read_or_create_supplied_private_paths(tmp_path: Path) -> None:
    root = tmp_path / "unopened-library"
    capture = tmp_path / "unread-capture.json"
    output = io.StringIO()

    assert (
        run(
            ["runtime-info", "--json", "--root", str(root), "--browser-capture", str(capture)],
            stdout=output,
        )
        == EXIT_COMPLETE
    )
    assert not root.exists()
    assert not capture.exists()
    assert str(root) not in output.getvalue()
    assert str(capture) not in output.getvalue()


@pytest.mark.parametrize("arguments", [["runtime-info", "--python"], ["runtime-info", "--json"]])
def test_runtime_info_preserves_the_venv_interpreter_path(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    interpreter = tmp_path / "synthetic-venv" / "bin" / "python"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(sys.executable)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    output = io.StringIO()

    assert run(arguments, stdout=output) == EXIT_COMPLETE
    if "--python" in arguments:
        assert output.getvalue() == f"{interpreter}\n"
    else:
        assert json.loads(output.getvalue())["python_executable"] == str(interpreter)


def test_runtime_info_human_output_contains_installation_and_helpers() -> None:
    output = io.StringIO()

    assert run(["runtime-info"], stdout=output) == EXIT_COMPLETE
    assert f"Python executable: {sys.executable}" in output.getvalue()
    assert f"Python version: {platform.python_version()}" in output.getvalue()
    assert f"Module path: {ntulearn_skill.__file__}" in output.getvalue()
    assert f"Package version: {metadata.version('ntulearn-skill')}" in output.getvalue()
    assert "prepare_browser_capture_directory: available" in output.getvalue()
    assert "write_browser_capture_manifest: available" in output.getvalue()


def test_runtime_info_reports_missing_metadata_and_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing_version(_name: str) -> NoReturn:
        raise metadata.PackageNotFoundError("ntulearn-skill")

    monkeypatch.setattr(metadata, "version", missing_version)
    monkeypatch.delattr(client, "prepare_browser_capture_directory")
    monkeypatch.setattr(client, "write_browser_capture_manifest", None)
    output = io.StringIO()

    assert run(["runtime-info", "--json"], stdout=output) == EXIT_COMPLETE
    payload = json.loads(output.getvalue())
    assert payload["package_version"] is None
    assert payload["browser_capture_helpers"] == {
        "prepare_browser_capture_directory": False,
        "write_browser_capture_manifest": False,
    }


def test_runtime_info_rejects_conflicting_output_modes() -> None:
    output = io.StringIO()

    assert run(["runtime-info", "--python", "--json"], stdout=output) == EXIT_USAGE
    payload = json.loads(output.getvalue())
    assert payload["command"] == "runtime-info"
    assert payload["errors"][0]["code"] == "CLI_INVALID_ARGUMENTS"
