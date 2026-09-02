"use strict";

(() => {
  const PAGE_SIZE = 20;
  const REQUIRED_ROW_FIELDS = [
    "date",
    "announcement_type",
    "title",
    "agency",
    "job_number",
    "bid_deadline",
    "open_date",
    "budget",
    "award_price",
    "companies",
  ];

  const elements = {
    status: document.querySelector("#data-status"),
    statusLabel: document.querySelector("#status-label"),
    statusMessage: document.querySelector("#status-message"),
    generatedAt: document.querySelector("#generated-at"),
    sourceMaxDate: document.querySelector("#source-max-date"),
    schemaVersion: document.querySelector("#schema-version"),
    pccRowCount: document.querySelector("#pcc-row-count"),
    snapshotRowCount: document.querySelector("#snapshot-row-count"),
    dateRange: document.querySelector("#date-range"),
    announcementSummary: document.querySelector("#announcement-summary"),
    budgetSummary: document.querySelector("#budget-summary"),
    budgetKnownCount: document.querySelector("#budget-known-count"),
    awardSummary: document.querySelector("#award-summary"),
    awardKnownCount: document.querySelector("#award-known-count"),
    nhiRowCount: document.querySelector("#nhi-row-count"),
    nhiFetchedAt: document.querySelector("#nhi-fetched-at"),
    form: document.querySelector("#filters"),
    keyword: document.querySelector("#keyword"),
    type: document.querySelector("#announcement-type"),
    agency: document.querySelector("#agency"),
    dateFrom: document.querySelector("#date-from"),
    dateTo: document.querySelector("#date-to"),
    sort: document.querySelector("#sort-order"),
    reset: document.querySelector("#reset-filters"),
    body: document.querySelector("#records-body"),
    resultCount: document.querySelector("#result-count"),
    previous: document.querySelector("#previous-page"),
    next: document.querySelector("#next-page"),
    pageStatus: document.querySelector("#page-status"),
  };

  const state = { rows: [], filtered: [], page: 1 };
  const numberFormat = new Intl.NumberFormat("zh-TW");
  const moneyFormat = new Intl.NumberFormat("zh-TW", {
    style: "currency",
    currency: "TWD",
    maximumFractionDigits: 0,
  });

  function requireSnapshot(payload) {
    if (!payload || payload.schema_version !== "1.0") {
      throw new Error("不支援或缺少 snapshot schema_version");
    }
    if (!payload.status || !["fresh", "stale", "degraded", "empty"].includes(payload.status.state)) {
      throw new Error("snapshot status 無效");
    }
    if (!payload.datasets || !payload.datasets.pcc_tender || !payload.datasets.nhi_clinic) {
      throw new Error("snapshot datasets 不完整");
    }
    if (!payload.summary || !payload.summary.pcc_tender || !Array.isArray(payload.rows)) {
      throw new Error("snapshot summary/rows 不完整");
    }
    for (const row of payload.rows) {
      if (!row || REQUIRED_ROW_FIELDS.some((field) => !(field in row))) {
        throw new Error("snapshot row 欄位不完整");
      }
    }
    return payload;
  }

  function setText(element, value, fallback = "—") {
    if (element) {
      element.textContent = value === null || value === undefined || value === "" ? fallback : String(value);
    }
  }

  function formatCount(value) {
    return Number.isFinite(Number(value)) ? numberFormat.format(Number(value)) : "—";
  }

  function formatMoney(value) {
    return typeof value === "number" && Number.isFinite(value) ? moneyFormat.format(value) : "—";
  }

  function statusLabel(status) {
    return {
      fresh: "資料狀態：新鮮",
      stale: "資料狀態：已過期",
      degraded: "資料狀態：降級",
      empty: "資料狀態：無資料",
      error: "資料狀態：載入失敗",
    }[status] || "資料狀態：未知";
  }

  function updateStatus(status, message) {
    elements.status.dataset.state = status;
    elements.status.className = `data-status data-status--${status}`;
    setText(elements.statusLabel, statusLabel(status));
    setText(elements.statusMessage, message);
  }

  function populateSelect(select, values) {
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      select.append(option);
    }
  }

  function hydrateSummary(payload) {
    const summary = payload.summary.pcc_tender;
    const pcc = payload.datasets.pcc_tender;
    const nhi = payload.datasets.nhi_clinic;
    setText(elements.generatedAt, payload.generated_at);
    setText(elements.sourceMaxDate, payload.status.source_max_date);
    setText(elements.schemaVersion, payload.schema_version);
    setText(elements.pccRowCount, formatCount(pcc.row_count));
    setText(elements.snapshotRowCount, formatCount(summary.snapshot_row_count));
    setText(
      elements.dateRange,
      `涵蓋 ${summary.date_range.min || "無法確認"} – ${summary.date_range.max || "無法確認"}`,
    );
    setText(
      elements.announcementSummary,
      summary.announcement_types.map((item) => `${item.name} ${formatCount(item.count)}`).join(" · "),
      "沒有公告類型資料",
    );
    setText(elements.budgetSummary, formatMoney(summary.budget.sum_twd));
    setText(elements.budgetKnownCount, formatCount(summary.budget.known_count));
    setText(elements.awardSummary, formatMoney(summary.award_amount.sum_twd));
    setText(elements.awardKnownCount, formatCount(summary.award_amount.known_count));
    setText(elements.nhiRowCount, formatCount(nhi.row_count));
    setText(elements.nhiFetchedAt, nhi.last_fetched_at);
    updateStatus(payload.status.state, payload.status.message);
  }

  function searchableText(row) {
    return [row.title, row.agency, row.job_number, row.companies]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase("zh-Hant");
  }

  function compareNullableNumber(left, right) {
    const leftKnown = typeof left === "number" && Number.isFinite(left);
    const rightKnown = typeof right === "number" && Number.isFinite(right);
    if (!leftKnown && !rightKnown) return 0;
    if (!leftKnown) return 1;
    if (!rightKnown) return -1;
    return right - left;
  }

  function sortRows(rows, order) {
    const sorted = [...rows];
    if (order === "date-asc") {
      sorted.sort((a, b) => String(a.date || "").localeCompare(String(b.date || "")));
    } else if (order === "budget-desc") {
      sorted.sort((a, b) => compareNullableNumber(a.budget, b.budget));
    } else if (order === "award-desc") {
      sorted.sort((a, b) => compareNullableNumber(a.award_price, b.award_price));
    } else {
      sorted.sort((a, b) => String(b.date || "").localeCompare(String(a.date || "")));
    }
    return sorted;
  }

  function cell(value, className = "") {
    const td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
    return td;
  }

  function renderRows() {
    const totalPages = Math.max(1, Math.ceil(state.filtered.length / PAGE_SIZE));
    state.page = Math.min(Math.max(1, state.page), totalPages);
    const start = (state.page - 1) * PAGE_SIZE;
    const visible = state.filtered.slice(start, start + PAGE_SIZE);
    const fragment = document.createDocumentFragment();

    if (visible.length === 0) {
      const row = document.createElement("tr");
      row.className = "empty-row";
      const message = cell("沒有符合目前條件的資料");
      message.colSpan = 10;
      row.append(message);
      fragment.append(row);
    } else {
      for (const record of visible) {
        const row = document.createElement("tr");
        const typeCell = document.createElement("td");
        const typeBadge = document.createElement("span");
        typeBadge.className = "record-type";
        typeBadge.textContent = record.announcement_type || "—";
        typeCell.append(typeBadge);
        row.append(
          cell(record.date),
          typeCell,
          cell(record.title),
          cell(record.agency),
          cell(record.job_number),
          cell(record.bid_deadline),
          cell(record.open_date),
          cell(formatMoney(record.budget)),
          cell(formatMoney(record.award_price)),
          cell(record.companies),
        );
        fragment.append(row);
      }
    }

    elements.body.replaceChildren(fragment);
    setText(elements.resultCount, `共 ${formatCount(state.filtered.length)} 筆結果；每頁 ${PAGE_SIZE} 筆。`);
    setText(elements.pageStatus, `第 ${state.page} / ${totalPages} 頁`);
    elements.previous.disabled = state.page <= 1;
    elements.next.disabled = state.page >= totalPages;
  }

  function applyFilters({ resetPage = true } = {}) {
    const keyword = elements.keyword.value.trim().toLocaleLowerCase("zh-Hant");
    const type = elements.type.value;
    const agency = elements.agency.value;
    const dateFrom = elements.dateFrom.value;
    const dateTo = elements.dateTo.value;
    const filtered = state.rows.filter((row) => {
      if (keyword && !searchableText(row).includes(keyword)) return false;
      if (type && row.announcement_type !== type) return false;
      if (agency && row.agency !== agency) return false;
      if (dateFrom && (!row.date || row.date < dateFrom)) return false;
      if (dateTo && (!row.date || row.date > dateTo)) return false;
      return true;
    });
    state.filtered = sortRows(filtered, elements.sort.value);
    if (resetPage) state.page = 1;
    renderRows();
  }

  function enableControls() {
    for (const control of elements.form.elements) control.disabled = false;
  }

  function bindEvents() {
    elements.form.addEventListener("input", () => applyFilters());
    elements.form.addEventListener("change", () => applyFilters());
    elements.form.addEventListener("reset", () => {
      window.setTimeout(() => applyFilters(), 0);
    });
    elements.previous.addEventListener("click", () => {
      state.page -= 1;
      renderRows();
    });
    elements.next.addEventListener("click", () => {
      state.page += 1;
      renderRows();
    });
  }

  async function load() {
    try {
      const response = await fetch("../data/current.json", { cache: "no-store" });
      if (!response.ok) throw new Error(`snapshot HTTP ${response.status}`);
      const payload = requireSnapshot(await response.json());
      hydrateSummary(payload);
      state.rows = payload.rows;
      populateSelect(elements.type, payload.filters.announcement_types || []);
      populateSelect(elements.agency, payload.filters.agencies || []);
      enableControls();
      bindEvents();
      applyFilters();
    } catch (error) {
      updateStatus("error", "快照無法載入或格式不符；保留建置時摘要，不呈現假成功。完整查詢請使用 MCP。");
      elements.body.replaceChildren();
      const row = document.createElement("tr");
      row.className = "empty-row";
      const message = cell("資料載入失敗；請稍後重試或查看 GitHub Actions 狀態。");
      message.colSpan = 10;
      row.append(message);
      elements.body.append(row);
      console.error("Dashboard snapshot load failed", error);
    }
  }

  load();
})();
