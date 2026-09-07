# Pages 資料集矩陣改由 catalog 驅動

補充 `2026-09-01-pages-dashboard-p0.md`,不推翻它。P0 的邊界（快照是唯讀
projection、不公開 SQLite、不在瀏覽器跑 MCP、金額缺值不折成 0）全部沿用。

對應 issue #25 Slice 3。

## 問題

P0 的 `datasets` 是一個**固定兩個 key 的物件**：

- `export_board_data.build_snapshot` 把 `pcc_tender` / `nhi_clinic` 連同
  `source_url` 字串寫死在 payload literal 裡
- `schema-v1.json` 的 `datasets` 是 `additionalProperties: false` +
  `required: ["pcc_tender", "nhi_clinic"]`
- `verify_dashboard.validate_snapshot` 逐一檢查那兩個名字
- `_derive_status` 收一個硬編碼的兩元素 `source_runs`，訊息也寫死「PCC／NHI」

因此 #26 建立的 catalog 一旦啟用新資料集，Pages **看不到它**，而且 schema 會
直接拒收。資料集名單有兩個真相來源（catalog 與 Pages），必然漂移。

## 決策

**資料集名單由 catalog（已啟用者）∪ 資料庫產生；`schema-v1.json` 放寬為對
資料集開放，而不是列舉。**

### 為什麼是放寬 v1，不是切 v2

考慮過把 `datasets` 改成陣列並切 schema v2。否決的理由：

- 放寬是**向後相容**的：先前每一份合法快照仍然合法，只是多了一些也合法的快照。
  切 v2 會讓已發布的 `$id` 契約斷裂，且 `schema_version` 的 `const` 會拒收舊快照。
- 陣列形狀沒有買到任何東西：消費端本來就是以 dataset id 取用。
- 唯一的消費者都在本 repo 內（`dashboard.js`、`render_dashboard`），
  但那不是「可以隨便破壞」的理由 —— 契約已經以 `$id` 公開發布。

故 `schema_version` 維持 `"1.0"`。

### 快照契約的變更（皆為放寬或新增選填）

| 位置 | 原本 | 現在 |
|---|---|---|
| `datasets` | `additionalProperties: false`，列舉兩個 key | `additionalProperties: {$ref dataset}`，`propertyNames` 限 `^[a-z0-9_]+$` |
| `datasets.required` | `["pcc_tender", "nhi_clinic"]` | `["pcc_tender"]` |
| `dataset.row_count` | `integer, minimum 0` | `integer \| null` |
| `dataset.source_url` | `string, uri` | `string uri \| null` |
| `dataset.*` | — | 新增選填 `title` / `collection` / `update_cadence` / `verified_at` |

### 不可協商的語意

- **`row_count: null` 代表物化表不存在，即從未同步成功。** 與「同步成功但 0 筆」
  是兩件事，不得折成 0。前端顯示「—」，不顯示 0。
- **停用的 catalog 候選不發布。** 它們尚未對官方端點實查（見 #25 Slice 2）；
  列在公開頁面上會讓訪客誤以為已涵蓋。
- **出處欄位只從 catalog 來。** PCC 不是 catalog 驅動的（半月 XML + 關鍵字過濾），
  其 `title` / `verified_at` 等一律為 `null`，不臆造。
- **`source_url` 只有 PCC 在程式碼裡明列**（`NON_CATALOG_SOURCE_URLS`），
  其餘一律取自 catalog。同一件事不在兩個地方維護。

## 狀態判定

`_derive_status` 的來源清單改由「實際發布的資料集反推 `source_id`」產生，
標籤取 `data_sources.name`，缺列時退回 `source_id`（退回本身也是可見的事實）。
`fresh` 訊息不再寫死「PCC／NHI」。

尚未同步過的資料集沒有 `source_id`，因此**不會**把整頁狀態拉成 degraded ——
它的 `row_count: null` 本身已經是可見訊號，不需要重複懲罰。

## UI

新增「資料集概覽」矩陣（`#dataset-matrix-body`），欄位：資料集、筆數、
最後同步、最近一次同步狀態、更新頻率、出處驗證日、授權。

- **由 `dashboard.js` 以 `createElement` / `textContent` 渲染**，不用 `innerHTML`。
  `verify_dashboard` 對此有靜態檢查，本次不放寬。
- 沒有以 build-time 預渲染，是因為 template 的取代迴圈對每個值做 `html.escape`；
  要塞 `<tr>` 就得開一個 raw-HTML 槽，等於在一個刻意禁用 `innerHTML` 的專案裡
  自己開 XSS 面。無 JS 時矩陣顯示明確的說明列，上方 KPI 摘要仍為預渲染。
- 既有的 NHI KPI 保留，但改為容忍 `nhi_clinic` 缺席（顯示「無資料」）。

## 驗證閘門

`verify_dashboard.py` 維持 **stdlib-only**（`pages.yml` 不安裝套件），因此它
**只驗形狀不驗成員**：`pcc_tender` 必須存在且 `row_count` 為整數（看板的摘要與
表格都靠它），其餘資料集只檢查 key 命名、必填欄位齊全、`row_count` 為整數或 null。

這條界線是刻意的：驗證器若知道資料集名單，每加一個資料集就要改部署閘門 ——
那正是本次要消滅的耦合。

## 未處理

- `docs/data/current.json` **未重新產生**。它是一次真實同步的產物，本環境沒有
  那個 DB；用 fixture 重建等於用假資料覆蓋真資料。新增的出處欄位為選填，舊快照
  仍合法，矩陣的出處欄位會顯示「—」直到下一次真實 export —— 那是誠實的呈現。
- Pages 仍未自動同步（P1 範圍，見 #21）。
