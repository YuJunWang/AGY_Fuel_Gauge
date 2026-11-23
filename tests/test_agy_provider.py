import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from providers import agy
from providers.agy import parse_quota

FIX = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 23, 15, 0, 0)


def _load(name):
    with open(FIX / name, encoding="utf-8") as f:
        return json.load(f)


def _write_transcript(brain_dir, conv_id, records, mtime=None):
    log_dir = Path(brain_dir) / conv_id / ".system_generated" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    p = log_dir / "transcript.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    if mtime is not None:
        ts = mtime.timestamp()
        import os
        os.utime(p, (ts, ts))
    return p


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


class TestAgyTranscriptScanner(unittest.TestCase):
    def _iso_utc(self, local_dt):
        return local_dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def test_separates_hydrogen_and_argon_and_calculates_reset_countdowns(self):
        """
        Verify intent: PLANNER_RESPONSE steps are routed to HYD vs ARG by model name,
        remaining fuel % is deducted from calibrated Pro budgets, and reset ISO timestamps are populated.
        """
        with tempfile.TemporaryDirectory() as tmp:
            t_hyd_5h = NOW - timedelta(hours=1)
            t_arg_wk = NOW - timedelta(minutes=30)
            t_hyd_wk = NOW - timedelta(days=2)
            _write_transcript(tmp, "conv-1", [
                {"step_index": 1, "type": "PLANNER_RESPONSE",
                 "created_at": self._iso_utc(t_hyd_wk),
                 "model": "gemini-4-hydrogen-xhigh",
                 "input_tokens": 2_000_000, "output_tokens": 1_080_000},
                {"step_index": 2, "type": "PLANNER_RESPONSE",
                 "created_at": self._iso_utc(t_hyd_5h),
                 "model": "gemini-4-hydrogen-high",
                 "input_tokens": 1_000_000, "output_tokens": 710_000},
                {"step_index": 3, "type": "PLANNER_RESPONSE",
                 "created_at": self._iso_utc(t_arg_wk),
                 "model": "gemini-4-argon-medium",
                 "input_tokens": 500_000, "output_tokens": 434_000},
                {"step_index": 4, "type": "USER_INPUT",
                 "created_at": self._iso_utc(NOW - timedelta(minutes=20)),
                 "input_tokens": 9_999_999},
            ], mtime=NOW)

            sc = agy.AgyTokenScanner(tmp)
            events = sc.scan(NOW)
            snap = agy.build_transcript_snapshot(events, NOW, wk_anchor=t_hyd_wk)

            self.assertEqual(snap["status"], "ok")
            bars = snap["bars"]

            # HYD 5H: 1.71M / 8.55M used = 20% used -> 80.0% remaining, resets at t_hyd_5h + 5h
            self.assertEqual(bars["gem_5h"]["label"], "HYD")
            self.assertAlmostEqual(bars["gem_5h"]["pct"], 80.0)
            self.assertEqual(bars["gem_5h"]["reset"], self._iso_utc(t_hyd_5h + timedelta(hours=5)))

            # HYD WK: (3.08M + 1.71M) = 4.79M / 30.8M used -> 84.4% remaining, resets at t_hyd_wk + 7d
            self.assertEqual(bars["gem_wk"]["label"], "HYD")
            self.assertAlmostEqual(bars["gem_wk"]["pct"], 84.4)
            self.assertEqual(bars["gem_wk"]["reset"], self._iso_utc(t_hyd_wk + timedelta(days=7)))

            # ARG (5H & WK views show Argon weekly bucket): 0.934M / 4.67M = 20% used -> 80.0% remaining
            for key in ("ext_5h", "ext_wk"):
                self.assertEqual(bars[key]["label"], "ARG")
                self.assertAlmostEqual(bars[key]["pct"], 80.0)
                self.assertEqual(bars[key]["reset"], self._iso_utc(t_arg_wk + timedelta(days=7)))


if __name__ == "__main__":
    unittest.main()
