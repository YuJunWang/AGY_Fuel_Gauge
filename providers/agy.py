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


def fetch():
    return parse_quota(fetcher.get_quota())
