import os
import sys
import threading
import time
import tkinter as tk
import ctypes
from datetime import datetime, timedelta
import pystray
from PIL import Image, ImageDraw, ImageTk
import math
import random

# ── Single Instance Lock ───────────────────────────────────────────────────
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

import data_fetcher
import history_logger

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

DIGITAL_FONT        = "Fira Code" # Neo-Brutalism monospace

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


class VerticalFuelGauge(tk.Canvas):
    def __init__(self, parent, width=44, height=200, title="Title", is_gemini=True, text_side="right", **kwargs):
        super().__init__(parent, width=width, height=height, bg=BG_COLOR, highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.is_gemini = is_gemini
        
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
        
        # Percentage (Fira Code)
        self.pct_text = self.create_text(self.cx, self.y_bottom + 18, text="--%", fill=TEXT_FG, font=(DIGITAL_FONT, 10, "bold"), justify="center")
        
        title_color = COLOR_GEM_SAFE if is_gemini else COLOR_EXT_SAFE
        self.title_text = self.create_text(self.cx, self.y_top - 16, text=title, fill=title_color, font=("Segoe UI", 8, "bold"), justify="center")
        
        text_x = self.cx + 15 if text_side == "right" else self.cx - 15
        self.time_text = self.create_text(text_x, self.height / 2, text="--h", fill=TEXT_MUTED, font=("Segoe UI", 9), justify="center", angle=270)

    def set_value(self, pct_remaining, reset_time):
        val = max(0.0, min(100.0, pct_remaining))
        fill_height = self.track_height * (val / 100.0)
        curr_y = self.y_bottom - fill_height
        
        if self.is_gemini:
            if val <= 20:
                core, base, tip = COLOR_GEM_DANGER, "#7F1D1D", "#FECACA"
            elif val <= 50:
                core, base, tip = COLOR_GEM_WARN, "#713F12", "#FEF08A"
            else:
                core, base, tip = COLOR_GEM_SAFE, "#14532D", "#86EFAC"
        else:
            if val <= 20:
                core, base, tip = COLOR_EXT_DANGER, "#7F1D1D", "#FECACA"
            elif val <= 50:
                core, base, tip = COLOR_EXT_WARN, "#9A3412", "#FDBA74"
            else:
                core, base, tip = COLOR_EXT_SAFE, "#7C2D12", "#FED7AA"
        
        self.coords(self.line_base, self.cx, self.y_bottom, self.cx, curr_y)
        self.itemconfig(self.line_base, fill=base)
        self.coords(self.line_core, self.cx, self.y_bottom, self.cx, curr_y)
        self.itemconfig(self.line_core, fill=core)
        
        if val > 0:
            r = (self.bar_width - 4) / 2
            self.coords(self.tip_dot, self.cx - r, curr_y - r, self.cx + r, curr_y + r)
            self.itemconfig(self.tip_dot, fill=tip, state="normal")
        else:
            self.itemconfig(self.tip_dot, state="hidden")
            
        self.itemconfig(self.pct_text, text=f"{int(val)}%", fill=core)
            
        time_str = reset_time
        if reset_time and "Z" in reset_time:
            try:
                dt = datetime.strptime(reset_time, "%Y-%m-%dT%H:%M:%SZ")
                now_utc = datetime.utcnow()
                if dt > now_utc:
                    delta = dt - now_utc
                    hours, remainder = divmod(delta.seconds, 3600)
                    minutes = remainder // 60
                    days = delta.days
                    if days > 0:
                        time_str = f"{days}d {hours}h"
                    else:
                        time_str = f"{hours}h {minutes}m"
                else:
                    time_str = "Reset..."
            except Exception:
                pass
        self.itemconfig(self.time_text, text=time_str)

class VerticalHistoryChart(tk.Canvas):
    def __init__(self, parent, width=100, height=140, **kwargs):
        super().__init__(parent, width=width, height=height, bg="#000000", highlightthickness=0, **kwargs)
        self.width = width
        self.height = height
        self.buckets = []
        self.y_top = 10
        self.y_bottom = height - 15
        self._init_gradients()
        
        self.bind("<Motion>", self.on_mouse_move)
        self.bind("<Leave>", self.on_mouse_leave)
        

    def _init_gradients(self):
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
        
        c_green = hex2rgb("#10B981")
        c_yellow = hex2rgb("#EAB308")
        c_red = hex2rgb("#EF4444")
        
        for b in range(11):
            x_right = mid_x - 2 - (b * BLOCK_STEP)
            x_left = x_right - BLOCK_STEP
            
            factor = b / 10.0
            if factor <= 0.5:
                f = factor * 2
                base = interpolate(c_green, c_yellow, f)
            else:
                f = (factor - 0.5) * 2
                base = interpolate(c_yellow, c_red, f)
                
            color_dim = deepen(base, 0.5) + (255,)
            color_bright = highlight(base, 1.2, 20) + (255,)
            
            d_dim.rectangle([x_left, 0, x_right, self.height], fill=color_dim)
            d_bright.rectangle([x_left, 0, x_right, self.height], fill=color_bright)

        c_orange = hex2rgb("#F97316")
        c_purple = hex2rgb("#7C3AED")
        c_blue = hex2rgb("#2563EB")
        
        for b in range(11):
            x_left = mid_x + 3 + (b * BLOCK_STEP)
            x_right = x_left + BLOCK_STEP
            
            factor = b / 10.0
            if factor <= 0.5:
                f = factor * 2
                base = interpolate(c_orange, c_purple, f)
            else:
                f = (factor - 0.5) * 2
                base = interpolate(c_purple, c_blue, f)
                
            color_dim = deepen(base, 0.5) + (255,)
            color_bright = highlight(base, 1.2, 20) + (255,)
            
            d_dim.rectangle([x_left, 0, x_right, self.height], fill=color_dim)
            d_bright.rectangle([x_left, 0, x_right, self.height], fill=color_bright)

    def render(self, history):
        self.delete("all")
        if not history or len(history) < 2:
            self.create_text(self.width/2, self.height/2, text="Waiting...", fill=TEXT_MUTED, font=("Segoe UI", 7))
            return
            
        mid_x = int(self.width / 2)
        self.y_top = 10
        # Force y_range to be exactly 240 pixels (6 hours * 20 buckets/hr * 2px = 240 pixels)
        # This ensures the time ticks perfectly align with the 2 pixel-per-bucket data rendering.
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
        
        parsed = []
        for r in history:
            try:
                ts = datetime.fromisoformat(r["timestamp"])
                parsed.append((ts, r.get("gemini_5h", 100), r.get("external_5h", 100)))
            except Exception:
                continue
        parsed.sort(key=lambda x: x[0])
        if len(parsed) < 2:
            return
            
        now = datetime.now()
        y_range = self.y_bottom - self.y_top
        
        # Pixel-first mapping: 2px per row for uniform density
        NUM_ROWS = int(y_range // 3)
        bucket_duration_min = 300.0 / NUM_ROWS
        
        buckets = [] 
        for i in range(0, NUM_ROWS):
            b_end = now - timedelta(minutes=i * bucket_duration_min)
            b_start = now - timedelta(minutes=(i + 1) * bucket_duration_min)
            
            before = [(ts, g, e) for ts, g, e in parsed if ts <= b_start]
            in_win = [(ts, g, e) for ts, g, e in parsed if b_start < ts <= b_end]
            
            if in_win:
                ref_g, ref_e = (before[-1][1], before[-1][2]) if before else (in_win[0][1], in_win[0][2])
                end_g, end_e = in_win[-1][1], in_win[-1][2]
                gem_drop = max(0, ref_g - end_g)
                ext_drop = max(0, ref_e - end_e)
            else:
                gem_drop = 0
                ext_drop = 0
            buckets.append((gem_drop, ext_drop))
            
        self.buckets = buckets
        
        # ABSOLUTE FIXED SCALE: 1.0% per full block, 0.5% per half block. Max 10 blocks = 10.0%
        # Block rendering: 2px wide (full) or 1px wide (half), 1px gap -> Step is 3px
        BLOCK_STEP = 3
        mid_x = int(self.width / 2)
        # Clear previous PIL image
        self.delete("chart_img")
        
        mask_dim = Image.new("L", (self.width, self.height), 0)
        mask_bright = Image.new("L", (self.width, self.height), 0)
        d_mask_dim = ImageDraw.Draw(mask_dim)
        d_mask_bright = ImageDraw.Draw(mask_bright)
        
        for idx, (g, e) in enumerate(buckets):
            cy = int(self.y_top + idx * 3)
            
            # ── GEMINI ──
            g_capped = min(10.0, g)
            g_full = int(g_capped // 1.0)
            g_half = 1 if (g_capped % 1.0) >= 0.5 else 0
            g_total = g_full + g_half
            
            if g_total > 0:
                b = g_total - 1
                x_right_block = mid_x - 2 - (b * BLOCK_STEP)
                x_left_block = x_right_block - (1 if g_half else 2)
                
                if g < 10.0:
                    d_mask_dim.rectangle([x_left_block, cy, mid_x - 2, cy + 2], fill=255)
                else:
                    d_mask_bright.rectangle([x_left_block, cy, mid_x - 2, cy + 2], fill=255)
                    
            # ── EXTERNAL ──
            e_capped = min(10.0, e)
            e_full = int(e_capped // 1.0)
            e_half = 1 if (e_capped % 1.0) >= 0.5 else 0
            e_total = e_full + e_half
            
            if e_total > 0:
                b = e_total - 1
                x_left_block = mid_x + 3 + (b * BLOCK_STEP)
                x_right_block = x_left_block + (1 if e_half else 2)
                
                if e < 10.0:
                    d_mask_dim.rectangle([mid_x + 3, cy, x_right_block, cy + 2], fill=255)
                else:
                    d_mask_bright.rectangle([mid_x + 3, cy, x_right_block, cy + 2], fill=255)
                    
        # Composite the masks
        bg_img = Image.new("RGBA", (self.width, self.height), (0,0,0,0))
        layer_dim = Image.composite(self.grad_dim, bg_img, mask_dim)
        final_img = Image.composite(self.grad_bright, layer_dim, mask_bright)
            
        # Stamp the perfectly rendered pixel buffer onto the Tkinter canvas
        self.chart_tk_img = ImageTk.PhotoImage(final_img)
        self.create_image(0, 0, anchor="nw", image=self.chart_tk_img, tags="chart_img")
        self.tag_lower("chart_img")
        
        gem_total = sum(g for g, e in buckets)
        ext_total = sum(e for g, e in buckets)
        gem_hourly = gem_total / 5.0
        ext_hourly = ext_total / 5.0

        gem_color = COLOR_GEM_DANGER if gem_hourly > 20 else COLOR_GEM_SAFE
        ext_color = COLOR_EXT_DANGER if ext_hourly > 20 else COLOR_EXT_SAFE
        
        # Floating Burn Rates (Reading top-to-bottom)
        self.create_text(8, self.height / 2, text=f"{round(gem_hourly, 1)} %/h", fill=gem_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")
        self.create_text(self.width-8, self.height / 2, text=f"{round(ext_hourly, 1)} %/h", fill=ext_color, font=(DIGITAL_FONT, 8, "bold"), angle=270, anchor="center")
        
        # -6H label pushed snugly below the bottom axis
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
            
            # Direction 1: Split labels — fixed left (green), fixed right (orange)
            for text, color, x, anchor_val in [
                (f"{g:.1f}%", COLOR_GEM_SAFE, 2, "w"),
                (f"{e:.1f}%", COLOR_EXT_SAFE, self.width - 2, "e"),
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


class UsageWidget:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title("AGY Fuel Gauge")
        
        # Total width 134 (24 tab + 110 main)
        self.width = 134
        self.base_height = 560
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

        # Toggle
        self.toggle_container = tk.Frame(self.main_frame, bg=BG_COLOR, bd=0)
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
        self.bars_frame = tk.Frame(self.main_frame, bg=BG_COLOR)
        self.bars_frame.pack(fill=tk.X, pady=4)
        
        self.view_5h_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=150)
        self.view_5h_frame.pack(fill=tk.X)
        self.view_5h_frame.pack_propagate(False)
        self.gemini_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=44, height=150, title="GEM", is_gemini=True, text_side="left")
        self.gemini_5h_gauge.place(x=9, y=0)
        self.ext_5h_gauge = VerticalFuelGauge(self.view_5h_frame, width=44, height=150, title="EXT", is_gemini=False, text_side="right")
        self.ext_5h_gauge.place(x=57, y=0)
        
        self.view_wk_frame = tk.Frame(self.bars_frame, bg=BG_COLOR, height=150)
        self.view_wk_frame.pack_propagate(False)
        self.gemini_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=44, height=150, title="GEM", is_gemini=True, text_side="left")
        self.gemini_wk_gauge.place(x=9, y=0)
        self.ext_wk_gauge = VerticalFuelGauge(self.view_wk_frame, width=44, height=150, title="EXT", is_gemini=False, text_side="right")
        self.ext_wk_gauge.place(x=57, y=0)

        # Vertical History Chart
        self.chart_frame = tk.Frame(self.main_frame, bg=SURFACE_COLOR, bd=0, highlightthickness=0)
        self.chart_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 8), padx=6)
        
        self.chart = VerticalHistoryChart(self.chart_frame, width=96, height=325)
        self.chart.pack(fill=tk.BOTH, expand=True)
        
        history = history_logger.get_history(minutes=360)
        self.chart.render(history)

    def start_move(self, event):
        self.x = event.x
        self.y = event.y

    def do_move(self, event):
        deltax = event.x - self.x
        deltay = event.y - self.y
        x = self.root.winfo_x() + deltax
        y = self.root.winfo_y() + deltay
        self.root.geometry(f"+{x}+{y}")

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
            data = data_fetcher.fetch_usage_data()
            self.root.after(0, self.update_ui_with_data, data)
        except Exception as e:
            print(f"[AGY Fuel Gauge] Fetch failed: {e}")
        finally:
            self.is_fetching = False
            self.root.after(0, lambda: self.refresh_btn.config(fg=TEXT_MUTED))
            
    def update_ui_with_data(self, data):
        now_str = datetime.now().strftime("%H:%M")
        self.last_update_lbl.config(text=f"{now_str}")
        
        g = data.get("gemini", {})
        e = data.get("external", {})
        
        g_wk_pct = g.get("weekly_percent", 0)
        e_wk_pct = e.get("weekly_percent", 0)
        
        # If weekly usage is 0, 5H usage should also be 0 and have no relevant reset time
        g_5h_pct = 0 if g_wk_pct == 0 else g.get("5hr_percent", 0)
        g_5h_time = "-h --m" if g_wk_pct == 0 else g.get("reset_time_5h", "--")
        
        e_5h_pct = 0 if e_wk_pct == 0 else e.get("5hr_percent", 0)
        e_5h_time = "-h --m" if e_wk_pct == 0 else e.get("reset_time_5h", "--")
        
        self.gemini_5h_gauge.set_value(g_5h_pct, g_5h_time)
        self.ext_5h_gauge.set_value(e_5h_pct, e_5h_time)
        
        self.gemini_wk_gauge.set_value(g_wk_pct, g.get("reset_time_weekly", "--"))
        self.ext_wk_gauge.set_value(e_wk_pct, e.get("reset_time_weekly", "--"))
        
        history = history_logger.get_history(minutes=360)
        self.chart.render(history)
        
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
    app = UsageWidget()
    app.run()



