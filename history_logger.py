import json
import os
from datetime import datetime, timedelta

HISTORY_FILE = os.path.join(os.path.dirname(__file__), "usage_history.json")

def log_usage(gemini_5h_pct, external_5h_pct, argon_wk_pct=None):
    """
    Appends the current usage to the history file.
    Enforces a 2.5-minute cooldown to prevent manual refreshes from
    polluting the time-series data with spurious near-zero delta values.
    """
    history = _load_history()
    
    # Cooldown guard: skip if last entry was less than 2.5 minutes ago
    if history:
        try:
            last_ts = datetime.fromisoformat(history[-1]["timestamp"])
            if (datetime.now() - last_ts).total_seconds() < 150:  # 2.5 min
                return  # Too soon — skip this log entry
        except Exception:
            pass  # If timestamp is malformed, just proceed to log
    
    now = datetime.now().isoformat()
    record = {
        "timestamp": now,
        "gemini_5h": gemini_5h_pct,
        "external_5h": external_5h_pct
    }
    if argon_wk_pct is not None:
        record["argon_wk"] = argon_wk_pct
    
    history.append(record)
    
    # Prune history to keep only last 24 hours (approx 480 records at 3 min intervals)
    cutoff = datetime.now() - timedelta(hours=24)
    history = [r for r in history if datetime.fromisoformat(r["timestamp"]) > cutoff]
    
    _save_history(history)

def get_history(minutes=90):
    """
    Returns history from the last N minutes.
    """
    history = _load_history()
    cutoff = datetime.now() - timedelta(minutes=minutes)
    return [r for r in history if datetime.fromisoformat(r["timestamp"]) > cutoff]

def _load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"[history_logger] Failed to load history: {e}")
        return []

def _save_history(history):
    try:
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
    except OSError as e:
        print(f"[history_logger] Failed to save history: {e}")


