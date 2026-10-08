# NAS 增量同步範圍變更

**日期：** 2026-10-07
**最後修訂：** 2026-10-08（精簡輸出、兩個 volume、移除心跳與 HEALTHCHECK、容器強化併入 PR 10）
**狀態：** 使用者已批准（對應 GitHub Issue #32）
**性質：** 範圍變更決策文件。本文件不包含程式實作。

## 1. 背景

目前流程需要人工執行工具、匯出文字檔，再手動提供給 ChatGPT。
Issue #32 希望讓 NAS 定期產生最新訓練資料，並讓 ChatGPT 在
一般對話或 Scheduled Task 中直接讀取，不再需要人工整理。

## 2. 使用者決策

1. 本版本只負責把資料自動產生並存放在 NAS 的本機資料夾。
2. 將 NAS 指定資料夾同步到 Google 雲端硬碟，由 NAS 上的第三方同步軟體
   處理（例如 Synology Cloud Sync、Drive 同步用戶端或 rclone 排程）。
3. 專案本身不呼叫 Google Drive API，不處理 Google OAuth，不新增
   Google 相關依賴。
4. 部署方式採用 Docker，容器常駐在 NAS 背景執行，並設定
   `restart: unless-stopped`。不使用「Task Scheduler 加 `docker run`」
   的模式。
5. 定時同步由容器內的程式自己負責。排程器只使用 Python 標準函式庫，
   排程時間以環境變數設定，不新增排程套件，也不另建設定檔。
6. 目標硬體為 Synology DS720+（x86_64），NAS 上已有其他服務運行，
   本專案容器必須與既有服務共存，不得佔用其連接埠。
7. 手機觸發與通知使用 Telegram 機器人（使用者為 Android 手機）。
   機器人以主動輪詢（long polling）接收指令，只對外連線，不開連接埠。
   不採用 LINE，因為 LINE 接收訊息需要對外 Webhook。
8. 不做心跳監控與 Docker HEALTHCHECK（2026-10-08）。Telegram 是唯一
   告警管道。代價：NAS 或容器整個停掉時不會有任何通知，使用者需自行
   察覺 `/status` 沒有回應，或雲端硬碟的資料沒有更新。
9. 本專案僅供使用者個人使用，不做多使用者、權限管理或公開服務。
10. 正常使用的同步頻率只有兩種（2026-10-07 架構確認）：
    - 自動排程：容器啟動後，每天排程同步一次。
    - 手動強制：使用者在手機上按下 Telegram 的同步按鈕，立即同步一次。
    `docker exec ... sync` 保留給部署測試與除錯使用。
11. AI 讀取 `current/` 時，依需要自行選擇精簡或詳細的檔案（見 3.5），
    以節省 token 或取得更多細節。

資料流程：

```text
Garmin Connect / TCX
  -> NAS 上常駐的 Docker 容器（定時增量抓取、去重、產生檔案）
  -> 掛載的 NAS 本機輸出資料夾 RunningData/current/
  -> NAS 上的第三方同步軟體（不屬於本專案）
  -> Google 雲端硬碟
  -> ChatGPT
```

## 3. 核准範圍（In scope）

以下項目為本次核准的 post-MVP 例外，僅限 NAS 本機執行：

- 一個 `sync` 核心流程，供三種入口共用同一套邏輯（分層見 3.6）：
  - 常駐模式：容器內排程器每天自動呼叫一次。
  - Telegram `/sync`：使用者在手機上手動強制同步。
  - `docker exec <container> garmin-tcx-ai sync`（執行一次後結束），
    供部署測試與除錯，不新增網路入口。
- 同一時間只允許一個同步在執行。手動觸發遇到進行中的同步時，應
  明確回報並結束，不得並行執行。
- 增量同步狀態：保存 `last_successful_sync`。
  - 起點為 `max(last_successful_sync - overlap_days, today - 30 days)`。
  - `overlap_days` 預設 2 天，最大回溯 30 天。
- 以 Garmin activity ID 或其他穩定唯一識別值去重。
- 在本機輸出資料夾固定路徑 `current/` 產生分層的輸出集合（見 3.5）。
  `runs/*.txt` 為 2026-10-07 使用者核准的範圍擴充。
- 同步失敗時不更新 `last_successful_sync`，也不把舊資料標示為最新成功。
- 以明確的 log 呈現狀態，資料是否過期由 Telegram 通知與
  `data_status.json` 呈現。`docker exec` 手動執行另以 exit code 回報結果。
- Telegram 手機觸發與通知（見 3.2）。

### 3.1 已移除項目（2026-10-08）

`sync_log.txt`、心跳監控與 Docker HEALTHCHECK 存活檔均已取消。同步結果由
Telegram 通知與 `data_status.json` 呈現。

### 3.2 Telegram 觸發與通知

- 指令只有兩個，並提供對話中的按鈕：
  - `/sync`：立即執行一次同步，與排程共用同一套邏輯與同步鎖。同步進行中
    時回覆「同步進行中」，不得並行執行。
  - `/status`：回覆 `data_status.json` 的重點（狀態、最後成功同步時間、
    最後活動日期）。
- 只接受事先設定的使用者 chat ID，其他來源的訊息一律忽略且不回覆。
- 通知規則：
  - Telegram `/sync`：完成後一定回報成功或失敗。
  - `docker exec` 手動執行：結果只顯示在終端機與 exit code，不發
    Telegram 通知。
  - 排程執行：成功不通知，避免每天打擾；失敗時通知。
  - 連續失敗時，第一次失敗通知一次，之後最多每 24 小時提醒一次；
    恢復成功時通知一次。
  - `auth_required` 的通知需明確提示重新執行 `garmin-login`。
- 訊息內容只含狀態與數量，不含訓練數值、帳號、密碼、token 或 GPS。
  Telegram 一般對話並非端對端加密，因此不傳送任何健康資料。
- 只使用 Python 標準函式庫對 Telegram Bot API 發出 HTTPS 請求，不新增依賴。
- 機器人 token 與 chat ID 視同憑證，存放在 state volume 的 `secrets/`（見 5.2），
  不放在環境變數、compose 檔、映像檔或 Git。
- Android 主畫面圖示：將機器人對話加入主畫面，或在對話中點「同步」按鈕。
  實際操作流程需使用者在手機上實測。

### 3.3 （已移除）

心跳監控已取消，見第 2 節第 8 點。

### 3.4 設計約束

- 分析邏輯沿用現有 `ai_text.py` 與 `summary.py`，不得為新輸出另寫
  一套計算。
- `current/` 以先寫暫存檔、再替換的方式更新，避免同步軟體上傳到
  寫到一半的檔案。
- `data_status.json` 至少包含 `last_sync_at`、`last_activity_date`、
  `source`、`status`、`activities`。
- 新輸出不含 GPS 座標。
- 狀態檔（含 `schema_version`）與 Garmin token 快取放在專案目錄之外，不進 Git。
- Docker 部署約束：
  - 映像檔只包含核心依賴與 Garmin 下載所需的選用依賴
    （`garminconnect`、`curl_cffi`），不含 `streamlit` UI、`keyring`
    與 Windows 專用功能所需的套件，除非 PR 說明必要性。
  - 映像檔內不得包含帳號、密碼、token 或任何個人資料。
  - 使用兩個 volume 掛載到 NAS，容器移除後資料仍保留（見 3.6）。
  - 容器內無 Windows 認證管理員，憑證來源見第 5 節。
  - 容器常駐，同步失敗時程序不得結束，只記錄錯誤並等待下一次排程或
    手動觸發，不另外加快重試，狀態寫入 `data_status.json`。
  - 排程時間與時區以環境變數設定（預設時區 `Asia/Taipei`）。
  - 容器不對外開放任何連接埠，以免與 NAS 上既有服務衝突。
  - 容器資源（記憶體與 CPU）設有上限，避免影響同一台 NAS 的其他服務。

### 3.5 `current/` 輸出（2026-10-08 精簡）

使用情境：使用者每週請 AI 檢視上週課表與實際執行，再規劃下週課表與賽事
配速。主要需要「每次跑步的每圈資料」與「近期跑量」，很少查長期趨勢。
有時週三才詢問，需要涵蓋「上週一到今天」。

| 檔案 | 涵蓋範圍 | 用途 |
| ---- | -------- | ---- |
| `data_status.json` | — | 資料是否最新、本次產生的檔案清單 |
| `training_summary.md` | 最近 7 天、28 天跑量；最近 28 天週、日、每次跑步摘要 | 日常問答，AI 先讀本檔 |
| `runs/*.txt` | 最近 28 天，每次跑步一檔 | 單次課表、每圈與軌跡取樣 |
| `activities.csv` | 全部歷史，每次跑步一列 | 少用的長期趨勢與查找特定一次跑步 |

已取消：`index.md`、`sync_log.txt`、`summary.txt`、`all_in_one.txt`。
`summary.txt` 的內容已併入 `training_summary.md`；`index.md` 的導覽改為
`training_summary.md` 開頭幾行（產生時間、涵蓋期間、`runs/` 檔案清單）。
檔案內容與用法由使用者寫在 ChatGPT 專案或排程任務的指示中。

規則：

- 「最近 28 天」以設定時區的本地日期計算，含今天，`training_summary.md`
  與 `runs/*.txt` 使用同一個範圍。
- 週以週一為週首分段，未滿一週的本週標示為「至今」（沿用 v0.2.0 的
  不完整週標示）。AI 自行取用「上週一到今天」，不依賴詢問日。
- 輸出的資料來源是 state volume 中累積的全部原始 TCX，不受下載回溯
  30 天的限制（見 3.6）。
- 軌跡取樣密度固定為 `standard`。
- 所有文字輸出不含 GPS 座標。內容含心率、配速等健康資料，雲端硬碟
  資料夾的分享權限由使用者自行管理。
- 跑步移出 28 天範圍後，其 `runs/*.txt` 會從 `current/runs/` 刪除。
  原始 TCX 仍保留，可隨時重新產生。
- 多個檔案無法同時原子替換。`data_status.json` 最後寫入，作為本次輸出
  完成的標記，並列出本次產生的檔案。
- 數值計算一律沿用 `ai_text.py`。

### 3.6 程式分層與執行模型（2026-10-07 架構確認）

程式分層：

| 層 | 模組 | 責任 |
| -- | ---- | ---- |
| 設定 | `sync_config.py` | 從環境變數讀出不可變的設定（路徑、時區、排程時間） |
| 核心 | `sync_job.py` | `run_sync()` 執行一次同步，回傳結構化結果，不輸出畫面、不結束程序 |
| 入口 | `cli.py` 的 `sync`、`run` | 把結果轉成 exit code，或交給常駐迴圈 |
| 通知 | Telegram、心跳模組 | 依結果決定是否通知，網路請求可替換以便測試 |

- `sync_job` 與既有 `pipeline.run_bundle()` 是兩個平行的用例，
  共用 parser、normalizer 與 `ai_text`，不互相呼叫。
- 下載器重構：把既有 `download_tcx_activities` 登入後的下載迴圈抽成
  可傳入 client 的函式，只回報下載、略過、失敗的數量與路徑，不自行
  判定成功。`import-garminconnect` 維持「有檔案即成功」的既有行為；
  `sync` 採用「任一活動下載失敗即整次失敗」。

常駐程序（`run`）採單執行緒迴圈：

- Telegram 長輪詢的等待時間取「距離下次排程」與「輪詢上限」兩者的
  較小值。
- 排程到點或收到 `/sync` 時，在同一個執行緒內執行同步。同步期間
  `/status` 會延後回覆。
- `docker exec ... sync` 是另一個程序，與常駐程序以既有的檔案鎖互斥。
- Telegram update offset 與通知節流狀態（連續失敗起點、上次提醒時間）
  各自保存在 data volume，與同步狀態分開，各帶 `schema_version`。

Volume（兩個）：

| Volume | 內容 | 同步到雲端 | 備份 |
| ------ | ---- | ---------- | ---- |
| output（`RunningData/`） | `current/` | 是 | 不需要，可重新產生 |
| state | 同步狀態、通知狀態、原始 TCX、同步鎖、`secrets/` 子資料夾（Garmin token、Telegram 機器人 token 與 chat ID） | 否 | 整個納入 Hyper Backup，目的地必須啟用用戶端加密 |

- state 不得位於 output 之下，程式在同步前檢查，違反時拒絕執行。
  output 會被第三方軟體整個上傳到雲端，放進去會洩漏 GPS 與 token。
- 原始 TCX 含 GPS，且是唯一的完整歷史來源（下載最多回溯 30 天），
  因此 state 必須備份並加密。token 因為同在 state 內，一併受加密保護。
- 在 Garmin 上刪除的活動，不會從本地原始 TCX 與輸出中消失。

### 3.7 （已移除）

HEALTHCHECK 與存活檔已取消，見第 2 節第 8 點。`restart: unless-stopped`
仍保留，由 Docker 處理程序結束後的重啟。

## 4. 仍不在範圍內（Out of scope）

- Google Drive API、Google OAuth 與任何 Google 依賴。
- 以 cron、APScheduler 等外部排程套件實作排程，以及 watch-folder。
- Docker Compose 多服務編排、映像檔發佈到公開 registry。
- Webhook 與任何接收外部連入請求的觸發方式、LINE 機器人、REST API、
  MCP Server。Telegram 輪詢只對外連線，不屬於此項。
- Telegram 機器人的其他功能（查詢訓練資料、對話、多使用者）。
- `archive/` 月份分檔與單次活動 Lap JSON。單次活動的文字檔
  `runs/*.txt` 已於 2026-10-07 核准（見 3.5），JSON 格式仍不在範圍內。
- 心跳監控、Docker HEALTHCHECK、`sync_log.txt`、`index.md`、`all_in_one.txt`
  與 `current/` 內的 `summary.txt`（2026-10-08 取消）。
- 多使用者、商業化、公開 SaaS、即時串流。
- 資料庫儲存。去重與同步狀態只使用本機檔案。
- 重寫既有 TCX parser。
- AI 教練、課表推薦與醫療解讀，沿用 No-Inference Policy。

上述第二階段候選項目需要另行取得使用者批准。

## 5. Garmin 登入安全設計

使用者已同意以 token 為主的登入模式。常駐容器只使用 token，
不長期持有帳號密碼。

### 5.1 登入流程

1. 首次登入由使用者手動執行一次互動命令 `garmin-tcx-ai garmin-login`，
   有兩種做法：
   - **方案 A（主要）**：在 DSM 的 Container Manager 開啟該容器的
     終端機（網頁介面），在容器內執行 `garmin-login`。不需要開啟 SSH。
     此功能需在使用者的 DSM 上確認可用。
   - **方案 B（備選）**：在使用者自己的電腦上執行同一個
     `garmin-login`，產生 token 後，用 File Station 上傳到 NAS 的
     token 資料夾。密碼完全不經過 NAS。token 能否跨機器使用需實測。
   不採用「容器內開登入網頁」與「密碼放環境變數或 Docker secret」，
   前者需要開連接埠，後者會讓密碼長期留在磁碟。
2. 密碼以 `getpass` 輸入，不回顯。若帳號啟用 MFA，驗證碼同樣以互動
   方式輸入。
3. 登入成功後，只把 token 寫入 secrets volume。密碼不寫入磁碟，
   命令結束後即從記憶體釋放。
4. 常駐容器每次同步只讀取 token，並使用 Garmin 函式庫的 token
   更新機制延長有效期，不使用密碼。
5. Token 失效時，容器不得退回使用密碼。容器將 `data_status.json` 的
   `status` 標為 `auth_required`，log 提示重新執行 `garmin-login`，
   並等待下一次每日排程或手動觸發再試，不另外加快重試，避免頻繁失敗
   觸發帳號保護。每日一次的排程本身即為低頻重試。

### 5.2 憑證保護

- 密碼不得出現在環境變數、`docker-compose.yml`、映像檔、Git、
  log 或錯誤訊息中。現有 importer 的敏感字串遮罩邏輯需沿用。
- Token 視同密碼處理，存放在 state volume 的 `secrets/` 子資料夾（見
  3.6）。Telegram 機器人 token 與 chat ID 也放在這裡：
  - 不得位於 `RunningData/` 之下，因為該資料夾會被第三方軟體同步到
    Google 雲端硬碟。
  - 目錄權限 0700，檔案權限 0600。
  - 隨 state 一起納入 Hyper Backup，備份目的地必須已加密。
- 建議使用者在 Garmin 帳號啟用雙重驗證。

### 5.3 容器強化

- 容器以非 root 使用者執行。
- 啟用 `no-new-privileges`、移除所有 Linux capabilities。
- 以上設定與記憶體上限寫在 PR 10 的 compose。唯讀根目錄與 tmpfs 需實測
  才知道是否影響啟動，待上線穩定後再評估，不預先承諾。
- 不開放任何連接埠，只有對外連線。
- 映像檔不含 shell 歷史、除錯工具與測試資料。

### 5.4 已知風險與限制

- 本專案使用的 `garminconnect` 屬非官方函式庫，Garmin 變更登入機制時
  可能失效。失效時容器依 5.1 第 5 點標示 `auth_required`，手動匯出
  TCX 的原流程仍可使用。
- Token 的實際有效期限與更新行為由 Garmin 與函式庫決定，需在 NAS 上
  實測後才能確認重新登入的頻率。文件不預先承諾。
- 同步到 Google 雲端硬碟的檔案含心率、配速與跑步時間等健康資料。
  輸出不含 GPS，但使用者需自行確認雲端硬碟資料夾的分享權限。

### 5.5 已實作行為（PR 4）

指令：

```text
garmin-tcx-ai garmin-login [--token-dir DIR] [--email EMAIL] [--check]
```

- Token 資料夾由 `--token-dir` 或環境變數 `GARMIN_TOKEN_DIR` 指定，兩者
  都沒有時結束（exit code 2）。
- 只在互動式終端機執行。不是終端機時拒絕（exit code 2），避免密碼經由
  管線、腳本或 log 傳入。
- `--check` 只驗證已儲存的登入，不要求密碼。方案 B（在電腦登入後複製
  token 到 NAS）可用它確認 token 能否跨機器使用。
- exit code：0 成功；1 登入失敗或驗證失敗；2 使用方式錯誤。
- 登入成功後，程式會用「只有 token」的方式重新登入一次，確認儲存的
  token 真的可用。

安全檢查（`garmin_auth.py`）：

- 函式庫寫入 token 時會隱藏錯誤，登入「成功」卻沒有留下檔案是可能的。
  程式在登入後檢查 token 檔確實存在，否則報錯。
- 每次使用前將 token 資料夾設為 0700、檔案設為 0600，仍過寬則拒絕使用。
  從電腦複製過來的 token 檔常帶有過寬的權限，這一步會修正。Windows 不適用。
- Token 資料夾或檔案是符號連結時拒絕使用。
- `ensure_not_inside()` 拒絕位於同步輸出資料夾之下的 token 資料夾，
  將在 `sync` 命令（PR 9）呼叫，並一併檢查 data 資料夾。
- 錯誤訊息會移除密碼、email 與 token 樣式的字串，錯誤的原始例外鏈不
  向外顯示。

錯誤分類（影響 `data_status.json` 的 `status`）：

| 例外 | 狀態 | 說明 |
| ---- | ---- | ---- |
| `AuthRequiredError` | `auth_required` | 沒有儲存的登入，或 Garmin 以 HTTP 401 拒絕 |
| `GarminServiceError` | `error` | 網路中斷、HTTP 403、5xx、被限流（429） |

`garminconnect` 在取得個人資料失敗時，會把包含網路中斷在內的任何錯誤
都包成「認證錯誤」。若只看例外名稱，NAS 一斷網就會要求使用者重新登入。
因此程式檢查完整的成因鏈：只有 HTTP 401，或沒有任何連線問題跡象的認證
錯誤，才會回報 `auth_required`。HTTP 403 常是機器人驗證擋下，不代表
登入失效，所以歸為 `error`。

### 5.6 套件版本與驗證狀況

`uv.lock` 目前鎖定 `garminconnect` 0.3.6。此登入模組以 0.3.6 與 0.3.17
各做了離線的端對端模擬（真實函式庫的 token 程式碼，假的 Garmin 伺服器）：

| 情境 | 0.3.6 | 0.3.17 |
| ---- | ----- | ------ |
| 密碼登入後 token 存檔，權限 0700 / 0600，檔內不含密碼 | 通過 | 通過 |
| 只用 token 重新載入，不送密碼 | 通過 | 通過 |
| 存取 token 過期後自動更新，新的 refresh token 存回檔案 | 通過 | 通過 |
| 更新被拒且 API 回 401 | `auth_required` | `auth_required` |
| 網路中斷 | `error` | `error` |
| token 檔損毀 | `auth_required` | `auth_required` |

兩個版本的差異在檔案保護：

- 0.3.17 以暫存檔加原子替換寫入 token，並拒絕符號連結路徑。
- 0.3.6 以截斷方式直接覆寫 token 檔，寫入中途當機可能留下損毀的檔案，
  使用者就必須重新登入。它也沒有符號連結路徑的防護。

結論：NAS 部署應使用較新版本。更新 `pyproject.toml` 與 `uv.lock` 不在
本 PR 範圍。2026-10-07 架構確認後，改為在 `sync` 命令之前以獨立的
依賴 PR 處理（見第 8 節 PR A），並要求以 `uv lock` 重新鎖定。

尚未驗證（需要真實帳號，必須在 NAS 上手動測試）：

- 真實登入與 MFA 流程。
- Token 實際可使用多久、refresh token 何時失效。
- 在電腦登入後複製到 NAS 的 token 是否仍可使用（Garmin 是否綁定裝置或 IP）。
- 函式庫版本更新後行為是否改變。

## 6. Docker 部署慣例

使用者在同一台 NAS 上已有以 Docker 運行的 podcast-keep 專案。
本專案沿用其部署流程，降低維運負擔。

沿用的做法：

- 映像檔由 GitHub Actions 建置（`linux/amd64`）。Pull request 只建置
  並做 smoke test，推送 `v*` tag 才推到 GHCR 私有映像。
- NAS 以 `read:packages` 權限的 token 執行一次 `docker login`，
  之後由 Container Manager 的「專案」使用 `docker-compose.yml` 啟動。
- compose 固定版本號，並設定 `pull_policy: always`、
  `restart: unless-stopped` 與 `TZ: Asia/Taipei`。升級與回復都只修改
  版本號。
- 以 tini 作為 PID 1。不設定 HEALTHCHECK（見 2 第 8 點）。
- 資料放在 `docker` 共用資料夾下，並以 volume 掛載：
  - 輸出：`RunningData/`（內含 `current/`），位於第三方同步軟體監看的
    資料夾。
  - state：同步狀態、通知狀態、原始 TCX，以及 `secrets/` 子資料夾
    （Garmin token、Telegram 憑證），不被同步到雲端（見 3.6、5.2）。

與 podcast-keep 不同之處：

- podcast-keep 的 entrypoint 先以 root 啟動，修正資料夾擁有者後用
  `setpriv` 降權。本專案在 compose 直接設定 `user: "PUID:PGID"`，
  程序從不使用 root，並搭配 5.3 的 capabilities 移除。代價是 NAS 上的
  資料夾必須事先以正確擁有者建立，文件需明確說明步驟。
- 不加入 Cloudflare Tunnel 所在的 Docker 網路，不設定 `ports`，使用
  獨立的預設網路，避免影響既有服務。
- 不在容器啟動時自動升級套件。podcast-keep 需要因 YouTube 改版而
  自動更新 yt-dlp，本專案不採用此做法，避免在執行期安裝未經審查的
  程式。`garminconnect` 的更新透過發行新版映像檔處理。
- 狀態檔含 `schema_version`。podcast-keep 的經驗是舊版程式不認得新
  資料結構，回復版本時需連同資料一併還原。本專案要求新版程式必須能
  讀取舊版狀態檔，並在無法辨識時拒絕執行，不得覆寫。

備份：

- state 資料夾必須整個納入 Hyper Backup，且目的地必須啟用用戶端加密，
  因為其中的原始 TCX 含 GPS 且是唯一的完整歷史來源，`secrets/` 內的
  token 也在其中。

## 7. 待決事項

1. 每日排程的預設時間與容器資源上限的實際數值，待常駐 `run` 命令的
   PR 提出。
2. Telegram 憑證在 `state/secrets/` 內的檔案配置，待 Telegram 的 PR 提出。
3. ChatGPT 實際如何讀取雲端硬碟資料夾，需使用者部署後實測。若讀取效果
   不佳，再評估是否加回索引或合併檔，現階段不做。

已解決：

- `sync` 的環境變數（PR 9）：`SYNC_OUTPUT_DIR`、`SYNC_STATE_DIR` 必填；
  `GARMIN_TOKEN_DIR` 預設為 `$SYNC_STATE_DIR/secrets`；`SYNC_TIMEZONE`
  預設 `Asia/Taipei`。原始 TCX 放在 `$SYNC_STATE_DIR/raw/`，輸出在
  `$SYNC_OUTPUT_DIR/current/`。

- `training_summary.md` 已併入同步輸出（PR 5），明細範圍統一為 28 天。

## 8. PR 拆分計畫

每個 PR 維持單一目標，依序進行。2026-10-07 架構確認後，PR 6 起的順序
調整為：先在 NAS 上部署可手動執行的測試版，讓真實帳號的登入與 token
驗證盡早開始，再完成常駐功能。

| 順序 | 目標 | 主要檔案 | 狀態 |
| ---- | ---- | -------- | ---- |
| 1 | 範圍變更文件 | `docs/`、`AGENTS.md` | 已合併 |
| 2 | 產出 `activities.csv` 與 `data_status.json` 的輸出器 | 輸出模組與測試 | 已合併 |
| 3 | 增量同步狀態、overlap、30 天上限與去重 | 同步狀態模組與測試 | 已合併 |
| 4 | `garmin-login` 命令與 token store（權限、遮罩、失效處理） | 登入模組與測試 | 已合併 |
| 5 | `training_summary.md`、`sync_log.txt` 與同步鎖（純輔助模組） | 摘要、log、鎖模組與測試 | 已合併（`sync_log.txt` 於 2026-10-08 取消，程式於 PR 8 移除） |
| 6 | 架構確認文件 | `docs/` | 已合併 |
| 6b | 精簡輸出與部署決策（本次修訂） | `docs/`、`AGENTS.md` | 本 PR |
| 7 | NAS 依賴配置：升級 `garminconnect` 至 0.3.17、`streamlit` 與 `keyring` 移到 `ui` extra（本機開發預設仍安裝）、重新 `uv lock` | `pyproject.toml`、`uv.lock`、README | 已合併 |
| 8 | `current/` 輸出：28 天範圍的 `runs/*.txt` 與舊檔清理、`data_status.json` 的 `files`、`training_summary.md` 的 `runs/` 檔案清單、移除 `sync_log.py`。`training_summary.md` 按週一分段並標示部分週的邏輯 v0.2.0 已有，沿用 | `sync_runs.py`、`sync_summary.py`、`sync_outputs.py`、測試 | 已合併 |
| 9 | `sync` 命令：`sync_config`、`sync_job`、下載器重構、exit code | `cli.py`、下載器、新模組、測試 | 已合併 |
| 10 | NAS 測試部署：Dockerfile、compose、GHCR 建置 workflow。compose 一併設定 `user: PUID:PGID`、`cap_drop: ALL`、`no-new-privileges` 與記憶體上限。容器只待命，以 DSM 終端機手動執行 `garmin-login` 與 `sync` | `Dockerfile`、`.github/workflows/`、`docs/` | 本 PR |
| 11 | 常駐 `run` 命令：單執行緒迴圈、每日排程 | `cli.py`、新模組、compose、測試 | 未開始 |
| 12 | Telegram 觸發與通知（標準函式庫、僅對外連線） | 通知模組與測試 | 未開始 |
| 13 | 正式部署文件。唯讀根目錄與 tmpfs 待上線穩定後再評估，不預先承諾 | `docs/` | 未開始 |

`sync` 命令的 exit code：0 成功；1 失敗；2 使用方式錯誤；3 需要重新
登入；4 已有同步在執行。

原計畫中的「Google Drive 寫入」PR 已取消，改由 NAS 同步軟體負責。
原 PR 5 因範圍過大，拆成輔助模組（PR 5）與 `sync` 串接（現為 PR 9）
以利審查；2026-10-07 再依上表重新排序。

## 9. 驗收條件對照

Issue #32 的驗收條件中，與 Google Drive 直接相關的項目調整如下：

- 「Google Drive 上永遠有固定路徑的最新三個檔案」改為
  「NAS 輸出資料夾 `current/` 永遠有固定路徑的最新輸出集合（見 3.5）」。
  上傳到 Google 雲端硬碟由同步軟體負責，不屬於本專案驗收。
- 「容器常駐無人工介入定期執行」的前提是 Garmin token 仍有效。token
  失效時需使用者重新執行 `garmin-login`（見 5.1），實際頻率待 NAS 實測。
- 其餘條件（容器常駐無人工介入定期執行、去重、中斷後補抓且最多回溯 30 天、
  `data_status.json` 標明最後同步時間與最後活動日期、失敗不標示成功、
  保留既有手動輸出）維持不變。
