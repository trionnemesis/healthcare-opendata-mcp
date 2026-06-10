# Changelog

## [0.1.0] - 2026-06-10

### Added
- MVP:NHI 健保特約醫事機構(地區/區域醫院)+ pcc-tender-mohw 衛福部標案,`SourceAdapter → ETL → 物化表 → query_rows` 全鏈路
- MCP 工具:`list_datasets` / `get_dataset` / `query_rows` / `search_records` / `get_record` / `list_sources`(對齊 Twinkle Hub query_rows)
- SQL 安全護欄:唯讀連線、單一 SELECT 白名單、limit 硬上限 200

### Verified
- 52 unit/integration tests 全數通過
- Live E2E(2026-06-10):`hcmcp-sync` 實抓 nhi-opendata 457 筆 + pcc-opendata 9 筆;`query_rows(dataset_id='nhi-hospital-district', limit=10)` 成功回傳 10 筆
