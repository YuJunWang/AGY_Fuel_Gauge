"""
providers/claude.py — AGY Fuel Gauge
Claude subscription usage via the OAuth usage endpoint (read-only; never writes credentials).
"""
import http.client
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
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
    except (urllib.error.URLError, TimeoutError, OSError,
            http.client.HTTPException) as e:
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


def scan_window_start(now):
    """Earliest event time we keep: the 5h waveform and the whole local day."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return min(now - timedelta(hours=5), midnight)


def today_total(events, now):
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return sum(t for ts, t in events if ts >= midnight)


def _parse_event(line):
    """One JSONL line -> (dedup_key, local naive datetime, tokens) or None."""
    try:
        rec = json.loads(line)
        msg = rec["message"]
        usage = msg["usage"]
        key = msg.get("id") or rec.get("requestId")
        ts = datetime.fromisoformat(rec["timestamp"].replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        total = sum(int(usage.get(k) or 0) for k in (
            "input_tokens", "output_tokens", "cache_creation_input_tokens"))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    if not key:
        return None
    return key, ts, total


class TokenScanner:
    """Incrementally scans Claude Code transcripts (*.jsonl) for token events."""

    def __init__(self, projects_dir):
        self.projects_dir = Path(projects_dir)
        self.offsets = {}
        self.seen_ids = set()
        self.events = []
        self._day = None

    def scan(self, now):
        start = scan_window_start(now)  # looked up at call time (monkeypatchable)
        if self._day != now.date():
            self._day = now.date()
            self.seen_ids.clear()
        try:
            paths = list(self.projects_dir.rglob("*.jsonl"))
        except OSError:
            paths = []
        for path in paths:
            try:
                st = path.stat()
                if datetime.fromtimestamp(st.st_mtime) < start:
                    continue
                offset = self.offsets.get(path, 0)
                if st.st_size < offset:
                    offset = 0
                with open(path, "rb") as f:
                    f.seek(offset)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n")
            if end < 0:
                continue
            self.offsets[path] = offset + end + 1
            for raw in data[:end].split(b"\n"):
                ev = _parse_event(raw.decode("utf-8", errors="replace"))
                if ev is None:
                    continue
                key, ts, total = ev
                if key in self.seen_ids:
                    continue
                self.seen_ids.add(key)
                self.events.append((ts, total))
        self.events = [e for e in self.events if e[0] >= start]
        return list(self.events)


scanner = TokenScanner(claude_dir() / "projects")


def fetch():
    """Snapshot for the Claude provider: subscription bars + local token events."""
    now = datetime.now()
    status, bars = fetch_usage(int(time.time() * 1000))
    if status != "ok":
        bars = parse_usage(None)
    history = {}
    for bar_id, key in (("cl_5h", "claude_5h"), ("cl_wk", "claude_wk")):
        pct = bars[bar_id]["pct"]
        if pct is not None:
            history[key] = pct
    try:
        events = scanner.scan(now)
    except Exception as e:  # never let transcript scanning crash the widget
        print(f"[claude] token scan failed: {type(e).__name__}")
        events = []
    return {"status": status, "bars": bars, "history": history,
            "extra": {"token_events": events,
                      "today_tokens": today_total(events, now)}}
