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
