# NAS 測試部署（PR 10）

> 本公開倉庫是展示用快照，不發佈 Docker 映像；`docker.yml` 只建置並跑
> smoke test。映像發佈流程在原開發倉庫使用。

範圍與決策見 `docs/11_nas_sync_scope_change.md`。本文件只說明
目前這個階段怎麼把容器放上 DS720+ 並手動執行 `garmin-login` 與 `sync`。
容器目前只待命，每日排程與 Telegram 在 PR 11、12 才加入。

## 1. 發佈映像

1. 合併後在 `main` 推送 tag。測試階段用 `v0.3.0-rc.1`，有問題再發
   `rc.2`；常駐與 Telegram 完成後發正式的 `v0.3.0`。
2. `Docker image` workflow 建置 `linux/amd64`、跑 smoke test，通過才推到
   `ghcr.io/roy1648/garmin-tcx-ai:0.3.0-rc.1`（私有）。
3. Pull request 只建置與測試，不推送。
4. smoke test 檢查三件事：`--help` 可執行、映像內沒有 `streamlit`、
   沒設環境變數時 `sync` 以 exit code 2 結束。

## 2. NAS 準備

1. 在 NAS 用 `read:packages` 權限的 token 執行一次 `docker login ghcr.io`。
2. 建立兩個資料夾，擁有者必須是容器使用的使用者（`user:` 的 PUID:PGID），
   因為容器不使用 root：
   - `RunningData/`：輸出，之後由 NAS 同步軟體上傳到 Google 雲端硬碟。
   - `garmin-state/`：原始 TCX、同步狀態、token。不要同步到雲端，
     要納入 Hyper Backup 並啟用用戶端加密。
3. 兩個資料夾不可互相包含。程式在 `sync` 前會檢查，違反就拒絕執行。
4. 複製 `deploy/docker-compose.yml` 到 Container Manager 的專案，改掉標
   `CHANGE` 的值（PUID:PGID、兩個 NAS 路徑）。映像版本固定寫死，升級或
   回復只改這個字串。

## 3. 首次登入與同步

在 DSM Container Manager 開啟容器的終端機（方案 A），依序執行：

```bash
garmin-tcx-ai garmin-login   # 輸入帳號、密碼，若有 MFA 再輸入驗證碼
garmin-tcx-ai sync
```

`sync` 的 exit code：0 成功、1 失敗、2 設定缺少、3 需要重新登入、
4 已有同步在執行。成功後 `RunningData/current/` 會有
`data_status.json`、`training_summary.md`、`activities.csv` 與 `runs/`。

密碼只在互動輸入時使用，不寫入磁碟；token 存在
`garmin-state/secrets/`。若 DSM 終端機無法使用，改用方案 B：在自己的電腦
執行 `garmin-login` 後，把 token 檔上傳到 `garmin-state/secrets/`。

## 4. 容器設定

`deploy/docker-compose.yml` 已包含：非 root 使用者、
`no-new-privileges`、移除所有 Linux capabilities、記憶體 512 MB 與
1 顆 CPU 的上限、不開放任何連接埠。唯讀根目錄與 tmpfs 待上線穩定後再
評估。

## 5. 尚未驗證

- DSM 終端機是否能互動輸入密碼與 MFA。
- 真實 Garmin 登入、下載、token 有效期與更新。
- 電腦登入後複製的 token 是否能在 NAS 使用。
- 映像實際在 DS720+ 上的記憶體用量，512 MB 上限需視實測調整。
