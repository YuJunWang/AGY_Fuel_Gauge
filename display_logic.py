"""Pure display logic for the fuel gauge widget.

No function here reads the clock: time always arrives as a parameter.
"""
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKENS_PER_BLOCK: int = 20000  # calibrated in Task 9: P99/10 ~ 21k of 3-min buckets

MODES = ("agy", "claude", "both")
STATE_FILE = Path(__file__).with_name("widget_state.json")
TAB_WIDTH = 24
PANEL_WIDTH = 110


def mode_width(mode, tab_w=TAB_WIDTH, panel_w=PANEL_WIDTH):
    """Window width: the tab plus one column per shown provider (24 + 110 each at 96 DPI;
    the widget passes its DPI-scaled sizes)."""
    return tab_w + panel_w * (2 if mode == "both" else 1)


def anchored_x(old_x, old_w, new_w, screen_left=0):
    """New window x that keeps the right edge fixed, never past the screen's left edge."""
    return max(screen_left, old_x + old_w - new_w)


def load_mode(path):
    """Saved display mode; a missing, corrupt or unknown state falls back to 'agy'."""
    try:
        mode = json.loads(Path(path).read_text(encoding="utf-8")).get("mode")
    except FileNotFoundError:
        return "agy"
    except Exception as e:
        print(f"[widget] unreadable mode state {path}: {e}")
        return "agy"
    return mode if mode in MODES else "agy"


def save_mode(path, mode):
    try:
        Path(path).write_text(json.dumps({"mode": mode}), encoding="utf-8")
    except OSError as e:
        print(f"[widget] could not save mode state {path}: {e}")


def format_countdown(pct, reset, now_utc):
    """Countdown text to `reset` (ISO string); `now_utc` must be tz-aware."""
    if pct is None:
        return "--"
    if pct >= 99.9:
        return "Full"
    if not reset:
        return "--"
    try:
        s = reset[:-1] + "+00:00" if reset.endswith("Z") else reset
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt <= now_utc:
            return "Reset..."
        delta = dt - now_utc
        hours, remainder = divmod(delta.seconds, 3600)
        minutes = remainder // 60
        if delta.days > 0:
            return f"{delta.days}d {hours}h"
        return f"{hours}h {minutes}m"
    except Exception:
        return "--"


def format_tokens(n):
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M tok"
    if n >= 1000:
        return f"{n / 1000:.1f}k tok"
    return f"{n} tok"


RATE_ALERT_COLOR = "#EF4444"
RATE_ALERT_THRESHOLD = 20  # blocks per hour


def rate_color(hourly, normal, alert=True):
    """Burn-rate text colour: red above the threshold, unless the side opts out of alerts."""
    return RATE_ALERT_COLOR if alert and hourly > RATE_ALERT_THRESHOLD else normal


def count_records(history, keys):
    """Number of history records carrying at least one of `keys`."""
    return sum(1 for r in history if any(k in r for k in keys))


def bucket_drops(history, key, now, num_rows, total_minutes):
    """Per-row drop of `key` (row 0 = newest); resets never yield negatives."""
    parsed = []
    for r in history:
        if key not in r:
            continue
        try:
            parsed.append((datetime.fromisoformat(r["timestamp"]), r[key]))
        except Exception:
            continue
    parsed.sort(key=lambda x: x[0])
    if len(parsed) < 2:
        return [0.0] * num_rows

    d = total_minutes / num_rows
    out = []
    for i in range(num_rows):
        # Row i is centred on now - i*d (+-d/2; row 0 is the half-row up to now), so samples
        # polled every d minutes land mid-row instead of on a row edge.
        b_end = now if i == 0 else now - timedelta(minutes=(i - 0.5) * d)
        b_start = now - timedelta(minutes=(i + 0.5) * d)
        before = [v for ts, v in parsed if ts <= b_start]
        in_win = [v for ts, v in parsed if b_start < ts <= b_end]
        if in_win:
            ref = before[-1] if before else in_win[0]
            out.append(max(0, ref - in_win[-1]))
        else:
            out.append(0)
    return out


def bucket_tokens(events, now, num_rows, total_minutes, unit):
    """Token totals per row in blocks of `unit`, on bucket_drops' centred rows:
    row 0 covers (now-d/2, now], row i covers (now-(i+0.5)d, now-(i-0.5)d]."""
    d = total_minutes / num_rows
    sums = [0.0] * num_rows
    for ts, n in events:
        age = (now - ts).total_seconds() / 60.0
        if age < 0:
            continue
        i = math.floor(age / d + 0.5)
        if i >= num_rows:  # older than the last row's start, now-(num_rows-0.5)d
            continue
        sums[i] += n
    return [s / unit for s in sums]
