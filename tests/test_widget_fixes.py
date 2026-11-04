import threading
import tkinter as tk
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import widget
from display_logic import RATE_ALERT_COLOR, count_records, rate_color

NOW_UTC = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


class RateColorTests(unittest.TestCase):
    def test_alert_side_turns_red_above_threshold(self):
        # 防 AGY / Claude 5H 側的燃燒率紅色警示被一般化改壞（AGY 像素不得改變）
        self.assertEqual(rate_color(20.1, "#4ADE80"), RATE_ALERT_COLOR)
        self.assertEqual(rate_color(20, "#4ADE80"), "#4ADE80")

    def test_token_side_never_red(self):
        # 防 token 側（吞吐量而非額度）在高用量時誤報紅色警示（R7）
        self.assertEqual(rate_color(1000, "#818CF8", alert=False), "#818CF8")


class CountRecordsTests(unittest.TestCase):
    def test_claude_only_records_do_not_count_for_agy(self):
        # 防只有 Claude 欄位的紀錄讓 AGY 波形跳過 "Waiting..."、畫出一片空白
        hist = [{"timestamp": "t", "claude_5h": 50.0}] * 5 + [{"timestamp": "t", "gemini_5h": 90.0}]
        self.assertEqual(count_records(hist, ("gemini_5h", "external_5h")), 1)
        self.assertEqual(count_records(hist, ("claude_5h",)), 5)


class RefreshLockTests(unittest.TestCase):
    def _fake(self):
        return SimpleNamespace(is_fetching=False, _fetch_lock=threading.Lock(),
                               root=SimpleNamespace(after=lambda *a, **k: None),
                               refresh_btn=None)

    def test_concurrent_triggers_start_one_fetch(self):
        # 防 check-then-set 競態：托盤、按鈕與自動更新同時觸發時重複抓取並重複寫入歷史
        fake, gate, started = self._fake(), threading.Event(), []
        fake.fetch_and_update = lambda: (started.append(1), gate.wait(5))
        try:
            for _ in range(5):
                widget.UsageWidget.trigger_refresh(fake)
            self.assertEqual(len(started), 1)
            self.assertTrue(fake.is_fetching)
        finally:
            gate.set()

    def test_failed_fetch_releases_lock(self):
        # 防抓取拋例外後鎖未釋放，之後再也無法更新
        fake = self._fake()
        fake._fetch_lock.acquire()
        fake.is_fetching = True
        with patch.object(widget.data_fetcher, "fetch_all", side_effect=RuntimeError("x")):
            widget.UsageWidget.fetch_and_update(fake)
        self.assertFalse(fake.is_fetching)
        self.assertTrue(fake._fetch_lock.acquire(blocking=False))


class GaugeTkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
        except tk.TclError as e:
            raise unittest.SkipTest(f"no Tk display: {e}")
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def _model_gauge(self):
        g = widget.HorizontalFuelGauge(self.root, width=96, height=36, title="--", theme_id="cl_model")
        g.pack()
        self.root.update_idletasks()
        return g

    def test_null_model_bar_title_is_grey(self):
        # 防兩個模型桶皆 null 時 "--" 標題顯示為靛色（規格 2.2：灰色 --）
        g = self._model_gauge()
        g.set_value(None, "", NOW_UTC)
        self.assertEqual(g.itemcget(g.title_text, "fill"), widget.TEXT_MUTED)
        g.set_stale(False)
        self.assertEqual(g.itemcget(g.title_text, "fill"), widget.TEXT_MUTED)

    def test_offline_after_login_recentres_time_text(self):
        # 防 LOGIN 狀態左移的倒數文字在轉為離線（--%）後仍停在左側
        g = self._model_gauge()
        g.set_login()
        self.assertNotEqual(g.coords(g.time_text)[0], g.width / 2)
        g.set_stale(True)
        self.assertEqual(g.coords(g.time_text), [g.width / 2, 9.0])


if __name__ == "__main__":
    unittest.main()
