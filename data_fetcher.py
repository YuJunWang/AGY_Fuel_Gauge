"""
data_fetcher.py — AGY Fuel Gauge
Legacy adapter: quota fetching now lives in providers/agy.py.
Kept (temporarily) so widget.py keeps receiving the old dict format.
"""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import history_logger
from providers import agy, claude


def _error_snapshot():
    return {"status": "error", "bars": {}, "history": {}, "extra": {}}


def fetch_all():
    """Fetch AGY and Claude in parallel; a failing source never affects the other."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = {"agy": pool.submit(agy.fetch), "claude": pool.submit(claude.fetch)}
    out = {}
    for name, fut in futures.items():
        try:
            out[name] = fut.result()
        except Exception as e:
            print(f"[data_fetcher] {name} fetch failed: {e}")
            out[name] = _error_snapshot()
    merged = {}
    merged.update(out["agy"]["history"])
    merged.update(out["claude"]["history"])
    history_logger.log_usage(**merged)
    out["last_updated"] = datetime.now().strftime("%H:%M:%S")
    return out


def fetch_usage_data():
    snap = agy.parse_quota(agy.fetcher.get_quota())

    result = {
        "gemini": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": ""},
        "external": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": ""},
        "argon": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": "", "available": False},
        "last_updated": datetime.now().strftime("%H:%M:%S"),
    }

    if snap["status"] != "ok":
        return result

    legacy_keys = {
        "gem_5h": ("gemini", "5hr_percent", "reset_time_5h", "5hr_used"),
        "ext_5h": ("external", "5hr_percent", "reset_time_5h", "5hr_used"),
        "gem_wk": ("gemini", "weekly_percent", "reset_time_weekly", "weekly_used"),
        "ext_wk": ("external", "weekly_percent", "reset_time_weekly", "weekly_used"),
        "arg_wk": ("argon", "weekly_percent", "reset_time_weekly", "weekly_used"),
    }
    argon_available = snap["extra"]["argon_available"]
    result["argon"]["available"] = argon_available
    for bar_id, (grp, pk, rk, uk) in legacy_keys.items():
        if grp == "argon" and not argon_available:
            continue
        bar = snap["bars"][bar_id]
        result[grp][pk] = bar["pct"]
        result[grp][rk] = bar["reset"]
        result[grp][uk] = round(100 - bar["pct"], 1)

    fields = {
        "gemini_5h": result["gemini"].get("5hr_percent", 100),
        "external_5h": result["external"].get("5hr_percent", 100),
    }
    if result["argon"].get("available"):
        fields["argon_wk"] = result["argon"].get("weekly_percent")
    history_logger.log_usage(**fields)

    return result
