import pytest

from agilent34401a.sim import TIME_SCALE_ENV_VAR


@pytest.fixture(autouse=True)
def _instant_simulator(monkeypatch):
    # Tests never wait on simulated measurement time unless they ask for a time scale explicitly.
    monkeypatch.setenv(TIME_SCALE_ENV_VAR, "0")
