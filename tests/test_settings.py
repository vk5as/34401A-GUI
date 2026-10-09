import json
from pathlib import Path

import pytest

from agilent34401a.backend import Backend
from agilent34401a.connection import ConnectionSettings
from agilent34401a.serial_config import FlowControl, Framing, Parity, SerialSettings, Terminator
from agilent34401a.settings import SETTINGS_FILE, LastConnection, Settings, Theme, XAxis, config_dir


def test_defaults_match_the_spec():
    settings = Settings.in_memory()

    assert settings.theme is Theme.SYSTEM
    assert settings.compact_mode is False
    assert settings.history_length == 10_000
    assert settings.chart_x_axis is XAxis.TIME
    assert settings.last_connection is None
    assert settings.auto_reconnect is False
    assert settings.error_check_interval_s == 5.0


def test_loading_from_a_folder_with_no_settings_file_gives_the_defaults(tmp_path):
    assert Settings.load(tmp_path) == Settings.in_memory(tmp_path / SETTINGS_FILE)


def test_settings_survive_a_save_and_load(tmp_path):
    settings = Settings.load(tmp_path)
    settings.theme = Theme.DARK
    settings.compact_mode = True
    settings.history_length = 500
    settings.chart_x_axis = XAxis.SAMPLE
    settings.auto_reconnect = True
    settings.error_check_interval_s = 2.5
    settings.last_connection = LastConnection(
        simulate=False,
        connection=ConnectionSettings(backend=Backend.PYVISA_PY, resource=None, gpib_board=1, gpib_address=7),
    )

    settings.save()

    assert Settings.load(tmp_path) == settings


def test_the_simulator_can_be_the_remembered_connection(tmp_path):
    settings = Settings.load(tmp_path)
    settings.last_connection = LastConnection(simulate=True, connection=ConnectionSettings())

    settings.save()

    assert Settings.load(tmp_path).last_connection == LastConnection(simulate=True, connection=ConnectionSettings())


def test_saving_creates_the_folder_and_writes_readable_json(tmp_path):
    folder = tmp_path / "not" / "yet"
    settings = Settings.load(folder)
    settings.theme = Theme.LIGHT

    settings.save()

    data = json.loads((folder / SETTINGS_FILE).read_text(encoding="utf-8"))
    assert data["theme"] == "light"
    assert data["version"] == 1


def test_saving_leaves_no_temporary_files_behind(tmp_path):
    settings = Settings.load(tmp_path)

    settings.save()
    settings.save()

    assert [path.name for path in tmp_path.iterdir()] == [SETTINGS_FILE]


def test_settings_made_in_memory_never_touch_the_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings.in_memory()
    settings.theme = Theme.DARK

    settings.save()

    assert list(tmp_path.iterdir()) == []


def test_a_corrupt_file_gives_the_defaults_instead_of_crashing(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text("{not json", encoding="utf-8")

    assert Settings.load(tmp_path).theme is Theme.SYSTEM


@pytest.mark.parametrize("content", ["[]", '"text"', "42", "null"])
def test_a_file_that_is_not_an_object_gives_the_defaults(tmp_path, content):
    (tmp_path / SETTINGS_FILE).write_text(content, encoding="utf-8")

    assert Settings.load(tmp_path).history_length == 10_000


def test_a_bad_value_falls_back_to_its_default_without_losing_the_good_ones(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(
        json.dumps(
            {
                "version": 1,
                "theme": "purple",
                "compact_mode": "yes",
                "history_length": -5,
                "chart_x_axis": "sample",
                "auto_reconnect": True,
                "error_check_interval_s": 0,
                "last_connection": {"simulate": False, "backend": "nonsense"},
            }
        ),
        encoding="utf-8",
    )

    settings = Settings.load(tmp_path)

    assert settings.theme is Theme.SYSTEM
    assert settings.compact_mode is False
    assert settings.history_length == 10_000
    assert settings.chart_x_axis is XAxis.SAMPLE
    assert settings.auto_reconnect is True
    assert settings.error_check_interval_s == 5.0
    assert settings.last_connection is None


def test_a_file_from_a_newer_version_is_read_for_the_fields_it_shares(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(json.dumps({"version": 99, "theme": "dark", "future": 1}), encoding="utf-8")

    assert Settings.load(tmp_path).theme is Theme.DARK


def test_unreadable_files_give_the_defaults(tmp_path):
    (tmp_path / SETTINGS_FILE).mkdir()  # a folder where the file should be

    assert Settings.load(tmp_path).theme is Theme.SYSTEM


@pytest.mark.parametrize("length", [0, -1, 1_000_001])
def test_a_history_length_outside_the_allowed_range_is_rejected(length):
    settings = Settings.in_memory()

    with pytest.raises(ValueError, match="History length"):
        settings.history_length = length


@pytest.mark.parametrize("interval", [0, -1, 0.1, 3601])
def test_an_error_check_interval_outside_the_allowed_range_is_rejected(interval):
    settings = Settings.in_memory()

    with pytest.raises(ValueError, match="interval"):
        settings.error_check_interval_s = interval


def test_windows_keeps_settings_under_appdata():
    folder = config_dir(environ={"APPDATA": r"C:\Users\me\AppData\Roaming"}, platform="win32", home=Path("/home/me"))

    assert folder == Path(r"C:\Users\me\AppData\Roaming") / "agilent34401a"


def test_windows_without_appdata_falls_back_to_the_home_folder():
    folder = config_dir(environ={}, platform="win32", home=Path("/home/me"))

    assert folder == Path("/home/me") / "AppData" / "Roaming" / "agilent34401a"


def test_linux_follows_xdg_config_home():
    folder = config_dir(environ={"XDG_CONFIG_HOME": "/xdg"}, platform="linux", home=Path("/home/me"))

    assert folder == Path("/xdg") / "agilent34401a"


def test_linux_without_xdg_uses_dot_config():
    folder = config_dir(environ={}, platform="linux", home=Path("/home/me"))

    assert folder == Path("/home/me") / ".config" / "agilent34401a"


def test_a_relative_xdg_config_home_is_ignored_as_the_xdg_spec_says():
    folder = config_dir(environ={"XDG_CONFIG_HOME": "relative/path"}, platform="linux", home=Path("/home/me"))

    assert folder == Path("/home/me") / ".config" / "agilent34401a"


def test_a_windows_style_xdg_config_home_is_relative_on_linux_whatever_the_host_os():
    # Absoluteness follows the requested platform's rules, not the rules of the machine running the test.
    folder = config_dir(environ={"XDG_CONFIG_HOME": "C:\\xdg"}, platform="linux", home=Path("/home/me"))

    assert folder == Path("/home/me") / ".config" / "agilent34401a"


def test_a_relative_appdata_is_ignored_on_windows_whatever_the_host_os():
    folder = config_dir(environ={"APPDATA": "relative"}, platform="win32", home=Path("/home/me"))

    assert folder == Path("/home/me") / "AppData" / "Roaming" / "agilent34401a"


def test_tests_are_isolated_from_the_users_real_config_folder(tmp_path):
    assert tmp_path in config_dir().parents


def test_loading_settings_without_a_folder_uses_the_platform_config_folder():
    settings = Settings.load()
    settings.theme = Theme.DARK
    settings.save()

    assert Settings.load(config_dir()).theme is Theme.DARK
    assert (config_dir() / SETTINGS_FILE).exists()


def test_a_failed_save_raises_and_leaves_no_temporary_file(tmp_path):
    (tmp_path / SETTINGS_FILE).mkdir()  # the file cannot be replaced by a folder's contents
    settings = Settings.load(tmp_path)

    with pytest.raises(OSError):  # noqa: PT011 - the exact error differs between platforms
        settings.save()

    assert [path.name for path in tmp_path.iterdir()] == [SETTINGS_FILE]


def test_the_serial_settings_of_the_remembered_connection_survive_a_restart(tmp_path):
    settings = Settings.load(tmp_path)
    serial = SerialSettings(
        port="/dev/ttyUSB1",
        baud=2400,
        framing=Framing(7, Parity.ODD, 1),
        flow_control=FlowControl.RTS_CTS,
        terminator=Terminator.CRLF,
        dtr=True,
        rts=False,
    )
    settings.last_connection = LastConnection(simulate=False, connection=ConnectionSettings(serial=serial))

    settings.save()

    assert Settings.load(tmp_path).last_connection == settings.last_connection


def test_serial_settings_that_cannot_be_read_back_leave_no_remembered_connection(tmp_path):
    (tmp_path / SETTINGS_FILE).write_text(
        json.dumps(
            {
                "last_connection": {
                    "simulate": False,
                    "backend": "py",
                    "resource": None,
                    "gpib_board": 0,
                    "gpib_address": 22,
                    "serial": {"port": "COM1", "baud": 1234},
                }
            }
        ),
        encoding="utf-8",
    )

    assert Settings.load(tmp_path).last_connection is None
