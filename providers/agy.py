"""
providers/agy.py — AGY Fuel Gauge
100% Deterministic & Instant Credential Discovery Strategy:
  1. Primary: Extract --csrf_token directly from language_server.exe process cmdline (via psutil or PowerShell CIM).
  2. Secondary: Query listening ports for language_server.exe directly.
  3. Tertiary: Probe ports with RetrieveUserQuotaSummary via gRPC-Web HTTPS POST.
  4. Fallback: Parse Antigravity's main.log (for older Antigravity versions).
"""
import json
import os
import re
import ssl
import subprocess
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import psutil
except ImportError:
    psutil = None

LOG_DIR = os.path.expanduser(r"~\AppData\Roaming\Antigravity\logs")
GRPC_PATH = "/exa.language_server_pb.LanguageServerService/RetrieveUserQuotaSummary"


class QuotaFetcher:
    def __init__(self):
        self.cdp_port = None
        self.grpc_port = None
        self.csrf_token = None

    def _extract_csrf_token(self):
        """
        Extract the latest --csrf_token from:
        1. language_server.exe process command line via psutil
        2. language_server.exe process command line via PowerShell CIM
        3. Antigravity's main.log (legacy fallback)
        """
        # 1. psutil strategy (instant and direct)
        if psutil:
            try:
                for p in psutil.process_iter(['name', 'cmdline']):
                    if p.info['name'] and 'language_server.exe' in p.info['name'].lower():
                        cmd_str = ' '.join(p.info['cmdline'] or [])
                        tokens = re.findall(r'--csrf_token\s+([a-f0-9\-]+)', cmd_str)
                        if tokens:
                            return tokens[-1]
            except Exception:
                pass

        # 2. PowerShell CIM strategy (standard Windows fallback)
        try:
            cmd = 'powershell -NoProfile -Command "(Get-CimInstance Win32_Process -Filter \\"Name = \'language_server.exe\'\\").CommandLine"'
            out = subprocess.check_output(cmd, shell=True, timeout=3).decode(errors='ignore')
            tokens = re.findall(r'--csrf_token\s+([a-f0-9\-]+)', out)
            if tokens:
                return tokens[-1]
        except Exception:
            pass

        # 3. Legacy log search fallback
        main_log = os.path.join(LOG_DIR, "main.log")
        if not os.path.exists(main_log):
            for root, _, files in os.walk(LOG_DIR):
                if "main.log" in files:
                    main_log = os.path.join(root, "main.log")
                    break

        if os.path.exists(main_log):
            try:
                with open(main_log, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                    tokens = re.findall(r'--csrf_token\s+([a-f0-9\-]+)', content)
                    if tokens:
                        return tokens[-1]
            except Exception:
                pass

        return None

    def _find_antigravity_ports(self):
        """Find gRPC port by directly querying language_server.exe's listening ports."""
        candidate_ports = []

        # 1. psutil strategy
        if psutil:
            try:
                for p in psutil.process_iter(['name']):
                    if p.info['name'] and 'language_server.exe' in p.info['name'].lower():
                        try:
                            for conn in p.net_connections(kind='inet'):
                                if conn.status == psutil.CONN_LISTEN:
                                    candidate_ports.append(conn.laddr.port)
                        except Exception:
                            pass
            except Exception:
                pass

        # 2. tasklist + netstat fallback
        if not candidate_ports:
            try:
                ls_out = subprocess.check_output(
                    'tasklist /FI "IMAGENAME eq language_server.exe"', shell=True, timeout=3
                ).decode(errors='ignore')
                ls_pids = set()
                for line in ls_out.splitlines():
                    if 'language_server.exe' in line:
                        parts = line.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            ls_pids.add(parts[1])

                if ls_pids:
                    net_out = subprocess.check_output('netstat -ano', shell=True, timeout=3).decode(errors='ignore')
                    for line in net_out.splitlines():
                        if 'LISTENING' in line:
                            parts = line.split()
                            if len(parts) >= 5 and parts[-1] in ls_pids:
                                port_str = parts[1].split(':')[-1]
                                if port_str.isdigit():
                                    candidate_ports.append(int(port_str))
            except Exception as e:
                print(f"[agy] Error discovering ports via netstat: {e}")

        if not candidate_ports:
            print("[agy] No listening ports found for language_server.exe.")
            return None, None

        candidate_ports = sorted(list(set(candidate_ports)))

        # 3. Probe candidate ports with gRPC-Web request
        csrf_token = self.csrf_token or self._extract_csrf_token()
        if not csrf_token:
            return None, candidate_ports[0]

        ctx = ssl._create_unverified_context()
        for port in candidate_ports:
            try:
                probe_url = f"https://127.0.0.1:{port}{GRPC_PATH}"
                probe_req = urllib.request.Request(
                    probe_url,
                    data=b'\x00\x00\x00\x00\x02{}',
                    headers={
                        "Content-Type": "application/grpc-web+json",
                        "x-grpc-web": "1",
                        "x-codeium-csrf-token": csrf_token,
                    },
                    method="POST"
                )
                with urllib.request.urlopen(probe_req, context=ctx, timeout=2) as resp:
                    resp.read()
                    return None, port
            except urllib.error.HTTPError:
                return None, port
            except Exception:
                continue

        return None, candidate_ports[0]

    def _query_endpoint(self, port, token):
        url = f"https://127.0.0.1:{port}{GRPC_PATH}"
        headers = {
            "Content-Type": "application/grpc-web+json",
            "Accept": "application/grpc-web+json",
            "x-grpc-web": "1",
            "x-codeium-csrf-token": token,
        }
        body = b'\x00\x00\x00\x00\x02{}'
        ctx = ssl._create_unverified_context()
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, context=ctx, timeout=3) as resp:
                res_body = resp.read().decode("utf-8", errors="ignore")
                match = re.search(r'({"response":.*?"}})', res_body)
                if match:
                    return json.loads(match.group(1))
        except Exception:
            pass
        return None

    def get_quota(self):
        # 1. Discover Token
        self.csrf_token = self._extract_csrf_token()
        if not self.csrf_token:
            print("[agy] Could not locate CSRF token.")
            return None

        # 2. Try cached port first
        if self.grpc_port:
            data = self._query_endpoint(self.grpc_port, self.csrf_token)
            if data:
                return data

        # 3. Discover Ports
        self.cdp_port, self.grpc_port = self._find_antigravity_ports()
        if not self.grpc_port:
            print("[agy] Could not locate Antigravity gRPC port.")
            return None

        # 4. Query gRPC Endpoint
        return self._query_endpoint(self.grpc_port, self.csrf_token)


# Singleton instance
fetcher = QuotaFetcher()


_BARS = (
    # bar_id, group key, pct key, reset key, label
    ("gem_5h", "gemini", "5hr_percent", "reset_time_5h", "GEM"),
    ("ext_5h", "external", "5hr_percent", "reset_time_5h", "EXT"),
    ("gem_wk", "gemini", "weekly_percent", "reset_time_weekly", "GEM"),
    ("ext_wk", "external", "weekly_percent", "reset_time_weekly", "EXT"),
    ("arg_wk", "argon", "weekly_percent", "reset_time_weekly", "ARG"),
)


def parse_quota(data):
    """Convert a raw quota response into a Snapshot (see providers contract)."""
    if not data or "response" not in data:
        return {"status": "offline", "bars": {}, "history": {}, "extra": {"argon_available": False}}

    groups = {
        "gemini": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": ""},
        "external": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": ""},
        "argon": {"5hr_percent": 0, "weekly_percent": 0, "reset_time_5h": "", "reset_time_weekly": ""},
    }
    argon_available = False

    for group in data["response"].get("groups", []):
        display_name = group.get("displayName", "")
        is_argon = "argon" in display_name.lower()
        is_gemini = ("gemini" in display_name.lower()) and not is_argon

        for bucket in group.get("buckets", []):
            bucket_id = bucket.get("bucketId", "").lower()
            if is_argon or "argon" in bucket_id:
                key = "argon"
                argon_available = True
            elif is_gemini:
                key = "gemini"
            else:
                key = "external"

            remaining_pct = round(bucket.get("remainingFraction", 1) * 100, 1)
            reset = bucket.get("resetTime", "")

            if bucket.get("window") == "5h":
                groups[key]["5hr_percent"] = remaining_pct
                groups[key]["reset_time_5h"] = reset
            elif bucket.get("window") == "weekly":
                groups[key]["weekly_percent"] = remaining_pct
                groups[key]["reset_time_weekly"] = reset

    bars = {
        bar_id: {"label": label, "pct": groups[grp][pk], "reset": groups[grp][rk]}
        for bar_id, grp, pk, rk, label in _BARS
    }
    history = {
        "gemini_5h": groups["gemini"]["5hr_percent"],
        "external_5h": groups["external"]["5hr_percent"],
    }
    if argon_available:
        history["argon_wk"] = groups["argon"]["weekly_percent"]
    return {"status": "ok", "bars": bars, "history": history,
            "extra": {"argon_available": argon_available}}


# ── Local Transcript Scanner (Calibrated to Pro Quota & Reset Cycles) ─────
HYD_5H_BUDGET = 8_550_000
HYD_WK_BUDGET = 30_800_000
ARG_WK_BUDGET = 4_670_000
ARG_5H_BUDGET = ARG_WK_BUDGET  # Argon only has a weekly quota bucket in Pro plans


def agy_brain_dir():
    return Path(os.environ.get("AGY_BRAIN_DIR") or (Path.home() / ".gemini" / "antigravity" / "brain"))


def _parse_transcript_event(line, file_key):
    """One transcript.jsonl line -> (dedup_key, local naive dt, family, active_tokens) or None."""
    try:
        rec = json.loads(line)
        if rec.get("type") != "PLANNER_RESPONSE":
            return None
        ts_str = rec["created_at"]
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        if ts.tzinfo is not None:
            ts = ts.astimezone().replace(tzinfo=None)
        model = (rec.get("model") or "").lower()
        family = "arg" if "argon" in model else "hyd"
        inp = int(rec.get("input_tokens") or 0)
        out = int(rec.get("output_tokens") or 0)
        step_idx = rec.get("step_index")
        key = (file_key, step_idx, ts_str)
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return key, ts, family, inp + out


class AgyTokenScanner:
    """Incrementally scans Antigravity conversation transcripts (transcript.jsonl) for model token usage."""

    def __init__(self, brain_dir):
        self.brain_dir = Path(brain_dir)
        self.offsets = {}
        self.seen_ids = set()
        self.events = []

    def scan(self, now):
        start = now - timedelta(days=7)
        try:
            paths = list(self.brain_dir.glob("*/.system_generated/logs/transcript.jsonl"))
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
            file_key = str(path)
            for raw in data[:end].split(b"\n"):
                ev = _parse_transcript_event(raw.decode("utf-8", errors="replace"), file_key)
                if ev is None:
                    continue
                key, ts, family, active_tok = ev
                if key in self.seen_ids:
                    continue
                self.seen_ids.add(key)
                self.events.append((ts, family, active_tok))
        self.events = [e for e in self.events if e[0] >= start]
        return list(self.events)


def _remaining_pct(used, budget):
    return round(max(0.0, min(100.0, 100.0 - (used / budget) * 100.0)), 1)


def _detect_wk_anchor_from_history(now):
    """Detect the latest weekly cycle start from usage_history.json (100% -> <100% transition after idle)."""
    try:
        import history_logger
        hist = history_logger._load_history()
        cut = now - timedelta(days=7)
        prev_full_ts = None
        for r in hist:
            g = r.get("gemini_5h")
            if g is None:
                continue
            ts = datetime.fromisoformat(r["timestamp"])
            if ts < cut:
                continue
            if g >= 99.9:
                prev_full_ts = ts
            elif prev_full_ts is not None:
                return prev_full_ts
    except Exception:
        pass
    return None


def _hyd_cycles(events, now, wk_anchor=None):
    """Compute (used_5h, reset_5h_iso, used_wk, reset_wk_iso) for Hydrogen/Gemini."""
    hyd_ev = sorted((ts, tok) for ts, fam, tok in events if fam == "hyd" and tok > 0 and ts <= now)
    if not hyd_ev:
        return 0, "", 0, ""

    # Weekly cycle start: use history anchor or latest >12h idle gap
    wk_start = hyd_ev[0][0]
    for i in range(1, len(hyd_ev)):
        if (hyd_ev[i][0] - hyd_ev[i - 1][0]) > timedelta(hours=12):
            wk_start = hyd_ev[i][0]

    reset_wk_base = wk_anchor if (wk_anchor is not None and wk_anchor <= wk_start) else wk_start
    while reset_wk_base + timedelta(days=7) <= now:
        reset_wk_base += timedelta(days=7)
    wk_end = reset_wk_base + timedelta(days=7)
    used_wk = sum(tok for ts, tok in hyd_ev if ts >= reset_wk_base)
    reset_wk_iso = wk_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 5H cycle: steps in 5-hour intervals from the active session's first turn (wk_start)
    c5_start = wk_start
    while c5_start + timedelta(hours=5) <= now:
        c5_start += timedelta(hours=5)
    c5_end = c5_start + timedelta(hours=5)
    used_5h = sum(tok for ts, tok in hyd_ev if c5_start <= ts <= now)
    reset_5h_iso = "" if used_5h == 0 else c5_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return used_5h, reset_5h_iso, used_wk, reset_wk_iso


def _arg_cycle(events, now):
    """Compute (used_wk, reset_wk_iso) for Argon (weekly bucket in Pro plans)."""
    arg_ev = sorted((ts, tok) for ts, fam, tok in events if fam == "arg" and tok > 0 and ts <= now)
    if not arg_ev:
        return 0, ""
    cycle_start = arg_ev[0][0]
    while cycle_start + timedelta(days=7) <= now:
        cycle_start += timedelta(days=7)
    cycle_end = cycle_start + timedelta(days=7)
    used = sum(tok for ts, tok in arg_ev if cycle_start <= ts <= now)
    reset_iso = "" if used == 0 else cycle_end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return used, reset_iso


def build_transcript_snapshot(events, now, wk_anchor=None):
    """Build Dual-Engine snapshot (HYD vs ARG with calibrated percentages & countdowns)."""
    cut_5h = now - timedelta(hours=5)

    hyd_5h, hyd_5h_reset, hyd_wk, hyd_wk_reset = _hyd_cycles(events, now, wk_anchor=wk_anchor)
    arg_wk, arg_wk_reset = _arg_cycle(events, now)

    hyd_5h_pct = _remaining_pct(hyd_5h, HYD_5H_BUDGET)
    hyd_wk_pct = _remaining_pct(hyd_wk, HYD_WK_BUDGET)
    arg_wk_pct = _remaining_pct(arg_wk, ARG_WK_BUDGET)

    # Argon only has a weekly quota bucket in Pro plans, so both 5H and WK views show its active quota & countdown
    bars = {
        "gem_5h": {"label": "HYD", "pct": hyd_5h_pct, "reset": hyd_5h_reset},
        "ext_5h": {"label": "ARG", "pct": arg_wk_pct, "reset": arg_wk_reset},
        "gem_wk": {"label": "HYD", "pct": hyd_wk_pct, "reset": hyd_wk_reset},
        "ext_wk": {"label": "ARG", "pct": arg_wk_pct, "reset": arg_wk_reset},
    }
    history = {
        "gemini_5h": hyd_5h_pct,
        "external_5h": arg_wk_pct,
    }
    hyd_events = [(ts, tok) for ts, fam, tok in events if ts >= cut_5h and fam == "hyd"]
    arg_events = [(ts, tok) for ts, fam, tok in events if ts >= cut_5h and fam == "arg"]

    return {
        "status": "ok",
        "bars": bars,
        "history": history,
        "extra": {
            "argon_available": True,
            "hyd_events": hyd_events,
            "arg_events": arg_events,
        },
    }


scanner = AgyTokenScanner(agy_brain_dir())


def fetch():
    data = fetcher.get_quota()
    if data and "response" in data:
        return parse_quota(data)
    now = datetime.now()
    try:
        if scanner.brain_dir.exists():
            events = scanner.scan(now)
            wk_anchor = _detect_wk_anchor_from_history(now)
            return build_transcript_snapshot(events, now, wk_anchor=wk_anchor)
    except Exception as e:
        print(f"[agy] transcript scan failed: {type(e).__name__}: {e}")
    return parse_quota(None)
