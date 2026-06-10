"""SQLite 基底表 DDL — 對應 spec/erm.dbml。

物化表 ds_{dataset_id} 不在此定義:由 SqliteRepository.upsert_batch
依各 dataset 的 schema_json 動態生成(spec 不變量 #5)。
"""

BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS data_sources (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    platform        TEXT NOT NULL,
    access_strategy TEXT NOT NULL,
    config          TEXT NOT NULL DEFAULT '{}',
    schedule_cron   TEXT,
    enabled         INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS datasets (
    id              TEXT PRIMARY KEY,
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    schema_json     TEXT,
    collection      TEXT,
    license         TEXT,
    last_fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS records (
    dataset_id  TEXT NOT NULL,
    natural_key TEXT NOT NULL,
    payload     TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (dataset_id, natural_key)
);
CREATE INDEX IF NOT EXISTS idx_records_ingested ON records(ingested_at);

CREATE TABLE IF NOT EXISTS ingestion_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL,
    fetched_count INTEGER DEFAULT 0,
    error_detail  TEXT
);
"""
