"""Installation diagnostics that never discover or initialize private runtime state."""

from __future__ import annotations

import json
import platform
import sys
from importlib import metadata

import ntulearn_skill
from ntulearn_skill import client


def render_runtime_info(*, json_output: bool) -> str:
    """Describe the process running the CLI, without resolving away its venv path."""

    try:
        package_version = metadata.version("ntulearn-skill")
    except metadata.PackageNotFoundError:
        package_version = None
    helpers = {
        name: callable(getattr(client, name, None))
        for name in (
            "prepare_browser_capture_directory",
            "write_browser_capture_manifest",
        )
    }
    payload = {
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "module_path": ntulearn_skill.__file__,
        "package_version": package_version,
        "browser_capture_helpers": helpers,
    }
    if json_output:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    lines = [
        f"Python executable: {sys.executable}",
        f"Python version: {platform.python_version()}",
        f"Module path: {ntulearn_skill.__file__}",
        f"Package version: {package_version or 'not installed'}",
    ]
    lines.extend(
        f"{name}: {'available' if available else 'missing'}" for name, available in helpers.items()
    )
    return "\n".join(lines)
