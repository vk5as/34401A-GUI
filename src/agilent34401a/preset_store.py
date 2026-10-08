"""The Preset store: the user's Presets, kept as JSON in the platform config folder beside the settings.

The store keeps its Presets in memory and writes the whole file after every change, atomically, so a crash or a full
disk leaves the old file. A file that cannot be used is copied to `presets.json.bad` before anything replaces it, so
that a damaged or newer file is never lost to the next save. Presets can be exported to and imported from any file.
"""

import contextlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agilent34401a.errors import PresetError
from agilent34401a.meter import Setup
from agilent34401a.preset import Preset, PresetFile, clean_name, presets_from_json, presets_to_json
from agilent34401a.settings import config_dir

_LOG = logging.getLogger(__name__)

PRESETS_FILE = "presets.json"
_DAMAGED_SUFFIX = ".bad"


class Collision(Enum):
    """What an import does with a Preset whose name the store already has."""

    RENAME = "rename"  # keep both: the imported one gets a number after its name
    REPLACE = "replace"  # the imported one takes the place of the one in the store
    SKIP = "skip"  # keep the one in the store


@dataclass(frozen=True)
class ImportReport:
    """What an import did. `added` holds the names as they now are in the store, `renamed` the old and new names."""

    added: tuple[str, ...] = ()
    replaced: tuple[str, ...] = ()
    skipped: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()
    problems: tuple[str, ...] = ()


class PresetStore:
    """The Presets, in the order the user made them. Build with `load` (or `in_memory`); every change is saved at once."""

    def __init__(self, path: Path | None = None, presets: Iterable[Preset] = (), problems: Iterable[str] = ()) -> None:
        self.path = path
        self._presets = tuple(presets)
        self.problems: tuple[str, ...] = tuple(problems)
        """What was wrong with the file when it was loaded: a file that could not be used, or Presets in it that were left out."""

    @classmethod
    def in_memory(cls) -> "PresetStore":
        """Return an empty store that is never written to disk."""
        return cls()

    @classmethod
    def load(cls, directory: Path | None = None) -> "PresetStore":
        """Read the Presets in `directory` (the platform config folder by default).

        A missing file is an empty store. A file that cannot be used gives an empty store with a problem to show the
        user, and what could be used of a damaged one is kept; either way the file is copied first (see the module).
        """
        path = (config_dir() if directory is None else directory) / PRESETS_FILE
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(path)
        except (OSError, UnicodeDecodeError) as error:
            return cls(path, problems=[f"Could not read {path}: {error}"])
        try:
            read = _parse(text, path.name)
        except PresetError as error:
            _keep_a_copy(path)
            return cls(path, problems=[f"{path} was not used. {error}"])
        if read.problems:
            _keep_a_copy(path)
        return cls(path, read.presets, read.problems)

    @property
    def presets(self) -> tuple[Preset, ...]:
        return self._presets

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(preset.name for preset in self._presets)

    def __contains__(self, name: str) -> bool:
        return self._find(name) is not None

    def get(self, name: str) -> Preset:
        """Return the Preset called `name` (the case does not matter), or raise `PresetError`."""
        return self._presets[self._index(name)]

    def save(self, name: str, setup: Setup, *, replace: bool = False) -> Preset:
        """Store `setup` as a Preset called `name`. A name in use is a `PresetError` unless `replace` says to take it over."""
        name = clean_name(name)
        preset = Preset(name, setup)
        existing = self._find(name)
        presets = list(self._presets)
        if existing is None:
            presets.append(preset)
        elif replace:
            presets[existing] = preset
        else:
            raise _in_use(self._presets[existing].name)
        self._commit(presets)
        return preset

    def rename(self, name: str, new_name: str) -> None:
        """Give the Preset called `name` another name, keeping its Setup and its place."""
        index = self._index(name)
        new_name = clean_name(new_name)
        clash = self._find(new_name)
        if clash is not None and clash != index:
            raise _in_use(self._presets[clash].name)
        presets = list(self._presets)
        presets[index] = Preset(new_name, presets[index].setup)
        self._commit(presets)

    def delete(self, name: str) -> None:
        """Remove the Preset called `name`."""
        index = self._index(name)
        self._commit([preset for position, preset in enumerate(self._presets) if position != index])

    def export_file(self, path: Path, names: Iterable[str] | None = None) -> None:
        """Write the Presets called `names` (all of them by default) to `path` as a Presets file."""
        chosen = self._presets if names is None else tuple(self.get(name) for name in names)
        _write(path, presets_to_json(chosen), make_folder=False)

    def collisions_with(self, path: Path) -> tuple[str, ...]:
        """Return the names, as the file has them, that importing `path` would find already in use."""
        return tuple(preset.name for preset in _read_file(path).presets if preset.name in self)

    def import_file(self, path: Path, collision: Collision = Collision.RENAME) -> ImportReport:
        """Add the Presets in the Presets file at `path`, settling names already in use as `collision` says.

        A file that is not a usable Presets file is a `PresetError` and changes nothing. A Preset in it that is wrong
        is left out and described in the report's `problems`.
        """
        read = _read_file(path)
        incoming = {preset.name.casefold() for preset in read.presets}
        presets = list(self._presets)
        added: list[str] = []
        replaced: list[str] = []
        skipped: list[str] = []
        renamed: list[tuple[str, str]] = []
        for preset in read.presets:
            clash = _position(presets, preset.name)
            if clash is None:
                presets.append(preset)
                added.append(preset.name)
            elif collision is Collision.REPLACE:
                presets[clash] = preset
                replaced.append(preset.name)
            elif collision is Collision.SKIP:
                skipped.append(preset.name)
            else:
                name = _free_name(preset.name, {other.name.casefold() for other in presets} | incoming)
                presets.append(Preset(name, preset.setup))
                added.append(name)
                renamed.append((preset.name, name))
        self._commit(presets)
        return ImportReport(tuple(added), tuple(replaced), tuple(skipped), tuple(renamed), read.problems)

    def _find(self, name: str) -> int | None:
        return _position(self._presets, name)

    def _index(self, name: str) -> int:
        index = self._find(name)
        if index is None:
            message = f"There is no Preset called '{name}'"
            raise PresetError(message)
        return index

    def _commit(self, presets: list[Preset]) -> None:
        """Write `presets` to the file and only then make them the store's, so a failed write changes nothing."""
        if self.path is not None:
            _write(self.path, presets_to_json(presets), make_folder=True)
        self._presets = tuple(presets)


def _position(presets: Iterable[Preset], name: str) -> int | None:
    folded = name.strip().casefold()
    return next((index for index, preset in enumerate(presets) if preset.name.casefold() == folded), None)


def _in_use(name: str) -> PresetError:
    message = f"There is already a Preset called '{name}'"
    return PresetError(message)


def _free_name(name: str, taken: set[str]) -> str:
    number = 2
    while f"{name} ({number})".casefold() in taken:
        number += 1
    return f"{name} ({number})"


def _parse(text: str, where: str) -> PresetFile:
    try:
        data = json.loads(text)
    except ValueError as error:
        message = f"{where} is not valid JSON ({error})"
        raise PresetError(message) from error
    return presets_from_json(data)


def _read_file(path: Path) -> PresetFile:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        message = f"Could not read {path}: {error}"
        raise PresetError(message) from error
    try:
        return _parse(text, path.name)
    except PresetError as error:
        message = f"{path.name} cannot be imported. {error}"
        raise PresetError(message) from error


def _keep_a_copy(path: Path) -> None:
    """Copy a file that is about to be replaced, because the program could not use all of it."""
    copy = path.with_name(path.name + _DAMAGED_SUFFIX)
    try:
        shutil.copy2(path, copy)
    except OSError:
        _LOG.warning("Could not keep a copy of %s as %s", path, copy)


def _write(path: Path, document: dict[str, object], *, make_folder: bool) -> None:
    """Write `document` to `path` through a temporary file in the same folder, so the file is whole or unchanged."""
    temporary = None
    try:
        if make_folder:
            path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=".presets-", suffix=".tmp")
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(document, file, indent=2, ensure_ascii=False)
        Path(temporary).replace(path)
    except OSError as error:
        if temporary is not None:
            with contextlib.suppress(OSError):
                Path(temporary).unlink()
        message = f"Could not write {path}: {error}"
        raise PresetError(message) from error
