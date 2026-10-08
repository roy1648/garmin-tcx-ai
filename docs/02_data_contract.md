# 資料契約

本專案 MVP 以 Garmin Connect 匯出的 Running TCX 為輸入，輸出
normalized activity、atomic per-activity artifacts，以及 coach-facing
session bundle artifacts。所有 AI-ready 輸出都必須遵守 No-Inference
Policy。

## 1. No-Inference Policy

本工具只輸出下列資料：

- TCX 原始欄位。
- 由 TCX 數值以固定公式計算的 metrics。
- data quality flags。
- privacy policy 與 source/data policy。
- time-gap rule 產生的 session candidate grouping。

不得自動產生下列內容：

- activity、lap 或 segment 的課表角色。
- warmup、main set、cooldown、interval、tempo、easy、recovery、
  long run、quality session 等訓練語意。
- fatigue、collapse、workout quality、workout type 等解讀。
- coaching advice、medical interpretation 或主動分析問題。

所有 activity 與 lap 的 role 欄位固定如下：

```json
{
  "role": null,
  "role_source": "not_inferred"
}
```

## 2. Coach-Facing 標準輸出

AI 教練或其他 AI 工具的標準輸出來源是 session bundle：

```text
session_bundle/
  session_bundle.json
  session_bundle.md
```

不論輸入是單一 TCX 或多個 TCX，都必須輸出同一組 session bundle
artifacts。單一 TCX 的 `export_scope` 範例如下：

術語說明：`session_bundle` 是 report/package format，可包含一個或多個
activity records，以及一個或多個 session candidates。它不代表多個 TCX
已被合併成一堂訓練；每個 TCX activity identity 必須保留，session
candidate 只表示候選分組。

```json
{
  "type": "session_bundle",
  "activity_count": 1,
  "session_candidate_count": 1,
  "contains_multiple_activities": false
}
```

Atomic per-activity artifacts 仍保留作為 debug、audit 與資料交換用途，
但不是 AI coach-facing 的主要輸出。

```text
<safe_activity_id>/
  activity.json
  trackpoints.csv
  ai_summary.json
  ai_summary.md
```

## 3. Atomic Activity Identity

- 一個 normalized activity 永遠等於一個來源 TCX file。
- Session bundle 不合併 activity identity。
- Session candidate grouping 只表示相鄰活動可能屬於同一訓練時段，
  不表示 TCX 來源資料已有該事實。

## 4. Session Bundle Schema

`session_bundle.json` top-level keys：

```json
{
  "schema_version": "tcx_training_data_v1",
  "export_scope": {},
  "data_policy": {},
  "sessions": [],
  "data_quality": {},
  "privacy": {}
}
```

`data_policy` 必須包含：

```json
{
  "activity_equals_one_tcx_file": true,
  "session_may_contain_multiple_activities": true,
  "grouping_is_candidate_not_fact": true,
  "no_workout_role_inference": true,
  "no_coaching_advice": true,
  "no_medical_interpretation": true,
  "manual_context_fields_are_placeholders": true
}
```

## 5. Session Candidate

每個 session candidate 必須包含：

- `session_id`
- `grouping_confidence: "candidate"`
- `grouping_rule`
- `role_inference: "disabled"`
- `activity_count`
- `start_time` / `end_time`
- `start_time_local` / `end_time_local`
- `timezone`
- `local_date`
- total distance / duration
- weighted average heart rate
- maximum heart rate
- cadence factual metrics
- power factual metrics
- `manual_context`
- `activities`
- `data_quality`

`manual_context` 固定為 manual-only placeholders：

```json
{
  "planned_workout_text": null,
  "planned_workout_source": "manual_only",
  "completion": null,
  "rpe_1_to_10": null,
  "pain_before": null,
  "pain_during": null,
  "pain_after": null,
  "next_day_status": null
}
```

這些欄位不得從 TCX 推論，也不得猜測課表完成度、疼痛、RPE 或隔日狀態。

## 6. Session Grouping

`build_session_bundle()` 使用 deterministic grouping rule：

- 依 `start_time` 排序。
- 使用 `timezone_name` 將 `start_time` 轉為 local date。
- 相同 local date。
- 相同 sport。
- 相鄰 `start_time` 間隔不超過 `max_gap_minutes`，預設 30 分鐘。
- 缺少 `start_time` 的 activity 必須獨立成為 singleton candidate。

`grouping_rule` 範例：

```json
{
  "same_local_date": true,
  "timezone": "Asia/Taipei",
  "same_sport": true,
  "max_gap_minutes": 30
}
```

無效的 `timezone_name` 必須 raise `ValueError`，不得靜默 fallback 成其他
timezone。

## 7. Activity Summary Local Time

每個 activity summary 必須保留 UTC time，並額外提供 local time 欄位：

```json
{
  "start_time": "2026-07-05T13:43:35Z",
  "start_time_local": "2026-07-05T21:43:35+08:00",
  "timezone": "Asia/Taipei",
  "local_date": "2026-07-05"
}
```

Timezone conversion 使用 Python standard library `zoneinfo`，不新增
production dependency。

## 8. Lap Pace Reliability

每個 lap summary 必須包含 `pace_reliability` 與 `reliability_reason`。
這是 data-quality flag，不是訓練解讀。

| 條件 | pace_reliability | reliability_reason |
|---|---|---|
| distance 或 duration 缺漏 | `invalid` | `missing_distance_or_duration` |
| distance <= 0 或 duration <= 0 | `invalid` | `non_positive_distance_or_duration` |
| distance < 0.10 km | `low` | `lap_distance_below_0.1km` |
| 0.10 km <= distance < 0.30 km | `medium` | `lap_distance_between_0.1km_and_0.3km` |
| distance >= 0.30 km | `high` | `lap_distance_at_least_0.3km` |

## 9. Computed Split Metrics

`computed_split_metrics` 只可輸出固定公式數值：

- 前半段平均配速。
- 後半段平均配速。
- 後半段配速 delta，計算式為後半減前半。
- 前半段平均心率。
- 後半段平均心率。
- 後半段心率 delta，計算式為後半減前半。

必要 policy 欄位：

```json
{
  "interpretation_policy": "computed_metrics_only_no_training_interpretation",
  "interpretation_level": "limited_for_interval_or_mixed_lap_activity"
}
```

`notes` 必須包含：

```text
This split metric is a fixed-formula summary and must not be interpreted as fatigue, workout quality, or workout type.
```

不得輸出 `faster_later`、`slower_later`、`stable`、`fatigue`、
`decline`、`collapse`、`tempo`、`interval`、`easy` 或 `main set`。

## 10. Cadence Factual Metrics

Cadence 使用 Garmin RunCadence 或 normalized trackpoint 中的原始數值，
本 PR 不做 x2 conversion。

Activity-level 範例：

```json
{
  "cadence": {
    "avg_run_cadence_raw": 83.1,
    "max_run_cadence_raw": 88,
    "trackpoints_with_run_cadence_count": 2485,
    "source": "tcx_extension_RunCadence_or_normalized_trackpoint",
    "avg_cadence_spm": null,
    "conversion_rule": null
  }
}
```

Lap-level `source` 使用 `normalized_trackpoints_by_lap`。Session-level
`source` 使用 `activity_trackpoint_aggregate`。

## 11. Power Factual Metrics

Power 使用 Garmin Watts 或 normalized trackpoint 中的原始數值，不推論
訓練強度。

```json
{
  "power": {
    "avg_watts": 230.5,
    "max_watts": 310,
    "trackpoints_with_power_count": 2485,
    "source": "tcx_extension_Watts_or_normalized_trackpoint"
  }
}
```

沒有 power 資料時，平均與最大值為 `null`，count 為 `0`。

## 12. Data Quality

Activity-level 與 session-level `data_quality` 必須包含：

- `trackpoints_with_run_cadence_count`
- `trackpoints_with_power_count`

Cadence 與 power 是 optional Garmin extensions，缺漏時不得列入
`missing_key_fields`。

## 13. Markdown Contract

`session_bundle.md` 必須包含：

- `# TCX Multi-Activity Report`
- `## Data Policy`
- `## Export Scope`
- `## Session Candidates`
- `## Activities`
- `## Lap Summaries`
- `## Computed Split Metrics`
- `## Data Quality`
- `## Privacy`

Data Policy 需明示：

- This report packages one or more TCX activities for AI-readable review.
  It does not merge them into one recorded workout.
- Session candidates are candidate activity groups for review; they do not
  merge activities into one recorded workout.
- Manual context fields are placeholders only and were not inferred from TCX.
- Cadence values are raw Garmin RunCadence values; no cadence x2 conversion is applied.

Markdown 不得包含 GPS 座標、route details、Suggested AI Analysis Questions、
訓練角色推論、教練建議或醫療解讀。

## 14. Privacy Contract

GPS policy 行為：

- `keep`：`activity.json` 與 CSV 可保留緯度與經度；AI summary 與
  session bundle 仍不得包含座標或路線細節。
- `remove`：JSON 中將緯度與經度設為 `null`，CSV 中留空。
- `redact_start_end`：遮蔽活動開頭與結尾的 GPS 座標。

輸出必須記錄所選 GPS policy。Raw TCX、`.env`、tokens、credentials、
`data/raw/` 與 `data/processed/` 不得 commit。

## 15. NAS Sync Structured Outputs

NAS 增量同步（見 `docs/11_nas_sync_scope_change.md`）在 `current/` 資料夾
產生兩個結構化檔案。兩者都不含 GPS 座標，數值計算沿用
`ai_text.py`，與 `summary.txt` 一致。寫入採用「同資料夾暫存檔再替換」，
避免同步軟體上傳到寫到一半的檔案。

### 15.1 `activities.csv`

UTF-8、`\n` 換行、含標題列，每個活動一列，依開始時間由早到晚排列，
沒有開始時間的活動排在最後。缺漏值為空白儲存格。

| 欄位 | 說明 |
| ---- | ---- |
| `activity_id` | 見下方識別規則 |
| `date` | 設定時區的日期 `YYYY-MM-DD` |
| `start_time` | 設定時區的 `HH:MM` |
| `distance_km` | 公里，兩位小數 |
| `duration` | `h:mm:ss`，未滿一小時為 `m:ss` |
| `avg_hr` / `max_hr` | 活動層級心率 |
| `pace` | 每公里配速 `m:ss`，總時間除以總距離 |
| `power` | trackpoint 功率平均值，與 `summary.txt` 相同 |

`activity_id` 識別規則：

1. 檔名符合 Garmin Connect 下載格式 `<start>_activity_<id>_<type>.tcx`
   時，使用其中的數字 Garmin activity ID。
2. 否則使用 TCX 的 `Id` 元素（手動匯出時為開始時間）。
3. 兩者都沒有時為空白。

### 15.2 `data_status.json`

UTF-8、縮排 2 格。欄位：

| 欄位 | 說明 |
| ---- | ---- |
| `schema_version` | 目前為 `1`。讀取者遇到不認得的版本必須拒絕使用。 |
| `status` | `ok`、`error` 或 `auth_required` |
| `source` | 資料來源標籤，預設 `garmin_tcx` |
| `last_sync_at` | 最後一次**成功**同步的時間（含時區偏移），從未成功為 `null` |
| `last_attempt_at` | 最近一次嘗試的時間 |
| `last_activity_date` | 資料中最後一次活動的本地日期，無活動為 `null` |
| `activities` | `activities.csv` 的活動數 |
| `files` | 最近一次成功輸出的檔案清單（相對 `current/`）；`data_status.json` 最後寫入，作為輸出完成的標記；失敗時沿用上次的值 |
| `last_error` | 失敗時的簡短訊息，成功為 `null`；不得含帳號、密碼或 token |

失敗規則：同步失敗時資料檔沒有被替換，因此 `last_sync_at`、
`last_activity_date` 與 `activities` 沿用前一次成功的值，只更新
`status`、`last_attempt_at` 與 `last_error`。失敗不得被標示為較新的成功。
讀取者在 `status` 不是 `ok` 時，應回報資料更新到 `last_sync_at`，
不得把缺少資料解讀成「沒有訓練」。

## 16. NAS Sync State, Window and De-duplication

此節定義增量同步的純邏輯，不涉及網路。範圍見
`docs/11_nas_sync_scope_change.md`。

### 16.1 `sync_state.json`

放在專案目錄之外的狀態資料夾，不進 Git，也不放在會同步到雲端的
`RunningData/` 之下。

```json
{
  "schema_version": 1,
  "last_successful_sync": "2026-10-07T06:00:00+08:00"
}
```

- `last_successful_sync` 記錄最後一次**完全成功**的同步**開始**時間
  （含時區），從未成功為 `null`。記錄開始時間，是為了讓同步進行中才上傳
  的活動仍落在下一次的範圍內。
- 只有整個同步成功後才寫入。失敗的同步不得修改此檔。
- 檔案不存在代表從未同步。檔案損毀、缺少或不認得 `schema_version`、
  版本比程式新、時間缺少時區，一律拒絕執行（`SyncStateError`），
  不得覆寫。

### 16.2 同步日期範圍

日期為設定時區的本地日期，範圍含頭尾：

```text
start = max(last_success_day - overlap_days, today - max_lookback_days)
end   = today
```

- 預設 `overlap_days = 2`、`max_lookback_days = 30`。
- 從未成功時，`start = today - max_lookback_days`。
- 超過回溯上限時，範圍被截斷，並標示 `truncated`，讓呼叫端可以記錄
  「更早的活動可能缺漏」。
- 時鐘異常（上次成功晚於現在）時，`start` 不得晚於今天。

### 16.3 去重

兩個活動符合任一條件即視為同一次跑步：

1. `resolve_activity_id()` 的結果相同。
2. 開始時間相同（精確到秒）。第二條用來處理手動匯出的 TCX（以開始時間
   識別）與同一活動的 Garmin 下載檔（以 Garmin ID 識別）。

重複時保留：有 Garmin activity ID 的，其次 trackpoint 較多的，其次
輸入順序較前的。沒有識別碼也沒有開始時間的活動不會被合併。

## 17. `current/` 的其他檔案

### 17.1 `training_summary.md`

給 AI 先讀 `data_status.json`，再讀本檔。UTF-8、Markdown，不含 GPS，
不含教練建議或訓練解讀。內容依序為：

1. **資料狀態**：狀態、最後成功同步時間、最後活動日期、本檔涵蓋的日期
   範圍與跑步次數。這些值取自同一份 `data_status` dict，與
   `data_status.json` 一致。狀態不是 `ok` 時，說明資料只更新到最後成功
   同步的時間，不得把之後沒有出現的活動當成沒有訓練。
2. **最近 7 天與 28 天**：Markdown 表格，含次數、距離、時間、平均配速、
   平均心率與最長單次。區間含今天，以設定時區的本地日期計算。
3. **詳細摘要**：最近 28 天的 `summary.txt` 內容（週、日、每次跑步），放在
   `text` 程式碼區塊內。數字由 `ai_text.py` 計算，與 `summary.txt` 相同。

沒有開始時間的活動無法歸入任何一天，不列入本檔。

### 17.2 （已取消）

`sync_log.txt` 已於 2026-10-08 取消，見 `docs/11_nas_sync_scope_change.md`。

### 17.3 同步鎖

同一時間只允許一個同步。鎖是狀態資料夾內 `sync.lock` 的作業系統檔案
鎖，行程結束或當機時自動釋放，不會留下殘留的鎖。已有同步執行時，後來
的同步立即回報並結束，不等待、不並行。僅支援 POSIX 系統（NAS 容器為
Linux）。

