import math
import time
import tkinter as tk
from collections.abc import Callable, Iterator

import numpy as np
import pytest

from agilent34401a.applied_signal import AppliedSignal
from agilent34401a.gui.chart_tab import ChartTab
from agilent34401a.gui.main_window import MainWindow
from agilent34401a.meter import Function, Reading, parse_reading
from agilent34401a.settings import Settings, XAxis
from agilent34401a.sim import Simulator
from tests.test_gui_app import TIMEOUT_S, pump, pump_for


def reading(value: float, function: Function = Function.DC_VOLTAGE) -> Reading:
    return Reading(value=value, function=function, raw=f"{value:+.8E}")


@pytest.fixture
def make_chart(tk_root) -> Iterator[Callable[..., ChartTab]]:
    tops: list[tk.Toplevel] = []

    def make(settings: Settings | None = None) -> ChartTab:
        top = tk.Toplevel(tk_root)
        tops.append(top)
        chart = ChartTab(top, settings if settings is not None else Settings.in_memory())
        chart.pack(fill="both", expand=True)
        return chart

    yield make
    for top in tops:
        top.destroy()


def feed(chart: ChartTab, values: list[float], function: Function = Function.DC_VOLTAGE, start: float = 0.0) -> None:
    for index, value in enumerate(values):
        chart.add_reading(reading(value, function), start + index * 0.5)
    chart.refresh()


def plotted(chart: ChartTab) -> tuple[list[float], list[float]]:
    return (
        np.asarray(chart.line.get_xdata(), dtype=float).tolist(),
        np.asarray(chart.line.get_ydata(), dtype=float).tolist(),
    )


# --- the chart on its own ---------------------------------------------------------------------------------------


def test_chart_plots_the_history_against_time_since_the_first_reading(make_chart):
    chart = make_chart()

    feed(chart, [1.0, 2.0, 4.0], start=100.0)

    assert plotted(chart) == ([0.0, 0.5, 1.0], [1.0, 2.0, 4.0])


def test_chart_can_plot_against_the_sample_number_instead(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 4.0], start=100.0)

    chart.set_x_axis(XAxis.SAMPLE)

    assert plotted(chart) == ([1, 2, 3], [1.0, 2.0, 4.0])
    assert chart.axes.get_xlabel() == "Sample number"


def test_choosing_the_x_axis_is_remembered_in_the_settings(make_chart, tmp_path):
    settings = Settings.load(tmp_path)
    chart = make_chart(settings)

    chart.set_x_axis(XAxis.SAMPLE)

    assert Settings.load(tmp_path).chart_x_axis is XAxis.SAMPLE


def test_chart_starts_on_the_x_axis_saved_in_the_settings(make_chart):
    settings = Settings.in_memory()
    settings.chart_x_axis = XAxis.SAMPLE

    chart = make_chart(settings)

    assert chart.x_axis is XAxis.SAMPLE
    assert chart.axes.get_xlabel() == "Sample number"


def test_the_x_axis_radio_buttons_switch_the_axis(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0])

    chart.sample_axis_button.invoke()
    chosen = [chart.x_axis]
    chart.time_axis_button.invoke()
    chosen.append(chart.x_axis)

    assert chosen == [XAxis.SAMPLE, XAxis.TIME]


def test_y_axis_names_the_function_and_unit_of_the_latest_readings(make_chart):
    chart = make_chart()

    feed(chart, [1.0], Function.RESISTANCE_2W)

    assert chart.axes.get_ylabel() == "2-wire Ω (Ω)"


def test_overload_readings_leave_a_gap_in_the_line(make_chart):
    chart = make_chart()
    chart.add_reading(reading(1.0), 0.0)
    chart.add_reading(parse_reading("+9.90000000E+37", Function.DC_VOLTAGE), 1.0)
    chart.add_reading(reading(3.0), 2.0)
    chart.refresh()

    _, ys = plotted(chart)

    assert ys[0] == 1.0
    assert ys[1] != ys[1]  # NaN: not drawn
    assert ys[2] == 3.0


def test_a_break_marker_is_drawn_where_the_function_changes_and_the_line_does_not_join_across_it(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0])
    feed(chart, [1000.0, 2000.0], Function.RESISTANCE_2W, start=1.0)

    assert chart.break_marker_count == 1
    _, ys = plotted(chart)
    assert [y for y in ys if not math.isnan(y)] == [1.0, 2.0, 1000.0, 2000.0]
    assert len(ys) == 5  # a NaN between the two runs lifts the pen
    assert chart.break_marker_positions() == [pytest.approx(1.0)]


def test_a_break_marker_in_sample_numbers_sits_at_the_first_reading_of_the_new_function(make_chart):
    chart = make_chart()
    chart.set_x_axis(XAxis.SAMPLE)
    feed(chart, [1.0, 2.0])
    feed(chart, [1000.0], Function.RESISTANCE_2W, start=1.0)

    assert chart.break_marker_positions() == [3]


def test_no_break_markers_without_a_change_of_function(make_chart):
    chart = make_chart()

    feed(chart, [1.0, 2.0, 3.0])

    assert chart.break_marker_count == 0


def test_clear_history_empties_the_chart_and_the_statistics(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])

    chart.clear()

    assert len(chart.history) == 0
    assert plotted(chart) == ([], [])
    assert chart.statistic_text("N") == "—"
    assert chart.break_marker_count == 0


def test_the_clear_button_clears_the_history(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])

    chart.clear_button.invoke()

    assert len(chart.history) == 0


def test_statistics_strip_shows_n_mean_std_dev_min_max_and_peak_to_peak(make_chart):
    chart = make_chart()

    feed(chart, [1.0, 2.0, 3.0, 4.0])

    assert chart.statistic_text("N") == "4"
    assert chart.statistic_text("Mean") == "2.500000 V"
    assert chart.statistic_text("Std dev") == "1.290994 V"
    assert chart.statistic_text("Min") == "1.000000 V"
    assert chart.statistic_text("Max") == "4.000000 V"
    assert chart.statistic_text("Pk-Pk") == "3.000000 V"


def test_statistics_strip_is_blank_before_there_are_readings(make_chart):
    chart = make_chart()

    assert [chart.statistic_text(name) for name in ("N", "Mean", "Std dev", "Min", "Max", "Pk-Pk")] == ["—"] * 6


def test_statistics_strip_counts_overloads_apart_from_n(make_chart):
    chart = make_chart()
    chart.add_reading(reading(1.0), 0.0)
    chart.add_reading(parse_reading("+9.90000000E+37", Function.DC_VOLTAGE), 1.0)
    chart.refresh()

    assert chart.statistic_text("N") == "1"
    assert chart.statistic_text("OVLD") == "1"


def test_statistics_follow_the_latest_function_so_volts_and_ohms_are_not_mixed(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0])
    feed(chart, [1000.0, 3000.0], Function.RESISTANCE_2W, start=1.0)

    assert chart.statistic_text("N") == "2"
    assert chart.statistic_text("Mean") == "2.000000 kΩ"


def test_the_chart_redraws_only_when_the_history_changed(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0])
    drawn = chart.draw_count

    chart.refresh()
    assert chart.draw_count == drawn

    chart.add_reading(reading(3.0), 5.0)
    chart.refresh()
    assert chart.draw_count == drawn + 1


def test_a_burst_of_readings_costs_one_redraw_not_one_each(make_chart):
    chart = make_chart()
    before = chart.draw_count

    for index in range(500):
        chart.add_reading(reading(float(index)), index * 0.001)
    chart.refresh()

    assert chart.draw_count == before + 1


def test_the_chart_redraws_by_itself_on_a_timer(make_chart):
    chart = make_chart()
    chart.add_reading(reading(1.0), 0.0)

    pump_until(chart, lambda: len(chart.line.get_xdata()) == 1)


def pump_until(chart: ChartTab, condition: Callable[[], bool]) -> None:
    deadline = time.monotonic() + TIMEOUT_S
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail("timed out waiting for the chart")
        chart.update()
        time.sleep(0.002)


def test_autoscale_fits_the_axes_to_the_latest_function_with_a_margin(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])
    feed(chart, [1000.0, 2000.0], Function.RESISTANCE_2W, start=1.5)

    low, high = chart.axes.get_ylim()
    assert 900.0 < low <= 1000.0
    assert 2000.0 <= high < 2200.0
    assert chart.axes.get_xlim()[0] <= 0.0
    assert chart.axes.get_xlim()[1] >= 2.0


def test_autoscale_copes_with_a_flat_line(make_chart):
    chart = make_chart()

    feed(chart, [5.0, 5.0, 5.0])

    low, high = chart.axes.get_ylim()
    assert low < 5.0 < high


def test_zooming_switches_autoscale_off_and_new_readings_leave_the_view_alone(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])
    chart.axes.set_ylim(10.0, 20.0)

    feed(chart, [4.0], start=10.0)

    assert chart.axes.get_ylim() == (10.0, 20.0)
    assert chart.autoscale is False


def test_the_autoscale_button_fits_the_view_again(make_chart):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])
    chart.axes.set_ylim(10.0, 20.0)

    chart.autoscale_button.invoke()

    assert chart.autoscale is True
    assert chart.axes.get_ylim()[0] < 1.0
    assert chart.axes.get_ylim()[1] > 3.0


def test_the_chart_has_the_navigation_toolbar_for_pan_and_zoom(make_chart):
    chart = make_chart()

    assert {"Pan", "Zoom", "Home"} <= {item[0] for item in chart.toolbar.toolitems}


def test_history_length_is_taken_from_the_settings(make_chart):
    settings = Settings.in_memory()
    settings.history_length = 50

    chart = make_chart(settings)

    assert chart.history.length == 50
    assert chart.length_var.get() == "50"


def test_changing_the_history_length_applies_it_and_saves_it(make_chart, tmp_path):
    settings = Settings.load(tmp_path)
    chart = make_chart(settings)
    feed(chart, [float(value) for value in range(10)])

    chart.length_var.set("4")
    chart.apply_history_length()

    assert chart.history.length == 4
    assert len(chart.history) == 4
    assert Settings.load(tmp_path).history_length == 4


@pytest.mark.parametrize("text", ["0", "-3", "abc", "", "2000000"])
def test_a_bad_history_length_is_refused_and_the_old_one_comes_back(make_chart, text):
    settings = Settings.in_memory()
    settings.history_length = 20
    chart = make_chart(settings)

    chart.length_var.set(text)
    chart.apply_history_length()

    assert chart.history.length == 20
    assert chart.length_var.get() == "20"


@pytest.mark.parametrize("name", ["chart.png", "chart.svg"])
def test_chart_exports_to_png_and_svg(make_chart, tmp_path, name):
    chart = make_chart()
    feed(chart, [1.0, 2.0, 3.0])
    path = tmp_path / name

    chart.export(path)

    data = path.read_bytes()
    assert data.startswith(b"\x89PNG") if name.endswith("png") else b"<svg" in data


def test_export_refuses_other_formats(make_chart, tmp_path):
    chart = make_chart()

    with pytest.raises(ValueError, match=r"\.png or \.svg"):
        chart.export(tmp_path / "chart.pdf")


def test_closing_the_chart_stops_its_timer(make_chart, tk_root):
    chart = make_chart()
    top = chart.winfo_toplevel()

    top.destroy()
    tk_root.update()

    assert chart.timer_id is None


# --- in the main window -----------------------------------------------------------------------------------------


def tab_titles(window: MainWindow) -> list[str]:
    notebook = window.notebook
    return [str(notebook.tab(tab, "text")) for tab in notebook.tabs()]  # type: ignore[no-untyped-call]


def test_the_main_window_has_a_chart_tab_that_fills_from_continuous_readings(make_window):
    window = make_window(Simulator(dc_voltage=2.0))
    assert "Chart" in tab_titles(window)

    pump(window, lambda: len(window.chart.history) >= 5)
    window.chart.refresh()

    assert len(window.chart.line.get_xdata()) == len(window.chart.history)
    assert {entry.reading.value for entry in window.chart.history} == {2.0}


def test_changing_function_in_the_window_puts_a_break_marker_in_the_history(make_window):
    window = make_window(Simulator())
    pump(window, lambda: len(window.chart.history) >= 2)

    window.function_buttons[Function.RESISTANCE_2W].invoke()
    pump(window, lambda: window.chart.history.statistics().function is Function.RESISTANCE_2W)

    assert len(window.chart.history.break_markers()) == 1
    window.chart.refresh()
    assert window.chart.break_marker_count == 1


def test_the_chart_shows_noise_from_the_simulators_applied_signal(make_window):
    simulator = Simulator(signals={Function.DC_VOLTAGE: AppliedSignal(1.0, noise=0.05)}, seed=1)
    window = make_window(simulator)

    pump(window, lambda: len(window.chart.history) >= 30)

    statistics = window.chart.history.statistics()
    assert statistics.std_dev is not None
    assert statistics.std_dev > 0.01


def menu_labels(menu: tk.Menu) -> list[str]:
    return [str(menu.entrycget(index, "label")) for index in range((menu.index("end") or 0) + 1)]


def test_file_menu_offers_the_chart_exports_and_view_menu_clears_the_history(make_window):
    window = make_window(Simulator())

    assert {"Export chart as PNG…", "Export chart as SVG…"} <= set(menu_labels(window.menu("File")))
    assert "Clear History" in menu_labels(window.menu("View"))


def test_clear_history_from_the_menu_empties_the_history_and_restarts_sample_numbers(make_window):
    window = make_window(Simulator())
    pump(window, lambda: len(window.chart.history) >= 3)
    view = window.menu("View")

    view.invoke(menu_labels(view).index("Clear History"))

    assert len(window.chart.history) == 0
    pump(window, lambda: len(window.chart.history) >= 1)
    assert window.chart.history.entries()[0].sample == 1


def test_window_closing_leaves_no_chart_timer_behind(make_window):
    window = make_window(Simulator())
    pump_for(window, 0.1)
    chart = window.chart

    window.close()

    assert chart.timer_id is None


def test_the_x_axis_choice_in_the_window_is_saved_with_the_window_settings(make_window):
    window = make_window(Simulator())

    window.chart.set_x_axis(XAxis.SAMPLE)

    assert window.settings.chart_x_axis is XAxis.SAMPLE


def test_a_long_history_is_redrawn_less_often_than_a_short_one(make_chart):
    chart = make_chart()
    short = chart.refresh_interval_ms

    for index in range(2000):
        chart.add_reading(reading(float(index)), index * 0.001)

    assert chart.refresh_interval_ms > short
