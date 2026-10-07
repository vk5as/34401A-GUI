"""Command-line interface (`agilent34401a-cli`)."""

import argparse
from collections.abc import Sequence

from agilent34401a import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agilent34401a-cli",
        description="Remote control for the Agilent/HP 34401A digital multimeter.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.parse_args(argv)
    parser.print_help()
    return 0
