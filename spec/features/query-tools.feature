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
