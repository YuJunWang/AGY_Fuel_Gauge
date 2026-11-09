# Claude 雙來源監控 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 AGY Fuel Gauge 同時監控 Antigravity 與 Claude 的模型用量，並可在 AGY / CLD / BOTH 三種模式間切換。

**Architecture:** 兩個 provider（`providers/agy.py`、`providers/claude.py`）各自回傳統一格式的快照，由 `data_fetcher.fetch_all()` 平行調度。所有不碰 Tk 的計算（倒數、格式化、波形分桶、模式狀態）集中在新的 `display_logic.py`，全部以參數接收 `now`、不自己讀時鐘。`widget.py` 用 `THEMES` 主題表與 `ProviderPanel` 組裝畫面。

**Tech Stack:** Python 3.12（`C:\Users\yujunwang\AppData\Local\Programs\Python\Python312`）、tkinter、Pillow、pystray、psutil（選用）、標準函式庫 `unittest`。

**Spec:** `docs/superpowers/specs/2026-11-04-claude-dual-monitor-design.md`

## Global Constraints

- 不新增任何第三方依賴；測試只用 `unittest` 與 `unittest.mock`。
- 測試指令：`python -m unittest discover -s tests -v`（在 repo 根目錄執行）。
- 既有色值（GEM `#22C55E`、EXT `#F97316`、ARG `#06B6D4` 及其 base / tip / warn / danger）一律不改。
- Claude 色：5H `#A78BFA`、WK `#E879F9`、模型橫條 `#818CF8`；CLD 分頁選中色 `#D97757`。
- Claude 波形漸層：左 `#A78BFA→#E879F9→#EF4444`，右 `#818CF8→#3B82F6→#22D3EE`。
- 視窗寬度：單一模式 134，BOTH 244；高度 598。Panel 寬 110。
- Claude 額度端點：`GET https://api.anthropic.com/api/oauth/usage`，標頭 `Authorization: Bearer <token>`、`anthropic-beta: oauth-2025-04-20`，逾時 5 秒。
- Claude 設定目錄：環境變數 `CLAUDE_CONFIG_DIR`，未設定則 `~/.claude`。
- **絕不寫入** `.credentials.json`。
- token 計入 `input_tokens + output_tokens + cache_creation_input_tokens`，排除 `cache_read_input_tokens`。
- `display_logic.py` 內的函式不得呼叫 `datetime.now()` / `utcnow()`；時間一律由呼叫端傳入。`widget.py` 內所有時鐘讀取都透過模組層級的 `datetime` 名稱（像素比對腳本靠 monkeypatch 它）。
- 錯誤記錄沿用 `print("[模組名] ...")`。
- 每個 commit 訊息結尾加上 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。

## Review Focus

1. **憑證檔存在但 token 已過期**（`claudeAiOauth.expiresAt` 為過去的毫秒時間戳）：應直接回傳 `login_required`，不發出網路請求 → Task 3 補測試。
2. **JSONL 正在被 Claude Code 寫入**（最後一行沒有換行、JSON 不完整）：這一行不能被消耗掉，下次掃描要完整讀到 → Task 4 補測試。
3. **時區混用**：JSONL 時間戳是 UTC `Z`，歷史檔是本地 naive 時間；兩者分桶到同一條時間軸時不能偏移 8 小時 → Task 5 補測試。
4. **5H 額度重置造成剩餘 % 回升**：分桶消耗不能出現負值或假尖峰 → Task 5 補測試。
5. **視窗貼近螢幕左緣時切到 BOTH**：往左加寬後 x 不能變成負數跑出螢幕 → Task 8 補測試。

---

## 檔案結構

| 檔案 | 職責 | 動作 |
|---|---|---|
| `providers/__init__.py` | 套件標記 | 新增（空檔） |
| `providers/agy.py` | AGY gRPC 抓取（原 `QuotaFetcher`）＋ 轉快照 | 新增 |
| `providers/claude.py` | Claude 額度端點 ＋ JSONL token 掃描 ＋ 轉快照 | 新增 |
| `data_fetcher.py` | 平行調度兩個 provider、寫歷史 | 改寫 |
| `history_logger.py` | `log_usage(**fields)` | 修改 |
| `display_logic.py` | 純函式：倒數、token 格式、分桶、模式寬度與錨點、模式持久化 | 新增 |
| `widget.py` | THEMES、量表、波形圖、ProviderPanel、ModeTab、視窗 | 修改 |
| `tests/` | 單元測試；`tests/fixtures/` 放 fixture | 新增 |
| `scratch/pixel_check.py` | 一次性像素比對工具（gitignored） | 新增 |
| `.gitignore`、`README.md` | 忽略 `widget_state.json`；文件 | 修改 |

## 快照格式（所有 Task 共用）

```python
Snapshot = {
    "status": "ok" | "offline" | "login_required" | "error",
    "bars": {bar_id: {"label": str, "pct": float | None, "reset": str}},
    "history": {history_key: float},
    "extra": dict,
}
```
- AGY `bars`：`gem_5h, ext_5h, gem_wk, ext_wk, arg_wk`；`history`：`gemini_5h, external_5h`，以及 Argon 可用時的 `argon_wk`；`extra`：`{"argon_available": bool}`。
- Claude `bars`：`cl_5h, cl_wk, cl_model`；`history`：`claude_5h, claude_wk`（只放非 None 的值）；`extra`：`{"token_events": list[tuple[datetime, int]], "today_tokens": int}`。`token_events` 的 datetime 一律是**本地 naive**。

---

### Task 1: 像素比對工具與基準截圖

在改任何程式碼之前，先拍下 `4fbc37c` 版 AGY 畫面當基準。

**Files:**
- Create: `scratch/pixel_check.py`（gitignored，不 commit）
- Output: `scratch/baseline_agy.png`

**Interfaces:**
- Produces: `python scratch/pixel_check.py capture <out.png>` 和 `python scratch/pixel_check.py compare <a.png> <b.png> [--x0 N]`，後者印出 `diff_pixels=<n>`，n 為 0 時 exit code 0。

- [ ] **Step 1: 寫 `scratch/pixel_check.py`**

`capture` 模式的流程，從外部注入、不修改產品程式碼：
1. `sys.path.insert(0, repo_root)`，然後 `import widget, history_logger, data_fetcher`。
2. 固定時間：定義 `class FixedDatetime(datetime)`，覆寫 `now(tz=None)`，在 `tz` 為 None 時回傳 naive 本地 `2026-10-16 13:00:00`，有 `tz` 時回傳 `2026-10-16 05:00:00 UTC` 換算成該時區；覆寫 `utcnow()`，回傳 naive `2026-10-16 05:00:00`。指定 `widget.datetime = FixedDatetime`。
3. 歷史資料：`history_logger.get_history = lambda minutes=90: FIXTURE_HISTORY`，同時 `history_logger.log_usage = lambda *a, **k: None`。`FIXTURE_HISTORY` 是 30 筆紀錄，每 3 分鐘一筆，結尾在本地 13:00；`gemini_5h` 從 100 每筆減 0.8，`external_5h` 從 100 每筆減 1.5，且第 20 筆的 `external_5h` 一次掉 12，用來觸發霓虹高光。
4. gRPC 回應：從 `scratch/test_argon_upgrade.py` 複製 `MOCK_QUOTA_RESPONSE`。如果 `providers.agy` 可以 import，就 patch `providers.agy.fetcher.get_quota`；否則 patch `data_fetcher.fetcher.get_quota`。兩種情況都回傳 `MOCK_QUOTA_RESPONSE`。如果 `providers.claude` 存在，patch `providers.claude.fetch`，回傳 `{"status": "login_required", "bars": {}, "history": {}, "extra": {}}`。
5. 如果 `display_logic` 有 `load_mode`，patch 成回傳 `"agy"`。
6. 把 `widget.UsageWidget.auto_update_loop` 和 `setup_tray` patch 成 no-op。建立 `app = widget.UsageWidget()`，呼叫 `app.show_window()`；之後設定 `app.root.attributes('-alpha', 1.0)` 和 `app.root.geometry("+100+100")`。
7. 用 `app.root.after(4000, ...)` 排程：以 `winfo_rootx/rooty/width/height` 和 `PIL.ImageGrab.grab(bbox=...)` 截圖並存檔，然後 `app.root.destroy()`。最後執行 `app.root.mainloop()`。

`compare`：兩張圖尺寸取最小的寬高，只比對 `x >= x0` 的區域；用 `ImageChops.difference` 加 `getbbox`，再逐像素計算非零像素數。

- [ ] **Step 2: 拍基準圖**

Run: `python scratch/pixel_check.py capture scratch/baseline_agy.png`
Expected: 產生 134×598 的 PNG，畫面有 GEM / EXT 油量條、ARG 橫條和波形。

- [ ] **Step 3: 驗證工具本身是確定性的**

Run: `python scratch/pixel_check.py capture scratch/baseline_agy_2.png` 接著 `python scratch/pixel_check.py compare scratch/baseline_agy.png scratch/baseline_agy_2.png`
Expected: `diff_pixels=0`。如果不是 0，找出不確定的來源（通常是動畫還沒結束或時鐘沒被 patch 到）並修正工具，直到結果穩定為 0 才能往下做。

（scratch 被 gitignore，這個 Task 不需要 commit。）

---

### Task 2: `providers/agy.py`（搬家，邏輯不改）

**Files:**
- Create: `providers/__init__.py`、`providers/agy.py`
- Create: `tests/__init__.py`、`tests/fixtures/agy_quota_response.json`（內容為 `MOCK_QUOTA_RESPONSE`）、`tests/fixtures/agy_expected_legacy.json`
- Modify: `data_fetcher.py`（改成從 provider 取資料，保留相容的 `fetch_usage_data()`）
- Test: `tests/test_agy_provider.py`

**Interfaces:**
- Produces: `providers.agy.fetcher`（`QuotaFetcher` 單例）、`providers.agy.parse_quota(data: dict | None) -> Snapshot`、`providers.agy.fetch() -> Snapshot`。
- Produces（暫時，Task 7 刪除）：`data_fetcher.fetch_usage_data() -> dict`，回傳舊格式，讓 widget 在 Task 7 之前照常運作。

- [ ] **Step 1: 產生舊版黃金輸出**

寫一段一次性的指令：在目前程式碼（等同 `4fbc37c`）下，把 `data_fetcher.fetcher.get_quota` patch 成回傳 fixture，`history_logger.log_usage` patch 成 no-op。呼叫 `data_fetcher.fetch_usage_data()`，刪掉 `last_updated` 後，把結果寫入 `tests/fixtures/agy_expected_legacy.json`。

- [ ] **Step 2: 寫失敗的測試**

```python
class TestAgyParse(unittest.TestCase):
    def setUp(self):
        self.data = json.load(open(FIX / "agy_quota_response.json"))
        self.legacy = json.load(open(FIX / "agy_expected_legacy.json"))

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
```

- [ ] **Step 3: 執行，確認失敗**

Run: `python -m unittest tests.test_agy_provider -v`
Expected: FAIL（`ModuleNotFoundError: providers`）

- [ ] **Step 4: 實作**

把 `QuotaFetcher`、`LOG_DIR`、`GRPC_PATH`、psutil 的 import 原樣移到 `providers/agy.py`，並建立 `fetcher = QuotaFetcher()`。`parse_quota` 沿用舊版 `fetch_usage_data` 的分組迴圈；每條 bar 在 bucket 不存在時，維持舊版預設值 `pct=0`、`reset=""`。`data_fetcher.fetch_usage_data()` 改為呼叫 `providers.agy.fetcher.get_quota()` 加上 `parse_quota`，再組回舊的 dict 格式（含 `5hr_used` / `weekly_used` 和 `argon.available`），並照舊呼叫 `history_logger.log_usage`。

- [ ] **Step 5: 執行，確認通過**

Run: `python -m unittest tests.test_agy_provider -v`
Expected: 3 個測試 PASS

- [ ] **Step 6: 像素比對**

Run: `python scratch/pixel_check.py capture scratch/after_t2.png`，接著 `python scratch/pixel_check.py compare scratch/baseline_agy.png scratch/after_t2.png`
Expected: `diff_pixels=0`

- [ ] **Step 7: Commit**

```bash
git add providers/ tests/ data_fetcher.py
git commit -m "refactor: move AGY quota fetching into providers/agy.py"
```

---

### Task 3: `providers/claude.py` — 訂閱額度

**Files:**
- Create: `providers/claude.py`
- Test: `tests/test_claude_usage.py`

**Interfaces:**
- Produces: `claude_dir() -> Path`、`read_access_token(creds_path: Path, now_ms: int) -> str | None`、`parse_usage(payload: dict) -> dict[str, dict]`（回傳 `cl_5h`、`cl_wk`、`cl_model` 三條 bar）、`fetch_usage(now_ms: int) -> tuple[str, dict]`（回傳 status 與 bars）。

- [ ] **Step 1: 寫失敗的測試**

```python
def bucket(u, r="2026-11-04T15:00:00.123456+00:00"): return {"utilization": u, "resets_at": r}

class TestParseUsage(unittest.TestCase):
    def test_remaining_is_100_minus_utilization(self):
        b = parse_usage({"five_hour": bucket(35.0), "seven_day": bucket(14.0)})
        self.assertEqual(b["cl_5h"]["pct"], 65.0)
        self.assertEqual(b["cl_wk"]["pct"], 86.0)
        self.assertEqual(b["cl_5h"]["reset"], "2026-11-04T15:00:00.123456+00:00")

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

    def test_model_bar_prefers_opus_then_sonnet(self):
        b = parse_usage({"seven_day_opus": None, "seven_day_sonnet": bucket(40)})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("SON", 60.0))
        b = parse_usage({"seven_day_opus": bucket(10), "seven_day_sonnet": bucket(40)})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("OPS", 90.0))
        b = parse_usage({})
        self.assertEqual((b["cl_model"]["label"], b["cl_model"]["pct"]), ("--", None))

class TestToken(unittest.TestCase):
    # 用 tempfile 寫入 credentials JSON
    def test_missing_file_or_key_is_none(self): ...  # 不存在的檔案、只有 mcpOAuth 的檔案 → None
    def test_expired_token_is_none(self):
        # Review Focus 1：過期的 token 不發網路請求，直接當成未登入
        # {"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}，now_ms=2000 → None
    def test_valid_token(self): ...  # expiresAt=5000，now_ms=2000 → "t"

class TestFetchUsage(unittest.TestCase):
    def test_no_token_is_login_required_without_network(self):
        # patch read_access_token → None；patch urllib.request.urlopen，並斷言它沒被呼叫
    def test_401_is_login_required(self):
        # urlopen 拋出 HTTPError(code=401) → status "login_required"
    def test_timeout_is_offline(self):
        # urlopen 拋出 URLError / socket.timeout → status "offline"
    def test_ok(self):
        # urlopen 回傳 BytesIO(json) → status "ok"，bars 等於 parse_usage 的結果；
        # 斷言 request 帶有 anthropic-beta: oauth-2025-04-20 和 Bearer 標頭
```

上面 `...` 的部分，實作者依註解寫出對應的建立資料與斷言。

- [ ] **Step 2: 執行，確認失敗**

Run: `python -m unittest tests.test_claude_usage -v`
Expected: FAIL（ImportError）

- [ ] **Step 3: 實作**

- `claude_dir()`：使用 `Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")`。
- `read_access_token`：在檔案不存在、JSON 損毀、缺少 key，或 `expiresAt <= now_ms` 時回傳 None。`expiresAt` 不存在時視為有效。
- `fetch_usage`：使用 `urllib.request`、逾時 5 秒。HTTP 401 / 403 回傳 `login_required`；其他 `HTTPError`（含 5xx）、`URLError`、`TimeoutError`、`OSError` 回傳 `offline`；回應 JSON 解析失敗回傳 `error`。三條 bar 的 pct 全部是 None 時也回傳 `error`。
- bar 的 label：`cl_5h` 為 `"5H"`，`cl_wk` 為 `"WK"`。

- [ ] **Step 4: 執行，確認通過**

Run: `python -m unittest tests.test_claude_usage -v`
Expected: 全部 PASS

- [ ] **Step 5: Commit**

```bash
git add providers/claude.py tests/test_claude_usage.py
git commit -m "feat(claude): fetch subscription usage from oauth usage endpoint"
```

---

### Task 4: `providers/claude.py` — JSONL token 掃描與 `fetch()`

**Files:**
- Modify: `providers/claude.py`
- Test: `tests/test_claude_tokens.py`

**Interfaces:**
- Consumes: Task 3 的 `fetch_usage`、`claude_dir`。
- Produces: `scan_window_start(now: datetime) -> datetime`，回傳 `min(now - 5h, 今天 00:00)`；`class TokenScanner(projects_dir: Path)`，提供 `scan(now: datetime) -> list[tuple[datetime, int]]`（本地 naive 時間、已去重、只保留 `>= scan_window_start(now)` 的事件）；`today_total(events: list[tuple[datetime, int]], now: datetime) -> int`；`fetch() -> Snapshot`。

- [ ] **Step 1: 寫失敗的測試**

測試用 `tempfile.TemporaryDirectory()` 建出 `projects/<proj>/<session>.jsonl`。輔助函式 `rec(mid, ts, inp=0, out=0, cc=0, cr=0)` 產生一行 assistant JSON，含 `timestamp`、`requestId` 和 `message.id`、`message.usage`。

```python
NOW = datetime(2026, 11, 4, 18, 0, 0)  # 本地 naive

def test_counts_input_output_cache_creation_not_cache_read(self):
    # 防 cache 讀取把波形灌到持續觸頂
    write(f, [rec("m1", utc_iso(NOW - 10min), inp=10, out=20, cc=30, cr=99999)])
    self.assertEqual([t for _, t in TokenScanner(d).scan(NOW)], [60])

def test_dedup_by_message_id(self):
    # 防串流重複寫入把 token 量灌水
    write(f, [rec("m1", ts, out=5), rec("m1", ts, out=5), rec("m2", ts, out=7)])
    self.assertEqual(sum(t for _, t in TokenScanner(d).scan(NOW)), 12)

def test_utc_timestamps_converted_to_local_naive(self):
    # 防時區錯位導致波形偏移
    ev = TokenScanner(d).scan(NOW)          # 寫入 utc_iso(NOW - 10min)
    self.assertEqual(ev[0][0], NOW - timedelta(minutes=10))

def test_incremental_and_partial_line(self):
    # Review Focus 2：寫到一半的最後一行不能被消耗掉
    s = TokenScanner(d)
    f.write_text(line1 + "\n" + line2_without_newline[:20])
    self.assertEqual(len(s.scan(NOW)), 1)
    f.write_text(line1 + "\n" + line2_full + "\n")
    self.assertEqual(len(s.scan(NOW)), 2)

def test_truncated_file_rescanned(self): ...   # 檔案變小 → 從頭重讀，不重複也不遺漏
def test_bad_lines_skipped(self): ...          # 中間夾一行 "not json" 不影響其他行
def test_old_files_and_events_ignored(self): ...  # NOW=18:00：mtime 為昨天的檔案不讀；昨天的事件不回傳

def test_window_covers_whole_day(self):
    # 防「今日 token」只算到最近 5 小時：18:00 時，早上 09:00 的事件必須保留
    self.assertEqual(scan_window_start(NOW), NOW.replace(hour=0, minute=0))
    self.assertEqual(scan_window_start(NOW.replace(hour=2)), NOW.replace(hour=2) - timedelta(hours=5))

def test_today_total(self):
    ev = [(NOW.replace(hour=0, minute=5), 100), (NOW - timedelta(days=1), 999), (NOW, 50)]
    self.assertEqual(today_total(ev, NOW), 150)

def test_fetch_combines_usage_and_tokens(self):
    # usage 為 login_required 時，token 資料照樣回傳
    with patch("providers.claude.fetch_usage", return_value=("login_required", {})):
        s = fetch()
    self.assertEqual(s["status"], "login_required")
    self.assertIn("token_events", s["extra"])
```

- [ ] **Step 2: 執行，確認失敗**

Run: `python -m unittest tests.test_claude_tokens -v`
Expected: FAIL

- [ ] **Step 3: 實作**

- `TokenScanner` 在記憶體保存三樣東西：`{path: offset}`、`seen_ids: set`、`events: list`。
- `scan` 的流程：
  1. 先算出 `start = scan_window_start(now)`。
  2. 遍歷 `projects_dir.rglob("*.jsonl")`，只處理 `mtime >= start` 的檔案。檔案大小小於 offset 時，把 offset 歸零重讀。
  3. 以二進位模式從 offset 讀到最後一個 `\n`，offset 只前進到那個位置。
  4. 去重 key 用 `message.id`，沒有的話用 `requestId`。
  5. 時間戳以 `datetime.fromisoformat` 解析後，用 `.astimezone()` 轉成本地時間，再 `.replace(tzinfo=None)`。
  6. 回傳前，把 `events` 修剪到 `>= start`；`seen_ids` 在日期改變時清空。
- `fetch()`：
  1. 用模組層級的 `scanner = TokenScanner(claude_dir() / "projects")`，配合 `datetime.now()` 和 `int(time.time() * 1000)`。
  2. bars 來自 `fetch_usage`。status 不是 `ok` 時，bars 仍給齊三條，pct 為 None。
  3. `history` 只收 `cl_5h` / `cl_wk` 中 pct 不是 None 的值，key 對應為 `claude_5h` / `claude_wk`。
  4. `extra = {"token_events": events, "today_tokens": today_total(events, now)}`。波形圖只用最近 5 小時的事件，`bucket_tokens` 會自己忽略較早的事件。

- [ ] **Step 4: 執行，確認通過**

Run: `python -m unittest tests.test_claude_tokens -v`
Expected: 全部 PASS

- [ ] **Step 5: 用真實資料煙霧測試**

Run: `python -c "from providers import claude; s=claude.fetch(); print(s['status'], s['extra']['today_tokens'], len(s['extra']['token_events']))"`
Expected: 印出 `login_required <正整數> <正整數>`（目前本機沒有 CLI 登入），且沒有 traceback。

- [ ] **Step 6: Commit**

```bash
git add providers/claude.py tests/test_claude_tokens.py
git commit -m "feat(claude): scan local Claude Code transcripts for token usage"
```

---

### Task 5: `display_logic.py` 純函式

**Files:**
- Create: `display_logic.py`
- Test: `tests/test_display_logic.py`

**Interfaces:**
- Produces:
  - `format_countdown(pct: float | None, reset: str, now_utc: datetime) -> str`（`now_utc` 帶時區）
  - `format_tokens(n: int) -> str`
  - `bucket_drops(history: list[dict], key: str, now: datetime, num_rows: int, total_minutes: float) -> list[float]`
  - `bucket_tokens(events: list[tuple[datetime, int]], now: datetime, num_rows: int, total_minutes: float, unit: float) -> list[float]`（回傳的單位是「格」）
  - `TOKENS_PER_BLOCK: int = 20000`（Task 9 校準後更新）

- [ ] **Step 1: 寫失敗的測試**

```python
UTC_NOW = datetime(2026, 10, 16, 5, 0, 0, tzinfo=timezone.utc)

def test_countdown_formats_equivalent(self):
    # 防 Claude 倒數永遠顯示 "--"：三種 ISO 寫法結果相同
    for r in ["2026-10-16T07:30:00Z", "2026-10-16T07:30:00.123456+00:00", "2026-10-16T15:30:00+08:00"]:
        self.assertEqual(format_countdown(50, r, UTC_NOW), "2h 30m")

def test_countdown_edges(self):
    self.assertEqual(format_countdown(99.95, "2026-10-16T07:30:00Z", UTC_NOW), "Full")
    self.assertEqual(format_countdown(50, "2026-10-18T07:30:00Z", UTC_NOW), "2d 2h")
    self.assertEqual(format_countdown(50, "2026-10-16T04:00:00Z", UTC_NOW), "Reset...")
    self.assertEqual(format_countdown(50, "", UTC_NOW), "--")
    self.assertEqual(format_countdown(50, "garbage", UTC_NOW), "--")
    self.assertEqual(format_countdown(None, "2026-10-16T07:30:00Z", UTC_NOW), "--")

def test_format_tokens(self):
    self.assertEqual([format_tokens(n) for n in (950, 12345, 1234567)],
                     ["950 tok", "12.3k tok", "1.2M tok"])

def test_bucket_drops_matches_legacy_algorithm(self):
    # 防重構改變 AGY 波形：與舊版 VerticalHistoryChart.render 的分桶邏輯逐格一致
    # 以 Task 1 的 FIXTURE_HISTORY 為輸入（複製到 tests/fixtures/history_fixture.json），
    # 預期值由舊版 render 內嵌的分桶程式碼（widget.py@4fbc37c 第 443–466 行）
    # 對同一輸入跑出，存成 tests/fixtures/legacy_buckets.json
    self.assertEqual(bucket_drops(H, "gemini_5h", NOW, 100, 300.0), legacy["gem"])
    self.assertEqual(bucket_drops(H, "external_5h", NOW, 100, 300.0), legacy["ext"])

def test_bucket_drops_reset_is_not_negative(self):
    # Review Focus 4：5H 重置讓剩餘 % 回升時，不能出現負值
    h = [rec(NOW - 9min, claude_5h=10), rec(NOW - 6min, claude_5h=100), rec(NOW - 1min, claude_5h=98)]
    out = bucket_drops(h, "claude_5h", NOW, 100, 300.0)
    self.assertTrue(all(v >= 0 for v in out))
    self.assertAlmostEqual(sum(out), 2.0)

def test_bucket_drops_missing_key_is_zero(self):
    # 防舊歷史紀錄（沒有 claude_*）產生假尖峰或 KeyError
    h = [rec(NOW - 9min, gemini_5h=90), rec(NOW - 3min, gemini_5h=80)]
    self.assertEqual(sum(bucket_drops(h, "claude_5h", NOW, 100, 300.0)), 0)

def test_bucket_tokens_alignment(self):
    # Review Focus 3：事件落在正確的格子（第 0 格為最新）
    ev = [(NOW - timedelta(minutes=1), 40000), (NOW - timedelta(minutes=7), 10000)]
    out = bucket_tokens(ev, NOW, 100, 300.0, 20000)
    self.assertEqual(out[0], 2.0)
    self.assertEqual(out[2], 0.5)
```

- [ ] **Step 2: 產生 `legacy_buckets.json`**

寫一段一次性的指令，把 `git show 4fbc37c:widget.py` 第 443–466 行的分桶迴圈原樣貼進函式，餵入 `history_fixture.json` 和固定的 `NOW`，把 `[(g, e), ...]` 拆成 `{"gem": [...], "ext": [...]}` 存檔。

- [ ] **Step 3: 執行，確認失敗**

Run: `python -m unittest tests.test_display_logic -v`
Expected: FAIL（ImportError）

- [ ] **Step 4: 實作**

- `format_countdown`：把結尾的 `Z` 換成 `+00:00` 後交給 `datetime.fromisoformat`；結果沒有 tzinfo 時視為 UTC。沿用舊版的 `delta.days` / `delta.seconds` 分支。
- `bucket_drops`：演算法與舊版逐行等價；只取有該 key 的紀錄，不足 2 筆時回傳全 0。
- `bucket_tokens`：第 i 格範圍是 `(now - (i+1)·d, now - i·d]`，值為 token 總和除以 unit。

- [ ] **Step 5: 執行，確認通過**

Run: `python -m unittest tests.test_display_logic -v`
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add display_logic.py tests/test_display_logic.py tests/fixtures/
git commit -m "feat: extract pure display logic (countdown, token format, bucketing)"
```

---

### Task 6: `history_logger` 一般化與 `data_fetcher.fetch_all`

**Files:**
- Modify: `history_logger.py:7-39`、`data_fetcher.py`
- Test: `tests/test_history_and_fetch.py`

**Interfaces:**
- Consumes: `providers.agy.fetch`、`providers.claude.fetch`。
- Produces: `history_logger.log_usage(**fields: float) -> None`；`data_fetcher.fetch_all() -> {"agy": Snapshot, "claude": Snapshot, "last_updated": "HH:MM:SS"}`。

- [ ] **Step 1: 寫失敗的測試**

```python
def test_log_usage_partial_fields(self):
    # AGY 離線時只寫 Claude 欄位，不寫 0
    with patch.object(history_logger, "HISTORY_FILE", tmp):
        history_logger.log_usage(claude_5h=64.0, claude_wk=88.0)
        rec = json.load(open(tmp))[-1]
    self.assertEqual(set(rec), {"timestamp", "claude_5h", "claude_wk"})

def test_log_usage_empty_fields_skipped(self):
    # 兩邊都失敗時不寫空紀錄，避免佔用冷卻
    history_logger.log_usage()
    self.assertFalse(os.path.exists(tmp))

def test_old_records_still_readable(self):
    # 防升級後舊歷史消失
    json.dump([{"timestamp": recent_iso, "gemini_5h": 90, "external_5h": 80}], open(tmp, "w"))
    self.assertEqual(len(history_logger.get_history(minutes=60)), 1)

def test_fetch_all_isolates_failures(self):
    # 單一來源拋例外不能拖垮另一來源
    with patch("providers.agy.fetch", side_effect=RuntimeError("boom")), \
         patch("providers.claude.fetch", return_value=OK_CLAUDE), \
         patch("history_logger.log_usage") as log:
        out = data_fetcher.fetch_all()
    self.assertEqual(out["agy"]["status"], "error")
    self.assertEqual(out["claude"]["status"], "ok")
    log.assert_called_once_with(**OK_CLAUDE["history"])
```

- [ ] **Step 2: 執行，確認失敗**

Run: `python -m unittest tests.test_history_and_fetch -v`
Expected: FAIL

- [ ] **Step 3: 實作**

- `log_usage(**fields)`：`fields` 為空時直接 return；否則照舊套用冷卻和 24 小時修剪，紀錄內容為 `{"timestamp": ..., **fields}`。
- `fetch_all`：用 `ThreadPoolExecutor(max_workers=2)` 平行呼叫兩個 provider。任一個 future 拋出例外時，印出 log，並以 `{"status": "error", "bars": {}, "history": {}, "extra": {}}` 代替。合併兩邊的 `history` 後呼叫 `log_usage(**merged)`。
- 暫時保留的 `fetch_usage_data()` 改為呼叫 `log_usage(gemini_5h=..., external_5h=..., argon_wk=...)`；Argon 不可用時不傳 `argon_wk`。

- [ ] **Step 4: 執行全部測試**

Run: `python -m unittest discover -s tests -v`
Expected: 全部 PASS（含 Task 2 的 legacy adapter 測試）

- [ ] **Step 5: Commit**

```bash
git add history_logger.py data_fetcher.py tests/test_history_and_fetch.py
git commit -m "feat: run providers in parallel and log partial history records"
```

---

### Task 7: widget 主題化與 ProviderPanel（AGY 像素不變）

**Files:**
- Modify: `widget.py`
- Modify: `data_fetcher.py`（刪除 `fetch_usage_data`）、`tests/test_agy_provider.py`（刪除 `test_legacy_adapter_unchanged`）

**Interfaces:**
- Consumes: `display_logic.*`、`data_fetcher.fetch_all`。
- Produces:
  - `THEMES: dict[str, dict]`，每個值含 `safe, warn, danger, safe_base, warn_base, danger_base, safe_tip, warn_tip, danger_tip, title_warn`。key：`gem, ext, arg, cl_5h, cl_wk, cl_model`。`title_warn` 是 warn 區間時標題的顏色；舊版 warn 區間的標題用 safe 色，所以 `title_warn` 設為 safe 色。
  - `VerticalFuelGauge(parent, width, height, title, theme_id, text_side)`、`HorizontalFuelGauge(parent, width, height, title, theme_id)`。兩者都提供 `set_value(pct: float | None, reset: str, now_utc: datetime)`、`set_title(text: str)`、`set_stale(stale: bool)`、`set_login()`。
  - `VerticalHistoryChart(parent, width, height, left_colors: tuple[str,str,str], right_colors: tuple[str,str,str], left_text_color: str, right_text_color: str, left_hover: Callable[[float], str], right_hover: Callable[[float], str], left_rate: Callable[[float], str], right_rate: Callable[[float], str])`，提供 `render(left: list[float] | None, right: list[float] | None)`，數值單位是「格」。`left_rate` / `right_rate` 接收每小時的格數，回傳側邊顯示文字。`*_text_color` 是燃燒率和懸停文字的顏色：AGY 為 `#22C55E` / `#F97316`，Claude 為 `#A78BFA` / `#818CF8`。
  - `class AgyPanel(tk.Frame)`、`class ClaudePanel(tk.Frame)`，寬 110、高 574（598 減掉標題列 24），兩者都提供 `apply(snapshot: Snapshot, history: list[dict], now: datetime) -> None`。

- [ ] **Step 1: 建立 THEMES，量表改吃 theme_id**

`gem`、`ext`、`arg` 的所有色值從舊版 `set_value` 分支逐字搬過來，包含 tip 色 `#FEF08A`/`#86EFAC`（gem）、`#FDBA74`/`#FED7AA`（ext）、`#FEF08A`/`#A5F3FC`（arg），以及 base 色。Claude 三組：`safe` 用 Global Constraints 的色值；`safe_base = deepen_rgb(safe, 0.35)`、`safe_tip = highlight_rgb(safe, 1.0, 80)`；warn / danger 的 base 和 tip 共用 gem 的值。算出來後寫成常數字面值。倒數改用 `display_logic.format_countdown`。

- [ ] **Step 2: 波形圖參數化**

`_init_gradients` 改用傳入的兩組三色漸層，演算法不變。`render` 內的分桶迴圈移除，改為接收已分桶好的數列。燃燒率顏色的判斷改為：`sum(blocks) / 5 > 20` 時用 `#EF4444`，否則用 `*_text_color`；這等同舊版的判斷。懸停標籤也用 `*_text_color`。側邊文字與懸停文字分別由 `*_rate` 和 `*_hover` 產生；AGY 的格式是 `f"{round(x,1)} %/h"` 和 `f"{v:.1f}%"`，都沿用舊版。`left` 與 `right` 都是 None 時顯示 `Waiting...`；只有一側是 None 時，該側當成全 0。

- [ ] **Step 3: AgyPanel**

把現有 `build_ui` 中，標題列以下的部分（5H/WK 切換、兩組直立條框架、ARG 橫條、波形圖）原樣搬進 `AgyPanel`，座標與 pack 參數都不變。`apply` 的流程：
1. `status != "ok"` 時呼叫各量表的 `set_stale(True)`，數值保留不動；從未成功過時顯示 `--%`。
2. 正常時把 bars 餵給量表。
3. 呼叫 `chart.render(bucket_drops(history, "gemini_5h", now, 100, 300.0), bucket_drops(history, "external_5h", ...))`。
4. history 不足 2 筆時傳入 None，讓圖表顯示 `Waiting...`，與舊版行為一致。

- [ ] **Step 4: ClaudePanel**

佈局與 AgyPanel 對齊：
- 頂列的 Label 位置與高度對應 AgyPanel 的切換列，顯示 `format_tokens(today_tokens)`，字型 `(DIGITAL_FONT, 7)`、顏色 `TEXT_MUTED`。
- 一個 150 高的框架放兩條直立條：5H 在 `x=9`（text_side `left`）、WK 在 `x=57`（text_side `right`）。
- 下方依序是模型橫條和波形圖，右側數列為 `bucket_tokens(events, now, 100, 300.0, TOKENS_PER_BLOCK)`。
- 波形懸停文字格式為 `f"{v*TOKENS_PER_BLOCK/1000:.0f}k"`，燃燒率為 `f"{x*TOKENS_PER_BLOCK/1000:.0f} k/h"`。
- `login_required` 時三條 bar 都呼叫 `set_login()`：灰色，百分比位置顯示 `LOGIN`，倒數位置顯示 `/login`。
- `offline` / `error` 時的處理與 AgyPanel 相同：呼叫 `set_stale(True)`、保留舊值；從未成功過時顯示 `--%`。不論哪種狀態，token 頂列和波形右半都照常更新。

- [ ] **Step 5: UsageWidget 改接 fetch_all**

`fetch_and_update` 改呼叫 `data_fetcher.fetch_all()`；`update_ui_with_data` 傳入 `now = datetime.now()` 和 `history_logger.get_history(minutes=360)`，再呼叫 `agy_panel.apply(...)`。這個 Task 只顯示 AGY 模式，ClaudePanel 建立但不 place，模式切換留給 Task 8。掃描線動畫改掛在目前顯示中的 panel 的 chart 上。刪除 `data_fetcher.fetch_usage_data` 和對應的測試。

- [ ] **Step 6: 跑全部測試**

Run: `python -m unittest discover -s tests -v`
Expected: 全部 PASS

- [ ] **Step 7: AGY 像素比對（整張）**

Run: `python scratch/pixel_check.py capture scratch/after_t7.png`，接著 `python scratch/pixel_check.py compare scratch/baseline_agy.png scratch/after_t7.png`
Expected: `diff_pixels=0`。不是 0 時，用 diff 的 bbox 找出差異位置並修正，不得放寬門檻。

- [ ] **Step 8: Claude 面板目視檢查**

暫時在 pixel_check 加上 `capture-claude` 模式：patch `providers.claude.fetch`，回傳一份 `ok` 快照（5H 65%、WK 86%、OPS 90%，加上 30 筆 token 事件），把 ClaudePanel place 到 AgyPanel 的位置後截圖成 `scratch/claude_panel.png`。用 Read 打開圖片確認：紫、洋紅、靛三色正確，排版與 AGY 欄對齊，沒有文字被截斷。

- [ ] **Step 9: Commit**

```bash
git add widget.py data_fetcher.py tests/
git commit -m "refactor(ui): theme table and provider panels; add Claude panel"
```

---

### Task 8: 三種模式、ModeTab、視窗外框與持久化

**Files:**
- Modify: `display_logic.py`、`widget.py`、`.gitignore`
- Test: `tests/test_modes.py`

**Interfaces:**
- Produces:
  - `MODES = ("agy", "claude", "both")`
  - `mode_width(mode: str) -> int`
  - `anchored_x(old_x: int, old_w: int, new_w: int, screen_left: int = 0) -> int`
  - `load_mode(path: Path) -> str`、`save_mode(path: Path, mode: str) -> None`
  - `STATE_FILE = Path(__file__).with_name("widget_state.json")`
  - widget 端：`UsageWidget.set_mode(mode: str)`、`build_window_region(width: int) -> int`（HRGN）、`class ModeTab(tk.Canvas)`

- [ ] **Step 1: 寫失敗的測試**

```python
def test_widths(self):
    self.assertEqual([mode_width(m) for m in MODES], [134, 134, 244])

def test_anchored_x_keeps_right_edge(self):
    self.assertEqual(anchored_x(1000, 134, 244), 890)
    self.assertEqual(anchored_x(890, 244, 134), 1000)

def test_anchored_x_clamps_left_edge(self):
    # Review Focus 5：貼左緣時加寬不能跑出螢幕
    self.assertEqual(anchored_x(50, 134, 244), 0)
    self.assertEqual(anchored_x(-1900, 134, 244, screen_left=-1920), -1920)

def test_mode_roundtrip_and_corruption(self):
    save_mode(p, "both"); self.assertEqual(load_mode(p), "both")
    p.write_text("{oops"); self.assertEqual(load_mode(p), "agy")
    p.write_text('{"mode": "nope"}'); self.assertEqual(load_mode(p), "agy")
    self.assertEqual(load_mode(p.with_name("missing.json")), "agy")
```

- [ ] **Step 2: 執行，確認失敗，再實作 display_logic 的部分，執行到通過**

Run: `python -m unittest tests.test_modes -v`
Expected: 先 FAIL，實作後 PASS

- [ ] **Step 3: widget 實作**

- `build_window_region(width)`：主體為 `CreateRoundRectRgn(24, 0, width, 598, 12, 12)`；分頁標籤、top patch、inner patch 的座標與舊版相同。舊版在 `__init__` 內的那段程式改為呼叫這個函式。
- `ModeTab`（24×140）：三段由上而下為 `AGY`（y 0–46）、`CLD`（46–93）、`BOTH`（93–140），文字直書（angle 270）、字型 `("Segoe UI", 7, "bold")`；選中段的顏色依 Global Constraints，未選中為 `TEXT_MUTED`。分段之間畫 1px `#1E293B` 分隔線。在分段上「按下並放開、且沒有拖曳超過 3px」才算點擊切換；拖曳一律交給 `start_move` / `do_move`。
- `set_mode(mode)` 的流程：
  1. 依模式 place / place_forget 兩個 panel：AGY 在 `x=24`；CLD 在 `x=24`；BOTH 時 AGY 在 `x=24`、CLD 在 `x=134`。
  2. 標題列寬度設為 `mode_width(mode) - 24`。
  3. 寬度變化時，用 `anchored_x` 加上 `winfo_vrootx` 計算新的 x，再 `geometry(f"{w}x598+{x}+{y}")`。
  4. 重設 `SetWindowRgn`，更新 ModeTab 和系統匣勾選狀態，最後 `save_mode`。
- 系統匣：在 `Show Widget` 之後加入三個 `pystray.MenuItem`，用 `checked=lambda item: self.mode == m` 和 `radio=True`。點選時以 `root.after(0, ...)` 回到 Tk 執行緒再呼叫 `set_mode`。
- 啟動時：`self.mode = load_mode(STATE_FILE)`，並在 `build_ui` 後呼叫 `set_mode`。`show_window` 的定位改用 `mode_width(self.mode)`。
- `.gitignore`：在 `usage_history.json` 下一行加入 `widget_state.json`。

- [ ] **Step 4: 跑全部測試**

Run: `python -m unittest discover -s tests -v`
Expected: 全部 PASS

- [ ] **Step 5: AGY 主體像素比對（排除分頁標籤）**

Run: `python scratch/pixel_check.py capture scratch/after_t8.png`，接著 `python scratch/pixel_check.py compare scratch/baseline_agy.png scratch/after_t8.png --x0 24`
Expected: `diff_pixels=0`

- [ ] **Step 6: 三種模式目視檢查**

在 pixel_check 加上 `capture-mode <mode>`，分別截 `agy`、`claude`、`both` 三張圖並用 Read 檢視。確認項目：
- BOTH 寬 244，兩欄頂端對齊。
- L 型外框沒有缺角或黑邊。
- 分頁標籤的選中色正確。
- 標題列的 ↻ ✕ 在最右側。

- [ ] **Step 7: Commit**

```bash
git add display_logic.py widget.py .gitignore tests/test_modes.py
git commit -m "feat(ui): AGY / CLD / BOTH display modes with persistent mode tab"
```

---

### Task 9: token 刻度校準與 README

**Files:**
- Modify: `display_logic.py`（`TOKENS_PER_BLOCK`）、`README.md`
- Create: `scratch/calibrate_tokens.py`（不 commit）

- [ ] **Step 1: 校準**

`scratch/calibrate_tokens.py` 的作法：
1. 用 `TokenScanner` 的同一套解析和去重規則，讀取 `claude_dir()/projects` 的**全部**檔案，不套用 5 小時過濾（可在 scanner 加一個 `max_age` 參數，或在腳本內重用它的行解析函式）。
2. 以 3 分鐘為一格，計算所有「非零」格的 token 數，算出 P50 和 P99。
3. 候選刻度為 `[5000, 10000, 20000, 25000, 50000, 100000]`，選出最接近 `P99 / 10` 的值。
4. 印出 `P50`、`P99`、選定的 unit，以及 `P50 / unit`（格數）。

- [ ] **Step 2: 套用並記錄**

把 `TOKENS_PER_BLOCK` 改成選定值。如果 `P50 / unit` 不在 3–5 之間，在 commit 訊息和最後的回報中明確寫出實際格數，交給使用者決定是否調整，不自行改變規則。

- [ ] **Step 3: README**

在中英文段落各加入：
- Claude 監控說明：額度 % 加上 token 波形。
- 三種模式與切換方式。
- 前置條件：以 `claude` CLI 執行 `/login`，桌面版 App 的登入不算。
- 額度端點為非官方、可能變動。
- Tech Specs 新增一項 Provider 架構說明。

既有段落的文字不改。

- [ ] **Step 4: 全部測試與 commit**

Run: `python -m unittest discover -s tests -v`
Expected: 全部 PASS

```bash
git add display_logic.py README.md
git commit -m "docs: document Claude monitoring; calibrate token scale (P50=<x>, P99=<y>)"
```

---

### Task 10: 實機驗證（需要使用者）

- [ ] **Step 1:** 請使用者在終端機執行 `claude` 並以 `/login` 登入。
- [ ] **Step 2:** 用 `scratch/start_widget.bat` 啟動，切到 BOTH 模式。Claude 欄的 5H / WK 剩餘 % 與 claude.ai 用量頁的換算值，差距必須在 ±1% 內。
- [ ] **Step 3:** 關閉 Antigravity 後按 ↻：AGY 欄保留舊值並變灰，Claude 欄正常更新。
- [ ] **Step 4:** 把 `.credentials.json` 暫時改名後按 ↻：Claude 欄顯示 `LOGIN`，token 數仍會更新；測完改回原名。
- [ ] **Step 5:** 依序切換三種模式並重新啟動，確認模式被記住、視窗沒有跑出螢幕。
- [ ] **Step 6:** 清理根目錄：確認沒有遺留的臨時檔，`git status` 只剩預期的變更。
