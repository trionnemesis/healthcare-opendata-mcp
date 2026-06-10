# language: zh-TW
Feature: 資料抓取與正規化
  作為 ETL 排程
  我想要抓取來源資料並正規化為統一 Record
  以便不同來源的資料能被同一套查詢介面使用

  Scenario: 抓取健保特約醫事機構並正規化
    Given 健保署特約醫事機構 CSV 已發布每日更新
    When ETL worker 下載該檔案
    Then 每筆機構應正規化為統一 Record(natural_key=醫事機構代碼)
    And Record 應以 upsert 寫入 repository(同 natural_key 不重複)

  Scenario: 抓取 PCC 半月決標並過濾衛福部
    Given PCC 半月決標 XML 含多個機關的決標公告
    When ETL worker 解析該 XML
    Then 僅機關名稱以「衛生福利部」開頭的列應被正規化為 Record
    And 欄位應對齊 twinkle pcc-tender(date/announcement_type/title/agency/job_number/companies/award_price 等)

  Scenario: 半月檔尚未發布時容忍空檔案
    Given 某期 PCC 半月 XML 尚未發布(回應為空內容)
    When ETL worker 抓取該期
    Then 該期應產生零筆 Record 且不視為錯誤

  Scenario: 來源限流時退避重試
    Given 官方 API 對抓取請求回傳 HTTP 429
    When 抓取觸發退避策略
    Then 應依 backoff 指數退避重試
    And 單一來源失敗不應中斷整個 ETL 排程
    And 失敗應記錄為 IngestionFailed 事件
