import json
import unittest
from pathlib import Path
from unittest.mock import patch

import data_fetcher
from providers.agy import parse_quota

FIX = Path(__file__).parent / "fixtures"


def _load(name):
    with open(FIX / name, encoding="utf-8") as f:
        return json.load(f)


class TestAgyParse(unittest.TestCase):
    def setUp(self):
        self.data = _load("agy_quota_response.json")
        self.legacy = _load("agy_expected_legacy.json")

    def test_matches_legacy_values(self):
        # 防搬家時改壞 AGY 解析：每條 bar 的 pct / reset 必須與舊版逐一相同
        s = parse_quota(self.data)
        self.assertEqual(s["status"], "ok")
        pairs = {"gem_5h": ("gemini", "5hr_percent", "reset_time_5h"),
                 "ext_5h": ("external", "5hr_percent", "reset_time_5h"),
                 "gem_wk": ("gemini", "weekly_percent", "reset_time_weekly"),
                 "ext_wk": ("external", "weekly_percent", "reset_time_weekly"),
                 "arg_wk": ("argon", "weekly_percent", "reset_time_weekly")}
        for bar, (grp, pk, rk) in pairs.items():
            self.assertEqual(s["bars"][bar]["pct"], self.legacy[grp][pk], bar)
            self.assertEqual(s["bars"][bar]["reset"], self.legacy[grp][rk], bar)
        self.assertEqual(s["history"], {"gemini_5h": self.legacy["gemini"]["5hr_percent"],
                                        "external_5h": self.legacy["external"]["5hr_percent"],
                                        "argon_wk": self.legacy["argon"]["weekly_percent"]})
        self.assertEqual(s["extra"], {"argon_available": True})
        self.assertEqual([s["bars"][b]["label"] for b in pairs],
                         ["GEM", "EXT", "GEM", "EXT", "ARG"])

    def test_none_is_offline(self):
        # 防 language_server 未啟動時誤寫一筆 0% 進歷史，造成假的 100% 消耗尖峰
        s = parse_quota(None)
        self.assertEqual(s["status"], "offline")
        self.assertEqual(s["history"], {})

    def test_legacy_adapter_unchanged(self):
        # 防 Task 7 之前 widget 讀到的資料格式被改變
        with patch("providers.agy.fetcher.get_quota", return_value=self.data), \
             patch("history_logger.log_usage"):
            out = data_fetcher.fetch_usage_data()
        out.pop("last_updated")
        self.assertEqual(out, self.legacy)


if __name__ == "__main__":
    unittest.main()
