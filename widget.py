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

# ── DPI Awareness & Dynamic Scaling ────────────────────────────────────────
# Enable DPI awareness to bypass Windows Desktop Window Manager (DWM) scaling.
# This forces 1 virtual pixel to map exactly to 1 physical pixel,
# mathematically eliminating any subpixel rounding gaps in rendering.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

def _detect_dpi_scale():
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX = 88
        ctypes.windll.user32.ReleaseDC(0, hdc)
        if dpi > 0:
            return dpi / 96.0
    except Exception:
        pass
    return 1.0

DPI_SCALE = _detect_dpi_scale()

def S(val):
    """Scale a 96-DPI base pixel value to physical integer pixels."""
    return int(round(val * DPI_SCALE))

def chart_row_step():
    """Physical pixels per 3-minute chart row: an integer, so every row gets identical pixels."""
    return max(3, int(round(3 * DPI_SCALE + 0.001)))

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
import display_logic
from display_logic import (MODES, STATE_FILE, TOKENS_PER_BLOCK,
                           anchored_x, bucket_drops, bucket_tokens, count_records,
                           format_countdown, format_tokens, mode_width, rate_color)

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
            self._title_color = TEXT_MUTED  # no value: grey title, e.g. the "--" model bar
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
    def __init__(self, parent, width=None, height=None, title="Title", theme_id="gem", text_side="right", **kwargs):
        width = S(44) if width is None else width
        height = S(200) if height is None else height
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self._init_state(theme_id)

        # Dimensions for the vertical bar
        self.bar_width = S(12)
        self.core_width = max(2, self.bar_width - S(4))
        self.padding_top = S(35)
        self.padding_bottom = S(40)

        self.cx = self.width / 2
        self.y_bottom = self.height - self.padding_bottom
        self.y_top = self.padding_top
        self.track_height = self.y_bottom - self.y_top

        # Track
        self.create_line(self.cx, self.y_bottom, self.cx, self.y_top, fill=TRACK_BG, width=self.bar_width, capstyle=tk.ROUND)

        # Reverted Glow (Base + Core)
        self.line_base = self.create_line(self.cx, self.y_bottom, self.cx, self.y_bottom, fill="#000", width=self.bar_width, capstyle=tk.ROUND)
        self.line_core = self.create_line(self.cx, self.y_bottom, self.cx, self.y_bottom, fill="#000", width=self.core_width, capstyle=tk.ROUND)
        self.tip_dot = self.create_oval(0, 0, 0, 0, fill="#000", outline="", state="hidden")

        # Percentage (JetBrains Mono)
        self.pct_text = self.create_text(self.cx, self.y_bottom + S(18), text="--%", fill=TEXT_FG, font=(DIGITAL_FONT, 10, "bold"), justify="center")

        self.title_text = self.create_text(self.cx, self.y_top - S(16), text=title, fill=self._title_color, font=(DIGITAL_FONT, 8, "bold"), justify="center")

        text_x = self.cx + S(14) if text_side == "right" else self.cx - S(14)
        self.time_text = self.create_text(text_x, self.height / 2, text="--h", fill=TEXT_MUTED, font=(DIGITAL_FONT, 8), justify="center", angle=270)

    def _fill_coords(self, val):
        # When val=0, leave a clean scaled stub so the track bottom is visible
        stub = max(2, S(2))
        curr_y = (self.y_bottom - stub) if val == 0.0 else (self.y_bottom - self.track_height * (val / 100.0))
        return (self.cx, self.y_bottom, self.cx, curr_y)

    def _tip_coords(self, coords):
        r = self.core_width / 2
        curr_y = coords[3]
        return (self.cx - r, curr_y - r, self.cx + r, curr_y + r)


class HorizontalFuelGauge(_FuelGaugeBase):
    def __init__(self, parent, width=None, height=None, title="ARG", theme_id="arg", **kwargs):
        width = S(96) if width is None else width
        height = S(36) if height is None else height
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self._init_state(theme_id)

        self.bar_width = S(10)
        self.core_width = max(2, self.bar_width - S(4))
        self.x_left = S(8)
        self.x_right = self.width - S(8)
        self.track_width = self.x_right - self.x_left
        self.cy = S(25)
        self.lbl_y = S(9)

        # Top telemetry labels: Title (Left), Reset Timer (Center), Percentage (Right)
        self.title_text = self.create_text(
            self.x_left - S(2), self.lbl_y, text=title, fill=self._title_color,
            font=(DIGITAL_FONT, 8, "bold"), anchor="w"
        )
        self.time_text = self.create_text(
            self.width / 2, self.lbl_y, text="--", fill=TEXT_MUTED,
            font=(DIGITAL_FONT, 7), anchor="center"
        )
        self.pct_text = self.create_text(
            self.x_right + S(2), self.lbl_y, text="--%", fill=TEXT_FG,
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
            fill="#000", width=self.core_width, capstyle=tk.ROUND
        )
        self.tip_dot = self.create_oval(0, 0, 0, 0, fill="#000", outline="", state="hidden")

    def _fill_coords(self, val):
        stub = max(2, S(2))
        curr_x = (self.x_left + stub) if val == 0.0 else (self.x_left + self.track_width * (val / 100.0))
        return (self.x_left, self.cy, curr_x, self.cy)

    def _tip_coords(self, coords):
        r = self.core_width / 2
        curr_x = coords[2]
        return (curr_x - r, self.cy - r, curr_x + r, self.cy + r)

    def set_value(self, pct, reset, now_utc):
        self.coords(self.time_text, self.width / 2, self.lbl_y)
        super().set_value(pct, reset, now_utc)

    def _clear(self, pct_text, time_text, color=TEXT_FG):
        # Undo set_login's shift (e.g. login -> offline); set_login re-shifts after this.
        self.coords(self.time_text, self.width / 2, self.lbl_y)
        super()._clear(pct_text, time_text, color)

    def set_login(self):
        super().set_login()
        # "LOGIN" is wider than a percentage: centre "/login" in the gap left of it.
        title_bb, pct_bb = self.bbox(self.title_text), self.bbox(self.pct_text)
        left = title_bb[2] if title_bb and self.itemcget(self.title_text, "text") else self.x_left - S(2)
        if pct_bb:
            self.coords(self.time_text, (left + pct_bb[0]) / 2, self.lbl_y)


class VerticalHistoryChart(tk.Canvas):
    """Two-sided burn chart. Values arrive pre-bucketed, in blocks (row 0 = newest)."""

    def __init__(self, parent, width=None, height=None, *, left_colors, right_colors, left_text_color, right_text_color,
                 left_hover, right_hover, left_rate, right_rate, left_alert=True, right_alert=True, **kwargs):
        self.num_rows = 100
        self.row_step = chart_row_step()
        self.block_step = 3.0 * DPI_SCALE
        self.y_top = S(10)
        self.y_range = self.num_rows * self.row_step
        self.y_bottom = self.y_top + self.y_range
        width = S(96) if width is None else width
        height = (self.y_bottom + S(15)) if height is None else height
        super().__init__(parent, width=width, height=height, bg="#000000", highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.buckets = []
        self.left_text_color = left_text_color
        self.right_text_color = right_text_color
        self.left_hover, self.right_hover = left_hover, right_hover
        self.left_rate, self.right_rate = left_rate, right_rate
        self.left_alert, self.right_alert = left_alert, right_alert
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
        axis_l = mid_x - max(2, S(2))
        axis_r = mid_x + max(3, S(3))

        self.grad_dim = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        self.grad_bright = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        d_dim = ImageDraw.Draw(self.grad_dim)
        d_bright = ImageDraw.Draw(self.grad_bright)

        for side, colors in (("left", left_colors), ("right", right_colors)):
            c_low, c_mid, c_high = (hex2rgb(c) for c in colors)
            for b in range(11):
                if side == "left":
                    x_right = round(axis_l - (b * self.block_step))
                    x_left = round(axis_l - ((b + 1) * self.block_step))
                else:
                    x_left = round(axis_r + (b * self.block_step))
                    x_right = round(axis_r + ((b + 1) * self.block_step))

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
        self.y_top = S(10)
        # Force y_range to be an exact integer multiple of num_rows (100 * row_step)
        # so every 3-minute bucket gets an identical integer number of physical pixels.
        y_range = self.num_rows * self.row_step
        self.y_range = y_range
        self.y_bottom = self.y_top + y_range

        # Center axis line
        self.create_line(mid_x, self.y_top, mid_x, self.y_bottom, fill="#1E293B", width=max(2, S(2)))

        # Time Ticks (Every 1h small, every 2h big)
        for h in range(1, 5):
            tick_y = int(self.y_top + (h / 5.0) * y_range)
            if h % 2 == 0:
                # 2H, 4H (Big tick)
                self.create_line(mid_x - S(3), tick_y, mid_x + S(3), tick_y, fill=TEXT_FG, width=max(1.5, 1.5 * DPI_SCALE))
            else:
                # 1H, 3H, 5H (Small tick)
                self.create_line(mid_x - max(1, S(1)), tick_y, mid_x + max(1, S(1)), tick_y, fill=TEXT_MUTED, width=max(1.0, 1.0 * DPI_SCALE))

        buckets = list(zip(left, right))
        self.buckets = buckets

        # ABSOLUTE FIXED SCALE: 1 unit per full block (block_step px). Max 10 blocks.
        axis_l = mid_x - max(2, S(2))
        axis_r = mid_x + max(3, S(3))

        # 1. Draw continuous organic waveform polygon for the entire history.
        # When two consecutive 3-minute buckets both have non-zero drops, insert a return-to-axis
        # valley vertex at the bucket boundary (y_valley) so adjacent peaks never fuse into a flat block.
        mask_poly = Image.new("L", (self.width, self.height), 0)
        d_poly = ImageDraw.Draw(mask_poly)

        pts_g = [(axis_l, self.y_top)]
        pts_e = [(axis_r, self.y_top)]

        for idx, (g, e) in enumerate(buckets):
            y0 = self.y_top + idx * self.row_step
            cy = y0 + (self.row_step - 1) // 2
            y_valley = y0 + self.row_step - 1

            # LEFT
            offset_g = min(10.0, g) * self.block_step
            xg = round(axis_l - offset_g)
            pts_g.append((xg, cy))
            if idx + 1 < len(buckets) and g > 0 and buckets[idx + 1][0] > 0:
                pts_g.append((axis_l, y_valley))

            # RIGHT
            offset_e = min(10.0, e) * self.block_step
            xe = round(axis_r + offset_e)
            pts_e.append((xe, cy))
            if idx + 1 < len(buckets) and e > 0 and buckets[idx + 1][1] > 0:
                pts_e.append((axis_r, y_valley))

        pts_g.append((axis_l, self.y_bottom))
        pts_e.append((axis_r, self.y_bottom))

        d_poly.polygon(pts_g, fill=255)
        d_poly.polygon(pts_e, fill=255)

        # 2. Identify Overflow Spikes (>= 10.0 blocks)
        mask_over = Image.new("L", (self.width, self.height), 0)
        d_over = ImageDraw.Draw(mask_over)

        for idx, (g, e) in enumerate(buckets):
            y0 = self.y_top + idx * self.row_step

            if g >= 10.0:
                g_top = (y0 - self.row_step // 2) if (idx > 0 and buckets[idx - 1][0] == 0) else y0
                g_bot = (y0 + self.row_step + self.row_step // 2 - 1) if (idx + 1 < len(buckets) and buckets[idx + 1][0] == 0) else (y0 + self.row_step - 2)
                d_over.rectangle([0, max(0, g_top), mid_x - 1, min(self.height - 1, g_bot)], fill=255)
            if e >= 10.0:
                e_top = (y0 - self.row_step // 2) if (idx > 0 and buckets[idx - 1][1] == 0) else y0
                e_bot = (y0 + self.row_step + self.row_step // 2 - 1) if (idx + 1 < len(buckets) and buckets[idx + 1][1] == 0) else (y0 + self.row_step - 2)
                d_over.rectangle([mid_x + 1, max(0, e_top), self.width, min(self.height - 1, e_bot)], fill=255)

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
        left_color = rate_color(left_hourly, self.left_text_color, self.left_alert)
        right_color = rate_color(right_hourly, self.right_text_color, self.right_alert)

        # Floating Burn Rates (Reading top-to-bottom)
        self.create_text(S(8), self.height / 2, text=self.left_rate(left_hourly), fill=left_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")
        self.create_text(self.width - S(8), self.height / 2, text=self.right_rate(right_hourly), fill=right_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")

        # -5H label pushed snugly below the bottom axis
        self.create_text(mid_x, self.y_bottom + S(13), text="-5H", fill="#94A3B8", font=("Segoe UI", 7, "bold"), anchor="s")

    def on_mouse_move(self, event):
        self.delete("tooltip")
        if not hasattr(self, 'buckets') or not self.buckets:
            return

        y = event.y
        if y < self.y_top or y >= self.y_bottom:
            return

        idx = int((y - self.y_top) // self.row_step)
        if 0 <= idx < len(self.buckets):
            g, e = self.buckets[idx]
            if g == 0 and e == 0:
                return

            bucket_y = self.y_top + idx * self.row_step

            # Direction 3: Subtle row highlight band
            self.create_rectangle(0, bucket_y - 1, self.width, bucket_y + self.row_step - 1, outline="#1E293B", fill="#1E293B", tags="tooltip")

            # Crosshair line (rendered on top of the highlight band)
            self.create_line(0, bucket_y, self.width, bucket_y, fill="#475569", dash=(1, 2), tags="tooltip")

            # Clamp text_y within vertical bounds
            text_y = max(self.y_top + S(6), min(self.y_bottom - S(6), y))

            # Direction 1: Split labels — fixed left, fixed right
            for text, color, x, anchor_val in [
                (self.left_hover(g), self.left_text_color, S(2), "w"),
                (self.right_hover(e), self.right_text_color, self.width - S(2), "e"),
            ]:
                lbl = self.create_text(x, text_y, text=text, fill=color, font=("Segoe UI", 7, "bold"), anchor=anchor_val, tags="tooltip")
                bbox = self.bbox(lbl)
                if bbox:
                    bg = self.create_rectangle(bbox[0] - S(2), bbox[1] - 1, bbox[2] + S(2), bbox[3] + 1, fill="#0F172A", outline="", tags="tooltip")
                    self.tag_lower(bg, lbl)

    def on_mouse_leave(self, event):
        self.delete("tooltip")


class SlidingToggle(tk.Canvas):
    def __init__(self, parent, command=None, *args, **kwargs):
        self.w = S(32)
        self.h = S(16)
        self.pad = max(2, S(2))
        self.knob_d = self.h - 2 * self.pad
        tk.Canvas.__init__(self, parent, width=self.w, height=self.h, bg=BG_COLOR, highlightthickness=0, bd=0, *args, **kwargs)
        self.command = command
        self.is_on = False

        # Draw pill background (using an oval left, oval right, and rectangle in middle)
        self.bg_color_off = "#333842"
        self.bg_color_on = "#3A4B6B" # Muted blue-grey, less prominent
        self.create_oval(0, 0, self.h, self.h, fill=self.bg_color_off, outline="", tags="bg")
        self.create_oval(self.w - self.h, 0, self.w, self.h, fill=self.bg_color_off, outline="", tags="bg")
        self.create_rectangle(self.h // 2, 0, self.w - self.h // 2, self.h, fill=self.bg_color_off, outline="", tags="bg")

        # Draw slider circle
        self.slider = self.create_oval(self.pad, self.pad, self.pad + self.knob_d, self.pad + self.knob_d, fill="#FFFFFF", outline="")
        
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
            
        # Update slider position
        travel = self.w - self.h
        x_offset = self.pad + (travel * next_progress)
        self.coords(self.slider, x_offset, self.pad, x_offset + self.knob_d, self.pad + self.knob_d)
        
        # Update background color at midpoint
        if next_progress > 0.5:
            self.itemconfig("bg", fill=self.bg_color_on)
        else:
            self.itemconfig("bg", fill=self.bg_color_off)
            
        # Use tolerance check instead of exact float equality
        if abs(next_progress - target_progress) > 0.001:
            self.after(16, lambda: self.animate(target_progress, next_progress))


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

    def __init__(self, parent, width, height):
        super().__init__(parent, bg=BG_COLOR, highlightthickness=0, width=width, height=height)

        # Toggle
        self.toggle_container = tk.Frame(self, bg=BG_COLOR, bd=0)
        self.toggle_container.pack(pady=S(8))

        self.lbl_5h = tk.Label(self.toggle_container, text="5H", font=("Segoe UI", 7, "bold"), bg=BG_COLOR, fg=TEXT_FG, cursor="hand2")
        self.lbl_5h.pack(side=tk.LEFT, padx=S(4))
        self.lbl_5h.bind("<Button-1>", lambda e: self.set_view(False))

        self.toggle_switch = SlidingToggle(self.toggle_container, command=self.set_view)
        self.toggle_switch.pack(side=tk.LEFT)

        self.lbl_wk = tk.Label(self.toggle_container, text="WK", font=("Segoe UI", 7, "bold"), bg=BG_COLOR, fg=TEXT_MUTED, cursor="hand2")
        self.lbl_wk.pack(side=tk.LEFT, padx=S(4))
        self.lbl_wk.bind("<Button-1>", lambda e: self.set_view(True))

        self.show_weekly = False

        # Vertical Fuel Bars
        self.bars_frame = tk.Frame(self, bg=BG_COLOR)
        self.bars_frame.pack(fill=tk.X, pady=S(4))

        gauge_w, gauge_h = S(44), S(150)
        self.view_5h_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=gauge_h)
        self.view_5h_frame.pack(fill=tk.X)
        self.view_5h_frame.pack_propagate(False)
        self.gemini_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=gauge_w, height=gauge_h, title="GEM", theme_id="gem", text_side="left")
        self.gemini_5h_gauge.place(x=S(9), y=0)
        self.ext_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=gauge_w, height=gauge_h, title="EXT", theme_id="ext", text_side="right")
        self.ext_5h_gauge.place(x=S(57), y=0)

        self.view_wk_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=gauge_h)
        self.view_wk_frame.pack_propagate(False)
        self.gemini_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=gauge_w, height=gauge_h, title="GEM", theme_id="gem", text_side="left")
        self.gemini_wk_gauge.place(x=S(9), y=0)
        self.ext_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=gauge_w, height=gauge_h, title="EXT", theme_id="ext", text_side="right")
        self.ext_wk_gauge.place(x=S(57), y=0)

        # Horizontal Argon Weekly Fuel Bar
        self.argon_frame = tk.Frame(self, bg=BG_COLOR, height=S(36))
        self.argon_frame.pack(fill=tk.X, pady=(0, S(2)), padx=S(7))
        self.argon_frame.pack_propagate(False)
        self.argon_gauge = HorizontalFuelGauge(self.argon_frame, width=S(96), height=S(36), title="ARG", theme_id="arg")
        self.argon_gauge.pack(fill=tk.BOTH, expand=True)

        # Vertical History Chart
        self.chart_frame = tk.Frame(self, bg=SURFACE_COLOR, bd=0, highlightthickness=0)
        self.chart_frame.pack(fill=tk.BOTH, expand=True, pady=(S(4), S(8)), padx=S(6))

        pct_hover = lambda v: f"{v:.1f}%"
        pct_rate = lambda x: f"{round(x, 1)} %/h"
        self.chart = VerticalHistoryChart(
            self.chart_frame, width=S(96),
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
        if count_records(history or [], ("gemini_5h", "external_5h")) < 2:
            self.chart.render(None, None)  # "Waiting..."
            return
        self.chart.render(bucket_drops(history, "gemini_5h", now, 100, 300.0),
                          bucket_drops(history, "external_5h", now, 100, 300.0))

    def apply(self, snapshot, history, now):
        _apply_bars(snapshot, self.gauges, datetime.now(timezone.utc))
        self.render_chart(history, now)


class ClaudePanel(tk.Frame):
    """Claude column: today's tokens, 5H/WK vertical bars, model bar, 5H-drop / token-rate chart."""

    def __init__(self, parent, width, height):
        super().__init__(parent, bg=BG_COLOR, highlightthickness=0, width=width, height=height)

        # Top row: same height as AgyPanel's toggle row (tallest of its labels and the S(16) toggle).
        probe = tk.Label(self, text="5H", font=("Segoe UI", 7, "bold"))
        row_h = max(S(16), probe.winfo_reqheight())
        probe.destroy()
        self.top_row = tk.Frame(self, bg=BG_COLOR, bd=0, height=row_h)
        self.top_row.pack(fill=tk.X, pady=S(8))
        self.top_row.pack_propagate(False)
        self.tokens_lbl = tk.Label(self.top_row, text=format_tokens(0), font=(DIGITAL_FONT, 7), bg=BG_COLOR, fg=TEXT_MUTED)
        self.tokens_lbl.pack(expand=True)

        # Vertical Fuel Bars (5H and WK side by side, no toggle)
        gauge_w, gauge_h = S(44), S(150)
        self.bars_frame = tk.Frame(self, bg=BG_COLOR, height=gauge_h)
        self.bars_frame.pack(fill=tk.X, pady=S(4))
        self.bars_frame.pack_propagate(False)
        self.gauge_5h = VerticalFuelGauge(self.bars_frame, width=gauge_w, height=gauge_h, title="5H", theme_id="cl_5h", text_side="left")
        self.gauge_5h.place(x=S(9), y=0)
        self.gauge_wk = VerticalFuelGauge(self.bars_frame, width=gauge_w, height=gauge_h, title="WK", theme_id="cl_wk", text_side="right")
        self.gauge_wk.place(x=S(57), y=0)

        # Horizontal model bar (OPS / SON)
        self.model_frame = tk.Frame(self, bg=BG_COLOR, height=S(36))
        self.model_frame.pack(fill=tk.X, pady=(0, S(2)), padx=S(7))
        self.model_frame.pack_propagate(False)
        self.model_gauge = HorizontalFuelGauge(self.model_frame, width=S(96), height=S(36), title="--", theme_id="cl_model")
        self.model_gauge.pack(fill=tk.BOTH, expand=True)

        # Vertical History Chart: left = 5H % drop, right = token blocks
        self.chart_frame = tk.Frame(self, bg=SURFACE_COLOR, bd=0, highlightthickness=0)
        self.chart_frame.pack(fill=tk.BOTH, expand=True, pady=(S(4), S(8)), padx=S(6))
        self.chart = VerticalHistoryChart(
            self.chart_frame, width=S(96),
            left_colors=("#A78BFA", "#E879F9", "#EF4444"), right_colors=("#818CF8", "#3B82F6", "#22D3EE"),
            left_text_color="#A78BFA", right_text_color="#818CF8",
            left_hover=lambda v: f"{v:.1f}%",
            right_hover=lambda v: f"{v * TOKENS_PER_BLOCK / 1000:.0f}k",
            left_rate=lambda x: f"{round(x, 1)} %/h",
            right_rate=lambda x: f"{x * TOKENS_PER_BLOCK / 1000:.0f} k/h",
            right_alert=False)  # token rate is throughput, not a quota: never red
        self.chart.pack(fill=tk.BOTH, expand=True)

        self.gauges = {"cl_5h": self.gauge_5h, "cl_wk": self.gauge_wk, "cl_model": self.model_gauge}
        self._model_label = "--"

    def render_chart(self, history, events, now):
        has_5h = count_records(history, ("claude_5h",)) >= 2
        left = bucket_drops(history, "claude_5h", now, 100, 300.0) if has_5h else None
        right = bucket_tokens(events, now, 100, 300.0, TOKENS_PER_BLOCK)
        self.chart.render(left, right)

    def apply(self, snapshot, history, now):
        status = snapshot.get("status")
        if status == "ok":  # stale states keep the last model label
            self._model_label = (snapshot.get("bars", {}).get("cl_model") or {}).get("label", "--")
        # Login state blanks the title so "/login" and "LOGIN" are not cramped.
        self.model_gauge.set_title("" if status == "login_required" else self._model_label)
        if status == "login_required":
            for gauge in self.gauges.values():
                gauge.set_login()
        else:
            _apply_bars(snapshot, self.gauges, datetime.now(timezone.utc))
        extra = snapshot.get("extra", {})
        self.tokens_lbl.config(text=format_tokens(extra.get("today_tokens", 0)))
        self.render_chart(history, extra.get("token_events", []), now)


def build_window_region(width, height):
    """L-shaped rounded HRGN: main body x=S(24)..width plus the 0..S(30) x 0..S(140) tab."""
    gdi32 = ctypes.windll.gdi32
    tab_w = S(24)
    corner_r = S(12)
    # Main body: x=tab_w to width, y=0 to height
    rgn_main = gdi32.CreateRoundRectRgn(tab_w, 0, width, height, corner_r, corner_r)
    # Tab: x=0 to S(30), y=0 to S(140)
    rgn_tab = gdi32.CreateRoundRectRgn(0, 0, S(30), S(140), corner_r, corner_r)
    # Patch regions to fix the notches created by overlapping rounded corners
    rgn_top_patch = gdi32.CreateRectRgn(S(10), 0, S(40), corner_r)          # Straight top edge
    rgn_inner_patch = gdi32.CreateRectRgn(tab_w, S(120), S(30), S(150)) # Sharp inner corner
    # Combine them all
    rgn_combined = gdi32.CreateRectRgn(0, 0, 0, 0)
    gdi32.CombineRgn(rgn_combined, rgn_main, rgn_tab, 2) # RGN_OR
    gdi32.CombineRgn(rgn_combined, rgn_combined, rgn_top_patch, 2)
    gdi32.CombineRgn(rgn_combined, rgn_combined, rgn_inner_patch, 2)
    for rgn in (rgn_main, rgn_tab, rgn_top_patch, rgn_inner_patch):
        gdi32.DeleteObject(rgn)
    return rgn_combined


MODE_COLORS = {"agy": "#22C55E", "claude": "#D97757", "both": "#F8FAFC"}


class ModeTab(tk.Canvas):
    """S(24)xS(140) tab with AGY / CLD / BOTH segments; a click switches mode, a drag moves the window."""

    SEGMENTS = (("agy", "AGY", 0, 46), ("claude", "CLD", 46, 93), ("both", "BOTH", 93, 140))  # 96-DPI y ranges
    DRAG_SLOP = 3

    def __init__(self, parent, on_select, on_press, on_drag):
        tab_w = S(24)
        super().__init__(parent, width=tab_w, height=S(140), bg=BG_COLOR, highlightthickness=0)
        self.on_select, self.on_press, self.on_drag = on_select, on_press, on_drag
        # Segment boundaries in physical pixels (scaled once, at the DPI the tab was built with).
        self.segments = [(mode, text, S(y0), S(y1)) for mode, text, y0, y1 in self.SEGMENTS]
        self.drag_slop = S(self.DRAG_SLOP)
        self.labels = {}
        for mode, text, y0, y1 in self.segments:
            self.labels[mode] = self.create_text(tab_w / 2, (y0 + y1) / 2, text=text, fill=TEXT_MUTED,
                                                 font=("Segoe UI", 7, "bold"), angle=270)
        for _, _, y, _ in self.segments[1:]:
            self.create_line(S(4), y, S(20), y, fill="#1E293B", width=S(1))
        self._press = None
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<B1-Motion>", self._on_motion)
        self.bind("<ButtonRelease-1>", self._on_release)

    def set_mode(self, mode):
        for m, item in self.labels.items():
            self.itemconfig(item, fill=MODE_COLORS[m] if m == mode else TEXT_MUTED)

    def _on_press(self, event):
        self._press = (event.x_root, event.y_root, event.y, False)
        self.on_press(event)

    def _on_motion(self, event):
        if self._press is None:
            return
        x0, y0, y_local, dragged = self._press
        if not dragged and max(abs(event.x_root - x0), abs(event.y_root - y0)) > self.drag_slop:
            self._press = (x0, y0, y_local, True)
        self.on_drag(event)

    def _on_release(self, event):
        press, self._press = self._press, None
        if press is None or press[3]:
            return
        for mode, _, y0, y1 in self.segments:
            if y0 <= press[2] < y1:
                self.on_select(mode)
                return


class UsageWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AGY Fuel Gauge")
        
        # Sync Tkinter font point-to-pixel scaling explicitly with detected DPI_SCALE
        try:
            self.root.tk.call('tk', 'scaling', (96.0 / 72.0) * DPI_SCALE)
        except Exception:
            pass

        # Single-mode width 134 at 96 DPI (24 tab + 110 panel), scaled proportionally by DPI_SCALE;
        # BOTH mode adds a second panel (set_mode).
        self.tab_w = S(24)
        self.panel_w = S(110)
        self.header_h = S(24)
        self.width = self.tab_w + self.panel_w
        self.chart_h = S(10) + (100 * chart_row_step()) + S(15)
        self.base_height = S(273) + self.chart_h
        self.panel_h = self.base_height - self.header_h
        self.root.geometry(f"{self.width}x{self.base_height}")
        
        # We use SetWindowRgn for the precise shape, so no transparent color is needed
        self.root.configure(bg=BG_COLOR)
        
        self.root.attributes('-topmost', True)
        self.root.overrideredirect(True)
        self.root.attributes('-alpha', 0.82)
        
        # Create true L-shaped rounded window region using Windows API
        self.root.update_idletasks()
        self.apply_window_region()

        self.root.withdraw()

        self.x = 0
        self.y = 0

        self.mode = display_logic.load_mode(STATE_FILE)
        self.build_ui()
        self.set_mode(self.mode)
        self.setup_tray()
        
        self.is_fetching = False
        self._fetch_lock = threading.Lock()
        self.update_thread = threading.Thread(target=self.auto_update_loop, daemon=True)
        self.update_thread.start()
        
    def build_ui(self):
        # ── Protruding Tab (Top Left) ────────
        self.mode_tab = ModeTab(self.root, on_select=self.set_mode, on_press=self.start_move, on_drag=self.do_move)
        self.mode_tab.place(x=0, y=0)

        # ── Main Content Body ──────────────────────────
        self.main_frame = tk.Frame(self.root, bg=BG_COLOR, highlightthickness=0)
        self.main_frame.place(x=self.tab_w, y=0, width=self.panel_w, height=self.base_height)

        # Header
        self.header = tk.Frame(self.main_frame, bg=HEADER_COLOR, height=self.header_h)
        self.header.pack(fill=tk.X)
        self.header.pack_propagate(False)
        self.header.bind("<ButtonPress-1>", self.start_move)
        self.header.bind("<B1-Motion>", self.do_move)

        close_btn = tk.Label(self.header, text="✕", bg=HEADER_COLOR, fg=TEXT_MUTED, font=("Segoe UI", 8), cursor="hand2")
        close_btn.pack(side=tk.RIGHT, padx=S(6))
        close_btn.bind("<Button-1>", lambda e: self.hide_window())
        close_btn.bind("<Enter>", lambda e: close_btn.config(fg=COLOR_EXT_DANGER))
        close_btn.bind("<Leave>", lambda e: close_btn.config(fg=TEXT_MUTED))
        
        self.refresh_btn = tk.Label(self.header, text="↻", bg=HEADER_COLOR, fg=TEXT_MUTED, font=("Segoe UI", 9), cursor="hand2")
        self.refresh_btn.pack(side=tk.RIGHT, padx=S(2))
        self.refresh_btn.bind("<Button-1>", lambda e: self.trigger_refresh())
        self.refresh_btn.bind("<Enter>", lambda e: self.refresh_btn.config(fg=TEXT_FG))
        self.refresh_btn.bind("<Leave>", lambda e: self.refresh_btn.config(fg=TEXT_MUTED if not getattr(self, "is_fetching", False) else COLOR_GEM_SAFE))
        
        self.last_update_lbl = tk.Label(self.header, text="--:--", bg=HEADER_COLOR, fg=TEXT_MUTED, font=(DIGITAL_FONT, 7))
        self.last_update_lbl.pack(side=tk.LEFT, padx=S(6))

        # Provider panels below the header; set_mode decides which are placed.
        self.agy_panel = AgyPanel(self.main_frame, self.panel_w, self.panel_h)
        self.agy_panel.place(x=0, y=self.header_h, width=self.panel_w, height=self.panel_h)
        self.claude_panel = ClaudePanel(self.main_frame, self.panel_w, self.panel_h)
        self.visible_panels = [self.agy_panel]

        self.agy_panel.render_chart(history_logger.get_history(minutes=360), datetime.now())

    @property
    def visible_panel(self):
        """Leftmost panel on screen."""
        return self.visible_panels[0]

    @property
    def chart(self):
        """Chart of the leftmost panel on screen."""
        return self.visible_panel.chart

    def apply_window_region(self):
        try:
            hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
            ctypes.windll.user32.SetWindowRgn(hwnd, build_window_region(self.width, self.base_height), True)
        except Exception as e:
            print(f"[widget] SetWindowRgn failed: {e}")

    def set_mode(self, mode):
        if mode not in MODES:
            mode = "agy"
        self.mode = mode
        # Panels sit inside main_frame (root x=tab_w): AGY at root x=tab_w, CLD at tab_w or tab_w+panel_w.
        panels = {"agy": [self.agy_panel], "claude": [self.claude_panel],
                  "both": [self.agy_panel, self.claude_panel]}[mode]
        for panel in (self.agy_panel, self.claude_panel):
            if panel not in panels:
                panel.place_forget()
        for i, panel in enumerate(panels):
            panel.place(x=i * self.panel_w, y=self.header_h, width=self.panel_w, height=self.panel_h)
        self.visible_panels = panels

        new_w = mode_width(mode, self.tab_w, self.panel_w)
        self.main_frame.place(x=self.tab_w, y=0, width=new_w - self.tab_w, height=self.base_height)
        if new_w != self.width:
            x = anchored_x(self.root.winfo_x(), self.width, new_w, self.root.winfo_vrootx())
            self.width = new_w
            self.root.geometry(f"{new_w}x{self.base_height}+{x}+{self.root.winfo_y()}")
            self.root.update_idletasks()
        self.apply_window_region()

        self.mode_tab.set_mode(mode)
        icon = getattr(self, "icon", None)
        if icon is not None:
            icon.update_menu()
        display_logic.save_mode(STATE_FILE, mode)

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
            *(self._mode_menu_item(m, label) for m, label in zip(MODES, ("AGY", "Claude", "Both"))),
            pystray.MenuItem('Refresh Now', self.trigger_refresh),
            pystray.MenuItem('Exit', self.exit_app)
        )
        self.icon = pystray.Icon("AGYMonitor", self.create_tray_icon(), "AGY Fuel Gauge", menu)
        threading.Thread(target=self.icon.run, daemon=True).start()

    def _mode_menu_item(self, mode, label):
        # pystray calls back on its own thread: hop to the Tk thread before touching widgets.
        return pystray.MenuItem(label, lambda icon, item: self.root.after(0, self.set_mode, mode),
                                checked=lambda item: self.mode == mode, radio=True)

    def show_window(self, icon=None, item=None):
        self.root.deiconify()
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = screen_width - mode_width(self.mode, self.tab_w, self.panel_w) - S(20)
        y = screen_height - self.base_height - S(60)
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
        # Atomic check-and-set: the tray, the button and auto_update_loop may race here.
        if not self._fetch_lock.acquire(blocking=False):
            return
        self.is_fetching = True
        try:
            threading.Thread(target=self.fetch_and_update, daemon=True).start()
        except Exception:
            self.is_fetching = False
            self._fetch_lock.release()
            raise
            
    def fetch_and_update(self):
        self.root.after(0, lambda: self.refresh_btn.config(fg=COLOR_GEM_SAFE))
        try:
            data = data_fetcher.fetch_all()
            self.root.after(0, self.update_ui_with_data, data)
        except Exception as e:
            print(f"[AGY Fuel Gauge] Fetch failed: {e}")
        finally:
            self.is_fetching = False
            self._fetch_lock.release()
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
        # Charts are captured once: a mode switch mid-sweep must not retarget it.
        # In BOTH mode one sweep drives a line on each chart, in lockstep.
        charts = [p.chart for p in self.visible_panels]
        scan_lines = [(c, c.create_line(0, 0, 0, 0, fill="#5C856D", width=1)) for c in charts]

        STEPS = 25  # Increased steps for a smoother, slower sweep
        def animate(step=0):
            if step > STEPS:
                for chart, scan_line in scan_lines:
                    chart.delete(scan_line)
                return
            for chart, scan_line in scan_lines:
                base_y = (step / STEPS) * chart.height

                # Generate polyline coordinates for creases
                coords = []
                x_segments = [0, chart.width * 0.3, chart.width * 0.6, chart.width]

                for x in x_segments:
                    # Balanced jitter magnitude for visible but subtle creases
                    jitter = random.randint(-2, 2) if step > 0 and step < STEPS else 0
                    y = max(0, min(chart.height, base_y + jitter))
                    coords.extend([x, y])

                # Add glitch effect: randomly drop opacity/visibility for a frame
                if random.random() < 0.15:
                    chart.itemconfig(scan_line, fill="#000000") # blend into background
                else:
                    chart.itemconfig(scan_line, fill="#5C856D")

                chart.coords(scan_line, *coords)
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




