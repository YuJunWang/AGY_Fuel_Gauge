"""
data_fetcher.py — AGY Fuel Gauge
Dispatcher: runs every provider in parallel and logs one merged history record.
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

