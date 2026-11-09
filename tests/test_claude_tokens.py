import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from providers.claude import TokenScanner, fetch, scan_window_start, today_total

NOW = datetime(2026, 11, 4, 18, 0, 0)  # local naive


def utc_iso(local_naive):
    """Local naive datetime -> UTC ISO string as written by Claude Code."""
    u = local_naive.astimezone(timezone.utc)
    return u.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def rec(mid, ts, inp=0, out=0, cc=0, cr=0):
    return json.dumps({
        "type": "assistant", "timestamp": ts, "requestId": "req_" + mid,
        "message": {"id": mid, "usage": {
            "input_tokens": inp, "output_tokens": out,
            "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr}},
    })


class Base(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        (self.dir / "proj").mkdir()
        self.f = self.dir / "proj" / "s1.jsonl"

    def write(self, lines):
        self.f.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestScanner(Base):
    def test_counts_input_output_cache_creation_not_cache_read(self):
        # 防 cache 讀取把波形灌到持續觸頂
        self.write([rec("m1", utc_iso(NOW - timedelta(minutes=10)), 10, 20, 30, 99999)])
        self.assertEqual([t for _, t in TokenScanner(self.dir).scan(NOW)], [60])

    def test_only_assistant_records_counted(self):
        # 防非 assistant 紀錄（如 user / summary 夾帶 message.usage）被重複計入 token（規格 2.3）
        ts = utc_iso(NOW - timedelta(minutes=10))
        other = json.loads(rec("u1", ts, out=500))
        other["type"] = "user"
        self.write([json.dumps(other), rec("m1", ts, out=7)])
        self.assertEqual([t for _, t in TokenScanner(self.dir).scan(NOW)], [7])

    def test_dedup_by_message_id(self):
        # 防串流重複寫入把 token 量灌水
        ts = utc_iso(NOW - timedelta(minutes=10))
        self.write([rec("m1", ts, out=5), rec("m1", ts, out=5), rec("m2", ts, out=7)])
        self.assertEqual(sum(t for _, t in TokenScanner(self.dir).scan(NOW)), 12)

    def test_utc_timestamps_converted_to_local_naive(self):
        # 防時區錯位導致波形偏移
        self.write([rec("m1", utc_iso(NOW - timedelta(minutes=10)), out=1)])
        ev = TokenScanner(self.dir).scan(NOW)
        self.assertEqual(ev[0][0], NOW - timedelta(minutes=10))
        self.assertIsNone(ev[0][0].tzinfo)

    def test_incremental_and_partial_line(self):
        # Review Focus 2：寫到一半的最後一行不能被消耗掉
        ts = utc_iso(NOW - timedelta(minutes=10))
        l1, l2 = rec("m1", ts, out=1), rec("m2", ts, out=2)
        s = TokenScanner(self.dir)
        self.f.write_text(l1 + "\n" + l2[:20], encoding="utf-8")
        self.assertEqual(len(s.scan(NOW)), 1)
        self.f.write_text(l1 + "\n" + l2 + "\n", encoding="utf-8")
        self.assertEqual(len(s.scan(NOW)), 2)

    def test_truncated_file_rescanned(self):
        # 防檔案被截斷/改寫後 offset 超界而漏讀或重複計算
        ts = utc_iso(NOW - timedelta(minutes=10))
        s = TokenScanner(self.dir)
        self.write([rec("m1", ts, out=1), rec("m2", ts, out=2)])
        self.assertEqual(len(s.scan(NOW)), 2)
        self.write([rec("m3", ts, out=9)])  # smaller than before
        ev = s.scan(NOW)
        self.assertEqual(sorted(t for _, t in ev), [1, 2, 9])

    def test_bad_lines_skipped(self):
        # 防單行壞資料讓整個檔案掃描中斷
        ts = utc_iso(NOW - timedelta(minutes=10))
        self.write([rec("m1", ts, out=1), "not json", rec("m2", ts, out=2)])
        self.assertEqual(sum(t for _, t in TokenScanner(self.dir).scan(NOW)), 3)

    def test_old_files_and_events_ignored(self):
        # 防讀取昨天的舊檔/舊事件而灌水今日 token
        old = NOW - timedelta(days=1)
        self.write([rec("m1", utc_iso(old), out=999)])
        yesterday = old.timestamp()
        os.utime(self.f, (yesterday, yesterday))
        self.assertEqual(TokenScanner(self.dir).scan(NOW), [])
        # fresh file but with an old event inside
        f2 = self.dir / "proj" / "s2.jsonl"
        f2.write_text(rec("m2", utc_iso(old), out=999) + "\n"
                      + rec("m3", utc_iso(NOW - timedelta(minutes=1)), out=4) + "\n")
        os.utime(f2, (NOW.timestamp(), NOW.timestamp()))
        self.assertEqual([t for _, t in TokenScanner(self.dir).scan(NOW)], [4])

    def test_scan_looks_up_window_start_at_call_time(self):
        # 防校準腳本 monkeypatch scan_window_start 失效（不可綁定舊參照）
        old = NOW - timedelta(days=3)
        self.write([rec("m1", utc_iso(old), out=7)])
        s = TokenScanner(self.dir)
        with patch("providers.claude.scan_window_start",
                   return_value=old - timedelta(hours=1)):
            os.utime(self.f, (NOW.timestamp(), NOW.timestamp()))
            self.assertEqual([t for _, t in s.scan(NOW)], [7])


class TestWindow(unittest.TestCase):
    def test_window_covers_whole_day(self):
        # 防「今日 token」只算到最近 5 小時
        self.assertEqual(scan_window_start(NOW), NOW.replace(hour=0, minute=0))
        self.assertEqual(scan_window_start(NOW.replace(hour=2)),
                         NOW.replace(hour=2) - timedelta(hours=5))

    def test_today_total(self):
        # 防把昨天的事件算進今日合計
        ev = [(NOW.replace(hour=0, minute=5), 100), (NOW - timedelta(days=1), 999), (NOW, 50)]
        self.assertEqual(today_total(ev, NOW), 150)


class TestFetch(unittest.TestCase):
    def test_fetch_combines_usage_and_tokens(self):
        # usage 為 login_required 時，token 資料照樣回傳
        with patch("providers.claude.fetch_usage", return_value=("login_required", {})):
            s = fetch()
        self.assertEqual(s["status"], "login_required")
        self.assertIn("token_events", s["extra"])
        self.assertEqual(set(s["bars"]), {"cl_5h", "cl_wk", "cl_model"})
        self.assertTrue(all(b["pct"] is None for b in s["bars"].values()))
        self.assertEqual(s["history"], {})

    def test_fetch_ok_history_keys(self):
        # 防 history 收進 None 或 key 命名錯誤
        bars = {"cl_5h": {"label": "5H", "pct": 65.0, "reset": ""},
                "cl_wk": {"label": "WK", "pct": None, "reset": ""},
                "cl_model": {"label": "--", "pct": None, "reset": ""}}
        with patch("providers.claude.fetch_usage", return_value=("ok", bars)):
            s = fetch()
        self.assertEqual(s["history"], {"claude_5h": 65.0})


if __name__ == "__main__":
    unittest.main()
