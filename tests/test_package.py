import importlib.metadata

import pytest

import agilent34401a


def test_version_matches_installed_metadata():
    assert agilent34401a.__version__ == importlib.metadata.version("agilent34401a")


def test_version_falls_back_when_package_metadata_is_unavailable(monkeypatch):
    def missing(_name):
        raise importlib.metadata.PackageNotFoundError

    monkeypatch.setattr(importlib.metadata, "version", missing)

    assert agilent34401a._read_version() == "0+unknown"


@pytest.mark.parametrize(
    ("script", "target"),
    [
        ("agilent34401a-gui", "agilent34401a.gui.app:main"),
        ("agilent34401a-cli", "agilent34401a.cli:main"),
        ("agilent34401a-sim", "agilent34401a.sim_server:main"),
    ],
)
def test_console_script_is_registered(script, target):
    scripts = {ep.name: ep.value for ep in importlib.metadata.entry_points(group="console_scripts")}

    assert scripts[script] == target
