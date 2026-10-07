import socket

import pytest

from agilent34401a.sim import TIME_SCALE_ENV_VAR


@pytest.fixture(autouse=True)
def _instant_simulator(monkeypatch):
    # Tests never wait on simulated measurement time unless they ask for a time scale explicitly.
    monkeypatch.setenv(TIME_SCALE_ENV_VAR, "0")


@pytest.fixture
def unused_port() -> int:
    """A TCP port that was free a moment ago, so connecting to it is refused."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


@pytest.fixture(autouse=True)
def _isolated_config_folder(monkeypatch, tmp_path):
    """Point the platform config folder at a temporary one, so no test can read or write the user's settings."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
