# 過期門檻由 update_cadence 推導

補充 `2026-09-01-pages-dashboard-p0.md` 與 `2026-09-07-pages-catalog-driven-datasets.md`。
對應 issue #31 第 2 項。

## 問題

#22 明文寫下：

> 不自行推論「healthy／stale」門檻。第一版只顯示資料庫中的實際 timestamp 與
> run status；**若未來要加 freshness SLA，需先建立每個 source 的正式 cadence 契約**。

〔查得〕實作與這條要求牴觸：`export_board_data.py` 有
`DEFAULT_STALE_AFTER_DAYS = 21`，對所有來源一體適用。那是一個沒有依據的猜測值 ——
一份每日更新的資料集停更 20 天顯然出事了，一份年度更新的資料集停更 20 天完全正常，
同一個門檻不可能同時對兩者成立。

轉折是：#26 之後 catalog 每筆 entry 都帶 `update_cadence`。**那份 cadence 契約現在
存在了**，門檻可以有依據。

## 決策

**過期門檻逐資料集推導，事實來自 catalog，政策留在 exporter。**

### 事實與政策的分界

| | 住在哪 | 是什麼 |
|---|---|---|
| `update_cadence` | `catalog.py` | **事實** —— 官方多久更新一次，實查時觀察到的 |
| 門檻天數 | `export_board_data.py` | **政策** —— 我們認為多久沒更新算過期 |

把政策塞進 catalog 會讓「資料集的出處」與「看板的呈現偏好」綁死；把事實搬進
exporter 則會讓同一件事在兩處維護。故兩者分開。

### 推導規則

**門檻 = 2 × 更新週期。**

漏掉一次更新是時序造成的常態（跑晚了、假日、上游延遲）；漏掉兩次代表出事了。
少於兩個週期會讓正常的一次漏跑就變紅，多於兩個週期則失去預警意義。

| cadence | 名目週期 | 門檻 |
|---|---:|---:|
| `daily` | 1 | 2 |
| `weekly` | 7 | 14 |
| `monthly` | 30 | 60 |
| `quarterly` | 91 | 182 |
| `yearly` | 365 | 730 |
| `irregular` | — | **無** |
| `unknown` | — | **無** |

`irregular` 與 `unknown` 沒有週期可推導，因此**沒有門檻，永遠不會被判為 stale**。

**catalog 新增 cadence 而未在此定政策時，`export_board_data` 在 import 時就失敗**
（`_uncovered` 檢查）。若讓它靜默落到「無週期」，一個真的有節奏的資料集會永遠不被
判為過期 —— 那比直接壞掉更糟。

### 非 catalog 驅動的來源

PCC 不是 catalog 驅動的（半月 XML + 關鍵字過濾），其門檻在
`NON_CATALOG_STALE_AFTER_DAYS` 明列，沿用 P0 的 21 天，刻意維持不變以免改動既有
頁面的狀態判定。`--stale-after-days` 旗標只影響這一類，catalog 驅動者不受其影響。

## 三值語意

`freshness` 有三個值，`unknown` 不是 `fresh` 的變體：

| 值 | 意思 | UI |
|---|---|---|
| `fresh` | 有契約，且在門檻內 | 最新 |
| `stale` | 有契約，且已超過門檻 | 過期（逾 N 天） |
| `unknown` | **沒有契約可依據**，或尚未同步 | 無契約 |

**`unknown` 必須在 UI 上看得見。** 若把它渲染成空白或與 `fresh` 同樣的樣式，一個
沒有新鮮度契約的資料集會被讀成「已驗證為最新」—— 那正是 #22 要避免的推論。

三種狀態都以**文字**呈現，不靠顏色區分（沿用 P0 的可及性原則）。

## 判定基準是 last_fetched_at，不是 ingestion_runs.finished_at

原本的全域判定用 `min(ingestion_runs.finished_at)`。改為逐資料集的
`datasets.last_fetched_at`，因為：

- run 是 **per-source** 的，而門檻是 **per-dataset** 的
- `ingestion/pipeline.py` 對單一 ref 失敗有容錯 —— **一個 SUCCEEDED 的 run 底下
  可能有某個 dataset 根本沒更新到**。用 run 的時間會把這種情況掩蓋掉

`ingestion_runs` 的時間戳仍用於 degraded 判定（run 未成功、完成時間缺失、時間戳
壞掉或晚於快照時間）。

## 全域狀態的匯總

優先序不變：`empty` → `degraded` → `stale` → `fresh`。

`stale` 改由逐資料集的 `freshness` 匯總：**任一資料集為 `stale` 則整頁 `stale`**，
訊息指名是哪些資料集、依據什麼門檻。`freshness=unknown` 的資料集**不參與**匯總 ——
沒有依據就不該拉低狀態，那個狀態在資料集矩陣裡本來就看得見。

## 契約變更

`schema-v1.json` 的 `$defs/dataset` 新增兩個**選填**欄位，`schema_version` 維持
`"1.0"`（放寬，非破壞；舊快照沒有這兩欄仍合法）：

- `stale_after_days`: `integer | null`
- `freshness`: `enum ["fresh", "stale", "unknown"]`

`verify_dashboard.py` 維持 stdlib-only，對這兩欄只在**存在時**驗型別與值域 ——
放行任意字串等於讓前端渲染一個沒人定義過的狀態。

## 未處理

`docs/data/current.json` 未重新產生（本環境無真實 DB）。新欄位為選填，舊快照仍
合法；矩陣的新鮮度欄位會顯示「—」直到下一次真實 export。
