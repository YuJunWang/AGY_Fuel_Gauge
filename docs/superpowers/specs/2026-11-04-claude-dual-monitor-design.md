# AGY Fuel Gauge — Claude 雙來源監控設計

- 日期：2026-11-04
- 狀態：待審閱
- 架構方案：方案 1（輕量 Provider 拆分）

## 1. 目標與成功標準

### 目標
將 AGY Fuel Gauge 擴充為同時監控 Antigravity（AGY）與 Claude 的模型用量，在單一小工具中提供三種顯示模式：`AGY`、`CLD`、`BOTH`，並維持既有的視覺風格（純黑底、JetBrains Mono、0.82 透明度、圓角 L 型視窗、霓虹油量條與波形圖）。

### 使用者已確認的需求
| 項目 | 決定 |
|---|---|
| Claude 資料 | 訂閱額度 %（主油量條）＋ 本機 token 統計（歷史波形圖輔助） |
| BOTH 模式版面 | 左右並排加寬，各欄保留各自的油量條與波形圖 |
| 模式切換 | 左側凸出分頁標籤改為三段式；系統匣選單亦可切換；記住上次選擇 |
| 架構 | 方案 1：拆出 `providers/agy.py`、`providers/claude.py`，widget 以 `ProviderPanel` 組裝 |

### 成功標準（全部成立才算完成）
1. **AGY 畫面像素不變**：以固定假資料在重構前 commit（`4fbc37c`）與重構後各截圖一次，AGY 模式的主體區域（x = 24–134，不含刻意改版的左側分頁標籤）像素差異為 0。（2026-11-04 合併上游 `e1191a4` DPI 縮放後，基準改為 `e1191a4`，並在模擬 150% 縮放下同樣比對，主體區域為 x ≥ S(24)。）
2. **單元測試全數通過**（第 7 節列出的每一項）。
3. **實機對照**：使用者以 Claude Code CLI 登入後，widget 顯示的 5H / WK 剩餘 % 與 claude.ai「設定 → 用量」頁一致（剩餘 % = 100 − 頁面顯示的已用 %，允許 ±1% 的更新時差）。
4. 未登入、AGY 未啟動、網路中斷三種情境下，widget 不崩潰，且另一來源照常更新。

### 非目標（YAGNI）
- 不做外掛系統或設定檔；只支援 AGY 與 Claude 兩個來源。
- widget 不自行刷新或改寫 Claude 的 OAuth token（過期時改請 Claude Code CLI 自己換新，見 2.2）。
- 不統計 claude.ai 網頁版聊天的 token（本機無資料）。
- 不支援 Windows 以外的平台（既有限制不變）。

## 2. 資料來源

### 2.1 AGY（既有，邏輯不改）
`language_server.exe` 的 gRPC-Web `RetrieveUserQuotaSummary`，產出 GEM / EXT / ARG 的 5H、WK 剩餘 % 與重置時間。程式碼原樣搬入 `providers/agy.py`。

### 2.2 Claude 訂閱額度（新）
- **端點**：`GET https://api.anthropic.com/api/oauth/usage`
- **標頭**：`Authorization: Bearer <accessToken>`、`anthropic-beta: oauth-2025-04-20`
- **Token 來源**：`~/.claude/.credentials.json` 的 `claudeAiOauth.accessToken`（使用者需先以 `claude` CLI 執行 `/login`）。**唯讀**，不寫回。
- **過期換新（2026-11-04 追加）**：access token 約 8 小時過期。當 token 已過期、或端點回 401/403，且憑證含 `refreshToken` 時，widget 以背景程序（`CREATE_NO_WINDOW`、逾時 20 秒）執行 `claude auth status`，由 CLI 自行換新並寫回憑證，widget 再重新讀取；401 時若換到不同 token 則重試一次。每 10 分鐘最多嘗試一次。從未登入（無 `refreshToken`）時不呼叫 CLI。`claude setup-token` 的長期 token 只有 `user:inference` scope、缺 `user:profile`，無法查用量，故不採用。待驗證：`auth status` 是否確實觸發換新（於首次自然過期時觀察）；若否，改為執行一次極小的 CLI 請求。
- **回應**（社群觀察，無官方文件）：各額度桶為 `{utilization: <已用 %>, resets_at: <ISO 8601>}` 或 `null`。
  - `five_hour` → Claude 5H 油量條
  - `seven_day` → Claude WK 油量條
  - `seven_day_opus` → 橫條（標籤 `OPS`）；若為 `null` 則改用 `seven_day_sonnet`（標籤 `SON`）；兩者皆 `null` 時橫條顯示灰色 `--`
- **換算**：剩餘 % = `100 − utilization`，夾在 [0, 100]。
- **逾時**：5 秒。
- **容錯**：任何欄位缺失或型別不符都視為該桶 `null`，不拋例外。

### 2.3 Claude token 統計（新）
- **來源**：`~/.claude/projects/**/*.jsonl` 中 `type == "assistant"` 的紀錄，讀取 `timestamp` 與 `message.usage`。
- **計入**：`input_tokens + output_tokens + cache_creation_input_tokens`。
- **不計入**：`cache_read_input_tokens`。理由：cache 讀取量常為其他三項總和的十倍以上，計入會讓波形持續觸頂，無法反映實際工作強度。
- **去重**：以 `message.id`（若缺則用 `requestId`）去重，避免串流造成的重複紀錄。
- **效能**：只處理最近 5 小時內有修改的檔案；以 `{路徑: 已讀位元組數}` 快取於記憶體，每次只讀新增內容。檔案變小（被截斷或重寫）時從頭重讀。
- **今日 token 數**：當地時間今日 00:00 起的總和，顯示於 Claude 欄頂列。
- 解析失敗的行直接跳過。

## 3. 架構

### 3.1 檔案結構
```
data_fetcher.py        調度者：平行呼叫兩個 provider → history_logger 記錄 → 回傳給 widget
providers/__init__.py
providers/agy.py       QuotaFetcher（原樣搬入）＋ 轉成快照格式
providers/claude.py    訂閱額度查詢 ＋ JSONL token 統計
history_logger.py      新增 claude_5h / claude_wk 選填欄位
widget.py              THEMES、format_countdown、ProviderPanel、ModeTab、視窗外框函式
widget_state.json      記住模式（加入 .gitignore）
tests/                 unittest 單元測試與 fixture
```

### 3.2 Provider 快照格式
每個 provider 對外只提供一個函式 `fetch() -> dict`，回傳：
```python
{
  "status": "ok" | "offline" | "login_required" | "error",
  "bars": {
    "<bar_id>": {"label": str, "pct": float | None, "reset": str},
    ...
  },
  "history": {"<history_key>": float, ...},  # 寫入 usage_history.json 的值
  "extra": {...},                            # provider 專屬資料（例：Claude 的 token 桶）
}
```
- AGY：`bars` 為 `gem_5h, ext_5h, gem_wk, ext_wk, arg_wk`；`history` 為 `gemini_5h, external_5h, argon_wk`（與現行欄位名相同）。
- Claude：`bars` 為 `cl_5h, cl_wk, cl_model`；`history` 為 `claude_5h, claude_wk`；`extra` 含 `today_tokens` 與每筆 `(timestamp, tokens)`。
- `status != "ok"` 時 `bars` 與 `history` 可為空。

### 3.3 data_fetcher（調度）
- `fetch_all()` 以 `ThreadPoolExecutor(max_workers=2)` 平行呼叫兩個 provider，各自在 provider 內處理逾時與例外。
- 合併兩邊的 `history` 為一筆紀錄交給 `history_logger.log_usage(**fields)`；某來源失敗則該來源欄位不寫入。
- 不論目前顯示模式，每輪都抓兩邊，確保歷史連續。
- 回傳 `{"agy": snapshot, "claude": snapshot, "last_updated": "HH:MM:SS"}`。

### 3.4 history_logger
- `log_usage(**fields)` 接受任意欄位子集，維持 2.5 分鐘共用冷卻與 24 小時修剪。
- 舊紀錄（無 `claude_*` 欄位）可正常讀取。
- 波形圖將缺值視為「無消耗」，不畫假尖峰。
- token 不寫入歷史檔（JSONL 已有時間戳，畫圖時即時計算）。

## 4. 畫面設計

### 4.1 主題表 THEMES
取代 `is_gemini` 分支，每條油量條依 `theme_id` 查表取得 `safe / base / tip` 色。警示色（≤50% 黃、≤20% 紅）全域共用。

| theme_id | safe | 用途 |
|---|---|---|
| `gem` | `#22C55E`（既有） | AGY GEM |
| `ext` | `#F97316`（既有） | AGY EXT |
| `arg` | `#06B6D4`（既有） | AGY ARG 橫條 |
| `cl_5h` | `#A78BFA` 紫 | Claude 5H |
| `cl_wk` | `#E879F9` 洋紅 | Claude WK |
| `cl_model` | `#818CF8` 靛 | Claude 單一模型橫條 |

既有三組的色值、base、tip 必須與現行程式碼完全相同（像素不變的前提）。Claude 三組的 base / tip 依既有規則推導（base ≈ 深色、tip ≈ 淺色），實作時以 `deepen_rgb` / `highlight_rgb` 計算並固定為常數。

### 4.2 共用倒數函式
`format_countdown(pct, reset) -> str` 取代兩個 class 中重複的倒數邏輯：
- `pct >= 99.9` → `Full`
- 可解析 `YYYY-MM-DDTHH:MM:SSZ`、帶小數秒、帶 `+00:00` 等時區偏移的 ISO 8601
- 未來 → `Xd Xh` 或 `Xh Xm`；已過 → `Reset...`；無法解析 → `--`

### 4.3 ProviderPanel（110 × 598 的一欄，96 DPI 基準；實際尺寸乘上 `S()` DPI 縮放）
由上而下：

| 區塊 | AGY 欄（現狀不變） | Claude 欄 |
|---|---|---|
| 頂列 | 5H ◯── WK 滑動切換 | 今日 token 數（例：`1.2M tok`），高度與切換列一致 |
| 直立油量條 ×2 | GEM / EXT（依 5H/WK 切換） | 5H / WK（同時顯示，不需切換） |
| 橫條 | ARG | OPS 或 SON |
| 波形圖 | 左 GEM、右 EXT | 左 Claude 5H 消耗、右 token 速率 |

狀態呈現：
- `login_required`：Claude 油量條灰色，百分比位置顯示 `LOGIN`，倒數位置顯示 `/login`。
- `offline` / `error`：保留上一次成功的數值，標題改為 `TEXT_MUTED` 灰色，表示資料過期。首次即失敗時顯示 `--%`。

### 4.4 波形圖一般化
`VerticalHistoryChart` 改為接收左右兩條數列設定：`(每格數值列表, 三色漸層, 每格刻度, 燃燒率單位)`。繪圖演算法（連續多邊形、≥10 格霓虹高光、懸停讀值、側邊燃燒率、5 小時範圍）不變。

| | 左側 | 右側 |
|---|---|---|
| AGY | GEM 5H 每格消耗 %，`#10B981→#EAB308→#EF4444`，1%/格 | EXT 5H 每格消耗 %，`#F97316→#7C3AED→#2563EB`，1%/格 |
| Claude | Claude 5H 每格消耗 %，`#A78BFA→#E879F9→#EF4444`，1%/格 | 每格 token 量，`#818CF8→#3B82F6→#22D3EE`，20k/格（已校準，見下） |

- Claude 燃燒率標示：左 `x %/h`，右 `y k/h`。
- token 刻度 20k/格為暫定值；實作後以使用者真實 JSONL 計算每格 token 分佈，調整至 P50 約落在 3–5 格、P99 接近 10 格，並將最終值與計算依據記入 commit 訊息。
- **校準結果**（`scratch/calibrate_tokens.py`，使用者真實 JSONL）：118 個非零 3 分鐘格，每格 token P50 = 34017、P99 = 212459。取 `TOKENS_PER_BLOCK = 20000`（最接近 P99/10 ≈ 21246）。此時 P50 = 1.70 格，低於 3–5 格目標；因 P99/P50 ≈ 6.2，「P50 3–5 格」與「P99 ≈ 10 格」無法同時滿足，接受此結果並以 P99 為準（避免常態觸頂）。
- **燃燒率紅色警示**：左側（5H %/h）沿用 AGY 規則，>20 格/h 轉紅 `#EF4444`；右側 token 側**不轉紅**，因為它量測的是吞吐量而非額度，沒有「快用完」的意義。AGY 兩側行為不變。

### 4.5 模式切換
- **ModeTab**：左側 24 × 140 分頁標籤切為三段，由上而下 `AGY` / `CLD` / `BOTH`，直書。選中段亮色（AGY `#22C55E`、CLD `#D97757`、BOTH `#F8FAFC`），未選中為 `TEXT_MUTED`。分段間保留可拖曳移動視窗的區域。
- **系統匣選單**：新增三個單選項目，與 ModeTab 同步。
- **持久化**：模式寫入 `widget_state.json`（`{"mode": "agy" | "claude" | "both"}`），啟動時讀取；檔案不存在或損毀時預設 `agy`。
- **視窗寬度**：單一模式 134px（24 標籤 + 110），BOTH 模式 244px（24 + 110 + 110），高度 598 不變。以上為 96 DPI 數值；實際為 `S(24) + n × S(110)`，高度 `S(273) + 圖表高`（上游 `e1191a4` 的 DPI 縮放）。
- **外框**：`build_window_region(width)` 依寬度重算 `SetWindowRgn` 的 L 型區域（含既有的 top patch 與 inner patch）。
- **錨點**：切換時固定視窗右邊緣，寬度變化往左延伸。
- **標題列**：BOTH 模式下共用一條橫跨兩欄的標題列；更新時間在左，↻ ✕ 在右。

## 5. 錯誤處理

| 情境 | 行為 |
|---|---|
| AGY `language_server.exe` 未啟動 | AGY 快照 `offline`；AGY 欄保留舊值並變灰；Claude 照常 |
| `.credentials.json` 不存在或無 `claudeAiOauth` | Claude `login_required` |
| token 已過期，或端點回 401 / 403 | 憑證含 `refreshToken` 時先請 CLI 換新（`claude auth status`，10 分鐘冷卻）並重讀；仍無有效 token 則 Claude `login_required` |
| 找不到 `claude` CLI 或 CLI 逾時 | 不崩潰；Claude `login_required` |
| 端點逾時、網路錯誤、5xx | Claude `offline` |
| 端點回應格式改變 | 缺少的桶視為 `null`；全部缺失則 `error` |
| JSONL 讀取失敗 | 略過該檔／該行，token 數僅反映可讀部分 |
| `widget_state.json` 損毀 | 預設 `agy` 模式並覆寫 |

所有錯誤以 `print("[模組名] ...")` 記錄（沿用既有風格），不彈出視窗。

## 6. 其他變更
- `.gitignore` 加入 `widget_state.json`。
- README 中英文段落：新增 Claude 監控說明、三種模式、前置條件（以 `claude` CLI 執行 `/login`）、並註明額度端點為非官方。
- 單一實例 mutex 名稱、系統匣圖示名稱維持不變。

## 7. 測試計畫

工具：Python 標準函式庫 `unittest`，不新增依賴。測試放在 `tests/`，fixture 放在 `tests/fixtures/`。

| 測試 | 防止的錯誤 |
|---|---|
| `format_countdown` 解析 `Z`、小數秒、`+00:00` 三種格式並得出相同結果 | Claude 倒數永遠顯示 `--` |
| `format_countdown` 對 `Full`、已過期、無法解析的輸入 | 邊界狀態顯示錯誤 |
| Claude 回應含 `null` 桶、缺欄位、`utilization` 超出 0–100 | 端點改版讓 widget 崩潰或顯示負值 |
| `seven_day_opus` 為 `null` 時橫條改用 `seven_day_sonnet` | 橫條選錯資料來源 |
| 無憑證檔／401 → `login_required` | 未登入被誤判為斷線，使用者不知道要登入 |
| JSONL 同一 `message.id` 出現多次只計一次 | token 量被串流重複灌水 |
| JSONL 統計排除 `cache_read_input_tokens` | 波形持續觸頂 |
| 舊格式歷史紀錄（無 `claude_*`）可讀取，且缺值視為無消耗 | 升級後歷史波形消失或出現假尖峰 |
| AGY provider 對既有 gRPC 回應 fixture 的解析結果，與 `4fbc37c` 版 `fetch_usage_data()` 的對應數值完全一致 | 搬家時改壞 AGY 解析邏輯 |
| 某一 provider 拋例外時 `fetch_all()` 仍回傳另一方的 `ok` 快照 | 單一來源故障拖垮整個 widget |

### 像素比對（成功標準 1）
`scratch/` 下的腳本以固定假資料啟動 widget 的 AGY 模式，以 `PIL.ImageGrab` 擷取視窗，分別在 `4fbc37c` 與新版執行，比對主體區域（x = 24–134）差異像素數必須為 0。腳本屬一次性驗證工具，不進版控。合併 `e1191a4` 後，基準圖改由 `e1191a4` 產生，並以環境變數 `PIXEL_DPI_SCALE=1.5` 模擬 150% 縮放再比對一次。

確保兩版輸出可重現的作法（舊版程式碼不修改，全靠腳本從外部注入）：
- monkeypatch `widget.datetime` 與 `history_logger` 的時間為固定值，`get_history` 回傳固定紀錄。
- monkeypatch 資料抓取函式回傳固定快照，並停用背景自動更新執行緒。
- 等掃描線動畫結束（約 2.5 秒）後再截圖，避開 `random` 造成的抖動。
- 視窗固定放在同一螢幕座標，並關閉 `-alpha` 透明度，避免桌面背景透出影響比對。

### 實機驗證（成功標準 3、4）
1. 使用者以 `claude` CLI `/login` 後，比對 widget 與 claude.ai 用量頁。
2. 關閉 Antigravity，確認 AGY 欄變灰、Claude 欄照常。
3. 暫時改名 `.credentials.json`，確認 Claude 欄顯示 `LOGIN`、AGY 欄照常。
4. 三種模式來回切換，確認寬度、外框、錨點與重啟後的模式記憶。

## 8. 已知風險
- **非官方端點**：`/api/oauth/usage` 隨時可能變動。影響範圍限於 `providers/claude.py`；失效時 Claude 額度條顯示 `error`，token 波形不受影響。
- **桌面版 Claude App 登入不會產生 CLI 憑證**：使用者必須另外以 CLI 登入一次。
- **token 刻度**：依個人使用習慣差異大，以實測資料定值，未來可能需要再調。
