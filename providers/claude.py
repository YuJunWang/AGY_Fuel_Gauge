"""
providers/claude.py — AGY Fuel Gauge
Claude subscription usage via the OAuth usage endpoint (read-only; never writes credentials).
"""
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
BETA_HEADER = "oauth-2025-04-20"
TIMEOUT = 5


def claude_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def read_access_token(creds_path, now_ms):
    """Return the OAuth access token, or None if missing/corrupt/expired."""
    try:
        with open(creds_path, encoding="utf-8") as f:
            oauth = json.load(f)["claudeAiOauth"]
        token = oauth["accessToken"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(token, str) or not token:
        return None
    exp = oauth.get("expiresAt")
    if isinstance(exp, (int, float)) and exp <= now_ms:
        return None
    return token


def _bar(label, bucket):
    pct, reset = None, ""
    if isinstance(bucket, dict):
        u = bucket.get("utilization")
        if isinstance(u, (int, float)) and not isinstance(u, bool):
            pct = max(0.0, min(100.0, 100.0 - float(u)))
            r = bucket.get("resets_at")
            reset = r if isinstance(r, str) else ""
    return {"label": label, "pct": pct, "reset": reset}


def parse_usage(payload):
    if not isinstance(payload, dict):
        payload = {}
    bars = {
        "cl_5h": _bar("5H", payload.get("five_hour")),
        "cl_wk": _bar("WK", payload.get("seven_day")),
    }
    model = _bar("--", None)
    for key, label in (("seven_day_opus", "OPS"), ("seven_day_sonnet", "SON")):
        b = _bar(label, payload.get(key))
        if b["pct"] is not None:
            model = b
            break
    bars["cl_model"] = model
    return bars


def fetch_usage(now_ms=None):
    """Return (status, bars). status: ok | login_required | offline | error."""
    if now_ms is None:
        now_ms = int(time.time() * 1000)
    token = read_access_token(claude_dir() / ".credentials.json", now_ms)
    if not token:
        print("[claude] no valid token; login required")
        return "login_required", {}
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": BETA_HEADER,
    })
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            print(f"[claude] usage endpoint HTTP {e.code}; login required")
            return "login_required", {}
        print(f"[claude] usage endpoint HTTP {e.code}")
        return "offline", {}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"[claude] usage endpoint unreachable: {type(e).__name__}")
        return "offline", {}
    except ValueError:
        print("[claude] usage response is not valid JSON")
        return "error", {}
    bars = parse_usage(payload)
    if all(b["pct"] is None for b in bars.values()):
        print("[claude] usage response has no usable buckets")
        return "error", {}
    return "ok", bars
