"""Release helper: the version and tag come from pyproject.toml, and the built files must agree with them.

The Release workflow uses this so the tag is never typed by hand and a stale ``dist/`` can never be published
under a newer version. Subcommands::

    release_info.py version                 prints 0.1.0
    release_info.py tag                     prints v0.1.0
    release_info.py check-dist dist          fails unless dist/ holds exactly the wheel and sdist of that version
"""

import argparse
import re
import sys
from collections.abc import Sequence
from pathlib import Path

import tomllib

_PACKAGE = "agilent34401a"
_DEFAULT_PYPROJECT = Path("pyproject.toml")
# A conservative PEP 440 subset (release segment plus optional pre/post/dev); enough for a safe tag name.
_VERSION = re.compile(r"\d+(\.\d+)+((a|b|rc)\d+)?(\.post\d+)?(\.dev\d+)?")


class ReleaseError(Exception):
    """The release cannot proceed; the message says why."""


def read_version(pyproject: Path) -> str:
    try:
        with pyproject.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as error:
        message = f"Cannot read {pyproject}: {error}"
        raise ReleaseError(message) from error
    version = data.get("project", {}).get("version")
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        message = f"{pyproject} has no usable [project] version (found {version!r})"
        raise ReleaseError(message)
    return version


def release_tag(version: str) -> str:
    return f"v{version}"


def find_distributions(directory: Path, version: str) -> tuple[Path, Path]:
    """Return (wheel, sdist), requiring exactly those two files, both for ``version``."""
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    expected_wheel = f"{_PACKAGE}-{version}-py3-none-any.whl"
    expected_sdist = f"{_PACKAGE}-{version}.tar.gz"
    if [wheel.name for wheel in wheels] != [expected_wheel]:
        message = f"Expected exactly one wheel, {expected_wheel}, in {directory}; found {[w.name for w in wheels]}"
        raise ReleaseError(message)
    if [sdist.name for sdist in sdists] != [expected_sdist]:
        message = f"Expected exactly one sdist, {expected_sdist}, in {directory}; found {[s.name for s in sdists]}"
        raise ReleaseError(message)
    return wheels[0], sdists[0]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Release helper for the Release workflow.")
    parser.add_argument("--pyproject", type=Path, default=_DEFAULT_PYPROJECT, help="Path to pyproject.toml")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("version", help="Print the version from pyproject.toml")
    subparsers.add_parser("tag", help="Print the release tag (v<version>)")
    check = subparsers.add_parser("check-dist", help="Check that a directory holds the wheel and sdist of the version")
    check.add_argument("directory", type=Path)
    # The shared option is also accepted after the subcommand, as the tests and the workflow write it.
    for sub in subparsers.choices.values():
        sub.add_argument("--pyproject", type=Path, default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    args = parser.parse_args(argv)
    try:
        version = read_version(args.pyproject)
        if args.command == "version":
            sys.stdout.write(f"{version}\n")
        elif args.command == "tag":
            sys.stdout.write(f"{release_tag(version)}\n")
        else:
            wheel, sdist = find_distributions(args.directory, version)
            sys.stdout.write(f"Distributions match version {version}: {wheel.name}, {sdist.name}\n")
    except ReleaseError as error:
        sys.stderr.write(f"release_info: error: {error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
