"""The Save dialog every CSV file in the window is chosen with: Recording, Export History and Export Burst."""

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import filedialog


def default_csv_name(stem: str = "34401A") -> str:
    """Return a file name for a CSV file made now, such as `34401A-20261009-153000.csv` (a local time, as people expect)."""
    return f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.csv"  # noqa: DTZ005 - a local time in a file name


def ask_csv_file(parent: tk.Misc, title: str, stem: str = "34401A") -> Path | None:
    """Ask where to save a CSV file, suggesting a name that starts with `stem`; None when the user cancels."""
    name = filedialog.asksaveasfilename(
        parent=parent,
        title=title,
        initialfile=default_csv_name(stem),
        defaultextension=".csv",
        filetypes=[("CSV file", "*.csv")],
    )
    return Path(name) if name else None
