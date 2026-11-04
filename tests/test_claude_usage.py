import io
import json
import os
import socket
import tempfile
import unittest
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


class TestFetchUsage(unittest.TestCase):
    def test_no_token_is_login_required_without_network(self):
        # 防沒有 token 時仍打網路
        with patch.object(claude, "read_access_token", return_value=None), \
             patch("urllib.request.urlopen") as uo:
            status, bars = fetch_usage(2000)
        self.assertEqual(status, "login_required")
        uo.assert_not_called()

    def _run(self, **kw):
        with patch.object(claude, "read_access_token", return_value="tok"), \
             patch("urllib.request.urlopen", **kw) as uo:
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


if __name__ == "__main__":
    unittest.main()
