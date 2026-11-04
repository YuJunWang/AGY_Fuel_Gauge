import io
import json
import os
import http.client
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.error
from pathlib import Path
from unittest.mock import patch

from providers import claude
from providers.claude import (claude_dir, fetch_usage, parse_usage,
                              read_access_token)


def bucket(u, r="2026-11-04T15:00:00.123456+00:00"):
    return {"utilization": u, "resets_at": r}


class TestParseUsage(unittest.TestCase):
    def test_remaining_is_100_minus_utilization(self):
        # 防把「已用 %」直接當「剩餘 %」畫出反向油量條
        b = parse_usage({"five_hour": bucket(35.0), "seven_day": bucket(14.0)})
        self.assertEqual(b["cl_5h"]["pct"], 65.0)
        self.assertEqual(b["cl_wk"]["pct"], 86.0)
        self.assertEqual(b["cl_5h"]["reset"], "2026-11-04T15:00:00.123456+00:00")
        self.assertEqual((b["cl_5h"]["label"], b["cl_wk"]["label"]), ("5H", "WK"))

    def test_out_of_range_clamped(self):
        # 防超額使用（utilization > 100）時畫出負值油量條
        b = parse_usage({"five_hour": bucket(130.0), "seven_day": bucket(-5)})
        self.assertEqual(b["cl_5h"]["pct"], 0.0)
        self.assertEqual(b["cl_wk"]["pct"], 100.0)

    def test_null_and_garbage_buckets(self):
        # 防端點改版讓 widget 崩潰
        b = parse_usage({"five_hour": None, "seven_day": {"utilization": "x"}})
        self.assertIsNone(b["cl_5h"]["pct"])
        self.assertIsNone(b["cl_wk"]["pct"])
        self.assertEqual(b["cl_5h"]["reset"], "")
        self.assertEqual(b["cl_wk"]["reset"], "")

    def test_model_bar_prefers_opus_then_sonnet(self):
        # 防 model bar 選錯來源或缺資料時出現假數值
        b = parse_usage({"seven_day_opus": None, "seven_day_sonnet": bucket(40)})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("SON", 60.0))
        b = parse_usage({"seven_day_opus": bucket(10), "seven_day_sonnet": bucket(40)})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("OPS", 90.0))
        b = parse_usage({})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("--", None))


class TestClaudeDir(unittest.TestCase):
    def test_env_override_else_home(self):
        # 防忽略 CLAUDE_CONFIG_DIR 而讀錯使用者的憑證位置
        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": "X:/cfg"}):
            self.assertEqual(claude_dir(), Path("X:/cfg"))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
            self.assertEqual(claude_dir(), Path.home() / ".claude")


class TestToken(unittest.TestCase):
    def _write(self, data, raw=False):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = Path(d.name) / ".credentials.json"
        p.write_text(data if raw else json.dumps(data), encoding="utf-8")
        return p

    def test_missing_file_or_key_is_none(self):
        # 防憑證不存在 / 無 claudeAiOauth / JSON 損毀時拋例外
        self.assertIsNone(read_access_token(Path("nope") / "x.json", 2000))
        self.assertIsNone(read_access_token(self._write({"mcpOAuth": {}}), 2000))
        self.assertIsNone(read_access_token(self._write("{bad", raw=True), 2000))

    def test_expired_token_is_none(self):
        # Review Focus 1：防過期 token 仍發網路請求，應直接當成未登入
        p = self._write({"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}})
        self.assertIsNone(read_access_token(p, 2000))

    def test_valid_token(self):
        # 防有效 token 被誤判為過期
        p = self._write({"claudeAiOauth": {"accessToken": "t", "expiresAt": 5000}})
        self.assertEqual(read_access_token(p, 2000), "t")

    def test_no_expiry_is_valid(self):
        # 防缺 expiresAt 的憑證被當成過期
        p = self._write({"claudeAiOauth": {"accessToken": "t"}})
        self.assertEqual(read_access_token(p, 2000), "t")


def _no_cli(test):
    # 防單元測試真的去執行使用者機器上的 claude CLI（使用者已登入時 has_login 為真）
    p = patch.object(claude, "refresh_via_cli", return_value=False)
    p.start()
    test.addCleanup(p.stop)


class TestFetchUsage(unittest.TestCase):
    def setUp(self):
        _no_cli(self)

    def test_no_token_is_login_required_without_network(self):
        # 防沒有 token 時仍打網路
        with patch.object(claude, "read_access_token", return_value=None), \
             patch.object(claude, "_open") as uo:
            status, bars = fetch_usage(2000)
        self.assertEqual(status, "login_required")
        uo.assert_not_called()

    def _run(self, **kw):
        with patch.object(claude, "read_access_token", return_value="tok"), \
             patch.object(claude, "_open", **kw) as uo:
            return fetch_usage(2000), uo

    def test_401_is_login_required(self):
        # 防 401/403 被當成離線而不提示重新登入
        for code in (401, 403):
            err = urllib.error.HTTPError("u", code, "x", {}, None)
            (status, _), _ = self._run(side_effect=err)
            self.assertEqual(status, "login_required")

    def test_timeout_is_offline(self):
        # 防網路錯誤 / 5xx / 逾時讓 widget 崩潰，應回報 offline
        for exc in (urllib.error.URLError("x"), socket.timeout(),
                    urllib.error.HTTPError("u", 503, "x", {}, None)):
            (status, _), _ = self._run(side_effect=exc)
            self.assertEqual(status, "offline")

    def test_incomplete_read_is_offline(self):
        # 防 resp.read() 中途斷線拋 HTTPException（非 OSError）讓 widget 崩潰
        for exc in (http.client.IncompleteRead(b"x"), http.client.BadStatusLine("x")):
            (status, _), _ = self._run(side_effect=exc)
            self.assertEqual(status, "offline")

    def test_bad_json_or_all_none_is_error(self):
        # 防端點改版（非 JSON / 全空 bucket）被當成 ok 顯示空資料
        (status, _), _ = self._run(return_value=io.BytesIO(b"<html>"))
        self.assertEqual(status, "error")
        (status, _), _ = self._run(return_value=io.BytesIO(b"{}"))
        self.assertEqual(status, "error")

    def test_ok(self):
        # 防 request 缺 Bearer / beta 標頭導致端點拒絕，且 bars 須等於 parse_usage
        payload = {"five_hour": bucket(35.0), "seven_day": bucket(14.0)}
        (status, bars), uo = self._run(
            return_value=io.BytesIO(json.dumps(payload).encode()))
        self.assertEqual(status, "ok")
        self.assertEqual(bars, parse_usage(payload))
        req = uo.call_args[0][0]
        self.assertEqual(req.full_url, "https://api.anthropic.com/api/oauth/usage")
        h = {k.lower(): v for k, v in req.header_items()}
        self.assertEqual(h["authorization"], "Bearer tok")
        self.assertEqual(h["anthropic-beta"], "oauth-2025-04-20")
        self.assertEqual(uo.call_args.kwargs.get("timeout"), 5)


class TestRedirectNotFollowed(unittest.TestCase):
    def setUp(self):
        _no_cli(self)

    def test_302_is_offline_and_token_not_forwarded(self):
        # 防端點 3xx 轉址時 urllib 把 Authorization 一併送到轉址目標（token 外洩）
        seen = []

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append((self.path, self.headers.get("Authorization")))
                if self.path == "/usage":
                    self.send_response(302)
                    self.send_header("Location", "/elsewhere")
                    self.end_headers()
                else:
                    body = json.dumps({"five_hour": bucket(1.0)}).encode()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *a):
                pass

        srv = HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        url = f"http://127.0.0.1:{srv.server_port}/usage"
        with patch.object(claude, "read_access_token", return_value="tok"), \
             patch.object(claude, "USAGE_URL", url), \
             patch.dict(os.environ, {"NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}):
            status, bars = fetch_usage(2000)
        self.assertEqual(status, "offline")
        self.assertEqual(bars, {})
        self.assertEqual(seen, [("/usage", "Bearer tok")])


OK_BODY = json.dumps({"five_hour": bucket(35.0), "seven_day": bucket(14.0)}).encode()


class TestCliRefreshInFetch(unittest.TestCase):
    """access token 每 8 小時過期；widget 只讀憑證，請 Claude Code CLI 自己換新。"""

    def _fetch(self, tokens, opens, logged_in=True, refreshed=True):
        with patch.object(claude, "read_access_token", side_effect=tokens), \
             patch.object(claude, "has_login", return_value=logged_in), \
             patch.object(claude, "refresh_via_cli", return_value=refreshed) as rc, \
             patch.object(claude, "_open", side_effect=opens) as uo:
            return fetch_usage(2000), rc, uo

    def test_expired_login_refreshed_by_cli_then_ok(self):
        # 防 token 過期後 widget 一直卡在 LOGIN，即使 CLI 能自動換新
        (status, _), rc, uo = self._fetch([None, "new"], [io.BytesIO(OK_BODY)])
        self.assertEqual(status, "ok")
        rc.assert_called_once()
        h = {k.lower(): v for k, v in uo.call_args[0][0].header_items()}
        self.assertEqual(h["authorization"], "Bearer new")

    def test_never_logged_in_does_not_run_cli(self):
        # 防從未登入時每輪都啟動 CLI（沒有 refresh token，CLI 也換不出來）
        (status, _), rc, uo = self._fetch([None], [], logged_in=False)
        self.assertEqual(status, "login_required")
        rc.assert_not_called()
        uo.assert_not_called()

    def test_refresh_still_expired_is_login_required(self):
        # 防 CLI 換不到 token 時誤用過期 token 打網路
        (status, _), _, uo = self._fetch([None, None], [])
        self.assertEqual(status, "login_required")
        uo.assert_not_called()

    def test_401_refreshes_and_retries_once_with_new_token(self):
        # 防伺服器提前作廢 token（401）時不嘗試換新
        err = urllib.error.HTTPError("u", 401, "x", {}, None)
        (status, _), rc, uo = self._fetch(["old", "new"], [err, io.BytesIO(OK_BODY)])
        self.assertEqual(status, "ok")
        self.assertEqual(uo.call_count, 2)

    def test_401_same_token_after_refresh_does_not_retry(self):
        # 防 CLI 沒換出新 token 時用同一個被拒的 token 重打
        err = urllib.error.HTTPError("u", 401, "x", {}, None)
        (status, _), _, uo = self._fetch(["old", "old"], [err])
        self.assertEqual(status, "login_required")
        self.assertEqual(uo.call_count, 1)


class TestRefreshViaCli(unittest.TestCase):
    def setUp(self):
        claude._last_cli_refresh = None
        self.addCleanup(setattr, claude, "_last_cli_refresh", None)

    def test_runs_auth_status_hidden_without_token(self):
        # 防 pythonw 下跳出主控台視窗，或把 token 放進命令列
        with patch.object(claude, "_find_cli", return_value="C:/x/claude.exe"), \
             patch("subprocess.run") as run:
            self.assertTrue(claude.refresh_via_cli(1000.0))
        args, kw = run.call_args
        self.assertEqual(args[0], ["C:/x/claude.exe", "auth", "status"])
        self.assertEqual(kw["creationflags"], getattr(claude.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(kw["timeout"], claude.CLI_TIMEOUT_S)

    def test_cooldown_limits_attempts(self):
        # 防 CLI 換不到 token 時每 3 分鐘都重跑一次 CLI
        with patch.object(claude, "_find_cli", return_value="claude"), \
             patch("subprocess.run") as run:
            self.assertTrue(claude.refresh_via_cli(1000.0))
            self.assertFalse(claude.refresh_via_cli(1000.0 + claude.CLI_REFRESH_COOLDOWN_S - 1))
            self.assertTrue(claude.refresh_via_cli(1000.0 + claude.CLI_REFRESH_COOLDOWN_S))
        self.assertEqual(run.call_count, 2)

    def test_missing_cli_or_failure_returns_false(self):
        # 防沒裝 CLI 或 CLI 逾時讓 widget 崩潰
        with patch.object(claude, "_find_cli", return_value=None), \
             patch("subprocess.run") as run:
            self.assertFalse(claude.refresh_via_cli(1000.0))
        run.assert_not_called()
        claude._last_cli_refresh = None
        with patch.object(claude, "_find_cli", return_value="claude"), \
             patch("subprocess.run",
                   side_effect=claude.subprocess.TimeoutExpired("claude", 20)):
            self.assertFalse(claude.refresh_via_cli(5000.0))


class TestHasLogin(unittest.TestCase):
    def test_requires_refresh_token(self):
        # 防只有 MCP 憑證（從未登入 Claude 帳號）時被當成「可換新」
        d = Path(tempfile.mkdtemp())
        p = d / ".credentials.json"
        p.write_text(json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}))
        self.assertTrue(claude.has_login(p))
        p.write_text(json.dumps({"mcpOAuth": {}}))
        self.assertFalse(claude.has_login(p))
        p.write_text("{oops")
        self.assertFalse(claude.has_login(p))
        self.assertFalse(claude.has_login(d / "missing.json"))


if __name__ == "__main__":
    unittest.main()
