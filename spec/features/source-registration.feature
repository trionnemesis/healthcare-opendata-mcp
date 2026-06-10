# language: zh-TW
Feature: 資料來源註冊與探索
  作為資料工程師
  我想要以統一介面註冊多個醫療健保開放資料來源
  以便用一致方式管理不同的取得策略

  Scenario: 註冊靜態檔案來源
    Given 一個靜態檔案來源設定(PCC 半月決標 XML 的固定 URL)
    When 我以 access_strategy=STATIC_FILE 註冊該來源
    Then 該來源應出現在 list_sources 結果
    And 來源的取得策略應為 STATIC_FILE

  Scenario: 探索平台 API 來源的資料資源
    Given 一個健保署 info.nhi.gov.tw 資料服務 API 來源
    When 我對該來源執行 discover
    Then 應回傳該來源底下可用的資料資源(ResourceRef)清單
    And 每個資源應標註格式(csv/json/xml)與下載或查詢 URL

  # 2026-06-10 擴充:Twinkle healthcare collection 剩餘 4 資料集(05 矩陣 ③)
  Scenario: 註冊靜態 CSV 來源(多檔合併單一資料集)
    Given 一個靜態 CSV 來源設定,其資料集「健保平均門診就診率」由多個年度區間檔案組成
    When 我對該來源執行 discover
    Then 應回傳多個 ResourceRef,且其 dataset id 相同
    And 各檔案的 records 應 upsert 合併至同一物化表

  Scenario: 靜態 CSV 欄位重命名正規化
    Given 「健保平均門診就診率」新年度檔案的欄位名為「疾病別(第10版)」
    When 我執行 normalize
    Then 該欄位應重命名為「疾病別」,與舊年度檔案 schema 一致

  Scenario: 複合 natural key 去重
    Given 「保險病床比率」資料列以(機構代碼, 統計年月)唯一識別,且來源含完全重複列
    When 我執行 normalize 與 upsert
    Then natural_key 應為「機構代碼|統計年月」
    And 完全重複列應被去重為單筆
