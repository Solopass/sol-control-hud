"""Unit tests for Sparkline and SegmentedBar visual monitoring widgets."""
import tkinter as tk
import pytest

from sol_control_hud.views.widgets import Sparkline, SegmentedBar


def test_sparkline_widget():
    root = tk.Tk()
    root.withdraw()
    try:
        spark = Sparkline(root, width=50, height=16, bg="#090d16", color="#38bdf8")
        assert spark.winfo_exists()

        # 1. Empty data
        spark.set_data([])
        assert len(spark.find_all()) == 0

        # 2. Single item (flat line)
        spark.set_data([42.0])
        assert len(spark.find_all()) == 1

        # 3. Identical items (zero spread)
        spark.set_data([50.0, 50.0, 50.0])
        assert len(spark.find_all()) == 2  # line + dot

        # 4. Normal metric range
        spark.set_data([10.0, 30.0, 25.0, 80.0, 60.0])
        assert len(spark.find_all()) == 2  # polyline + dot

        # 5. Theme update
        spark.configure_theme(bg="#000000", color="#00f0ff", dot_color="#22c55e")
        assert spark.color == "#00f0ff"
        assert spark.dot_color == "#22c55e"
    finally:
        root.destroy()


def test_segmented_bar_widget():
    root = tk.Tk()
    root.withdraw()
    try:
        bar = SegmentedBar(root, height=4, bg="#090d16")
        assert bar.winfo_exists()

        # 1. Empty segments
        bar.set_segments([])
        assert len(bar.find_all()) == 0

        # 2. Multi-segment breakdown (model, other apps, free headroom)
        bar.set_segments([
            (0.4, "#38bdf8"),
            (0.3, "#a855f7"),
            (0.3, "#1e293b"),
        ])
        assert len(bar.find_all()) == 3

        # 3. Zero or negative fractions skipped
        bar.set_segments([
            (0.5, "#38bdf8"),
            (0.0, "#a855f7"),
            (-0.1, "#f87171"),
            (0.5, "#1e293b"),
        ])
        assert len(bar.find_all()) == 2
    finally:
        root.destroy()
