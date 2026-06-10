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
