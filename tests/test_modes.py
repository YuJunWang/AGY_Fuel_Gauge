import tempfile
import unittest
from pathlib import Path

from display_logic import MODES, anchored_x, load_mode, mode_width, save_mode


class ModeTests(unittest.TestCase):
    def test_widths(self):
        # 防 BOTH 模式少一欄或單一模式多出空白：24 標籤 + 每欄 110
        self.assertEqual([mode_width(m) for m in MODES], [134, 134, 244])

    def test_anchored_x_keeps_right_edge(self):
        # 防切換模式時視窗右緣跳動：加寬往左長，縮窄回到原位
        self.assertEqual(anchored_x(1000, 134, 244), 890)
        self.assertEqual(anchored_x(890, 244, 134), 1000)

    def test_anchored_x_clamps_left_edge(self):
        # 防貼左緣時加寬跑出螢幕（含主螢幕左側的負座標副螢幕）
        self.assertEqual(anchored_x(50, 134, 244), 0)
        self.assertEqual(anchored_x(-1900, 134, 244, screen_left=-1920), -1920)

    def test_mode_roundtrip_and_corruption(self):
        # 防模式記憶失效，或損毀／未知內容讓 widget 啟動失敗：一律退回 agy
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "widget_state.json"
            save_mode(p, "both")
            self.assertEqual(load_mode(p), "both")
            p.write_text("{oops")
            self.assertEqual(load_mode(p), "agy")
            p.write_text('{"mode": "nope"}')
            self.assertEqual(load_mode(p), "agy")
            p.write_text('["claude"]')
            self.assertEqual(load_mode(p), "agy")
            self.assertEqual(load_mode(p.with_name("missing.json")), "agy")


if __name__ == "__main__":
    unittest.main()
