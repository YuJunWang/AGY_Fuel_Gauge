import os
import sys
import threading
import time
import tkinter as tk
import ctypes
from datetime import datetime, timezone
import pystray
from PIL import Image, ImageDraw, ImageTk, ImageChops
import math
import random

# ── Single Instance Lock ───────────────────────────────────────────────────
mutex = None
def _acquire_single_instance_lock():
    global mutex
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, "Global\\AGY_Fuel_Gauge_Mutex")
    if ctypes.windll.kernel32.GetLastError() == 183: # ERROR_ALREADY_EXISTS
        sys.exit(0)

# ── DPI Awareness ──────────────────────────────────────────────────────────
# Enable DPI awareness to bypass Windows Desktop Window Manager (DWM) scaling.
# This forces 1 virtual pixel to map exactly to 1 physical pixel, 
# mathematically eliminating any subpixel rounding gaps in rendering.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

# ── Load Bundled Monospace Font ───────────────────────────────────────────
# Dynamically register bundled JetBrains Mono so it renders cleanly even if not installed in Windows.
try:
    font_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "fonts")
    for font_file in ["JetBrainsMono-Regular.ttf", "JetBrainsMono-Bold.ttf"]:
        fp = os.path.join(font_dir, font_file)
        if os.path.exists(fp):
            ctypes.windll.gdi32.AddFontResourceExW(fp, 0x10, 0)  # FR_PRIVATE
except Exception:
    pass

import data_fetcher
import history_logger
from display_logic import (TOKENS_PER_BLOCK, bucket_drops, bucket_tokens,
                           format_countdown, format_tokens)

TRANSPARENT_COLOR = "#000001"
BG_COLOR       = "#000000" # Pure Black
HEADER_COLOR   = "#000000" # Pure Black
SURFACE_COLOR  = "#000000" # Pure Black
TRACK_BG       = "#111111" # Dark Neutral Grey
TEXT_FG        = "#F8FAFC" # Slate 50
TEXT_MUTED     = "#64748B" # Slate 500

# Gemini Dynamic Colors
COLOR_GEM_SAFE   = "#22C55E" # Neon Green
COLOR_GEM_WARN   = "#FACC15" # Yellow
COLOR_GEM_DANGER = "#EF4444" # Red
COLOR_GEM_CHART  = "#4ADE80" # Green 400 (matches Safe)
COLOR_GEM_CHART_OVERFLOW = "#EF4444" # Red 500

# External Dynamic Colors
COLOR_EXT_SAFE   = "#F97316" # Orange 500
COLOR_EXT_WARN   = "#EA580C" # Orange 600
COLOR_EXT_DANGER = "#EF4444" # Red 500
COLOR_EXT_CHART  = "#FB923C" # Orange 400 (matches Safe)
COLOR_EXT_CHART_OVERFLOW = "#EF4444" # Red 500

# Argon Dynamic Colors
COLOR_ARG_SAFE   = "#06B6D4" # Cyan 500
COLOR_ARG_WARN   = "#FACC15" # Yellow 400
COLOR_ARG_DANGER = "#EF4444" # Red 500

DIGITAL_FONT        = "JetBrains Mono" # Modern minimalist monospace

# ── Utility: Color Gradient ───────────────────────────────────────────────
def hex_to_rgb(hex_str):
    hex_str = hex_str.lstrip('#')
    return tuple(int(hex_str[i:i+2], 16) for i in (0, 2, 4))

def rgb_to_hex(rgb):
    return '#{:02x}{:02x}{:02x}'.format(int(rgb[0]), int(rgb[1]), int(rgb[2]))

def interpolate_color(start_hex, end_hex, factor):
    factor = max(0.0, min(1.0, factor))  # clamp to [0, 1]
    c1 = hex_to_rgb(start_hex)
    c2 = hex_to_rgb(end_hex)
    r = c1[0] + (c2[0] - c1[0]) * factor
    g = c1[1] + (c2[1] - c1[1]) * factor
    b = c1[2] + (c2[2] - c1[2]) * factor
    return rgb_to_hex((r, g, b))

def interpolate_color_3(c_low, c_mid, c_high, factor):
    """3-keyframe gradient: factor 0.0->0.5 goes c_low->c_mid, 0.5->1.0 goes c_mid->c_high."""
    factor = max(0.0, min(1.0, factor))
    if factor <= 0.5:
        return interpolate_color(c_low, c_mid, factor * 2.0)
    else:
        return interpolate_color(c_mid, c_high, (factor - 0.5) * 2.0)

def highlight_rgb(hex_str, multiplier=1.5, add_white=50):
    r, g, b = hex_to_rgb(hex_str)
    r = min(255, int(r * multiplier + add_white))
    g = min(255, int(g * multiplier + add_white))
    b = min(255, int(b * multiplier + add_white))
    return rgb_to_hex((r, g, b))

def deepen_rgb(hex_str, multiplier=0.7):
    r, g, b = hex_to_rgb(hex_str)
    r = max(0, min(255, int(r * multiplier)))
    g = max(0, min(255, int(g * multiplier)))
    b = max(0, min(255, int(b * multiplier)))
    return rgb_to_hex((r, g, b))


# ── Theme Table ───────────────────────────────────────────────────────────
# Per-gauge palette. Warning (<=50%) and danger (<=20%) bands are shared;
# title_warn is the title colour in the warning band (the legacy UI kept the safe colour).
# Claude bases/tips are fixed literals of deepen_rgb(safe, 0.35) / highlight_rgb(safe, 1.0, 80).
_SHARED_ALERTS = {
    "warn": "#FACC15", "warn_base": "#713F12", "warn_tip": "#FEF08A",
    "danger": "#EF4444", "danger_base": "#7F1D1D", "danger_tip": "#FECACA",
}
THEMES = {
    "gem": {"safe": COLOR_GEM_SAFE, "warn": COLOR_GEM_WARN, "danger": COLOR_GEM_DANGER,
            "safe_base": "#14532D", "warn_base": "#713F12", "danger_base": "#7F1D1D",
            "safe_tip": "#86EFAC", "warn_tip": "#FEF08A", "danger_tip": "#FECACA",
            "title_warn": COLOR_GEM_SAFE},
    "ext": {"safe": COLOR_EXT_SAFE, "warn": COLOR_EXT_WARN, "danger": COLOR_EXT_DANGER,
            "safe_base": "#7C2D12", "warn_base": "#9A3412", "danger_base": "#7F1D1D",
            "safe_tip": "#FED7AA", "warn_tip": "#FDBA74", "danger_tip": "#FECACA",
            "title_warn": COLOR_EXT_SAFE},
    "arg": {"safe": COLOR_ARG_SAFE, "warn": COLOR_ARG_WARN, "danger": COLOR_ARG_DANGER,
            "safe_base": "#164E63", "warn_base": "#713F12", "danger_base": "#7F1D1D",
            "safe_tip": "#A5F3FC", "warn_tip": "#FEF08A", "danger_tip": "#FECACA",
            "title_warn": COLOR_ARG_SAFE},
    "cl_5h": {"safe": "#A78BFA", "safe_base": "#3A3057", "safe_tip": "#F7DBFF",
              "title_warn": "#A78BFA", **_SHARED_ALERTS},
    "cl_wk": {"safe": "#E879F9", "safe_base": "#512A57", "safe_tip": "#FFC9FF",
              "title_warn": "#E879F9", **_SHARED_ALERTS},
    "cl_model": {"safe": "#818CF8", "safe_base": "#2D3156", "safe_tip": "#D1DCFF",
                 "title_warn": "#818CF8", **_SHARED_ALERTS},
}


class _FuelGaugeBase(tk.Canvas):
    """Shared state logic; subclasses supply geometry via _fill_coords / _tip_coords."""

    def _init_state(self, theme_id):
        self.theme = THEMES[theme_id]
        self._title_color = self.theme["safe"]
        self._has_value = False  # True once a real value has been shown

    def _band(self, val):
        t = self.theme
        if val == 0.0 or val <= 20:
            return t["danger"], t["danger_base"], t["danger_tip"], t["danger"]
        if val <= 50:
            return t["warn"], t["warn_base"], t["warn_tip"], t["title_warn"]
        return t["safe"], t["safe_base"], t["safe_tip"], t["safe"]

    def _clear(self, pct_text, time_text, color=TEXT_FG):
        # Hide the fill entirely: a zero-length round-capped line would leave a dark dot on the track.
        for item in (self.line_base, self.line_core, self.tip_dot):
            self.itemconfig(item, state="hidden")
        self.itemconfig(self.pct_text, text=pct_text, fill=color)
        self.itemconfig(self.time_text, text=time_text)

    def set_value(self, pct, reset, now_utc):
        if pct is None:
            self._has_value = False
            self._title_color = self.theme["safe"]
            self.itemconfig(self.title_text, fill=self._title_color)
            self._clear("--%", format_countdown(None, reset, now_utc))
            return
        val = max(0.0, min(100.0, pct))
        core, base, tip, title_color = self._band(val)
        self._has_value = True
        self._title_color = title_color
        self.itemconfig(self.title_text, fill=title_color)

        coords = self._fill_coords(val)
        self.coords(self.line_base, *coords)
        self.itemconfig(self.line_base, fill=base, state="normal")
        self.coords(self.line_core, *coords)
        self.itemconfig(self.line_core, fill=core, state="normal")

        if val > 0:
            self.coords(self.tip_dot, *self._tip_coords(coords))
            self.itemconfig(self.tip_dot, fill=tip, state="normal")
        else:
            self.itemconfig(self.tip_dot, state="hidden")

        # Display exact percentage (e.g. 0%, never misleading '--%')
        self.itemconfig(self.pct_text, text=f"{int(val)}%", fill=core)
        self.itemconfig(self.time_text, text=format_countdown(val, reset, now_utc))

    def set_title(self, text):
        self.itemconfig(self.title_text, text=text)

    def set_stale(self, stale):
        """Stale data keeps its last value with a muted title; never-loaded shows '--%'."""
        if stale and not self._has_value:
            self._clear("--%", "--")
        self.itemconfig(self.title_text, fill=TEXT_MUTED if stale else self._title_color)

    def set_login(self):
        self._has_value = False
        self.itemconfig(self.title_text, fill=TEXT_MUTED)
        self._clear("LOGIN", "/login", color=TEXT_MUTED)


class VerticalFuelGauge(_FuelGaugeBase):
    def __init__(self, parent, width=44, height=200, title="Title", theme_id="gem", text_side="right", **kwargs):
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self._init_state(theme_id)

        # Dimensions for the vertical bar
        self.bar_width = 12
        self.padding_top = 35
        self.padding_bottom = 40

        self.cx = self.width / 2
        self.y_bottom = self.height - self.padding_bottom
        self.y_top = self.padding_top
        self.track_height = self.y_bottom - self.y_top

        # Track
        self.create_line(self.cx, self.y_bottom, self.cx, self.y_top, fill=TRACK_BG, width=self.bar_width, capstyle=tk.ROUND)

        # Reverted Glow (Base + Core)
        self.line_base = self.create_line(self.cx, self.y_bottom, self.cx, self.y_bottom, fill="#000", width=self.bar_width, capstyle=tk.ROUND)
        self.line_core = self.create_line(self.cx, self.y_bottom, self.cx, self.y_bottom, fill="#000", width=self.bar_width - 4, capstyle=tk.ROUND)
        self.tip_dot = self.create_oval(0, 0, 0, 0, fill="#000", outline="", state="hidden")

        # Percentage (JetBrains Mono)
        self.pct_text = self.create_text(self.cx, self.y_bottom + 18, text="--%", fill=TEXT_FG, font=(DIGITAL_FONT, 10, "bold"), justify="center")

        self.title_text = self.create_text(self.cx, self.y_top - 16, text=title, fill=self._title_color, font=(DIGITAL_FONT, 8, "bold"), justify="center")

        text_x = self.cx + 14 if text_side == "right" else self.cx - 14
        self.time_text = self.create_text(text_x, self.height / 2, text="--h", fill=TEXT_MUTED, font=(DIGITAL_FONT, 8), justify="center", angle=270)

    def _fill_coords(self, val):
        # When val=0, leave a clean 2px stub so the track bottom is visible
        curr_y = (self.y_bottom - 2) if val == 0.0 else (self.y_bottom - self.track_height * (val / 100.0))
        return (self.cx, self.y_bottom, self.cx, curr_y)

    def _tip_coords(self, coords):
        r = (self.bar_width - 4) / 2
        curr_y = coords[3]
        return (self.cx - r, curr_y - r, self.cx + r, curr_y + r)


class HorizontalFuelGauge(_FuelGaugeBase):
    def __init__(self, parent, width=96, height=36, title="ARG", theme_id="arg", **kwargs):
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self._init_state(theme_id)

        self.bar_width = 10
        self.x_left = 8
        self.x_right = self.width - 8
        self.track_width = self.x_right - self.x_left
        self.cy = 25

        # Top telemetry labels: Title (Left), Reset Timer (Center), Percentage (Right)
        self.title_text = self.create_text(
            self.x_left - 2, 9, text=title, fill=self._title_color,
            font=(DIGITAL_FONT, 8, "bold"), anchor="w"
        )
        self.time_text = self.create_text(
            self.width / 2, 9, text="--", fill=TEXT_MUTED,
            font=(DIGITAL_FONT, 7), anchor="center"
        )
        self.pct_text = self.create_text(
            self.x_right + 2, 9, text="--%", fill=TEXT_FG,
            font=(DIGITAL_FONT, 8, "bold"), anchor="e"
        )

        # Horizontal Track
        self.create_line(
            self.x_left, self.cy, self.x_right, self.cy,
            fill=TRACK_BG, width=self.bar_width, capstyle=tk.ROUND
        )

        # Glow (Base + Core) & Tip Dot
        self.line_base = self.create_line(
            self.x_left, self.cy, self.x_left, self.cy,
            fill="#000", width=self.bar_width, capstyle=tk.ROUND
        )
        self.line_core = self.create_line(
            self.x_left, self.cy, self.x_left, self.cy,
            fill="#000", width=self.bar_width - 4, capstyle=tk.ROUND
        )
        self.tip_dot = self.create_oval(0, 0, 0, 0, fill="#000", outline="", state="hidden")

    def _fill_coords(self, val):
        curr_x = (self.x_left + 2) if val == 0.0 else (self.x_left + self.track_width * (val / 100.0))
        return (self.x_left, self.cy, curr_x, self.cy)

    def _tip_coords(self, coords):
        r = (self.bar_width - 4) / 2
        curr_x = coords[2]
        return (curr_x - r, self.cy - r, curr_x + r, self.cy + r)

    def set_value(self, pct, reset, now_utc):
        self.coords(self.time_text, self.width / 2, 9)
        super().set_value(pct, reset, now_utc)

    def set_login(self):
        super().set_login()
        # "LOGIN" is wider than a percentage: centre "/login" in the gap left of it.
        title_bb, pct_bb = self.bbox(self.title_text), self.bbox(self.pct_text)
        if title_bb and pct_bb:
            self.coords(self.time_text, (title_bb[2] + pct_bb[0]) / 2, 9)


class VerticalHistoryChart(tk.Canvas):
    """Two-sided burn chart. Values arrive pre-bucketed, in blocks (row 0 = newest)."""

    def __init__(self, parent, width, height, left_colors, right_colors, left_text_color, right_text_color,
                 left_hover, right_hover, left_rate, right_rate, **kwargs):
        super().__init__(parent, width=width, height=height, bg="#000000", highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.buckets = []
        self.y_top = 10
        self.y_bottom = height - 15
        self.left_text_color = left_text_color
        self.right_text_color = right_text_color
        self.left_hover, self.right_hover = left_hover, right_hover
        self.left_rate, self.right_rate = left_rate, right_rate
        self._init_gradients(left_colors, right_colors)

        self.bind("<Motion>", self.on_mouse_move)
        self.bind("<Leave>", self.on_mouse_leave)


    def _init_gradients(self, left_colors, right_colors):
        def hex2rgb(h):
            h = h.lstrip('#')
            return tuple(int(h[i:i+2], 16) for i in (0, 2, 4))

        def interpolate(c1, c2, factor):
            return tuple(int(c1[i] + (c2[i] - c1[i]) * factor) for i in range(3))

        def deepen(rgb, factor):
            return tuple(int(c * factor) for c in rgb)

        def highlight(rgb, factor, add):
            return tuple(min(255, int(c * factor + add)) for c in rgb)

        mid_x = int(self.width / 2)
        BLOCK_STEP = 3

        self.grad_dim = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        self.grad_bright = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        d_dim = ImageDraw.Draw(self.grad_dim)
        d_bright = ImageDraw.Draw(self.grad_bright)

        for side, colors in (("left", left_colors), ("right", right_colors)):
            c_low, c_mid, c_high = (hex2rgb(c) for c in colors)
            for b in range(11):
                if side == "left":
                    x_right = mid_x - 2 - (b * BLOCK_STEP)
                    x_left = x_right - BLOCK_STEP
                else:
                    x_left = mid_x + 3 + (b * BLOCK_STEP)
                    x_right = x_left + BLOCK_STEP

                factor = b / 10.0
                if factor <= 0.5:
                    f = factor * 2
                    base = interpolate(c_low, c_mid, f)
                else:
                    f = (factor - 0.5) * 2
                    base = interpolate(c_mid, c_high, f)

                color_dim = deepen(base, 0.5) + (255,)
                color_bright = highlight(base, 1.2, 20) + (255,)

                d_dim.rectangle([x_left, 0, x_right, self.height], fill=color_dim)
                d_bright.rectangle([x_left, 0, x_right, self.height], fill=color_bright)

    def render(self, left, right):
        self.delete("all")
        if left is None and right is None:
            self.create_text(self.width/2, self.height/2, text="Waiting...", fill=TEXT_MUTED, font=("Segoe UI", 7))
            return
        n = len(left) if left is not None else len(right)
        if left is None:
            left = [0.0] * n
        if right is None:
            right = [0.0] * n

        mid_x = int(self.width / 2)
        self.y_top = 10
        # Fixed 300px y-range: 100 rows x 3px, so the hour ticks align with the rows.
        y_range = 300
        self.y_bottom = self.y_top + y_range

        # Center axis line
        self.create_line(mid_x, self.y_top, mid_x, self.y_bottom, fill="#1E293B", width=2)

        # Time Ticks (Every 1h small, every 2h big)
        for h in range(1, 5):
            tick_y = int(self.y_top + (h / 5.0) * y_range)
            if h % 2 == 0:
                # 2H, 4H (Big tick)
                self.create_line(mid_x - 3, tick_y, mid_x + 3, tick_y, fill=TEXT_FG, width=1.5)
            else:
                # 1H, 3H, 5H (Small tick)
                self.create_line(mid_x - 1, tick_y, mid_x + 1, tick_y, fill=TEXT_MUTED, width=1.0)

        buckets = list(zip(left, right))
        self.buckets = buckets

        # ABSOLUTE FIXED SCALE: 1 unit per full block. Max 10 blocks.
        # Block rendering: 2px wide (full) or 1px wide (half), 1px gap -> Step is 3px
        BLOCK_STEP = 3

        # 1. Draw continuous polygon for the entire history
        mask_poly = Image.new("L", (self.width, self.height), 0)
        d_poly = ImageDraw.Draw(mask_poly)

        pts_g = [(mid_x - 2, self.y_top)]
        pts_e = [(mid_x + 3, self.y_top)]

        for idx, (g, e) in enumerate(buckets):
            # cy is the exact center Y of this 3px block for the polygon point
            cy = int(self.y_top + idx * 3 + 1.5)

            # LEFT
            offset_g = min(10.0, g) * BLOCK_STEP
            xg = round(mid_x - 2 - offset_g)

            # RIGHT
            offset_e = min(10.0, e) * BLOCK_STEP
            xe = round(mid_x + 3 + offset_e)

            pts_g.append((xg, cy))
            pts_e.append((xe, cy))

        pts_g.append((mid_x - 2, int(self.y_top + len(buckets) * 3)))
        pts_e.append((mid_x + 3, int(self.y_top + len(buckets) * 3)))

        d_poly.polygon(pts_g, fill=255)
        d_poly.polygon(pts_e, fill=255)

        # 2. Identify Overflow Spikes
        mask_over = Image.new("L", (self.width, self.height), 0)
        d_over = ImageDraw.Draw(mask_over)

        for idx, (g, e) in enumerate(buckets):
            cy = int(self.y_top + idx * 3)

            if g >= 10.0:
                d_over.rectangle([0, cy, mid_x - 1, cy + 2], fill=255)
            if e >= 10.0:
                d_over.rectangle([mid_x + 1, cy, self.width, cy + 2], fill=255)

        # 3. Intersect Masks
        # mask_bright gets only the polygon regions that intersect with overflow Y-coords
        mask_bright = ImageChops.multiply(mask_poly, mask_over)
        # mask_dim gets the rest of the polygon
        mask_dim = ImageChops.subtract(mask_poly, mask_bright)

        # 4. Composite the layers
        bg_img = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        layer_dim = Image.composite(self.grad_dim, bg_img, mask_dim)
        final_img = Image.composite(self.grad_bright, layer_dim, mask_bright)

        # Stamp the perfectly rendered pixel buffer onto the Tkinter canvas
        self.chart_tk_img = ImageTk.PhotoImage(final_img)
        self.create_image(0, 0, anchor="nw", image=self.chart_tk_img, tags="chart_img")
        self.tag_lower("chart_img")

        left_hourly = sum(left) / 5.0
        right_hourly = sum(right) / 5.0
        left_color = "#EF4444" if left_hourly > 20 else self.left_text_color
        right_color = "#EF4444" if right_hourly > 20 else self.right_text_color

        # Floating Burn Rates (Reading top-to-bottom)
        self.create_text(8, self.height / 2, text=self.left_rate(left_hourly), fill=left_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")
        self.create_text(self.width-8, self.height / 2, text=self.right_rate(right_hourly), fill=right_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")

        # -5H label pushed snugly below the bottom axis
        self.create_text(mid_x, self.y_bottom + 13, text="-5H", fill="#94A3B8", font=("Segoe UI", 7, "bold"), anchor="s")

    def on_mouse_move(self, event):
        self.delete("tooltip")
        if not hasattr(self, 'buckets') or not self.buckets:
            return

        y = event.y
        if y < self.y_top or y >= self.y_bottom:
            return

        idx = int((y - self.y_top) / 3)
        if 0 <= idx < len(self.buckets):
            g, e = self.buckets[idx]
            if g == 0 and e == 0:
                return

            bucket_y = self.y_top + idx * 3

            # Direction 3: Subtle row highlight band (2px height)
            self.create_rectangle(0, bucket_y - 1, self.width, bucket_y + 2, outline="#1E293B", fill="#1E293B", tags="tooltip")

            # Crosshair line (rendered on top of the highlight band)
            self.create_line(0, bucket_y, self.width, bucket_y, fill="#475569", dash=(1, 2), tags="tooltip")

            # Clamp text_y within vertical bounds
            text_y = max(self.y_top + 6, min(self.y_bottom - 6, y))

            # Direction 1: Split labels — fixed left, fixed right
            for text, color, x, anchor_val in [
                (self.left_hover(g), self.left_text_color, 2, "w"),
                (self.right_hover(e), self.right_text_color, self.width - 2, "e"),
            ]:
                lbl = self.create_text(x, text_y, text=text, fill=color, font=("Segoe UI", 7, "bold"), anchor=anchor_val, tags="tooltip")
                bbox = self.bbox(lbl)
                if bbox:
                    bg = self.create_rectangle(bbox[0]-2, bbox[1]-1, bbox[2]+2, bbox[3]+1, fill="#0F172A", outline="", tags="tooltip")
                    self.tag_lower(bg, lbl)

    def on_mouse_leave(self, event):
        self.delete("tooltip")


class SlidingToggle(tk.Canvas):
    def __init__(self, parent, command=None, *args, **kwargs):
        tk.Canvas.__init__(self, parent, width=32, height=16, bg=BG_COLOR, highlightthickness=0, bd=0, *args, **kwargs)
        self.command = command
        self.is_on = False
        
        # Draw pill background (using an oval left, oval right, and rectangle in middle)
        self.bg_color_off = "#333842"
        self.bg_color_on = "#3A4B6B" # Muted blue-grey, less prominent
        self.create_oval(0, 0, 16, 16, fill=self.bg_color_off, outline="", tags="bg")
        self.create_oval(16, 0, 32, 16, fill=self.bg_color_off, outline="", tags="bg")
        self.create_rectangle(8, 0, 24, 16, fill=self.bg_color_off, outline="", tags="bg")
        
        # Draw slider circle
        self.slider = self.create_oval(2, 2, 14, 14, fill="#FFFFFF", outline="")
        
        self.bind("<Button-1>", self.toggle)
        self.bind("<Enter>", lambda e: self.config(cursor="hand2"))
        
    def toggle(self, event=None):
        self.is_on = not self.is_on
        self.animate(1.0 if self.is_on else 0.0)
        if self.command:
            self.command(self.is_on)
            
    def set_state(self, state):
        if self.is_on == state: return
        self.is_on = state
        self.animate(1.0 if self.is_on else 0.0)
        
    def animate(self, target_progress, current_progress=None):
        if current_progress is None:
            current_progress = 0.0 if target_progress == 1.0 else 1.0
            
        step = 0.15 if target_progress > current_progress else -0.15
        next_progress = current_progress + step
        
        # Clamp to target to avoid floating-point overshoot
        if (step > 0 and next_progress >= target_progress) or (step < 0 and next_progress <= target_progress):
            next_progress = target_progress
            
        # Update slider position (x goes from 2 to 18)
        x_offset = 2 + (16 * next_progress)
        self.coords(self.slider, x_offset, 2, x_offset + 12, 14)
        
        # Update background color at midpoint
        if next_progress > 0.5:
            self.itemconfig("bg", fill=self.bg_color_on)
        else:
            self.itemconfig("bg", fill=self.bg_color_off)
            
        # Use tolerance check instead of exact float equality
        if abs(next_progress - target_progress) > 0.001:
            self.after(16, lambda: self.animate(target_progress, next_progress))


PANEL_WIDTH = 110
PANEL_HEIGHT = 574  # window height 598 minus the 24px header


def _apply_bars(snapshot, gauges, now_utc):
    """Feed snapshot bars to {bar_id: gauge}; non-ok status keeps old values, greyed."""
    if snapshot.get("status") != "ok":
        for gauge in gauges.values():
            gauge.set_stale(True)
        return
    bars = snapshot.get("bars", {})
    for bar_id, gauge in gauges.items():
        bar = bars.get(bar_id) or {}
        gauge.set_value(bar.get("pct"), bar.get("reset", ""), now_utc)


class AgyPanel(tk.Frame):
    """AGY column: 5H/WK toggle, GEM/EXT vertical bars, ARG bar, GEM/EXT burn chart."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG_COLOR, highlightthickness=0, width=PANEL_WIDTH, height=PANEL_HEIGHT)

        # Toggle
        self.toggle_container = tk.Frame(self, bg=BG_COLOR, bd=0)
        self.toggle_container.pack(pady=8)

        self.lbl_5h = tk.Label(self.toggle_container, text="5H", font=("Segoe UI", 7, "bold"), bg=BG_COLOR, fg=TEXT_FG, cursor="hand2")
        self.lbl_5h.pack(side=tk.LEFT, padx=4)
        self.lbl_5h.bind("<Button-1>", lambda e: self.set_view(False))

        self.toggle_switch = SlidingToggle(self.toggle_container, command=self.set_view)
        self.toggle_switch.pack(side=tk.LEFT)

        self.lbl_wk = tk.Label(self.toggle_container, text="WK", font=("Segoe UI", 7, "bold"), bg=BG_COLOR, fg=TEXT_MUTED, cursor="hand2")
        self.lbl_wk.pack(side=tk.LEFT, padx=4)
        self.lbl_wk.bind("<Button-1>", lambda e: self.set_view(True))

        self.show_weekly = False

        # Vertical Fuel Bars
        self.bars_frame = tk.Frame(self, bg=BG_COLOR)
        self.bars_frame.pack(fill=tk.X, pady=4)

        self.view_5h_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=150)
        self.view_5h_frame.pack(fill=tk.X)
        self.view_5h_frame.pack_propagate(False)
        self.gemini_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=44, height=150, title="GEM", theme_id="gem", text_side="left")
        self.gemini_5h_gauge.place(x=9, y=0)
        self.ext_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=44, height=150, title="EXT", theme_id="ext", text_side="right")
        self.ext_5h_gauge.place(x=57, y=0)

        self.view_wk_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=150)
        self.view_wk_frame.pack_propagate(False)
        self.gemini_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=44, height=150, title="GEM", theme_id="gem", text_side="left")
        self.gemini_wk_gauge.place(x=9, y=0)
        self.ext_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=44, height=150, title="EXT", theme_id="ext", text_side="right")
        self.ext_wk_gauge.place(x=57, y=0)

        # Horizontal Argon Weekly Fuel Bar
        self.argon_frame = tk.Frame(self, bg=BG_COLOR, height=36)
        self.argon_frame.pack(fill=tk.X, pady=(0, 2), padx=7)
        self.argon_frame.pack_propagate(False)
        self.argon_gauge = HorizontalFuelGauge(self.argon_frame, width=96, height=36, title="ARG", theme_id="arg")
        self.argon_gauge.pack(fill=tk.BOTH, expand=True)

        # Vertical History Chart
        self.chart_frame = tk.Frame(self, bg=SURFACE_COLOR, bd=0, highlightthickness=0)
        self.chart_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8), padx=6)

        pct_hover = lambda v: f"{v:.1f}%"
        pct_rate = lambda x: f"{round(x, 1)} %/h"
        self.chart = VerticalHistoryChart(
            self.chart_frame, width=96, height=325,
            left_colors=("#10B981", "#EAB308", "#EF4444"), right_colors=("#F97316", "#7C3AED", "#2563EB"),
            left_text_color=COLOR_GEM_SAFE, right_text_color=COLOR_EXT_SAFE,
            left_hover=pct_hover, right_hover=pct_hover, left_rate=pct_rate, right_rate=pct_rate)
        self.chart.pack(fill=tk.BOTH, expand=True)

        self.gauges = {"gem_5h": self.gemini_5h_gauge, "ext_5h": self.ext_5h_gauge,
                       "gem_wk": self.gemini_wk_gauge, "ext_wk": self.ext_wk_gauge,
                       "arg_wk": self.argon_gauge}

    def set_view(self, show_weekly):
        if getattr(self, 'show_weekly', None) == show_weekly: return
        self.show_weekly = show_weekly
        self.toggle_switch.set_state(show_weekly)

        if self.show_weekly:
            self.lbl_5h.config(fg=TEXT_MUTED)
            self.lbl_wk.config(fg=TEXT_FG)
            self.view_5h_frame.pack_forget()
            self.view_wk_frame.pack(fill=tk.X)
        else:
            self.lbl_wk.config(fg=TEXT_MUTED)
            self.lbl_5h.config(fg=TEXT_FG)
            self.view_wk_frame.pack_forget()
            self.view_5h_frame.pack(fill=tk.X)

    def render_chart(self, history, now):
        if not history or len(history) < 2:
            self.chart.render(None, None)  # "Waiting..."
            return
        self.chart.render(bucket_drops(history, "gemini_5h", now, 100, 300.0),
                          bucket_drops(history, "external_5h", now, 100, 300.0))

    def apply(self, snapshot, history, now):
        _apply_bars(snapshot, self.gauges, datetime.now(timezone.utc))
        self.render_chart(history, now)


class ClaudePanel(tk.Frame):
    """Claude column: today's tokens, 5H/WK vertical bars, model bar, 5H-drop / token-rate chart."""

    def __init__(self, parent):
        super().__init__(parent, bg=BG_COLOR, highlightthickness=0, width=PANEL_WIDTH, height=PANEL_HEIGHT)

        # Top row: same height as AgyPanel's toggle row (tallest of its labels and the 16px toggle).
        probe = tk.Label(self, text="5H", font=("Segoe UI", 7, "bold"))
        row_h = max(16, probe.winfo_reqheight())
        probe.destroy()
        self.top_row = tk.Frame(self, bg=BG_COLOR, bd=0, height=row_h)
        self.top_row.pack(fill=tk.X, pady=8)
        self.top_row.pack_propagate(False)
        self.tokens_lbl = tk.Label(self.top_row, text=format_tokens(0), font=(DIGITAL_FONT, 7), bg=BG_COLOR, fg=TEXT_MUTED)
        self.tokens_lbl.pack(expand=True)

        # Vertical Fuel Bars (5H and WK side by side, no toggle)
        self.bars_frame = tk.Frame(self, bg=BG_COLOR, height=150)
        self.bars_frame.pack(fill=tk.X, pady=4)
        self.bars_frame.pack_propagate(False)
        self.gauge_5h = VerticalFuelGauge(self.bars_frame, width=44, height=150, title="5H", theme_id="cl_5h", text_side="left")
        self.gauge_5h.place(x=9, y=0)
        self.gauge_wk = VerticalFuelGauge(self.bars_frame, width=44, height=150, title="WK", theme_id="cl_wk", text_side="right")
        self.gauge_wk.place(x=57, y=0)

        # Horizontal model bar (OPS / SON)
        self.model_frame = tk.Frame(self, bg=BG_COLOR, height=36)
        self.model_frame.pack(fill=tk.X, pady=(0, 2), padx=7)
        self.model_frame.pack_propagate(False)
        self.model_gauge = HorizontalFuelGauge(self.model_frame, width=96, height=36, title="--", theme_id="cl_model")
        self.model_gauge.pack(fill=tk.BOTH, expand=True)

        # Vertical History Chart: left = 5H % drop, right = token blocks
        self.chart_frame = tk.Frame(self, bg=SURFACE_COLOR, bd=0, highlightthickness=0)
        self.chart_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8), padx=6)
        self.chart = VerticalHistoryChart(
            self.chart_frame, width=96, height=325,
            left_colors=("#A78BFA", "#E879F9", "#EF4444"), right_colors=("#818CF8", "#3B82F6", "#22D3EE"),
            left_text_color="#A78BFA", right_text_color="#818CF8",
            left_hover=lambda v: f"{v:.1f}%",
            right_hover=lambda v: f"{v * TOKENS_PER_BLOCK / 1000:.0f}k",
            left_rate=lambda x: f"{round(x, 1)} %/h",
            right_rate=lambda x: f"{x * TOKENS_PER_BLOCK / 1000:.0f} k/h")
        self.chart.pack(fill=tk.BOTH, expand=True)

        self.gauges = {"cl_5h": self.gauge_5h, "cl_wk": self.gauge_wk, "cl_model": self.model_gauge}

    def render_chart(self, history, events, now):
        has_5h = sum(1 for r in history if "claude_5h" in r) >= 2
        left = bucket_drops(history, "claude_5h", now, 100, 300.0) if has_5h else None
        right = bucket_tokens(events, now, 100, 300.0, TOKENS_PER_BLOCK)
        self.chart.render(left, right)

    def apply(self, snapshot, history, now):
        status = snapshot.get("status")
        if status in ("ok", "login_required"):  # stale states keep the last model label
            self.model_gauge.set_title((snapshot.get("bars", {}).get("cl_model") or {}).get("label", "--"))
        if status == "login_required":
            for gauge in self.gauges.values():
                gauge.set_login()
        else:
            _apply_bars(snapshot, self.gauges, datetime.now(timezone.utc))
        extra = snapshot.get("extra", {})
        self.tokens_lbl.config(text=format_tokens(extra.get("today_tokens", 0)))
        self.render_chart(history, extra.get("token_events", []), now)


class UsageWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AGY Fuel Gauge")
        
        # Total width 134 (24 tab + 110 main)
        self.width = 134
        self.base_height = 598
        self.root.geometry(f"{self.width}x{self.base_height}")
        
        # We use SetWindowRgn for the precise shape, so no transparent color is needed
        self.root.configure(bg=BG_COLOR)
        
        self.root.attributes('-topmost', True)
        self.root.overrideredirect(True)
        self.root.attributes('-alpha', 0.82)
        
        # Create true L-shaped rounded window region using Windows API
        self.root.update_idletasks()
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            # Main body: x=24 to 134, y=0 to base_height, with 12px rounding (6px radius)
            rgn_main = ctypes.windll.gdi32.CreateRoundRectRgn(24, 0, 134, self.base_height, 12, 12)
            # Tab: x=0 to 30, y=0 to 140
            rgn_tab = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, 30, 140, 12, 12)
            
            # Patch regions to fix the notches created by overlapping rounded corners
            rgn_top_patch = ctypes.windll.gdi32.CreateRectRgn(10, 0, 40, 12)      # Straight top edge
            rgn_inner_patch = ctypes.windll.gdi32.CreateRectRgn(24, 120, 30, 150) # Sharp inner corner
            
            # Combine them all
            rgn_combined = ctypes.windll.gdi32.CreateRectRgn(0, 0, 0, 0)
            ctypes.windll.gdi32.CombineRgn(rgn_combined, rgn_main, rgn_tab, 2) # RGN_OR
            ctypes.windll.gdi32.CombineRgn(rgn_combined, rgn_combined, rgn_top_patch, 2)
            ctypes.windll.gdi32.CombineRgn(rgn_combined, rgn_combined, rgn_inner_patch, 2)
            
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn_combined, True)
        except Exception as e:
            pass
            
        self.root.withdraw()
        
        self.x = 0
        self.y = 0
        
        self.build_ui()
        self.setup_tray()
        
        self.is_fetching = False
        self.update_thread = threading.Thread(target=self.auto_update_loop, daemon=True)
        self.update_thread.start()
        
    def build_ui(self):
        # ── Protruding Tab (Top Left) ────────
        self.tab_canvas = tk.Canvas(self.root, width=24, height=140, bg=BG_COLOR, highlightthickness=0)
        self.tab_canvas.place(x=0, y=0)
        self.tab_canvas.bind("<ButtonPress-1>", self.start_move)
        self.tab_canvas.bind("<B1-Motion>", self.do_move)
        
        # Vertical Text reading top-to-bottom
        self.tab_canvas.create_text(12, 70, text="AGY FUEL GAUGE", fill=TEXT_MUTED, font=("Segoe UI", 8, "bold"), angle=270)
        
        # ── Main Content Body ──────────────────────────
        self.main_frame = tk.Frame(self.root, bg=BG_COLOR, highlightthickness=0)
        self.main_frame.place(x=24, y=0, width=110, height=self.base_height)

        # Header
        self.header = tk.Frame(self.main_frame, bg=HEADER_COLOR, height=24)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)
        self.header.bind("<ButtonPress-1>", self.start_move)
        self.header.bind("<B1-Motion>", self.do_move)
        
        close_btn = tk.Label(self.header, text="✕", bg=HEADER_COLOR, fg=TEXT_MUTED, font=("Segoe UI", 8), cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=6)
        close_btn.bind("<Button-1>", lambda e: self.hide_window())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=COLOR_EXT_DANGER))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=TEXT_MUTED))
        
        self.refresh_btn = tk.Label(self.header, text="↻", bg=HEADER_COLOR, fg=TEXT_MUTED, font=("Segoe UI", 9), cursor="hand2")
        self.refresh_btn.pack(side=tk.RIGHT, padx=2)
        self.refresh_btn.bind("<Button-1>", lambda e: self.trigger_refresh())
        self.refresh_btn.bind("<Enter>", lambda e: self.refresh_btn.config(fg=TEXT_FG))
        self.refresh_btn.bind("<Leave>", lambda e: self.refresh_btn.config(fg=TEXT_MUTED if not getattr(self, "is_fetching", False) else COLOR_GEM_SAFE))
        
        self.last_update_lbl = tk.Label(self.header, text="--:--", bg=HEADER_COLOR, fg=TEXT_MUTED, font=(DIGITAL_FONT, 7))
        self.last_update_lbl.pack(side=tk.LEFT, padx=6)

        # Provider panels below the header (ClaudePanel is built but not shown yet).
        self.agy_panel = AgyPanel(self.main_frame)
        self.agy_panel.place(x=0, y=24, width=PANEL_WIDTH, height=PANEL_HEIGHT)
        self.claude_panel = ClaudePanel(self.main_frame)
        self.visible_panel = self.agy_panel

        self.agy_panel.render_chart(history_logger.get_history(minutes=360), datetime.now())

    @property
    def chart(self):
        """Chart of the panel currently on screen (scanline animation target)."""
        return self.visible_panel.chart

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

    def create_tray_icon(self):
        try:
            icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
            return Image.open(icon_path)
        except Exception:
            img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
            dc = ImageDraw.Draw(img)
            dc.ellipse([8, 8, 56, 56], fill=COLOR_GEM_SAFE)
            return img

    def setup_tray(self):
        menu = pystray.Menu(
            pystray.MenuItem('Show Widget', self.show_window, default=True),
            pystray.MenuItem('Refresh Now', self.trigger_refresh),
            pystray.MenuItem('Exit', self.exit_app)
        )
        self.icon = pystray.Icon("AGYMonitor", self.create_tray_icon(), "AGY Fuel Gauge", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def show_window(self, icon=None, item=None):
        self.root.deiconify()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = screen_width - self.width - 20
        y = screen_height - self.base_height - 60
        self.root.geometry(f"+{x}+{y}")
        self.root.lift()
        self.trigger_refresh()

    def hide_window(self):
        self.root.withdraw()

    def exit_app(self, icon=None, item=None):
        self.icon.stop()
        self.root.quit()
        sys.exit(0)
        
    def trigger_refresh(self):
        if not self.is_fetching:
            threading.Thread(target=self.fetch_and_update, daemon=True).start()
            
    def fetch_and_update(self):
        self.is_fetching = True
        self.root.after(0, lambda: self.refresh_btn.config(fg=COLOR_GEM_SAFE))
        try:
            data = data_fetcher.fetch_all()
            self.root.after(0, self.update_ui_with_data, data)
        except Exception as e:
            print(f"[AGY Fuel Gauge] Fetch failed: {e}")
        finally:
            self.is_fetching = False
            self.root.after(0, lambda: self.refresh_btn.config(fg=TEXT_MUTED))
            
    def update_ui_with_data(self, data):
        now = datetime.now()
        self.last_update_lbl.config(text=now.strftime("%H:%M"))

        history = history_logger.get_history(minutes=360)
        self.agy_panel.apply(data["agy"], history, now)
        self.claude_panel.apply(data["claude"], history, now)

        # Trigger scanline micro-animation
        self.play_scanline_animation()

    def play_scanline_animation(self):
        # Subtle scanline sweeping across the chart area on each data refresh.
        # Thin neon-white-green polyline, variable speed with random jitter (creases).
        scan_line = self.chart.create_line(0, 0, 0, 0, fill="#5C856D", width=1)
        
        STEPS = 25  # Increased steps for a smoother, slower sweep
        def animate(step=0):
            if step > STEPS:
                self.chart.delete(scan_line)
                return
            base_y = (step / STEPS) * self.chart.height
            
            # Generate polyline coordinates for creases
            coords = []
            x_segments = [0, self.chart.width * 0.3, self.chart.width * 0.6, self.chart.width]
            
            for x in x_segments:
                # Balanced jitter magnitude for visible but subtle creases
                jitter = random.randint(-2, 2) if step > 0 and step < STEPS else 0
                y = max(0, min(self.chart.height, base_y + jitter))
                coords.extend([x, y])
            
            # Add glitch effect: randomly drop opacity/visibility for a frame
            if random.random() < 0.15:
                self.chart.itemconfig(scan_line, fill="#000000") # blend into background
            else:
                self.chart.itemconfig(scan_line, fill="#5C856D")
                
            self.chart.coords(scan_line, *coords)
            # Increased delay base to slow down the overall animation
            delay = random.randint(40, 90) # variable speed
            self.root.after(delay, lambda: animate(step + 1))
            
        animate(0)

    def auto_update_loop(self):
        while True:
            time.sleep(180)
            self.trigger_refresh()

    def run(self):
        self.show_window()
        self.root.mainloop()

if __name__ == "__main__":
    _acquire_single_instance_lock()
    app = UsageWidget()
    app.run()




