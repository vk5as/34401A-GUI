"""The Chart tab: a live plot of the History, a statistics strip, and export to PNG or SVG.

The tab keeps the History itself. Readings are added as they arrive, which is cheap, and the plot is redrawn at
most a few times a second, only if something changed, however fast the Meter or the Simulator is going.
"""

import logging
import math
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Any

from matplotlib.axes import Axes
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk  # type: ignore[attr-defined]
from matplotlib.figure import Figure

from agilent34401a.history import History, HistoryEntry
from agilent34401a.meter import Reading, Resolution, format_reading
from agilent34401a.settings import Settings, XAxis

if TYPE_CHECKING:
    from matplotlib.collections import LineCollection
    from matplotlib.text import Text

    from agilent34401a.gui.main_window import MainWindow
    from agilent34401a.worker import ReadingTaken

_LOG = logging.getLogger(__name__)

_REFRESH_MS = 100
_SLOW_REFRESH_MS = 250  # a long History takes about 60 ms to draw, so it is drawn less often
_LONG_HISTORY = 1000
_NOTHING = "—"
_STATISTICS = ("N", "Mean", "Std dev", "Min", "Max", "Pk-Pk", "OVLD")
_MAX_LENGTH = 1_000_000
_SPINBOX_STEP = 1000
_MARGIN = 0.05  # of the span, left around the data when the view fits it
_MAX_LABELLED_BREAKS = 8  # Break Markers named on the chart; older ones are just lines
_X_LABELS = {XAxis.TIME: "Time (s)", XAxis.SAMPLE: "Sample number"}
_EXPORT_SUFFIXES = (".png", ".svg")
_EXPORT_DPI = 150


class _Toolbar(NavigationToolbar2Tk):
    """The matplotlib toolbar, where Home fits the view to the History instead of going back to the first view."""

    def __init__(self, canvas: FigureCanvasTkAgg, parent: tk.Misc, on_home: Any) -> None:  # noqa: ANN401 - a callback
        self._on_home = on_home
        super().__init__(canvas, parent, pack_toolbar=False)  # type: ignore[no-untyped-call]

    def home(self, *_args: object) -> None:
        self._on_home()


def _margins(low: float, high: float) -> tuple[float, float]:
    pad = (abs(low) * 0.01 or 1.0) if high == low else (high - low) * _MARGIN
    return low - pad, high + pad


class ChartTab(ttk.Frame):
    """A live chart of the History with its x-axis, Clear, History length, view controls and statistics."""

    def __init__(self, parent: tk.Misc, settings: Settings) -> None:
        super().__init__(parent)
        self.settings = settings
        self.history = History(settings.history_length)
        self.autoscale = True
        self.draw_count = 0
        self.timer_id: str | None = None
        self._drawn_version = -1
        self._setting_limits = False
        self._marker_positions: list[float] = []
        self._marker_artists: list[LineCollection | Text] = []

        self._x_axis_var = tk.StringVar(value=settings.chart_x_axis.value)
        self.length_var = tk.StringVar(value=str(self.history.length))
        self._statistic_vars = {name: tk.StringVar(value=_NOTHING) for name in _STATISTICS}
        self._build_controls()
        self._build_plot()
        self._build_statistics_strip()

        self.bind("<Destroy>", self._on_destroy)
        self.timer_id = self.after(self.refresh_interval_ms, self._tick)
        self._draw()

    # --- construction -------------------------------------------------------------------------------------------

    def _build_controls(self) -> None:
        bar = ttk.Frame(self, padding=(0, 4))
        bar.pack(fill="x")
        ttk.Label(bar, text="X axis").pack(side="left", padx=(0, 4))
        self.time_axis_button = ttk.Radiobutton(
            bar, text="Time", value=XAxis.TIME.value, variable=self._x_axis_var, command=self._on_x_axis
        )
        self.sample_axis_button = ttk.Radiobutton(
            bar, text="Sample number", value=XAxis.SAMPLE.value, variable=self._x_axis_var, command=self._on_x_axis
        )
        self.time_axis_button.pack(side="left")
        self.sample_axis_button.pack(side="left", padx=(0, 12))
        self.autoscale_button = ttk.Button(bar, text="Autoscale", command=self.fit_view)
        self.autoscale_button.pack(side="left", padx=(0, 12))
        self.clear_button = ttk.Button(bar, text="Clear History", command=self.clear)
        self.clear_button.pack(side="left", padx=(0, 12))
        ttk.Label(bar, text="History length").pack(side="left", padx=(0, 4))
        self.length_box = ttk.Spinbox(
            bar,
            from_=1,
            to=_MAX_LENGTH,
            increment=_SPINBOX_STEP,
            width=9,
            textvariable=self.length_var,
            command=self.apply_history_length,
        )
        self.length_box.bind("<Return>", lambda _event: self.apply_history_length())
        self.length_box.bind("<FocusOut>", lambda _event: self.apply_history_length())
        self.length_box.pack(side="left")

    def _build_plot(self) -> None:
        self.figure = Figure(figsize=(8, 3.6), layout="constrained")
        self.axes: Axes = self.figure.add_subplot()
        self.axes.grid(visible=True, alpha=0.3)
        (self.line,) = self.axes.plot([], [], linewidth=1.0)
        self.axes.callbacks.connect("xlim_changed", self._on_view_changed)
        self.axes.callbacks.connect("ylim_changed", self._on_view_changed)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)  # type: ignore[no-untyped-call]
        self.toolbar = _Toolbar(self.canvas, self, self.fit_view)
        self.toolbar.pack(fill="x")
        self.canvas.get_tk_widget().pack(fill="both", expand=True)  # type: ignore[no-untyped-call]

    def _build_statistics_strip(self) -> None:
        strip = ttk.Frame(self, padding=(0, 4))
        strip.pack(fill="x")
        for name in _STATISTICS:
            ttk.Label(strip, text=name).pack(side="left", padx=(8, 3))
            ttk.Label(strip, textvariable=self._statistic_vars[name], font="TkFixedFont").pack(side="left")

    # --- the x-axis ---------------------------------------------------------------------------------------------

    @property
    def x_axis(self) -> XAxis:
        return XAxis(self._x_axis_var.get())

    def set_x_axis(self, x_axis: XAxis) -> None:
        self._x_axis_var.set(x_axis.value)
        self._on_x_axis()

    def _on_x_axis(self) -> None:
        self.settings.chart_x_axis = self.x_axis
        self._save_settings()
        self._draw()

    # --- History ------------------------------------------------------------------------------------------------

    def add_reading(self, reading: Reading, timestamp: float) -> None:
        """Put a Reading in the History. The plot catches up on the next refresh."""
        self.history.add(reading, timestamp)

    def on_reading(self, taken: "ReadingTaken") -> None:
        self.add_reading(taken.reading, taken.timestamp)

    def clear(self) -> None:
        """Forget every Reading, and show an empty chart straight away."""
        self.history.clear()
        self._draw()

    def apply_history_length(self) -> None:
        """Take the History length from the box, remember it in the settings, or put the old one back."""
        try:
            length = int(self.length_var.get())
            self.settings.history_length = length
        except ValueError:
            self.length_var.set(str(self.history.length))
            return
        if length != self.history.length:
            self.history.length = length
            self._save_settings()
            self.refresh()
        self.length_var.set(str(length))

    def _save_settings(self) -> None:
        try:
            self.settings.save()
        except OSError:
            _LOG.warning("Could not save the settings", exc_info=True)

    # --- drawing ------------------------------------------------------------------------------------------------

    def refresh(self) -> None:
        """Redraw if the History changed since the last draw."""
        if self.history.version != self._drawn_version:
            self._draw()

    @property
    def refresh_interval_ms(self) -> int:
        """How long the chart waits between looks at the History: longer when there is a lot to draw."""
        return _SLOW_REFRESH_MS if len(self.history) > _LONG_HISTORY else _REFRESH_MS

    def _tick(self) -> None:
        self.refresh()
        self.timer_id = self.after(self.refresh_interval_ms, self._tick)

    def _on_destroy(self, event: "tk.Event[tk.Misc]") -> None:
        if event.widget is self and self.timer_id is not None:
            self.after_cancel(self.timer_id)
            self.timer_id = None

    def _draw(self) -> None:
        entries = self.history.entries()
        origin = self.history.started_at or 0.0
        by_sample = self.x_axis is XAxis.SAMPLE
        xs: list[float] = []
        ys: list[float] = []
        breaks: list[HistoryEntry] = []
        positions: list[float] = []
        for entry in entries:
            x = entry.sample if by_sample else entry.timestamp - origin
            if entry.follows_break and xs:
                breaks.append(entry)
                positions.append(x)
                xs.append(x)
                ys.append(math.nan)  # lifts the pen: the Readings either side are not comparable
            xs.append(x)
            ys.append(math.nan if entry.reading.is_overload else entry.reading.value)
        self.line.set_data(xs, ys)
        self._draw_break_markers(breaks, positions)
        self._update_labels(entries)
        if self.autoscale:
            self._fit(xs, entries)
        self._update_statistics()
        self._drawn_version = self.history.version
        self.draw_count += 1
        self.canvas.draw_idle()  # type: ignore[no-untyped-call]

    def _draw_break_markers(self, breaks: list[HistoryEntry], positions: list[float]) -> None:
        for artist in self._marker_artists:
            artist.remove()
        self._marker_artists = []
        self._marker_positions = list(positions)
        if not positions:
            return
        transform = self.axes.get_xaxis_transform()
        self._marker_artists.append(
            self.axes.vlines(positions, 0, 1, transform=transform, colors="tab:red", linestyles="dashed", linewidth=1)
        )
        for position, entry in list(zip(positions, breaks, strict=True))[-_MAX_LABELLED_BREAKS:]:
            self._marker_artists.append(
                self.axes.text(
                    position,
                    0.98,
                    f" {entry.reading.function.label}",
                    transform=transform,
                    rotation=90,
                    va="top",
                    ha="left",
                    fontsize="small",
                    color="tab:red",
                )
            )

    def _update_labels(self, entries: tuple[HistoryEntry, ...]) -> None:
        self.axes.set_xlabel(_X_LABELS[self.x_axis])
        if not entries:
            self.axes.set_ylabel("")
            return
        function = entries[-1].reading.function
        self.axes.set_ylabel(f"{function.label} ({function.unit})" if function.unit else function.label)

    def _fit(self, xs: list[float], entries: tuple[HistoryEntry, ...]) -> None:
        """Fit the view to all of the History across, and to the latest Function's Readings up and down."""
        if not xs:
            return
        run: list[float] = []
        for entry in reversed(entries):
            if not entry.reading.is_overload:
                run.append(entry.reading.value)
            if entry.follows_break:
                break
        self._setting_limits = True
        try:
            self.axes.set_xlim(*_margins(min(xs), max(xs)))
            if run:
                self.axes.set_ylim(*_margins(min(run), max(run)))
        finally:
            self._setting_limits = False

    def _on_view_changed(self, _axes: Axes) -> None:
        if not self._setting_limits:
            self.autoscale = False  # the user panned or zoomed: stop moving the view under their hands

    def fit_view(self) -> None:
        """Fit the view to the History and keep fitting it as Readings arrive."""
        self.autoscale = True
        self._draw()

    # --- statistics ---------------------------------------------------------------------------------------------

    def statistic_text(self, name: str) -> str:
        return self._statistic_vars[name].get()

    def _update_statistics(self) -> None:
        statistics = self.history.statistics()
        function = statistics.function

        def show(value: float | None) -> str:
            if value is None or function is None:
                return _NOTHING
            return format_reading(Reading(value, function, ""), Resolution.SIX_HALF)

        texts = {
            "N": str(statistics.count) if function else _NOTHING,
            "Mean": show(statistics.mean),
            "Std dev": show(statistics.std_dev),
            "Min": show(statistics.minimum),
            "Max": show(statistics.maximum),
            "Pk-Pk": show(statistics.peak_to_peak),
            "OVLD": str(statistics.overloads) if function else _NOTHING,
        }
        for name, text in texts.items():
            self._statistic_vars[name].set(text)

    # --- Break Markers ------------------------------------------------------------------------------------------

    @property
    def break_marker_count(self) -> int:
        return len(self._marker_positions)

    def break_marker_positions(self) -> list[float]:
        """Return where the Break Markers sit on the x-axis, as last drawn."""
        return list(self._marker_positions)

    # --- export -------------------------------------------------------------------------------------------------

    def export(self, path: Path) -> None:
        """Save the chart as a PNG or SVG image, as the file's extension says."""
        suffix = path.suffix.lower()
        if suffix not in _EXPORT_SUFFIXES:
            message = f"The chart can be exported to a .png or .svg file, not {path.name!r}"
            raise ValueError(message)
        self.refresh()
        self.figure.savefig(path, format=suffix.removeprefix("."), dpi=_EXPORT_DPI)

    def ask_export(self, kind: str) -> None:
        """Ask where to save the chart as `kind` ("png" or "svg"), and save it there."""
        name = filedialog.asksaveasfilename(
            parent=self,
            title=f"Export chart as {kind.upper()}",
            defaultextension=f".{kind}",
            filetypes=[(f"{kind.upper()} image", f"*.{kind}")],
        )
        if not name:
            return
        try:
            self.export(Path(name))
        except (OSError, ValueError) as error:
            messagebox.showerror("Export chart", f"Could not export the chart: {error}", parent=self)


def install_chart(window: "MainWindow") -> ChartTab:
    """Add the Chart tab to `window`, feed it the Readings, and put its commands in the menus."""
    chart = ChartTab(window.notebook, window.settings)
    window.add_tab("Chart", chart)
    window.add_reading_listener(chart.on_reading)
    window.add_menu_command("File", "Export chart as PNG…", lambda: chart.ask_export("png"))
    window.add_menu_command("File", "Export chart as SVG…", lambda: chart.ask_export("svg"))
    window.add_menu_command("View", "Clear History", chart.clear)
    return chart
