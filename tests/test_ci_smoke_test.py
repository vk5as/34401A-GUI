"""scripts/ci_smoke_test.py is the Windows release gate, so it must run the window the way the real application does."""

import gc
import importlib.util
import sys
import tkinter as tk
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "ci_smoke_test.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ci_smoke_test", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["ci_smoke_test"] = module
    spec.loader.exec_module(module)
    return module


ci_smoke_test = _load_script()


def test_the_smoke_test_builds_the_window_with_automatic_collection_off_and_puts_it_back(tk_root, monkeypatch):
    """ADR-0008: a Tk object must not be finalised on the Worker thread, which the cycle collector could do."""
    seen = []
    real_window = ci_smoke_test.MainWindow

    def noting_window(*args, **kwargs):
        seen.append(gc.isenabled())
        return real_window(*args, **kwargs)

    # The shared Tk interpreter stands in for the script's own root, as in every other GUI test.
    monkeypatch.setattr(ci_smoke_test, "tk", SimpleNamespace(Tk=lambda: tk.Toplevel(tk_root)))
    monkeypatch.setattr(ci_smoke_test, "MainWindow", noting_window)
    was_enabled = gc.isenabled()
    gc.enable()
    try:
        code = ci_smoke_test.main([])
        after = gc.isenabled()
    finally:
        if not was_enabled:
            gc.disable()

    assert code == 0
    assert seen == [False]
    assert after is True


def test_the_smoke_test_leaves_automatic_collection_alone_when_it_was_already_off(tk_root, monkeypatch):
    monkeypatch.setattr(ci_smoke_test, "tk", SimpleNamespace(Tk=lambda: tk.Toplevel(tk_root)))
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        code = ci_smoke_test.main([])
        after = gc.isenabled()
    finally:
        if was_enabled:
            gc.enable()

    assert code == 0
    assert after is False


def test_the_smoke_test_still_refuses_a_source_tree_when_asked_for_an_installed_wheel(capsys):
    if "site-packages" in Path(ci_smoke_test.agilent34401a.__file__).parts:
        pytest.skip("this run is against an installed wheel")

    assert ci_smoke_test.main(["--installed"]) == 1
    assert "not from an installed wheel" in capsys.readouterr().err
