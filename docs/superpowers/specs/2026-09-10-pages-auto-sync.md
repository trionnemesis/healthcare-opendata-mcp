# Pages 自動同步與 last-known-good

補充 `2026-09-01-pages-dashboard-p0.md`。對應 issue #31 第 1 項（原 #21 PR B + #22
「Workflow／安全」）。

## 問題

P0 的看板是**可重建的，但不會自己重建**。`pages.yml` 只把已提交的 `docs/` 上傳部署；
資料要更新，得有人在本機跑 `hcmcp-sync` + `export_board_data.py` 再 commit。

## 兩條互相衝突的要求

#31 同時要求：

- 「上游失敗 → **保留前一份有效快照**，不部署空資料」
- 「sync 或 export 任一步失敗 → **workflow 失敗且不部署**」

字面上做不到兩者：workflow 失敗不部署，就沒有東西可以「輸出 degraded 狀態」。

而且「失敗就不部署」有個副作用：一個只改了 `docs/` 文案的 push，會因為當下上游剛好
掛掉而永遠上不了線。上游的健康狀況不該綁架我們自己的靜態內容。

### 決策：先部署 last-known-good，再讓 job 轉紅

```text
seed _site ← 委入的快照(last-known-good)
  ↓
sync → export → 驗證 staging → 通過才整份取代 _site   [continue-on-error]
  ↓
驗證 _site                                          [硬失敗]
  ↓
deploy
  ↓
refresh 失敗過 → exit 1                              [job 轉紅]
```

四條要求同時成立：

| 要求 | 由哪一步滿足 |
|---|---|
| 保留前一份有效快照 | `_site` 一開始就是委入的快照，refresh 失敗時原封不動 |
| 不部署空資料 | staging 的 `--require-non-empty` 閘門；沒過就不取代 `_site` |
| 失敗不得偽造成功 | 部署後的 `exit 1`，job 轉紅、維護者收到通知 |
| 上一個成功版本繼續存在 | 同第一列 |

**靜默成功等於用舊快照冒充今日資料** —— 這是 #21 / #22 的不可協商項，所以紅燈不能省。

## 原子性替換

`export_board_data.py` 內部已有 `atomic_write_many`（單一檔案層級）。本次在
**artifact 層級**再做一次：整個新站台先建在 `$RUNNER_TEMP/stage`，通過
`verify_dashboard --site "$STAGE" --require-non-empty` 之後才 `cp -r` 取代 `_site`。

中途任何一步失敗（sync 非零、export 拋錯、驗證不過、快照為空）都不會動到 `_site`。

### 為什麼 `--require-non-empty` 是選用的

`empty` 對快照契約而言是**合法狀態** —— 一個真的沒有資料的資料庫就該誠實地說 empty，
委入的快照走一般驗證路徑。但在「用新快照覆蓋舊快照」的那一刻，空資料等於用看起來
成功的東西蓋掉真實資料。故閘門只掛在部署路徑上。

## 不提交回 repo

考慮過讓 workflow 把新快照 commit 回 `master`。**否決**：

- 需要 `contents: write`，等於為了更新資料而讓部署 workflow 取得寫入 repo 的權限
- 會產生 bot 對 `master` 的提交流
- 沒有必要 —— `_site` 是 ephemeral artifact，git 裡的委入快照本來就是 last-known-good

`permissions` 維持 `contents: read`。#22 原本也是這樣設想的（「可在 workflow 建立暫存
site 目錄，例如 `_site/`」）。

## 快照自己的年紀（客戶端）

`status.state` 是**匯出當下**的判定，被凍結在 JSON 裡。靜態頁可能被服務很久：
**若自動同步停擺，一份三個月前的快照會永遠自稱 `fresh`。**

`dashboard.js` 因此在載入時以「現在」重新檢查快照本身的年紀：

- 門檻取所有資料集 `stale_after_days` 中**最嚴格的那個**（不另立一個憑空的數字）
- 超過就把橫幅覆寫為 stale，並說明快照產生於幾天前
- **只覆寫 `fresh`**。`degraded` / `empty` / `stale` 比它嚴重，不得被降級

這一條是 #31「不得用舊快照冒充今日資料」在自動化停擺情境下的最後一道防線。

## 排程

**尚未啟用**，`schedule:` 在 workflow 中維持註解。兩個理由：

1. **未實測。** 建立本 workflow 的 session 無法連外（egress 政策拒絕全部五個官方
   host），因此**無法驗證 GitHub-hosted runner 能否穩定完成官方來源同步**。#31
   明文要求「PR 中提供實測證據」—— 沒有就是沒有，不以推測代替。
2. **負載未評估。** 「不得假設 DB 已存在於 runner」意味著每次都是全量同步；預設回溯
   12 個月的 PCC 半月檔，每日跑一次對政府站台是持續性負載。這與 repo 既有的反爬倫理
   直接相關，屬維運決策。

啟用方式：以 `workflow_dispatch` 成功跑過一次、確認耗時與負載可接受後，取消
`schedule:` 的註解。`tender_months` / `award_months` 已開為 workflow input，可先用較
短的視窗評估。

## 未處理

- 排程（見上）。
- `docs/data/current.json` 仍是委入的舊快照；第一次成功的自動同步會取代它，屆時
  資料集矩陣的 `freshness` / `stale_after_days` 欄位才會有值（目前顯示「—」）。
- Pages workflow 重跑 repo 自身的同步流程；若未來要避免與 production CronJob 重複
  抓取，需另案接 GCS/OIDC（#22 已評估，屬未來另案）。
