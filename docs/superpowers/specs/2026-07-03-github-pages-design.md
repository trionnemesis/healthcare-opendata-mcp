# Healthcare OpenData MCP GitHub Pages Design

## Goal

新增一個公開 GitHub Pages 首頁，讓一般讀者與開發者先理解本專案要處理的問題，再依需求查看已實作能力、技術架構與使用方式。

## Audience

- 一般讀者：理解衛福部資訊勞務標案與健保診所開放資料在實際使用上的障礙。
- 開發者：確認資料來源、MCP 查詢介面、安全邊界與部署方式均有 repository evidence。

內容採問題敘事為主、技術細節為輔。首頁不假設讀者已理解 MCP、ETL 或 SQLite。

## Information Architecture

頁面採 Problem-first 單頁結構：

1. Hero：以「官方資料存在，卻不容易被 AI Agent 可靠查詢」定義核心問題。
2. Problem：說明來源分散、格式不一致、標案關鍵資訊缺漏，以及第三方聚合服務不可控。
3. Response：呈現官方來源 → 同步與正規化/enrich → 唯讀 SQLite → MCP tools 的資料流。
4. Current capabilities：列出目前 repository 已存在的 datasets、查詢工具、SQL safety 與 HTTP/GKE deployment 支援。
5. Call to action：連到 GitHub repository 與 README 快速開始。

## Content Boundaries

所有能力宣稱必須可由目前 `master` branch 的 README、source、tests 或 deployment manifests 驗證。頁面不得：

- 宣稱 GKE 已正式上線或已有 production service URL。
- 宣稱資料為即時或完整；PCC 為半月 XML 加明細 enrich，NHI 依官方 CSV 更新。
- 將 planned work 描述成已完成能力。
- 引入 README 未支持的醫療、採購或政策成效敘述。

## Implementation

- 在 `docs/` 建立無 framework、無 build dependency 的靜態 `index.html` 與單一 stylesheet。
- 使用 repository-relative 與 canonical GitHub links；不依賴外部 JavaScript。
- 加入 GitHub Actions Pages workflow，從 `docs/` 上傳並部署靜態 artifact。
- 保留 application runtime、Python dependencies、lockfiles、Docker 與 Kubernetes manifests 不變。
- 在 README 加入 GitHub Pages 入口，讓 repository 訪客可直接找到問題說明頁。

## Visual Direction

- 深藍 hero 強調公共資料與可靠性議題，搭配高對比文字。
- 內容區使用清楚的單欄閱讀順序；問題卡片可在寬螢幕四欄、窄螢幕單欄排列。
- 技術資料流使用純 HTML/CSS，不使用圖片或 diagram dependency。
- 支援 keyboard focus、semantic headings、可讀 contrast 與 mobile layout。

## Deployment Flow

Push 到 `master` 後，GitHub Actions workflow 將 `docs/` 作為 Pages artifact 部署。Repository Settings 的 Pages source 必須設為 GitHub Actions；若尚未啟用，workflow 會保留，但公開 URL 需由 repository owner 完成 Pages 啟用後才可驗證。

## Validation

1. 靜態檔案 smoke test：啟動本機 HTTP server，確認 `index.html` 與 stylesheet 回應成功。
2. HTML link/content check：確認主要 section、GitHub/README links 與 stylesheet path 存在。
3. Responsive browser check：以 desktop 與 mobile viewport 檢查版面、overflow 與可讀性。
4. Workflow syntax inspection：確認 Pages permissions、artifact upload 與 deploy job 正確。
5. Repository regression：執行既有 Python test suite，證明新增靜態頁面未影響 application behavior。

## Non-Goals

- 不建立動態 dashboard、即時資料 API、搜尋或互動式 MCP demo。
- 不新增 analytics、cookies、tracking 或第三方 frontend dependencies。
- 不修改資料同步、查詢、security 或 deployment runtime behavior。
