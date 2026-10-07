import os
import tkinter as tk

import pytest

from agilent34401a import __version__
from agilent34401a.gui.app import create_window, main


def _create_window_or_skip() -> tk.Tk:
    try:
        return create_window()
    except tk.TclError:
        # CI always has a display (xvfb on Linux), so a missing one there is a failure, not a skip.
        if os.environ.get("CI"):
            raise
        pytest.skip("no display available")


def test_window_is_titled_with_the_version_and_can_be_destroyed():
    root = _create_window_or_skip()

    try:
        assert __version__ in root.title()
    finally:
        root.destroy()


def test_running_the_app_shows_a_window_and_returns_success_when_it_is_closed(monkeypatch):
    shown_titles = []

    def close_immediately(self, _n=0):
        shown_titles.append(self.title())
        self.destroy()

    _create_window_or_skip().destroy()
    monkeypatch.setattr(tk.Tk, "mainloop", close_immediately)

    assert main([]) == 0
    assert shown_titles == [f"Agilent 34401A {__version__}"]


def test_version_flag_prints_the_version_and_exits_successfully(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["--version"])

    assert exit_info.value.code == 0
    assert __version__ in capsys.readouterr().out
