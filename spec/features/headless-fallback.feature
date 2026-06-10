# language: zh-TW
Feature: 無頭瀏覽器特例後備
  作為 ETL 排程
  我想要對僅提供 JS 互動查詢且無批次下載的來源保留無頭瀏覽器介面
  以便未來涵蓋無乾淨 API/檔案的資料,同時不影響查詢服務

  # healthcare 領域目前無必要場景(健保署為乾淨 CSV API、PCC 為 XML+HTTP enrich);
  # 本 Feature 為介面保留,MVP 不實作 adapter。

  Scenario: 無頭瀏覽器抓取為可選且預設關閉
    Given 系統未啟用 headless 抓取
    When ETL 排程執行
    Then 不應啟動任何 Chromium 進程
    And 標記為 HEADLESS_BROWSER 的來源應被跳過並記錄
