"""Exercise the published example against the real local Core and source provider."""

from __future__ import annotations

import runpy
from pathlib import Path


def test_offline_demo_uses_disposable_synthetic_data(tmp_path: Path, monkeypatch, capsys) -> None:
    fake_home = tmp_path / "unused-home"
    fake_default = tmp_path / "unused-default-runtime"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("NTULEARN_DATA_DIR", str(fake_default))
    example = Path(__file__).resolve().parents[1] / "examples" / "offline_demo.py"

    runpy.run_path(str(example), run_name="__main__")
    first = capsys.readouterr().out
    runpy.run_path(str(example), run_name="__main__")
    second = capsys.readouterr().out

    assert first == second
    assert "physical page 2" in first
    assert "Source text: The spectralneedle marks physical page two" in first
    assert "Repeat sync: version count 1 -> 1" in first
    assert "source coverage=PARTIAL, overall=UNKNOWN" in first
    assert "cannot prove remote absence" in first
    assert str(tmp_path) not in first
    assert not fake_home.exists()
    assert not fake_default.exists()
