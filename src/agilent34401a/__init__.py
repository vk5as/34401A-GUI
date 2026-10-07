"""Remote control for the Agilent/HP 34401A digital multimeter over GPIB or RS-232."""

import importlib.metadata


def _read_version() -> str:
    # A frozen or source-tree run may have no installed metadata; don't crash at import for that.
    try:
        return importlib.metadata.version("agilent34401a")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


__version__ = _read_version()
