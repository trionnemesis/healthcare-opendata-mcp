# language: zh-TW
Feature: 開放資料查詢(MCP 工具)
  作為 Claude/Agent
  我想要以自然語言驅動的 MCP 工具查詢醫療健保開放資料
  以便回答使用者的資料問題

  Scenario: 跨來源關鍵字查詢
    Given repository 已有多個來源的 Record
    When 我以關鍵字呼叫 search_records
    Then 應回傳符合的記錄,且每筆標註其來源與資料集

  Scenario: 取得資料集 metadata
    When 我以 dataset_id 呼叫 get_dataset
    Then 應回傳該資料集的 metadata 與欄位 schema

  Scenario: 列出所有已註冊來源
    When 我呼叫 list_sources
    Then 應回傳每個來源的 id、平台、取得策略與最後抓取時間

  # 2026-09-07 擴充:新鮮度也是資料 —— 呼叫端要能分辨「查無資料」與「沒同步成功」
  Scenario: 資料集清單附帶新鮮度
    Given repository 已同步某資料集
    When 我呼叫 list_datasets
    Then 每筆應附 last_fetched_at 與 row_count

  Scenario: 從未同步成功的資料集不得回報 0 筆
    Given datasets 有登錄某資料集但其物化表尚未建立
    When 我呼叫 get_dataset
    Then row_count 應為 null,而不是 0

  # 2026-09-07 擴充:Pages 快照的資料集矩陣改由 catalog 驅動(#25 Slice 3)
  Scenario: 快照的資料集名單不硬編碼
    Given catalog 啟用了一個新的資料集
    When 我重新產生 Pages 快照
    Then datasets 應包含該資料集,且不需要修改 schema 或驗證器

  Scenario: 停用的候選不發布到公開頁面
    Given catalog 中有 enabled=false 的候選
    When 我重新產生 Pages 快照
    Then datasets 不應包含該候選

  Scenario: 從未同步成功的資料集不得顯示為 0 筆
    Given 某資料集已登錄但物化表尚未建立
    When 我重新產生 Pages 快照
    Then 其 row_count 應為 null,前端顯示「—」而非 0
