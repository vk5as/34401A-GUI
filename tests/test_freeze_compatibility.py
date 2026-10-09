"""The code must stay freezable (PyInstaller and friends): no paths relative to source files, resources by package."""

import importlib.resources
import re
from pathlib import Path

import pytest

import agilent34401a
from agilent34401a import cli, sim_server

_PACKAGE_DIRECTORY = Path(agilent34401a.__file__).resolve().parent
_SOURCE_RELATIVE = re.compile(r"__file__|pkg_resources|os\.getcwd|sys\.argv\[0\]")


def test_the_package_never_locates_anything_relative_to_its_own_source_files():
    offenders = [
        f"{path.relative_to(_PACKAGE_DIRECTORY)}:{number}"
        for path in sorted(_PACKAGE_DIRECTORY.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1)
        if _SOURCE_RELATIVE.search(line)
    ]

    assert offenders == []


def test_the_typing_marker_is_reachable_through_package_resources():
    marker = importlib.resources.files("agilent34401a") / "py.typed"

    assert marker.is_file()


@pytest.mark.parametrize("entry_point", [cli.main, sim_server.main], ids=["agilent34401a-cli", "agilent34401a-sim"])
def test_every_command_line_entry_point_prints_help_and_exits_successfully(entry_point, capsys):
    with pytest.raises(SystemExit) as exit_info:
        entry_point(["--help"])

    assert exit_info.value.code == 0
    assert "usage:" in capsys.readouterr().out
