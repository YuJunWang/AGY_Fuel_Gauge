"""
data_fetcher.py — AGY Fuel Gauge
Legacy adapter: quota fetching now lives in providers/agy.py.
Kept (temporarily) so widget.py keeps receiving the old dict format.
"""
from providers import agy


def fetch_usage_data():
    from datetime import datetime
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

    import history_logger
    history_logger.log_usage(
        result["gemini"].get("5hr_percent", 100),
        result["external"].get("5hr_percent", 100),
        result["argon"].get("weekly_percent") if result["argon"].get("available") else None,
    )

    return result
