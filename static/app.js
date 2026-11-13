/* SQL 数据空间分析台 —— 前端逻辑（原生 JS，无依赖） */
"use strict";

const state = {
  spaces: [],
  currentSpace: null,
  tables: [],
  config: null,
  sampleQueries: null,
  currentResult: null,   // 最近一次查询结果
  polling: null,         // 取消后轮询用
  offset: 0,
  pageSize: 50,
};

const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function api(path, options = {}) {
  const resp = await fetch(path, {
    headers: options.body instanceof FormData ? {} : { "Content-Type": "application/json" },
    ...options,
  });
  let payload;
  try {
    payload = await resp.json();
  } catch (e) {
    throw new Error(`服务端返回了非 JSON 响应（HTTP ${resp.status}）`);
  }
  if (!payload.ok) {
    const err = new Error(payload.error?.message || "请求失败");
    err.detail = payload.error || {};
    err.status = resp.status;
    throw err;
  }
  return payload.data;
}

const STOP_REASON_TEXT = {
  completed: ["正常完成", "badge-ok"],
  row_limit: ["达到行数上限", "badge-warn"],
  timeout: ["执行超时中止", "badge-red"],
  cancelled: ["用户取消", "badge-purple"],
  denied: ["安全策略拦截", "badge-red"],
  error: ["执行错误", "badge-red"],
};

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------
async function init() {
  state.config = await api("/api/config");
  $("#limits-badge").innerHTML =
    `单次查询 <b>${state.config.query_timeout_ms} ms</b> · 最多 <b>${state.config.query_max_rows.toLocaleString()}</b> 行 · 导入 ≤ ${(state.config.import_max_bytes / 1048576).toFixed(0)} MB`;
  state.pageSize = state.config.page_size_options[1] || 50;
  $("#page-size").innerHTML = state.config.page_size_options
    .map((n) => `<option value="${n}"${n === state.pageSize ? " selected" : ""}>${n}</option>`).join("");

  state.sampleQueries = await api("/api/sample-queries");
  fillSampleSelect();
  bindEvents();
  await loadSpaces();
}

function fillSampleSelect() {
  const fill = (id, items) => {
    $(id).innerHTML = items.map((q, i) =>
      `<option value="${id}|${i}">${esc(q.title)}</option>`).join("");
  };
  fill("#opt-join", state.sampleQueries.join);
  fill("#opt-attack", state.sampleQueries.attack);
  fill("#opt-stress", state.sampleQueries.stress);
}

function bindEvents() {
  $("#btn-new-space").addEventListener("click", () => {
    $("#new-space-name").value = "";
    $("#dlg-space").showModal();
  });
  $("#dlg-space").addEventListener("close", async () => {
    if ($("#dlg-space").returnValue !== "ok") return;
    const name = $("#new-space-name").value.trim();
    if (!name) return;
    const space = await api("/api/spaces", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    await loadSpaces();
    selectSpace(space.id);
  });

  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => switchTab(tab.dataset.tab));
  });

  $("#sample-select").addEventListener("change", (e) => {
    const val = e.target.value;
    if (!val) return;
    const [groupId, idx] = val.split("|");
    const key = groupId.replace("#opt-", "");
    const q = state.sampleQueries[key][Number(idx)];
    $("#sql-editor").value = q.sql;
    if (q.expect) {
      $("#query-hint").textContent = `预期：${q.expect}`;
    }
  });

  $("#btn-run").addEventListener("click", runQuery);
  $("#btn-cancel").addEventListener("click", cancelQuery);
  $("#sql-editor").addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      runQuery();
    }
  });

  $("#btn-import").addEventListener("click", doImport);
  $("#btn-sample").addEventListener("click", loadSampleData);
  $("#btn-refresh-history").addEventListener("click", loadHistory);

  $("#btn-prev").addEventListener("click", () => turnPage(-1));
  $("#btn-next").addEventListener("click", () => turnPage(1));
  $("#page-size").addEventListener("change", (e) => {
    state.pageSize = Number(e.target.value);
    state.offset = 0;
    loadPage();
  });
  $("#btn-download").addEventListener("click", downloadCsv);
}

function switchTab(name) {
  document.querySelectorAll(".tab").forEach((t) =>
    t.classList.toggle("active", t.dataset.tab === name));
  ["query", "import", "history"].forEach((n) => {
    $(`#tab-${n}`).hidden = n !== name;
  });
  if (name === "history") loadHistory();
}

// ---------------------------------------------------------------------------
// 空间管理
// ---------------------------------------------------------------------------
async function loadSpaces() {
  state.spaces = await api("/api/spaces");
  const ul = $("#space-list");
  if (!state.spaces.length) {
    ul.innerHTML = `<li class="hint" style="cursor:default">还没有空间</li>`;
    return;
  }
  ul.innerHTML = state.spaces.map((s) => `
    <li data-id="${s.id}" class="${s.id === state.currentSpace?.id ? "active" : ""}">
      <span>
        <span class="sp-name">${esc(s.name)}</span><br>
        <span class="sp-date">${new Date(s.created_at * 1000).toLocaleString("zh-CN")}</span>
      </span>
      <button class="sp-del" data-del="${s.id}" title="删除空间">✕</button>
    </li>`).join("");
  ul.querySelectorAll("li[data-id]").forEach((li) => {
    li.addEventListener("click", (e) => {
      if (e.target.dataset.del) return;
      selectSpace(li.dataset.id);
    });
  });
  ul.querySelectorAll("[data-del]").forEach((btn) => {
    btn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const id = btn.dataset.del;
      if (!confirm("删除该空间及其全部数据表与历史？此操作不可恢复。")) return;
      await api("/api/spaces", { method: "DELETE", body: JSON.stringify({ space_id: id }) });
      if (state.currentSpace?.id === id) {
        state.currentSpace = null;
        $("#schema-card").hidden = true;
        $("#work-card").hidden = true;
        $("#empty-state").hidden = false;
      }
      await loadSpaces();
    });
  });
}

async function selectSpace(id) {
  state.currentSpace = state.spaces.find((s) => s.id === id);
  $("#schema-card").hidden = false;
  $("#work-card").hidden = false;
  $("#empty-state").hidden = true;
  document.querySelectorAll("#space-list li").forEach((li) =>
    li.classList.toggle("active", li.dataset.id === id));
  resetWorkspace();
  await Promise.all([loadSchema(), loadHistory()]);
}

function resetWorkspace() {
  state.currentResult = null;
  state.offset = 0;
  $("#result-wrap").hidden = true;
  $("#pager").hidden = true;
  $("#result-meta").hidden = true;
  $("#query-error").hidden = true;
  $("#import-error").hidden = true;
  $("#import-ok").hidden = true;
}

// ---------------------------------------------------------------------------
// 表结构
// ---------------------------------------------------------------------------
async function loadSchema() {
  state.tables = await api(`/api/spaces/${state.currentSpace.id}/tables`);
  const tree = $("#schema-tree");
  if (!state.tables.length) {
    tree.innerHTML = `<p class="hint">暂无数据表，去「导入 CSV」或点击「载入样本」</p>`;
    return;
  }
  tree.innerHTML = state.tables.map((t) => `
    <div class="table-block">
      <div class="tbl-head">
        <span>🗄 ${esc(t.name)} <span class="tbl-meta">${t.row_count} 行</span></span>
        <button class="drop" data-drop="${esc(t.name)}">删除</button>
      </div>
      ${t.columns.map((c) => `
        <div class="col-row">
          <span>${esc(c.name)}${c.pk ? " 🔑" : ""}</span>
          <span class="col-type">${esc(c.type)}</span>
        </div>`).join("")}
    </div>`).join("");
  tree.querySelectorAll("[data-drop]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const name = btn.dataset.drop;
      if (!confirm(`删除表 ${name}？`)) return;
      await api(`/api/spaces/${state.currentSpace.id}/tables/${encodeURIComponent(name)}`,
        { method: "DELETE" });
      await loadSchema();
    });
  });
}

// ---------------------------------------------------------------------------
// 导入
// ---------------------------------------------------------------------------
async function doImport() {
  $("#import-error").hidden = true;
  $("#import-ok").hidden = true;
  const fileInput = $("#import-file");
  const tableName = $("#import-table").value.trim();
  if (!fileInput.files.length) {
    return showImportError("请先选择 CSV 文件");
  }
  const fd = new FormData();
  fd.append("file", fileInput.files[0]);
  if (tableName) fd.append("table_name", tableName);
  $("#btn-import").disabled = true;
  $("#import-hint").textContent = "上传与校验中…";
  try {
    const result = await api(`/api/spaces/${state.currentSpace.id}/import`, {
      method: "POST",
      body: fd,
    });
    $("#import-ok").hidden = false;
    $("#import-ok").innerHTML =
      `✅ 建表成功：<b>${esc(result.table)}</b>，${result.row_count} 行，` +
      result.columns.map((c) => `<code>${esc(c.name)}:${esc(c.type)}</code>`).join(" ");
    $("#import-table").value = "";
    fileInput.value = "";
    await loadSchema();
  } catch (err) {
    showImportError(err.message);
  } finally {
    $("#btn-import").disabled = false;
    $("#import-hint").textContent = "";
  }
}

function showImportError(msg) {
  const box = $("#import-error");
  box.hidden = false;
  box.textContent = `❌ ${msg}`;
}

async function loadSampleData() {
  if (!confirm("将向当前空间导入 4 张样本表（customers/products/orders/order_items），已存在的同名表会报错，继续？")) {
    return;
  }
  $("#btn-sample").disabled = true;
  try {
    const result = await api(`/api/spaces/${state.currentSpace.id}/sample`, { method: "POST" });
    await loadSchema();
    alert(`样本已载入：${result.tables.join("、")}`);
  } catch (err) {
    alert(`载入失败：${err.message}`);
  } finally {
    $("#btn-sample").disabled = false;
  }
}

// ---------------------------------------------------------------------------
// 查询（异步提交 + 轮询，保证运行中即可取消）
// ---------------------------------------------------------------------------
let pollTimer = null;

async function runQuery() {
  if (!state.currentSpace) return;
  const sql = $("#sql-editor").value;
  $("#query-error").hidden = true;
  $("#result-wrap").hidden = true;
  $("#pager").hidden = true;
  $("#result-meta").hidden = true;
  $("#btn-run").disabled = true;
  $("#btn-cancel").disabled = true;
  $("#query-hint").textContent = "提交中…";

  let submitted;
  try {
    submitted = await api(`/api/spaces/${state.currentSpace.id}/query`, {
      method: "POST",
      body: JSON.stringify({ sql }),
    });
  } catch (err) {
    renderQueryError(err);
    state.currentResult = null;
    $("#btn-run").disabled = false;
    loadHistory();
    return;
  }

  // 拿到 query_id 后立即允许取消（查询在后台线程运行）
  state.currentResult = { query_id: submitted.query_id };
  $("#btn-cancel").disabled = false;
  $("#query-hint").textContent = "执行中…（可随时取消）";
  pollStatus(submitted.query_id);
}

function pollStatus(queryId) {
  clearTimeout(pollTimer);
  const tick = async () => {
    try {
      const status = await api(
        `/api/spaces/${state.currentSpace.id}/queries/${queryId}`);
      if (status.status === "running") {
        pollTimer = setTimeout(tick, 250);
        return;
      }
      finishPolling();
      if (status.status === "completed") {
        state.currentResult = status;
        state.offset = 0;
        renderResultMeta(status);
        renderTable(status.page.columns, status.page.rows);
        updatePager(status.page);
      } else {
        const err = new Error(status.error?.message || "查询失败");
        err.detail = status.error || {};
        renderQueryError(err);
        state.currentResult = { query_id: queryId };
      }
      loadHistory();
    } catch (err) {
      finishPolling();
      renderQueryError(err);
    }
  };
  pollTimer = setTimeout(tick, 200);
}

function finishPolling() {
  clearTimeout(pollTimer);
  pollTimer = null;
  $("#btn-run").disabled = false;
  $("#btn-cancel").disabled = true;
  $("#query-hint").textContent = "";
}

async function cancelQuery() {
  const r = state.currentResult;
  if (!r?.query_id) return;
  $("#btn-cancel").disabled = true;
  try {
    await api(`/api/spaces/${state.currentSpace.id}/cancel`, {
      method: "POST",
      body: JSON.stringify({ query_id: r.query_id }),
    });
    $("#query-hint").textContent = "已请求取消，等待服务端中止…";
    // 若轮询显示仍在运行，恢复按钮允许再次点击
    setTimeout(() => {
      if (pollTimer) $("#btn-cancel").disabled = false;
    }, 600);
  } catch (err) {
    $("#query-hint").textContent = err.message;
    $("#btn-cancel").disabled = false;
  }
}

function renderQueryError(err) {
  const box = $("#query-error");
  box.hidden = false;
  const d = err.detail || {};
  let html = `<div class="err-title">❌ ${esc(err.message)}</div>`;
  if (d.stop_reason && STOP_REASON_TEXT[d.stop_reason]) {
    const [text, cls] = STOP_REASON_TEXT[d.stop_reason];
    html += `<p><span class="badge ${cls}">${text}</span></p>`;
  }
  if (d.statement_number) {
    html += `<p>定位：第 <b>${d.statement_number}</b> 条语句</p>`;
  }
  if (d.statement) {
    html += `<p><code>${esc(d.statement)}</code></p>`;
  }
  box.innerHTML = html;
  $("#query-hint").textContent = "";
}

function renderResultMeta(result) {
  const box = $("#result-meta");
  box.hidden = false;
  const truncBadge = result.truncated
    ? `<span class="badge badge-warn">结果已截断：仅返回前 ${result.max_rows.toLocaleString()} 行</span>`
    : `<span class="badge badge-ok">完整结果</span>`;
  const reason = STOP_REASON_TEXT[result.stop_reason]?.[0] || result.stop_reason;
  box.innerHTML = `
    <span>返回 <b>${result.row_count.toLocaleString()}</b> 行 × ${result.columns.length} 列</span>
    <span>耗时 <b>${result.duration_ms}</b> ms</span>
    <span>停止原因：<b>${esc(reason)}</b></span>
    ${truncBadge}`;
}

function renderTable(columns, rows) {
  const table = $("#result-table");
  table.innerHTML =
    `<thead><tr>${columns.map((c) => `<th>${esc(c)}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.map((row) => `<tr>${row.map((v) =>
      v === null || v === undefined
        ? `<td class="null-val">NULL</td>`
        : `<td title="${esc(typeof v === "object" ? JSON.stringify(v) : v)}">${esc(v)}</td>`
    ).join("")}</tr>`).join("")}</tbody>`;
  $("#result-wrap").hidden = false;
}

async function loadPage() {
  if (!state.currentResult) return;
  const { result_id } = state.currentResult;
  const page = await api(
    `/api/spaces/${state.currentSpace.id}/results/${result_id}` +
    `?offset=${state.offset}&limit=${state.pageSize}`);
  renderTable(page.columns, page.rows);
  updatePager(page);
}

function updatePager(page) {
  $("#pager").hidden = false;
  const from = page.total === 0 ? 0 : page.offset + 1;
  const to = Math.min(page.offset + page.rows.length, page.total);
  $("#page-info").textContent = `第 ${from}–${to} 行 / 共 ${page.total.toLocaleString()} 行`;
  $("#btn-prev").disabled = page.offset === 0;
  $("#btn-next").disabled = !page.has_more;
}

function turnPage(delta) {
  state.offset = Math.max(0, state.offset + delta * state.pageSize);
  loadPage();
}

function downloadCsv() {
  if (!state.currentResult) return;
  // 直接走浏览器导航下载；响应头标明截断信息
  const url = `/api/spaces/${state.currentSpace.id}/results/${state.currentResult.result_id}/download`;
  const a = document.createElement("a");
  a.href = url;
  a.download = "";
  a.click();
}

// ---------------------------------------------------------------------------
// 历史
// ---------------------------------------------------------------------------
async function loadHistory() {
  if (!state.currentSpace) return;
  const rows = await api(`/api/spaces/${state.currentSpace.id}/history`);
  $("#history-body").innerHTML = rows.map((r) => {
    const [reasonText, cls] = STOP_REASON_TEXT[r.stop_reason] ||
      [r.stop_reason || "—", "badge-gray"];
    const statusBadge = r.status === "running"
      ? `<span class="badge badge-purple">运行中</span>`
      : r.status === "completed"
        ? `<span class="badge badge-ok">完成</span>`
        : `<span class="badge badge-red">失败</span>`;
    let errCell = "—";
    if (r.error) {
      const e = typeof r.error === "string" ? JSON.parse(r.error) : r.error;
      errCell = `<div>${esc(e.message || "")}</div>` +
        (e.statement_number ? `<div>第 ${e.statement_number} 条语句：<code>${esc(e.statement || "")}</code></div>` : "");
    }
    return `<tr>
      <td>${new Date(r.created_at * 1000).toLocaleTimeString("zh-CN")}</td>
      <td><span class="sql-snip" title="${esc(r.sql_text)}">${esc(r.sql_text)}</span></td>
      <td>${statusBadge}</td>
      <td><span class="badge ${cls}">${esc(reasonText)}</span></td>
      <td>${r.row_count ?? "—"}${r.truncated ? ' <span class="badge badge-warn">截断</span>' : ""}</td>
      <td>${r.duration_ms != null ? r.duration_ms + " ms" : "—"}</td>
      <td class="err-cell">${errCell}</td>
    </tr>`;
  }).join("");
}

init().catch((err) => {
  document.body.insertAdjacentHTML("afterbegin",
    `<div style="padding:20px;color:#ffb3b3">初始化失败：${esc(err.message)}</div>`);
});
