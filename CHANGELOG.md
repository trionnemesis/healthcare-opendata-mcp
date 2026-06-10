# Changelog

## [0.2.2] - 2026-06-10

### Added
- 啟動護欄:`hcmcp` 啟動時偵測空 DB,直接以明確訊息退出(指出 DB 路徑、指引先跑 `hcmcp-sync`),避免 server 起來後查無資料的隱性失敗

### Verified
- 62 tests 通過(新增 2 tests:空 DB 報錯含路徑與指引 / 有資料正常放行)

## [0.2.1] - 2026-06-10

### Added
- `nhi-clinic`(健保特約醫事機構-診所):NHI 一級 API,約 24.5k 筆/每日更新
  - 實查更正:需求提供之 rId `A21030000I-D32001-001` 查無資料;經 openapi 目錄盤點,正確 resource ID 為 `A21030000I-D21004-009`

### Verified
- 60 tests 通過;live sync nhi-opendata +125,701 筆,`query_rows("nhi-clinic", limit=10)` 回傳 10 筆,縣市聚合正常

## [0.2.0] - 2026-06-10

### Added
- 接通 Twinkle healthcare collection 剩餘 4 資料集(05 矩陣 ③ 完整覆蓋):
  - `nhi-hospital-bed-ratio`(#9402,info.nhi.gov.tw 一級 API,複合鍵 機構代碼|統計年月)
  - `nhi-insured-population`(#25842,vac.gov.tw CSV)
  - `mohw-outpatient-rate`(#176510,mohw.gov.tw 縣市別系列 2 檔合併,欄位重命名統一 schema)
  - `mnd-military-hospital-fee`(#142696,mnd.gov.tw CSV)
- `StaticCsvAdapter`:多 URL 合併單一資料集、column_renames、複合 natural key
- `NhiDatasetSpec.natural_key_columns`:複合鍵支援;CSV 正規化抽出共用 `_csv.normalize_csv`

### Verified
- 59 tests 通過;live E2E:nhi-opendata 101,150 筆 + gov-static 168,552 筆 + pcc 9 筆,4 個新資料集 `query_rows limit=10` 各回傳 10 筆,GROUP BY 聚合正常

## [0.1.0] - 2026-06-10

### Added
- MVP:NHI 健保特約醫事機構(地區/區域醫院)+ pcc-tender-mohw 衛福部標案,`SourceAdapter → ETL → 物化表 → query_rows` 全鏈路
- MCP 工具:`list_datasets` / `get_dataset` / `query_rows` / `search_records` / `get_record` / `list_sources`(對齊 Twinkle Hub query_rows)
- SQL 安全護欄:唯讀連線、單一 SELECT 白名單、limit 硬上限 200

### Verified
- 52 unit/integration tests 全數通過
- Live E2E(2026-06-10):`hcmcp-sync` 實抓 nhi-opendata 457 筆 + pcc-opendata 9 筆;`query_rows(dataset_id='nhi-hospital-district', limit=10)` 成功回傳 10 筆
