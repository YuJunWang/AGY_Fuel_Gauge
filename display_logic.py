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


def mode_width(mode):
    """Window width: the 24px tab plus one 110px column per shown provider."""
    return TAB_WIDTH + PANEL_WIDTH * (2 if mode == "both" else 1)


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
        b_end = now - timedelta(minutes=i * d)
        b_start = now - timedelta(minutes=(i + 1) * d)
        before = [v for ts, v in parsed if ts <= b_start]
        in_win = [v for ts, v in parsed if b_start < ts <= b_end]
        if in_win:
            ref = before[-1] if before else in_win[0]
            out.append(max(0, ref - in_win[-1]))
        else:
            out.append(0)
    return out


def bucket_tokens(events, now, num_rows, total_minutes, unit):
    """Token totals per row in blocks of `unit`; row i covers (now-(i+1)d, now-i*d]."""
    d = total_minutes / num_rows
    sums = [0.0] * num_rows
    for ts, n in events:
        age = (now - ts).total_seconds() / 60.0
        if age < 0 or age > total_minutes:
            continue
        i = min(max(math.ceil(age / d) - 1, 0), num_rows - 1)
        sums[i] += n
    return [s / unit for s in sums]
