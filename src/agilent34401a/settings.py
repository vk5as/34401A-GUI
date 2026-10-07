"""Application settings stored as JSON in the platform's config folder."""

import contextlib
import json
import logging
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from agilent34401a.backend import Backend
from agilent34401a.connection import ConnectionSettings

_LOG = logging.getLogger(__name__)

APP_FOLDER = "agilent34401a"
SETTINGS_FILE = "settings.json"
_VERSION = 1

_MIN_HISTORY = 1
_MAX_HISTORY = 1_000_000
_DEFAULT_HISTORY = 10_000
_MIN_ERROR_INTERVAL_S = 1.0
_MAX_ERROR_INTERVAL_S = 3600.0
_DEFAULT_ERROR_INTERVAL_S = 5.0


class Theme(Enum):
    """How the window is coloured: Light and Dark are built in, System follows the native look."""

    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


class XAxis(Enum):
    """What the chart plots Readings against."""

    TIME = "time"
    SAMPLE = "sample"


@dataclass(frozen=True)
class LastConnection:
    """The Connection to offer again at the next start: the Simulator, or a Meter on a Backend and resource."""

    simulate: bool
    connection: ConnectionSettings


def config_dir(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    """Return the platform's per-user config folder for this application (APPDATA on Windows, XDG elsewhere)."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path.home() if home is None else home
    if platform == "win32":
        base = Path(environ["APPDATA"]) if environ.get("APPDATA") else home / "AppData" / "Roaming"
    else:
        xdg = environ.get("XDG_CONFIG_HOME", "")
        base = Path(xdg) if xdg and Path(xdg).is_absolute() else home / ".config"
    return base / APP_FOLDER


@dataclass
class Settings:
    """What the user has chosen, and persists between runs. Build with `load`; `save` writes it back."""

    path: Path | None = field(default=None, compare=False)
    theme: Theme = Theme.SYSTEM
    compact_mode: bool = False
    chart_x_axis: XAxis = XAxis.TIME
    last_connection: LastConnection | None = None
    auto_reconnect: bool = False
    _history_length: int = field(default=_DEFAULT_HISTORY, repr=False)
    _error_check_interval_s: float = field(default=_DEFAULT_ERROR_INTERVAL_S, repr=False)

    @property
    def history_length(self) -> int:
        """How many Readings the History keeps."""
        return self._history_length

    @history_length.setter
    def history_length(self, length: int) -> None:
        if not _MIN_HISTORY <= length <= _MAX_HISTORY:
            message = f"History length must be {_MIN_HISTORY} to {_MAX_HISTORY}, got {length}"
            raise ValueError(message)
        self._history_length = length

    @property
    def error_check_interval_s(self) -> float:
        """Seconds between status byte checks for queued Meter errors while Readings are being taken."""
        return self._error_check_interval_s

    @error_check_interval_s.setter
    def error_check_interval_s(self, seconds: float) -> None:
        if not _MIN_ERROR_INTERVAL_S <= seconds <= _MAX_ERROR_INTERVAL_S:
            message = f"The error check interval must be {_MIN_ERROR_INTERVAL_S:g} to {_MAX_ERROR_INTERVAL_S:g} s, got {seconds}"
            raise ValueError(message)
        self._error_check_interval_s = float(seconds)

    @classmethod
    def in_memory(cls, path: Path | None = None) -> "Settings":
        """Return default settings that are saved to `path`, or not at all when it is None."""
        return cls(path=path)

    @classmethod
    def load(cls, directory: Path | None = None) -> "Settings":
        """Read the settings in `directory` (the platform config folder by default).

        A missing, unreadable or corrupt file gives the defaults, and an invalid value gives that one setting's
        default, so a hand-edited file can never stop the application starting.
        """
        path = (config_dir() if directory is None else directory) / SETTINGS_FILE
        settings = cls(path=path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return settings
        except (OSError, ValueError):
            _LOG.warning("Could not read %s; using the default settings", path)
            return settings
        if not isinstance(data, dict):
            _LOG.warning("%s does not hold a JSON object; using the default settings", path)
            return settings
        settings._apply(data)
        return settings

    def save(self) -> None:
        """Write the settings to disk atomically; does nothing for in-memory settings."""
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".settings-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(self._to_json(), file, indent=2)
            Path(temporary).replace(self.path)
        except BaseException:
            with contextlib.suppress(OSError):
                Path(temporary).unlink()
            raise

    def _to_json(self) -> dict[str, Any]:
        connection = self.last_connection
        return {
            "version": _VERSION,
            "theme": self.theme.value,
            "compact_mode": self.compact_mode,
            "history_length": self.history_length,
            "chart_x_axis": self.chart_x_axis.value,
            "auto_reconnect": self.auto_reconnect,
            "error_check_interval_s": self.error_check_interval_s,
            "last_connection": (
                None
                if connection is None
                else {
                    "simulate": connection.simulate,
                    "backend": connection.connection.backend.value,
                    "resource": connection.connection.resource,
                    "gpib_board": connection.connection.gpib_board,
                    "gpib_address": connection.connection.gpib_address,
                }
            ),
        }

    def _apply(self, data: Mapping[str, Any]) -> None:
        """Take every valid value from `data`, leaving the default for each invalid or missing one."""
        with contextlib.suppress(ValueError):
            self.theme = Theme(data.get("theme"))
        with contextlib.suppress(ValueError):
            self.chart_x_axis = XAxis(data.get("chart_x_axis"))
        if isinstance(data.get("compact_mode"), bool):
            self.compact_mode = data["compact_mode"]
        if isinstance(data.get("auto_reconnect"), bool):
            self.auto_reconnect = data["auto_reconnect"]
        history = data.get("history_length")
        if isinstance(history, int) and not isinstance(history, bool):
            with contextlib.suppress(ValueError):
                self.history_length = history
        interval = data.get("error_check_interval_s")
        if isinstance(interval, int | float) and not isinstance(interval, bool):
            with contextlib.suppress(ValueError):
                self.error_check_interval_s = interval
        self.last_connection = _read_connection(data.get("last_connection"))


def _read_connection(data: object) -> LastConnection | None:
    if not isinstance(data, dict):
        return None
    try:
        resource = data.get("resource")
        connection = ConnectionSettings(
            backend=Backend(data["backend"]),
            resource=resource if isinstance(resource, str) else None,
            gpib_board=int(data["gpib_board"]),
            gpib_address=int(data["gpib_address"]),
        )
        simulate = data["simulate"]
    except (KeyError, ValueError, TypeError):
        return None
    return LastConnection(simulate=simulate, connection=connection) if isinstance(simulate, bool) else None
