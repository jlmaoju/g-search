const ABOUT_LABELS = {
  data_version: "数据版本",
  library_updated_at: "本库更新时间",
  built_at: "构建时间",
  eligible_items: "可检索节目",
  participants_count: "参与者数量",
  collection_name: "索引集合",
  mode_label: "当前模式",
};

const BACKEND_MAINTENANCE_MESSAGE = "后端正在维护，请稍后重试（或联系作者PHSJ2019）";
const DEGRADED_SEARCH_DEFAULT_MESSAGE = "语义检索服务暂时不可用，当前显示的是关键词降级结果，相关性和排序质量会下降。";
const RESULT_LIMIT_STORAGE_KEY = "gsearch.resultLimit";
const AUTO_EXPAND_STORAGE_KEY = "gsearch.autoExpandDetails";
const DEFAULT_RESULT_LIMIT = 50;
const MAX_RESULT_LIMIT = 200;

const state = {
  runtimeConfig: {
    apiBase: "",
    downloadUrl: "",
    offlineMessage: "静态页面已经打开，但现在无法连接搜索后端。",
  },
  meta: null,
  programTypes: [],
  participants: [],
  selectedProgramTypes: new Set(),
  selectedParticipants: new Set(),
  scope: "content",
  results: [],
  relevanceOrderedResults: [],
  expandedResultDocIds: new Set(),
  collapsedResultDocIds: new Set(),
  lastPayload: null,
  resultLimit: readSavedResultLimit(),
  autoExpandDetails: readSavedAutoExpandDetails(),
  resultsSortedByTime: false,
  participantQuery: "",
  activeFilterTab: "categories",
  filterDropdownOpen: false,
  activeSheet: null,
  online: true,
};

const ABOUT_PANEL_LABELS = {
  mode_label: "当前模式",
  library_updated_at: "更新时间",
  eligible_items: "可检索节目数",
  timeline_notes: "时间轴数",
  participants_count: "参与者数",
  database_size: "数据库大小",
  viewer_download: "本机版本下载",
};

const els = {
  modePill: document.getElementById("mode-pill"),
  utilityButton: document.getElementById("utility-button"),
  settingsButton: document.getElementById("settings-button"),
  libraryNote: document.getElementById("library-note"),
  heroKicker: document.getElementById("hero-kicker"),
  pageTitle: document.getElementById("page-title"),
  offlineNotice: document.getElementById("offline-notice"),
  offlineMessage: document.getElementById("offline-message"),
  offlineDownloadLink: document.getElementById("offline-download-link"),
  searchForm: document.getElementById("search-form"),
  searchBox: document.querySelector(".search-box"),
  query: document.getElementById("query"),
  searchSubmit: document.getElementById("search-submit"),
  scopeSwitch: document.getElementById("scope-switch"),
  filterButton: document.getElementById("filter-button"),
  filterDropdown: document.getElementById("filter-dropdown"),
  filterTabs: document.getElementById("filter-tabs"),
  activeFilters: document.getElementById("active-filters"),
  statusLine: document.getElementById("status-line"),
  resultSortTime: document.getElementById("result-sort-time"),
  emptyState: document.getElementById("empty-state"),
  results: document.getElementById("results"),
  sheetRoot: document.getElementById("sheet-root"),
  sheetBackdrop: document.getElementById("sheet-backdrop"),
  sheetClose: document.getElementById("sheet-close"),
  sheetKicker: document.getElementById("sheet-kicker"),
  sheetTitle: document.getElementById("sheet-title"),
  aboutPanel: document.getElementById("about-panel"),
  settingsPanel: document.getElementById("settings-panel"),
  programTypeList: document.getElementById("program-type-list"),
  participantList: document.getElementById("participant-list"),
  participantSearch: document.getElementById("participant-search"),
  filterSummaryText: document.getElementById("filter-summary-text"),
  clearFilters: document.getElementById("clear-filters"),
  aboutMetaList: document.getElementById("about-meta-list"),
  resultLimit: document.getElementById("result-limit"),
  autoExpandDetails: document.getElementById("auto-expand-details"),
  viewerSettingsBlock: document.getElementById("viewer-settings-block"),
  viewerKeyForm: document.getElementById("viewer-key-form"),
  viewerApiKey: document.getElementById("viewer-api-key"),
  viewerSessionOnly: document.getElementById("viewer-session-only"),
  viewerKeyStatus: document.getElementById("viewer-key-status"),
  viewerClearKey: document.getElementById("viewer-clear-key"),
  backendToast: document.getElementById("backend-toast"),
};

let backendToastTimer = 0;
const PROGRAM_TYPE_LABEL_OVERRIDES = new Map([["会员专享", "会员专享（免费部分）"]]);

function clampInteger(value, { min, max, fallback }) {
  const parsed = Number.parseInt(String(value), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.min(max, Math.max(min, parsed));
}

function readStorageValue(key) {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStorageValue(key, value) {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Settings still work for the current session if storage is unavailable.
  }
}

function readSavedResultLimit() {
  return clampInteger(readStorageValue(RESULT_LIMIT_STORAGE_KEY), {
    min: 1,
    max: MAX_RESULT_LIMIT,
    fallback: DEFAULT_RESULT_LIMIT,
  });
}

function readSavedAutoExpandDetails() {
  return readStorageValue(AUTO_EXPAND_STORAGE_KEY) === "true";
}

function apiUrl(path) {
  const base = (state.runtimeConfig.apiBase || "").replace(/\/$/, "");
  return `${base}${path}`;
}

function proxiedMediaAsset(url) {
  if (!url) return "";
  return `${apiUrl("/api/media-asset")}?url=${encodeURIComponent(url)}`;
}

function escapeHtml(value) {
  return String(value || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

function escapeRegExp(value) {
  return String(value || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function highlightTerms(text, terms) {
  let html = escapeHtml(text || "");
  const ordered = Array.from(new Set((terms || []).filter(Boolean))).sort((a, b) => b.length - a.length);
  for (const term of ordered) {
    const escaped = escapeHtml(term);
    if (!escaped) continue;
    html = html.replace(new RegExp(escapeRegExp(escaped), "g"), `<mark>${escaped}</mark>`);
  }
  return html;
}

const HIGHLIGHT_STOP_TERMS = new Set([
  "什么",
  "以前",
  "一个",
  "一种",
  "一套",
  "这个",
  "那个",
  "就是",
  "好像",
  "感觉",
  "来回",
  "系统",
  "节目",
  "哪期",
  "那期",
  "记得",
  "忘了",
]);

function normalizeForHighlight(value) {
  return String(value || "").replace(/\s+/g, "").trim();
}

function deriveOverlapTerms(query, text, maxTerms = 6) {
  const normalizedQuery = normalizeForHighlight(query);
  const normalizedText = normalizeForHighlight(text);
  if (!normalizedQuery || !normalizedText) return [];
  const results = [];
  for (const size of [6, 5, 4, 3, 2]) {
    if (results.length >= maxTerms || normalizedQuery.length < size) continue;
    for (let index = 0; index <= normalizedQuery.length - size; index += 1) {
      const fragment = normalizedQuery.slice(index, index + size);
      if (!fragment || HIGHLIGHT_STOP_TERMS.has(fragment)) continue;
      if (!normalizedText.includes(fragment)) continue;
      if (results.some((existing) => existing.includes(fragment) || fragment.includes(existing))) continue;
      results.push(fragment);
      if (results.length >= maxTerms) break;
    }
  }
  return results;
}

function semanticDetailText(result) {
  const details = Array.isArray(result.match_details) ? result.match_details : [];
  const semantic = details.find((entry) => entry && entry.kind === "semantic" && entry.text);
  return semantic?.text || "";
}

function isLowSignalText(text) {
  const normalized = normalizeForHighlight(text).toLowerCase();
  if (!normalized) return true;
  if (normalized.includes("<unk>")) return true;
  if (/^(oh)+$/.test(normalized.replace(/[^a-z]/g, ""))) return true;
  return false;
}

function resolveResultContentText(result) {
  const primary = result.display_text || (result.doc_type === "timeline_note" ? result.timeline_content : "") || "";
  const semanticText = semanticDetailText(result);
  if (semanticText && isLowSignalText(primary)) {
    return semanticText;
  }
  return primary;
}

function resolveHighlightTerms(result, contentText) {
  const direct = Array.isArray(result.matched_terms) ? result.matched_terms.filter(Boolean) : [];
  if (direct.length) return direct;
  const query = els.query?.value || "";
  const semanticText = semanticDetailText(result);
  return deriveOverlapTerms(query, `${contentText} ${semanticText}`.trim());
}

function formatDateLabel(value, { withTime = true } = {}) {
  if (!value) return "-";
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return String(value);
  }
  const options = withTime
    ? { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }
    : { year: "numeric", month: "2-digit", day: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(parsed);
}

function formatBytes(value) {
  const size = Number(value);
  if (!Number.isFinite(size) || size <= 0) return "-";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let current = size;
  let index = 0;
  while (current >= 1024 && index < units.length - 1) {
    current /= 1024;
    index += 1;
  }
  const digits = current >= 100 || index === 0 ? 0 : current >= 10 ? 1 : 2;
  return `${current.toFixed(digits)} ${units[index]}`;
}

function docLabel(result) {
  if (result.doc_type === "timeline_note") return "时间轴";
  if (result.doc_type === "item_title") return "标题";
  return "内容";
}

function composeMetaLine(result) {
  const parts = [];
  if (result.category) parts.push(result.category);
  const users = (result.users || []).filter(Boolean);
  if (users.length) parts.push(users.join(" / "));
  if (result.published_at) parts.push(formatDateLabel(result.published_at, { withTime: false }));
  return parts.join(" · ");
}

function resultImage(result) {
  return proxiedMediaAsset(result.cover_url || result.thumb_url || "");
}

function hasExpandableContext(result, contentText) {
  const neighborCount = Array.isArray(result.neighbor_atoms) ? result.neighbor_atoms.length : 0;
  return Boolean((contentText && contentText.length > 110) || neighborCount > 0);
}

function resultDocId(result) {
  return String(result?.doc_id || "");
}

function isResultExpanded(result, contentText) {
  const docId = resultDocId(result);
  if (!docId || !hasExpandableContext(result, contentText)) return false;
  if (state.autoExpandDetails) {
    return !state.collapsedResultDocIds.has(docId);
  }
  return state.expandedResultDocIds.has(docId);
}

function clearResultExpansionState() {
  state.expandedResultDocIds.clear();
  state.collapsedResultDocIds.clear();
}

function resultChronologicalTimestamp(result) {
  const publishedAt = Date.parse(result?.published_at || result?.timeline_at || "");
  if (!Number.isFinite(publishedAt)) return Number.POSITIVE_INFINITY;
  const offset = Number(result?.start_ms ?? result?.timeline_ms ?? 0);
  return publishedAt + (Number.isFinite(offset) ? offset : 0);
}

function syncResultPayload() {
  if (state.lastPayload) {
    state.lastPayload = { ...state.lastPayload, results: state.results };
  }
}

function toggleCurrentResultsTimeSort() {
  if ((state.results || []).length < 2) return;
  if (state.resultsSortedByTime) {
    const relevanceResults = state.relevanceOrderedResults.length ? state.relevanceOrderedResults : state.results;
    state.results = [...relevanceResults];
    state.resultsSortedByTime = false;
  } else {
    state.results = [...state.results].sort((left, right) => {
      const leftTime = resultChronologicalTimestamp(left);
      const rightTime = resultChronologicalTimestamp(right);
      if (leftTime !== rightTime) return leftTime - rightTime;
      return resultDocId(left).localeCompare(resultDocId(right));
    });
    state.resultsSortedByTime = true;
  }
  syncResultPayload();
  updateStatusFromPayload(state.lastPayload || { results: state.results });
  renderResults();
}

function renderContextPanel(result, contentText) {
  const fullText = contentText
    ? `
      <div class="context-block">
        <p class="context-label">命中片段</p>
        <p class="context-text">${highlightTerms(contentText, result.matched_terms || [])}</p>
      </div>
    `
    : "";
  const neighbors = Array.isArray(result.neighbor_atoms) ? result.neighbor_atoms : [];
  const neighborMarkup = neighbors.length
    ? `
      <div class="context-block">
        <p class="context-label">上下文</p>
        <div class="context-list">
          ${neighbors.map((atom) => `
            <div class="context-item">
              ${atom.display_timestamp ? `<span class="context-time">${escapeHtml(atom.display_timestamp)}</span>` : ""}
              <p class="context-text">${highlightTerms(atom.text || "", result.matched_terms || [])}</p>
            </div>
          `).join("")}
        </div>
      </div>
    `
    : "";
  return `
    <div class="result-context-panel">
      ${fullText}
      ${neighborMarkup}
    </div>
  `;
}

function toggleResultContext(docId) {
  if (!docId) return;
  if (state.autoExpandDetails) {
    if (state.collapsedResultDocIds.has(docId)) {
      state.collapsedResultDocIds.delete(docId);
    } else {
      state.collapsedResultDocIds.add(docId);
    }
  } else if (state.expandedResultDocIds.has(docId)) {
    state.expandedResultDocIds.delete(docId);
  } else {
    state.expandedResultDocIds.add(docId);
  }
  renderResults();
}

function renderMeta(meta) {
  state.meta = meta;
  document.title = meta.title || "机核电台记忆检索";
  els.heroKicker.textContent = meta.mode === "viewer" ? "Local Mode" : "Server Mode";
  els.pageTitle.textContent = meta.title || "机核电台记忆检索";
  const updatedAt = meta.library_updated_at || meta.manifest?.built_at;
  els.libraryNote.textContent = `本库更新于 ${formatDateLabel(updatedAt)}`;
  els.utilityButton.textContent = "关于本库";
  els.settingsButton.textContent = "设置";
  if (meta.mode === "viewer") {
    els.modePill.textContent = meta.mode_label || "本地版";
    els.modePill.classList.remove("hidden");
  } else {
    els.modePill.classList.add("hidden");
  }
  renderAboutPanel(meta);
  renderViewerStatus(meta.viewer);
  renderSearchSettingsControls();
}

function renderAboutPanel(meta) {
  const timelineCount = Number(meta.doc_type_counts?.timeline_note || meta.manifest?.doc_type_counts?.timeline_note || 0);
  const downloadValue = meta.download_url
    ? `<a class="text-link" href="${escapeHtml(meta.download_url)}" target="_blank" rel="noopener noreferrer">立即下载</a>`
    : "待发布";
  const entries = [
    ["mode_label", meta.mode_label || "-"],
    ["library_updated_at", formatDateLabel(meta.library_updated_at || meta.manifest?.built_at)],
    ["eligible_items", String(meta.eligible_items ?? meta.manifest?.eligible_items ?? "-")],
    ["timeline_notes", String(timelineCount || "-")],
    ["participants_count", String(meta.participants_count || 0)],
    ["database_size", formatBytes(meta.database_size_bytes)],
    ["viewer_download", downloadValue],
  ];
  els.aboutMetaList.innerHTML = `
    <div class="about-note-card">
      <strong>说明</strong>
      <p>本项目仅覆盖免费节目，会不定期更新。</p>
      <p>来自一个野生怀旧老机组。</p>
    </div>
    ${entries.map(([key, value]) => `
    <div class="meta-row">
      <dt>${escapeHtml(ABOUT_PANEL_LABELS[key] || key)}</dt>
      <dd>${key === "viewer_download" ? value : escapeHtml(value)}</dd>
    </div>
  `).join("")}
  `;
}

function renderViewerStatus(payload) {
  els.viewerSettingsBlock.classList.toggle("hidden", !payload);
  if (!payload) {
    els.viewerKeyStatus.textContent = "当前在线版不需要在浏览器里保存 Key。";
    return;
  }
  const secureText = payload.secure_store_supported ? "支持安全存储" : "当前平台未启用安全存储";
  const keyText = payload.effective_key_present ? "已加载可用 Key" : "尚未加载 Key";
  els.viewerKeyStatus.textContent = `${payload.platform} · ${secureText} · ${keyText}`;
}

function renderSearchSettingsControls() {
  els.resultLimit.value = String(state.resultLimit);
  els.autoExpandDetails.checked = Boolean(state.autoExpandDetails);
}

function applyResultLimitSetting({ rerun = true } = {}) {
  const nextLimit = clampInteger(els.resultLimit.value, {
    min: 1,
    max: MAX_RESULT_LIMIT,
    fallback: state.resultLimit,
  });
  state.resultLimit = nextLimit;
  els.resultLimit.value = String(nextLimit);
  writeStorageValue(RESULT_LIMIT_STORAGE_KEY, String(nextLimit));
  if (rerun && els.query.value.trim()) {
    runSearch();
  }
}

function applyAutoExpandSetting() {
  state.autoExpandDetails = Boolean(els.autoExpandDetails.checked);
  writeStorageValue(AUTO_EXPAND_STORAGE_KEY, state.autoExpandDetails ? "true" : "false");
  clearResultExpansionState();
  renderResults();
}

function renderFilters(filterPayload) {
  state.programTypes = filterPayload.program_types || [];
  state.participants = filterPayload.participants || [];
  renderProgramTypeList();
  renderParticipantList();
  renderFilterSummary();
  renderActiveFilters();
  renderFilterTriggerState();
}

function hasSelectedFilters() {
  return state.selectedProgramTypes.size > 0 || state.selectedParticipants.size > 0;
}

function selectedFilterSummary() {
  const parts = [];
  if (state.selectedProgramTypes.size) {
    parts.push(`${state.selectedProgramTypes.size} 个节目类型`);
  }
  if (state.selectedParticipants.size) {
    parts.push(`${state.selectedParticipants.size} 位参与者`);
  }
  return parts.join("，");
}

function sortFilterOptions(items, selectedSet) {
  return [...items].sort((left, right) => {
    const leftName = String(left.name || "");
    const rightName = String(right.name || "");
    const leftSelected = selectedSet.has(leftName) ? 1 : 0;
    const rightSelected = selectedSet.has(rightName) ? 1 : 0;
    if (leftSelected !== rightSelected) {
      return rightSelected - leftSelected;
    }
    const leftCount = Number(left.count || 0);
    const rightCount = Number(right.count || 0);
    if (leftCount !== rightCount) {
      return rightCount - leftCount;
    }
    return leftName.localeCompare(rightName, "zh-CN");
  });
}

function formatProgramTypeLabel(name) {
  const normalized = String(name || "").trim();
  return PROGRAM_TYPE_LABEL_OVERRIDES.get(normalized) || normalized;
}

function renderCheckboxFilterList({ items, selectedSet, inputName, target, emptyText, formatItemLabel }) {
  if (!items.length) {
    target.innerHTML = `<div class="participant-empty">${emptyText}</div>`;
    return;
  }
  target.innerHTML = items.map((item) => {
    const name = String(item.name || "");
    const labelText = formatItemLabel ? formatItemLabel(name, item) : name;
    const checked = selectedSet.has(name) ? "checked" : "";
    const pinnedClass = checked ? " is-selected" : "";
    return `
      <label class="participant-item${pinnedClass}">
        <span class="participant-main">
          <input type="checkbox" name="${inputName}" value="${escapeHtml(name)}" ${checked} />
          <span>${escapeHtml(labelText)}</span>
        </span>
        <span class="participant-count">${escapeHtml(String(item.count ?? 0))}</span>
      </label>
    `;
  }).join("");
}

function renderProgramTypeList() {
  renderCheckboxFilterList({
    items: sortFilterOptions(state.programTypes, state.selectedProgramTypes),
    selectedSet: state.selectedProgramTypes,
    inputName: "categories",
    target: els.programTypeList,
    formatItemLabel: (name) => formatProgramTypeLabel(name),
    emptyText: "暂无可用的节目类型。",
  });
}

function renderParticipantList() {
  const query = state.participantQuery.trim().toLowerCase();
  const filtered = sortFilterOptions(
    state.participants.filter((participant) => !query || String(participant.name || "").toLowerCase().includes(query)),
    state.selectedParticipants,
  );
  renderCheckboxFilterList({
    items: filtered,
    selectedSet: state.selectedParticipants,
    inputName: "participants",
    target: els.participantList,
    emptyText: "没有匹配的参与者。",
  });
}

function renderFilterSummary() {
  const summary = selectedFilterSummary();
  els.filterSummaryText.textContent = summary
    ? `已筛选 ${summary}。`
    : `共 ${state.programTypes.length} 个节目类型，${state.participants.length} 位参与者，当前未筛选。`;
  els.clearFilters.classList.toggle("hidden", !hasSelectedFilters());
}

function renderActiveFilters() {
  const chips = [
    ...Array.from(state.selectedProgramTypes).map((name) => ({
      kind: "category",
      value: name,
      label: `节目：${name}`,
    })),
    ...Array.from(state.selectedParticipants).map((name) => ({
      kind: "participant",
      value: name,
      label: `参与者：${name}`,
    })),
  ];
  chips.forEach((chip) => {
    if (chip.kind === "category") {
      chip.label = formatProgramTypeLabel(chip.value);
    }
  });
  if (!chips.length) {
    els.activeFilters.classList.add("hidden");
    els.activeFilters.innerHTML = "";
    return;
  }
  els.activeFilters.classList.remove("hidden");
  els.activeFilters.innerHTML = `
    <span class="active-filter-label">当前筛选</span>
    ${chips.map((chip) => `
      <button
        type="button"
        class="active-filter-chip"
        data-remove-filter-kind="${escapeHtml(chip.kind)}"
        data-remove-filter-value="${escapeHtml(chip.value)}"
      >${escapeHtml(chip.label)}</button>
    `).join("")}
  `;
}

function renderFilterTriggerState() {
  const summary = selectedFilterSummary();
  const label = summary ? `筛选条件，已选 ${summary}` : "筛选条件";
  els.filterButton.classList.toggle("has-selection", hasSelectedFilters());
  els.filterButton.setAttribute("aria-label", label);
  els.filterButton.title = label;
}

function setFilterTab(tab) {
  const resolved = tab === "participants" ? "participants" : "categories";
  state.activeFilterTab = resolved;
  els.filterTabs?.querySelectorAll("[data-filter-tab]").forEach((button) => {
    const isActive = button.dataset.filterTab === resolved;
    button.classList.toggle("is-active", isActive);
    button.setAttribute("aria-selected", String(isActive));
  });
  els.filterDropdown?.querySelectorAll("[data-filter-panel]").forEach((panel) => {
    const isActive = panel.dataset.filterPanel === resolved;
    panel.classList.toggle("is-active", isActive);
  });
}

function renderOffline(errorText) {
  setSearchAvailability(false);
  els.offlineMessage.textContent = errorText || state.runtimeConfig.offlineMessage;
  if (state.runtimeConfig.downloadUrl) {
    els.offlineDownloadLink.href = state.runtimeConfig.downloadUrl;
    els.offlineDownloadLink.classList.remove("hidden");
  } else {
    els.offlineDownloadLink.classList.add("hidden");
  }
  els.offlineNotice.classList.remove("hidden");
}

function setSearchAvailability(isAvailable) {
  state.online = isAvailable;
  els.searchSubmit.classList.toggle("is-unavailable", !isAvailable);
  els.searchSubmit.setAttribute("aria-disabled", String(!isAvailable));
  els.searchSubmit.title = isAvailable ? "" : BACKEND_MAINTENANCE_MESSAGE;
}

function hideBackendToast() {
  window.clearTimeout(backendToastTimer);
  els.backendToast.classList.remove("is-visible");
}

function showBackendToast({ autoHide = true } = {}) {
  els.backendToast.textContent = BACKEND_MAINTENANCE_MESSAGE;
  els.backendToast.classList.add("is-visible");
  window.clearTimeout(backendToastTimer);
  if (autoHide) {
    backendToastTimer = window.setTimeout(() => {
      els.backendToast.classList.remove("is-visible");
    }, 2600);
  }
}

function markBackendUnavailable(message = BACKEND_MAINTENANCE_MESSAGE) {
  renderOffline(message);
  els.statusLine.textContent = message;
  showBackendToast();
}

function shouldEnterMaintenanceMode(error, statusCode) {
  if ([502, 503, 504].includes(statusCode)) return true;
  if (!(error instanceof Error)) return false;
  return /failed to fetch|networkerror|load failed/i.test(error.message);
}

function setScope(scope) {
  state.scope = scope;
  els.scopeSwitch.querySelectorAll(".scope-chip").forEach((button) => {
    button.classList.toggle("is-active", button.dataset.scope === scope);
  });
}

function openFilterDropdown() {
  state.filterDropdownOpen = true;
  els.filterDropdown.classList.remove("hidden");
  els.filterButton.classList.add("is-open");
  setFilterTab(state.activeFilterTab);
}

function closeFilterDropdown() {
  state.filterDropdownOpen = false;
  els.filterDropdown.classList.add("hidden");
  els.filterButton.classList.remove("is-open");
}

function toggleFilterDropdown() {
  if (state.filterDropdownOpen) {
    closeFilterDropdown();
  } else {
    openFilterDropdown();
  }
}

function openSheet(kind) {
  state.activeSheet = kind;
  els.sheetRoot.classList.remove("hidden");
  els.sheetRoot.setAttribute("aria-hidden", "false");
  els.aboutPanel.classList.toggle("hidden", kind !== "about");
  els.settingsPanel.classList.toggle("hidden", kind !== "settings");
  if (kind === "settings") {
    els.sheetKicker.textContent = "偏好设置";
    els.sheetTitle.textContent = "设置";
    renderSearchSettingsControls();
  } else {
    els.sheetKicker.textContent = "关于本库";
    els.sheetTitle.textContent = "当前索引";
  }
}

function closeSheet() {
  state.activeSheet = null;
  els.sheetRoot.classList.add("hidden");
  els.sheetRoot.setAttribute("aria-hidden", "true");
}

function emptyResultMessage() {
  if (!state.lastPayload) {
    return "从一段记忆开始搜索。";
  }
  if (isDegradedPayload(state.lastPayload)) {
    return `${degradedSearchMessage(state.lastPayload)}没有找到关键词降级结果。`;
  }
  return "没有找到明显匹配，换一个更具体的线索再试一次。";
}

function renderResultCard(result) {
  const image = resultImage(result);
  const metaLine = composeMetaLine(result);
  const isTimeline = result.doc_type === "timeline_note";
  const isSemanticHit = !((result.matched_terms || []).length);
  const coverMarkup = image
    ? `<img class="result-cover" src="${escapeHtml(image)}" alt="${escapeHtml(result.item_title || "节目封面")}" loading="lazy" />`
    : `<div class="result-cover is-fallback" aria-hidden="true"></div>`;
  const contentText = result.display_text || (result.doc_type === "timeline_note" ? result.timeline_content : "");
  const canExpand = hasExpandableContext(result, contentText);
  const isExpanded = isResultExpanded(result, contentText);
  const timelineFeature = isTimeline && result.timeline_asset_url
    ? `
      <div class="timeline-feature">
        <img
          class="timeline-feature-image"
          src="${escapeHtml(proxiedMediaAsset(result.timeline_asset_url))}"
          alt="${escapeHtml(result.timeline_title || result.item_title || "时间轴图片")}"
          loading="lazy"
        />
        ${result.timeline_title ? `<p class="timeline-feature-title">${escapeHtml(result.timeline_title)}</p>` : ""}
      </div>
    `
    : "";
  return `
    <article class="result-card" data-item-id="${escapeHtml(result.item_id)}">
      <div class="result-summary">
        ${coverMarkup}
        <div class="result-copy${isTimeline ? " is-timeline" : ""}">
          <div class="result-topline">
            <span>${escapeHtml(docLabel(result))}</span>
            ${result.display_timestamp ? `<span class="time-pill">${escapeHtml(result.display_timestamp)}</span>` : ""}
          </div>
          <h3>${escapeHtml(result.item_title || "未命名节目")}</h3>
          ${metaLine ? `<p class="result-meta">${escapeHtml(metaLine)}</p>` : ""}
          ${timelineFeature}
          ${contentText ? `<p class="result-excerpt">${highlightTerms(contentText, result.matched_terms || [])}</p>` : ""}
          ${result.match_summary ? `<p class="result-summary-text${isSemanticHit ? " is-semantic-hit" : ""}">${escapeHtml(result.match_summary)}</p>` : ""}
          ${isExpanded ? renderContextPanel(result, contentText) : ""}
          <div class="result-actions">
            ${canExpand ? `<button type="button" class="inline-button" data-toggle-context="${escapeHtml(result.doc_id)}">${isExpanded ? "收起上下文" : "展开更多上下文"}</button>` : ""}
            ${isTimeline && result.timeline_quote_href ? `<a class="text-link" href="${escapeHtml(result.timeline_quote_href)}" target="_blank" rel="noopener noreferrer">查看相关链接</a>` : ""}
            ${result.source_url ? `<a class="text-link" href="${escapeHtml(result.source_url)}" target="_blank" rel="noopener noreferrer">打开原页</a>` : ""}
          </div>
        </div>
      </div>
    </article>
  `;
}

function renderResultTools() {
  const count = (state.results || []).length;
  const canSort = count > 1;
  els.resultSortTime.classList.toggle("hidden", !canSort);
  if (!canSort) return;
  els.resultSortTime.textContent = state.resultsSortedByTime
    ? `恢复按语义相关度排序（当前 ${count} 个）`
    : `将当前 ${count} 个结果按时间顺序排列`;
}

function renderResults() {
  const results = state.results || [];
  renderResultTools();
  els.results.innerHTML = `${renderDegradedNotice(state.lastPayload)}${results.map(renderResultCard).join("")}`;
  els.emptyState.classList.toggle("hidden", results.length > 0);
  if (!results.length) {
    els.statusLine.textContent = emptyResultMessage();
  }
}

function isDegradedPayload(payload) {
  return Boolean(payload && (payload.degraded || payload.recall_stats?.lexical_fallback));
}

function degradedSearchMessage(payload) {
  if (payload?.degraded_message) return String(payload.degraded_message);
  const semanticError = String(payload?.semantic_error || "");
  if (/1113|余额不足|资源包/.test(semanticError)) {
    return "语义检索额度不足，当前显示的是关键词降级结果，相关性和排序质量会明显下降。";
  }
  if (/timed out|timeout/i.test(semanticError)) {
    return "语义检索服务响应超时，当前显示的是关键词降级结果，相关性和排序质量会下降。";
  }
  return DEGRADED_SEARCH_DEFAULT_MESSAGE;
}

function renderDegradedNotice(payload) {
  if (!isDegradedPayload(payload)) return "";
  const message = degradedSearchMessage(payload);
  const detail = payload?.degraded_detail || "本次没有使用向量语义召回，只使用本地关键词索引进行降级检索。";
  return `
    <section class="degraded-notice" role="alert" aria-live="assertive">
      <div class="degraded-notice-badge">降级检索</div>
      <div>
        <strong>当前结果已降级，不是完整语义检索结果。</strong>
        <p>${escapeHtml(message)}</p>
        <p class="degraded-notice-detail">${escapeHtml(detail)}</p>
      </div>
    </section>
  `;
}

function updateStatusFromPayload(payload) {
  const count = payload.results?.length || 0;
  if (!count) {
    els.statusLine.textContent = emptyResultMessage();
    return;
  }
  const baseText = isDegradedPayload(payload)
    ? `降级检索：这里是前 ${count} 条关键词结果，未使用完整语义召回`
    : `这里是相关度前 ${count} 的结果`;
  const summary = selectedFilterSummary();
  const filters = summary ? ` · 已筛选 ${summary}` : "";
  const sortHint = state.resultsSortedByTime ? " · 已按时间顺序排列" : "";
  const degradedHint = isDegradedPayload(payload) ? `。${degradedSearchMessage(payload)}` : "";
  els.statusLine.textContent = `${baseText}${filters}${sortHint}${degradedHint}`;
}

async function loadRuntimeConfig() {
  try {
    const response = await fetch("./assets/runtime-config.json", { cache: "no-store" });
    if (!response.ok) return;
    const payload = await response.json();
    state.runtimeConfig = { ...state.runtimeConfig, ...payload };
  } catch {
    // keep defaults
  }
}

async function loadMetaAndParticipants() {
  const metaResponse = await fetch(apiUrl("/api/meta"), { cache: "no-store" });
  if (!metaResponse.ok) {
    throw new Error(`meta HTTP ${metaResponse.status}`);
  }
  const metaPayload = await metaResponse.json();
  renderMeta(metaPayload);

  const participantsResponse = await fetch(apiUrl("/api/participants"), { cache: "no-store" });
  if (!participantsResponse.ok) {
    throw new Error(`participants HTTP ${participantsResponse.status}`);
  }
  const participantsPayload = await participantsResponse.json();
  renderFilters(participantsPayload);
}

function buildSearchParams(query) {
  const params = new URLSearchParams();
  params.set("q", query);
  params.set("scope", state.scope);
  params.set("limit", String(state.resultLimit));
  const docTypes = state.scope === "timeline"
    ? ["timeline_note"]
    : state.scope === "title"
      ? ["item_title"]
      : ["scene_summary", "episode_card"];
  for (const docType of docTypes) {
    params.append("doc_types", docType);
  }
  for (const category of state.selectedProgramTypes) {
    params.append("categories", category);
  }
  for (const participant of state.selectedParticipants) {
    params.append("participants", participant);
  }
  return params;
}

function searchErrorMessage(payload, statusCode) {
  const raw = payload?.error || `搜索失败 (${statusCode})`;
  if (state.meta?.mode === "viewer" && /zhipu|api[_ ]?key|embedding/i.test(raw)) {
    openSheet("settings");
    return "本地版还没有可用的智谱 Key。请先在“设置”里保存后再搜索。";
  }
  return raw;
}

async function runSearch(event) {
  if (event) event.preventDefault();
  if (!state.online) {
    showBackendToast();
    els.statusLine.textContent = BACKEND_MAINTENANCE_MESSAGE;
    return;
  }
  const query = els.query.value.trim();
  if (!query) return;
  els.statusLine.textContent = "正在搜索...";
  const params = buildSearchParams(query);
  try {
    const response = await fetch(`${apiUrl("/api/search")}?${params.toString()}`, { cache: "no-store" });
    const rawText = await response.text();
    let payload = {};
    if (rawText) {
      try {
        payload = JSON.parse(rawText);
      } catch {
        payload = { error: rawText };
      }
    }
    if (!response.ok) {
      const message = searchErrorMessage(payload, response.status);
      els.statusLine.textContent = message;
      if (shouldEnterMaintenanceMode(null, response.status)) {
        markBackendUnavailable();
      }
      return;
    }
    state.lastPayload = payload;
    state.results = payload.results || [];
    state.relevanceOrderedResults = [...state.results];
    state.resultsSortedByTime = false;
    clearResultExpansionState();
    updateStatusFromPayload(payload);
    renderResults();
  } catch (error) {
    if (shouldEnterMaintenanceMode(error)) {
      markBackendUnavailable();
      return;
    }
    els.statusLine.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function saveViewerKey(event) {
  event.preventDefault();
  const apiKey = els.viewerApiKey.value.trim();
  if (!apiKey) {
    els.viewerKeyStatus.textContent = "请先输入智谱 API Key。";
    return;
  }
  try {
    const response = await fetch(apiUrl("/api/local/key"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_key: apiKey,
        session_only: Boolean(els.viewerSessionOnly.checked),
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      els.viewerKeyStatus.textContent = payload.error || `保存失败 (${response.status})`;
      return;
    }
    els.viewerApiKey.value = "";
    els.viewerSessionOnly.checked = false;
    renderViewerStatus(payload);
  } catch (error) {
    els.viewerKeyStatus.textContent = error instanceof Error ? error.message : String(error);
  }
}

async function clearViewerKey() {
  try {
    const response = await fetch(apiUrl("/api/local/key"), { method: "DELETE" });
    const payload = await response.json();
    if (!response.ok) {
      els.viewerKeyStatus.textContent = payload.error || `清除失败 (${response.status})`;
      return;
    }
    renderViewerStatus(payload);
  } catch (error) {
    els.viewerKeyStatus.textContent = error instanceof Error ? error.message : String(error);
  }
}

function removeSelectedFilter(kind, value) {
  if (kind === "category") {
    state.selectedProgramTypes.delete(value);
    renderProgramTypeList();
  } else if (kind === "participant") {
    state.selectedParticipants.delete(value);
    renderParticipantList();
  } else {
    return;
  }
  renderFilterSummary();
  renderActiveFilters();
  renderFilterTriggerState();
  if (els.query.value.trim()) {
    runSearch();
  }
}

function handleProgramTypeToggle(target) {
  const name = target.value;
  if (!name) return;
  if (target.checked) {
    state.selectedProgramTypes.add(name);
  } else {
    state.selectedProgramTypes.delete(name);
  }
  renderProgramTypeList();
  renderFilterSummary();
  renderActiveFilters();
  renderFilterTriggerState();
  if (els.query.value.trim()) {
    runSearch();
  }
}

function handleParticipantToggle(target) {
  const name = target.value;
  if (!name) return;
  if (target.checked) {
    state.selectedParticipants.add(name);
  } else {
    state.selectedParticipants.delete(name);
  }
  renderParticipantList();
  renderFilterSummary();
  renderActiveFilters();
  renderFilterTriggerState();
  if (els.query.value.trim()) {
    runSearch();
  }
}

function handleScopeClick(target) {
  const scope = target.dataset.scope;
  if (!scope || scope === state.scope) return;
  setScope(scope);
  if (els.query.value.trim()) {
    runSearch();
  }
}

function bindEvents() {
  els.searchForm.addEventListener("submit", runSearch);
  els.searchSubmit.addEventListener("mouseenter", () => {
    if (!state.online) {
      showBackendToast({ autoHide: false });
    }
  });
  els.searchSubmit.addEventListener("mouseleave", hideBackendToast);
  els.searchSubmit.addEventListener("focus", () => {
    if (!state.online) {
      showBackendToast({ autoHide: false });
    }
  });
  els.searchSubmit.addEventListener("blur", hideBackendToast);
  els.searchSubmit.addEventListener("pointerdown", (event) => {
    if (!state.online && event.pointerType !== "mouse") {
      showBackendToast();
    }
  });
  els.searchSubmit.addEventListener("click", (event) => {
    if (!state.online) {
      event.preventDefault();
      showBackendToast();
    }
  });
  els.scopeSwitch.addEventListener("click", (event) => {
    const button = event.target.closest("[data-scope]");
    if (!button) return;
    handleScopeClick(button);
  });
  els.filterButton.addEventListener("click", (event) => {
    event.stopPropagation();
    toggleFilterDropdown();
  });
  els.filterTabs?.addEventListener("click", (event) => {
    const button = event.target.closest("[data-filter-tab]");
    if (!button) return;
    setFilterTab(button.dataset.filterTab || "categories");
  });
  els.utilityButton.addEventListener("click", () => openSheet("about"));
  els.settingsButton.addEventListener("click", () => openSheet("settings"));
  els.sheetBackdrop.addEventListener("click", closeSheet);
  els.sheetClose.addEventListener("click", closeSheet);
  els.resultLimit.addEventListener("change", () => applyResultLimitSetting());
  els.resultLimit.addEventListener("blur", () => applyResultLimitSetting({ rerun: false }));
  els.autoExpandDetails.addEventListener("change", applyAutoExpandSetting);
  els.resultSortTime.addEventListener("click", toggleCurrentResultsTimeSort);
  els.clearFilters.addEventListener("click", () => {
    state.selectedProgramTypes.clear();
    state.selectedParticipants.clear();
    renderProgramTypeList();
    renderParticipantList();
    renderFilterSummary();
    renderActiveFilters();
    renderFilterTriggerState();
    if (els.query.value.trim()) {
      runSearch();
    }
  });
  els.programTypeList.addEventListener("change", (event) => {
    const input = event.target.closest('input[name="categories"]');
    if (!input) return;
    handleProgramTypeToggle(input);
  });
  els.participantList.addEventListener("change", (event) => {
    const input = event.target.closest('input[name="participants"]');
    if (!input) return;
    handleParticipantToggle(input);
  });
  els.participantSearch.addEventListener("input", () => {
    setFilterTab("participants");
    state.participantQuery = els.participantSearch.value || "";
    renderParticipantList();
  });
  els.participantSearch.addEventListener("focus", () => {
    setFilterTab("participants");
  });
  els.filterDropdown.addEventListener("click", (event) => {
    event.stopPropagation();
  });
  els.activeFilters.addEventListener("click", (event) => {
    const button = event.target.closest("[data-remove-filter-kind]");
    if (!button) return;
    removeSelectedFilter(button.dataset.removeFilterKind || "", button.dataset.removeFilterValue || "");
  });
  els.results.addEventListener("click", (event) => {
    const button = event.target.closest("[data-toggle-context]");
    if (!button) return;
    toggleResultContext(button.dataset.toggleContext || "");
  });
  els.viewerKeyForm.addEventListener("submit", saveViewerKey);
  els.viewerClearKey.addEventListener("click", clearViewerKey);
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && state.activeSheet) {
      closeSheet();
      return;
    }
    if (event.key === "Escape" && state.filterDropdownOpen) {
      closeFilterDropdown();
    }
  });
  document.addEventListener("click", (event) => {
    if (!state.filterDropdownOpen) return;
    if (els.searchBox.contains(event.target)) return;
    closeFilterDropdown();
  });
}

async function bootstrap() {
  bindEvents();
  setSearchAvailability(true);
  setScope(state.scope);
  renderSearchSettingsControls();
  await loadRuntimeConfig();
  try {
    await loadMetaAndParticipants();
  } catch (error) {
    console.error("Failed to load search backend", error);
    renderOffline(BACKEND_MAINTENANCE_MESSAGE);
    els.statusLine.textContent = BACKEND_MAINTENANCE_MESSAGE;
  }
}

bootstrap();
