import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from display_logic import (TOKENS_PER_BLOCK, bucket_drops, bucket_tokens,
                           format_countdown, format_tokens)

FIX = Path(__file__).parent / "fixtures"
UTC_NOW = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 10, 16, 13, 0, 0)


def rec(ts, **fields):
    return {"timestamp": ts.isoformat(), **fields}


def mins(n):
    return timedelta(minutes=n)


class DisplayLogicTests(unittest.TestCase):
    def test_countdown_formats_equivalent(self):
        # 防 Claude 倒數永遠顯示 "--"：三種 ISO 寫法結果相同
        for r in ["2026-10-16T07:30:00Z", "2026-10-16T07:30:00.123456+00:00",
                  "2026-10-16T15:30:00+08:00"]:
            self.assertEqual(format_countdown(50, r, UTC_NOW), "2h 30m")

    def test_countdown_edges(self):
        # 防邊界狀態（滿格/跨日/已過期/空值/亂碼/無百分比）顯示錯誤文字或丟例外
        self.assertEqual(format_countdown(99.95, "2026-10-16T07:30:00Z", UTC_NOW), "Full")
        self.assertEqual(format_countdown(50, "2026-10-18T07:30:00Z", UTC_NOW), "2d 2h")
        self.assertEqual(format_countdown(50, "2026-10-16T04:00:00Z", UTC_NOW), "Reset...")
        self.assertEqual(format_countdown(50, "", UTC_NOW), "--")
        self.assertEqual(format_countdown(50, "garbage", UTC_NOW), "--")
        self.assertEqual(format_countdown(None, "2026-10-16T07:30:00Z", UTC_NOW), "--")

    def test_format_tokens(self):
        # 防 token 數字單位（tok/k/M）切換點錯誤
        self.assertEqual([format_tokens(n) for n in (950, 12345, 1234567)],
                         ["950 tok", "12.3k tok", "1.2M tok"])

    def test_bucket_drops_matches_legacy_algorithm(self):
        # 防重構改變 AGY 波形：與舊版 VerticalHistoryChart.render 的分桶邏輯逐格一致
        H = json.loads((FIX / "history_fixture.json").read_text(encoding="utf-8"))
        legacy = json.loads((FIX / "legacy_buckets.json").read_text(encoding="utf-8"))
        self.assertEqual(bucket_drops(H, "gemini_5h", NOW, 100, 300.0), legacy["gem"])
        self.assertEqual(bucket_drops(H, "external_5h", NOW, 100, 300.0), legacy["ext"])

    def test_bucket_drops_reset_is_not_negative(self):
        # Review Focus 4：5H 重置讓剩餘 % 回升時，不能出現負值
        h = [rec(NOW - mins(9), claude_5h=10), rec(NOW - mins(6), claude_5h=100),
             rec(NOW - mins(1), claude_5h=98)]
        out = bucket_drops(h, "claude_5h", NOW, 100, 300.0)
        self.assertTrue(all(v >= 0 for v in out))
        self.assertAlmostEqual(sum(out), 2.0)

    def test_bucket_drops_missing_key_is_zero(self):
        # 防舊歷史紀錄（沒有 claude_*）產生假尖峰或 KeyError
        h = [rec(NOW - mins(9), gemini_5h=90), rec(NOW - mins(3), gemini_5h=80)]
        self.assertEqual(sum(bucket_drops(h, "claude_5h", NOW, 100, 300.0)), 0)

    def test_bucket_drops_rows_are_centred(self):
        # 防 3 分鐘輪詢點落在格子邊界而讓相鄰尖峰錯位／融合（e1191a4）：第 i 格以 now-3i 分為中心 ±1.5 分。
        # 手算：d=3 分。第 0 格 (now-1.5, now]；第 1 格 (now-4.5, now-1.5]。
        #   1.4 分前 → 第 0 格；1.5 分前（剛好在邊界）→ 第 1 格；4.4 分前 → 第 1 格；4.5 分前 → 第 2 格。
        for age, row in ((1.4, 0), (1.5, 1), (4.4, 1), (4.5, 2)):
            h = [rec(NOW - mins(299), claude_5h=100), rec(NOW - mins(age), claude_5h=99)]
            out = bucket_drops(h, "claude_5h", NOW, 100, 300.0)
            self.assertEqual(out.index(1), row, f"age {age}")
            self.assertEqual(sum(out), 1)

    def test_bucket_tokens_alignment(self):
        # Review Focus 3：事件落在正確的格子（第 0 格為最新），格界與 bucket_drops 相同（置中）。
        # 手算：1 分前 → 第 0 格 (now-1.5, now]：40000/20000 = 2.0 格；
        #       7 分前 → 第 2 格 (now-7.5, now-4.5]：10000/20000 = 0.5 格。
        ev = [(NOW - mins(1), 40000), (NOW - mins(7), 10000)]
        out = bucket_tokens(ev, NOW, 100, 300.0, 20000)
        self.assertEqual(out[0], 2.0)
        self.assertEqual(out[2], 0.5)
        self.assertEqual(sum(out), 2.5)

    def test_bucket_tokens_rows_match_bucket_drops(self):
        # 防 Claude 圖表左右兩半錯位：同一時刻的 5H 下降與 token 事件必須落在同一列。
        # 手算邊界：0 分前 → 第 0 格；1.5 分前 → 第 1 格（屬於較舊那格，與 drops 的 ts <= b_end 一致）；
        #   298 分前 → 第 99 格 (now-298.5, now-295.5]。
        for age in (0, 1.4, 1.5, 4.5, 7, 298):
            drops = bucket_drops([rec(NOW - mins(299), claude_5h=100), rec(NOW - mins(age), claude_5h=99)],
                                 "claude_5h", NOW, 100, 300.0)
            toks = bucket_tokens([(NOW - mins(age), 20000)], NOW, 100, 300.0, 20000)
            self.assertEqual(toks.index(1.0), drops.index(1), f"age {age}")

    def test_bucket_tokens_ignores_out_of_window(self):
        # 防視窗外（太舊或未來）的事件被塞進首/末格造成假尖峰或 IndexError
        # 手算：最後一格 (第 99 格) 起點為 now-298.5 分，298.5 分前已在視窗外（與 bucket_drops 相同）。
        ev = [(NOW - mins(301), 99999), (NOW - mins(298.5), 99999), (NOW + mins(5), 99999),
              (NOW - mins(1), 20000)]
        out = bucket_tokens(ev, NOW, 100, 300.0, 20000)
        self.assertEqual(len(out), 100)
        self.assertAlmostEqual(sum(out), 1.0)

    def test_tokens_per_block_default(self):
        # 防常數被誤改而讓格數換算失準（Task 9 校準前為 20000）
        self.assertEqual(TOKENS_PER_BLOCK, 20000)


if __name__ == "__main__":
    unittest.main()
