"""Regression tests for gui/tray_app.py's frozen-vs-script path resolution.

Covers a real bug: under a PyInstaller-frozen build, sys.executable is the GUI .exe
itself (not a Python interpreter), so naively using it to launch grid_join.py just
relaunched the GUI instead of starting the worker. Needs the optional `gui` extras
(pystray, Pillow); skipped entirely if absent, since the base test environment does
not install them.
"""
import importlib
from pathlib import Path
import sys

import pytest

pytest.importorskip("pystray")
pytest.importorskip("PIL")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "gui"))


def _reload(monkeypatch, *, frozen: bool, executable: str):
    if frozen:
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys, "executable", executable)
    sys.modules.pop("tray_app", None)
    return importlib.import_module("tray_app")


def test_script_mode_uses_sys_executable_directly(monkeypatch):
    module = _reload(monkeypatch, frozen=False, executable=r"C:\venv\Scripts\python.exe")
    assert module.venv_python() == r"C:\venv\Scripts\python.exe"


def test_frozen_mode_finds_project_venv_not_itself(monkeypatch, tmp_path):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("")
    exe = tmp_path / "dist" / "CommunityAIGridWorker.exe"
    exe.parent.mkdir(parents=True)

    module = _reload(monkeypatch, frozen=True, executable=str(exe))

    assert module.ROOT == tmp_path
    assert module.venv_python() == str(venv_python)
    # Autostart should launch the exe directly; the worker subprocess, in contrast,
    # must use the real interpreter (checked above), never sys.executable as-is.
    assert module._autostart_command() == f'"{exe}"'


def test_frozen_mode_raises_clear_error_if_venv_missing(monkeypatch, tmp_path):
    exe = tmp_path / "dist" / "CommunityAIGridWorker.exe"
    exe.parent.mkdir(parents=True)

    module = _reload(monkeypatch, frozen=True, executable=str(exe))

    with pytest.raises(FileNotFoundError):
        module.venv_python()


def test_write_then_read_limits_round_trips(monkeypatch, tmp_path):
    module = _reload(monkeypatch, frozen=False, executable=sys.executable)
    monkeypatch.setattr(module, "LIMITS_PATH", tmp_path / "nested" / "limits.json")
    values = {"context_tokens": 2048, "cpu_threads": 4, "gpu_layers": 0, "max_output_tokens": 64}
    module.write_limits(values)
    assert module.read_limits() == values


@pytest.mark.parametrize("content", ["not json", "[1, 2, 3]"])
def test_read_limits_is_empty_for_invalid_file(monkeypatch, tmp_path, content):
    module = _reload(monkeypatch, frozen=False, executable=sys.executable)
    path = tmp_path / "limits.json"
    path.write_text(content)
    monkeypatch.setattr(module, "LIMITS_PATH", path)
    assert module.read_limits() == {}


def test_read_limits_is_empty_for_missing_file(monkeypatch, tmp_path):
    module = _reload(monkeypatch, frozen=False, executable=sys.executable)
    monkeypatch.setattr(module, "LIMITS_PATH", tmp_path / "missing.json")
    assert module.read_limits() == {}


def test_resolve_effective_limits_precedence(monkeypatch):
    module = _reload(monkeypatch, frozen=False, executable=sys.executable)
    bundle = {"CAG_OLLAMA_NUM_CTX": "4096", "CAG_OLLAMA_NUM_THREAD": "3"}
    limits = {"cpu_threads": 7}
    resolved = module.resolve_effective_limits(bundle, limits)
    assert resolved["cpu_threads"] == 7        # limits.json wins over the bundle field
    assert resolved["context_tokens"] == 4096  # bundle field wins when no override exists
    assert resolved["gpu_layers"] == 0         # hardcoded default when neither is present
    assert resolved["max_output_tokens"] == 32
