# language: zh-TW
Feature: SQL 式聚合查詢 query_rows(對齊 Twinkle Hub)
  作為 Claude/Agent
  我想要以 SQL 式條件對單一資料集做篩選與聚合
  以便重現 Twinkle Hub query_rows 的分析能力

  Scenario: 按機關聚合決標金額
    Given pcc-tender-mohw 資料集已物化為查詢表
    When 我以 columns=[agency, SUM(CAST(award_price AS INTEGER)) AS total]、group_by=[agency] 呼叫 query_rows
    Then 應回傳各機關的決標總額,依 total 遞減排序

  Scenario: get_dataset 回傳抽樣資料列
    When 我以 dataset_id 與 sample_rows=5 呼叫 get_dataset
    Then 除 metadata 與 schema 外,應回傳該資料集前 5 筆抽樣資料

  Scenario: 拒絕非 SELECT 語句(注入防護)
    Given query_rows 連線為唯讀
    When where 參數含有 "1=1; DROP TABLE records" 多語句注入
    Then 查詢應被拒絕並回傳參數錯誤
    And 不應執行任何資料變更

  Scenario: 拒絕跨表讀取
    Given query_rows 以 authorizer 白名單限制單一物化表
    When where 參數含有讀取 data_sources 表的子查詢
    Then 查詢應被拒絕

  Scenario: dataset_id 白名單映射
    When 我以不存在的 dataset_id 呼叫 query_rows
    Then 應回傳 dataset 不存在錯誤
    And 使用者輸入不應被拼接為 SQL 識別符

  Scenario: limit 硬上限
    When 我以 limit=10000 呼叫 query_rows
    Then 實際回傳列數不應超過 400
    And 回應應標註結果已截斷
