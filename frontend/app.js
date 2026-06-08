const statusEl = document.querySelector("#apiStatus");
const noticeEl = document.querySelector("#modeNotice");
const analyzeButton = document.querySelector("#analyzeButton");
const previewButton = document.querySelector("#previewButton");
const loadingBox = document.querySelector("#loadingBox");
const elapsedTimer = document.querySelector("#elapsedTimer");
const reportOutput = document.querySelector("#reportOutput");
const missingList = document.querySelector("#missingList");
const limitationsList = document.querySelector("#limitationsList");
const missingReasonList = document.querySelector("#missingReasonList");
const dataQualityList = document.querySelector("#dataQualityList");
const sourceList = document.querySelector("#sourceList");
const historyList = document.querySelector("#historyList");
const summaryPanel = document.querySelector("#summaryPanel");
const summaryOutput = document.querySelector("#summaryOutput");
const freshnessPanel = document.querySelector("#freshnessPanel");
const freshnessOutput = document.querySelector("#freshnessOutput");
const previewPanel = document.querySelector("#previewPanel");
const previewOutput = document.querySelector("#previewOutput");
const downloadHtmlButton = document.querySelector("#downloadHtmlButton");

let latestReport = "";
let latestResult = null;
let timerId = null;
let startedAt = 0;

document.querySelector("#copyButton").addEventListener("click", async () => {
  if (latestReport) await navigator.clipboard.writeText(latestReport);
});
downloadHtmlButton.addEventListener("click", downloadHtmlReport);
analyzeButton.addEventListener("click", analyzeNow);
previewButton.addEventListener("click", previewData);

checkHealth();
loadModels();
loadHistory();

async function checkHealth() {
  try {
    const res = await fetch("/health");
    const json = await res.json();
    statusEl.textContent = json.openai_configured
      ? `服務正常｜AI 可用｜${json.default_model || "預設模型"}`
      : "服務正常｜尚未設定 OpenAI 金鑰";
    showNotice(
      json.openai_configured ? "OpenAI key 已設定，可進行 AI 分析。" : "尚未設定 OpenAI key，系統會使用本機規則產生 fallback 報告。",
      json.openai_configured ? "ok" : "missing"
    );
  } catch {
    statusEl.textContent = "服務離線";
    showNotice("連不到 API，請確認 FastAPI 服務是否已啟動。", "missing");
  }
}

async function loadModels() {
  const modelInput = document.querySelector("#modelInput");
  try {
    const res = await fetch("/models");
    if (!res.ok) return;
    const json = await res.json();
    const options = json.options || [];
    if (!options.length) return;
    modelInput.innerHTML = options
      .map((item) => `<option value="${escapeAttr(item.value || "")}">${escapeHtml(item.label || item.value || "model")}</option>`)
      .join("");
  } catch {
    // Keep static HTML options when the endpoint is unavailable.
  }
}

async function analyzeNow() {
  const symbol = document.querySelector("#symbolInput").value.trim() || "2603.TW";
  const mode = document.querySelector("#modeInput").value || "general";
  const model = document.querySelector("#modelInput").value;
  const manualContext = document.querySelector("#manualContext").value.trim();

  setLoading(true);
  try {
    const res = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        mode,
        model,
        freight_overrides: readFreightOverrides(),
        manual_context: manualContext
      })
    });
    if (!res.ok) throw new Error(await res.text());

    const json = await res.json();
    latestResult = json;
    latestReport = json.report_markdown || json.ai_report || "";
    renderStatuses(json.data_status || {});
    renderSummary(json.summary || null, json.action_plan || null, json.elapsed_seconds, json.model_used, json.truthfulness || null);
    renderFreshness(json.data_freshness || {});
    renderMarkdown(latestReport);
    renderList(missingList, json.data_missing || [], "無重大資料缺漏。");
    renderList(limitationsList, json.data_limitations || [], "無重大資料限制。");
    renderDataQuality(json.data_quality || null, json.truthfulness || null);
    renderMissingReasons(json.missing_reasons || []);
    renderSources(json.sources || []);
    updateOpenAIMode(json);
    downloadHtmlButton.disabled = !latestReport;
    loadHistory();
  } catch (error) {
    latestResult = null;
    latestReport = `# 分析失敗\n\n${error.message}`;
    renderMarkdown(latestReport);
    showNotice("分析失敗，請查看錯誤訊息或確認後端服務。", "missing");
    downloadHtmlButton.disabled = true;
  } finally {
    setLoading(false);
  }
}

async function previewData() {
  const symbol = document.querySelector("#symbolInput").value.trim() || "2603.TW";
  const manualContext = document.querySelector("#manualContext").value.trim();

  setLoading(true, "資料檢查中...");
  try {
    const res = await fetch("/data-preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        mode: "general",
        freight_overrides: readFreightOverrides(),
        manual_context: manualContext
      })
    });
    if (!res.ok) throw new Error(await res.text());

    const json = await res.json();
    latestResult = json;
    renderStatuses(json.data_status || {});
    renderSummary(json.summary || null, json.action_plan || null, json.elapsed_seconds, "未呼叫 OpenAI", json.truthfulness || null);
    renderFreshness(json.data_freshness || {});
    renderPreview(json);
    renderList(missingList, json.data_missing || [], "無重大資料缺漏。");
    renderList(limitationsList, json.data_limitations || [], "無重大資料限制。");
    renderDataQuality(json.data_quality || null, json.truthfulness || null);
    renderMissingReasons(json.missing_reasons || []);
    renderSources(json.sources || []);
    statusEl.textContent = "API online | 資料預覽完成 | OpenAI 未呼叫";
    showNotice("資料預覽完成：這次只抓資料與來源時間戳，沒有呼叫 OpenAI，不會消耗 OpenAI token。", "ok");
  } catch (error) {
    showNotice(`資料預覽失敗：${error.message}`, "missing");
  } finally {
    setLoading(false);
  }
}

function readFreightOverrides() {
  const ids = {
    scfi_latest: "scfiLatest",
    scfi_weekly_change: "scfiWeeklyChange",
    scfi_streak_weeks: "scfiStreakWeeks",
    us_west: "usWestRate",
    us_east: "usEastRate",
    europe: "europeRate",
    us_west_weekly_change: "usWestWeeklyChange",
    us_east_weekly_change: "usEastWeeklyChange",
    europe_weekly_change: "europeWeeklyChange",
    red_sea_status: "redSeaStatus"
  };
  const data = {};
  for (const [key, id] of Object.entries(ids)) {
    const value = document.querySelector(`#${id}`).value.trim();
    if (value) data[key] = value;
  }
  return data;
}

function updateOpenAIMode(result) {
  const model = result.model_used || result.selected_model || "default";
  if (result.analysis_mode === "fallback") {
    statusEl.textContent = `服務正常｜本機規則模式｜${model}`;
    showNotice("OpenAI 沒有成功完成分析，本次使用即時資料加本機規則產生報告。", "missing");
  } else {
    statusEl.textContent = `服務正常｜AI 分析完成｜${model}`;
    showNotice("AI 分析已完成。", "ok");
  }
}

function renderFreshness(freshness) {
  freshnessPanel.classList.remove("hidden");
  freshnessOutput.innerHTML = `
    <div class="summary-metrics">
      <div><span>分析時間</span><strong>${escapeHtml(freshness.analysis_time || "--")}</strong></div>
      <div><span>股價資料日期</span><strong>${escapeHtml(freshness.price_data_date || "--")}</strong></div>
      <div><span>即時資料</span><strong>${freshness.is_realtime_price === true ? "是" : freshness.is_realtime_price === false ? "否" : "--"}</strong></div>
      <div><span>收盤資料</span><strong>${freshness.is_closing_price === true ? "是" : freshness.is_closing_price === false ? "否" : "--"}</strong></div>
    </div>
    ${freshness.warning ? `<p class="freshness-warning">${escapeHtml(freshness.warning)}</p>` : ""}
  `;
}

function renderSummary(summary, actionPlan, elapsedSeconds, modelUsed, truthfulness) {
  if (!summary) {
    summaryPanel.classList.add("hidden");
    return;
  }
  const items = [
    ["今日結論", summary.market_state],
    ["綜合分數", summary.conviction_score ? `${summary.conviction_score}/100` : "--"],
    ["資料覆蓋率", summary.data_coverage !== undefined ? `${summary.data_coverage}%` : "--"],
    ["資料可信度", truthfulness?.truthfulness_score !== undefined ? `${truthfulness.truthfulness_score}/100` : "--"],
    ["風險等級", summary.risk_level],
    ["今日動作", summary.action],
    ["建議張數", summary.suggested_lots !== undefined ? summary.suggested_lots : "--"],
    ["耗時", elapsedSeconds !== undefined ? `${elapsedSeconds} 秒` : "--"]
  ];
  summaryOutput.innerHTML = `
    <div class="summary-metrics">${items.map(([label, value]) => `<div><span>${escapeHtml(label)}</span><strong>${escapeHtml(value || "--")}</strong></div>`).join("")}</div>
    <div class="action-box">
      <h3>買賣建議</h3>
      <p><strong>買進：</strong>${escapeHtml(summary.buy_advice || actionPlan?.buy_advice || "--")}</p>
      <p><strong>賣出：</strong>${escapeHtml(summary.sell_advice || actionPlan?.sell_advice || "--")}</p>
      <p><strong>理由：</strong>${escapeHtml(actionPlan?.reason || summary.one_line || "--")}</p>
      <p><strong>觸發條件：</strong>${escapeHtml(actionPlan?.trigger || "--")}</p>
      <p><strong>失效條件：</strong>${escapeHtml(actionPlan?.invalidated_by || "--")}</p>
      <p><strong>模型：</strong>${escapeHtml(modelUsed || "--")}</p>
    </div>
    <div class="summary-gaps">
      <h3>最大資料缺口</h3>
      <ul>${(summary.key_data_gaps || []).map((x) => `<li>${escapeHtml(x)}</li>`).join("") || "<li>無核心缺口。</li>"}</ul>
    </div>
  `;
  summaryPanel.classList.remove("hidden");
}

function renderPreview(result) {
  const market = result.market_data || {};
  const stock = market.stock || {};
  const freight = market.freight || {};
  const intl = market.international_events || {};
  const oil = intl.oil_prices || {};
  const regime = market.market_regime || {};
  const sourceRows = (result.source_timestamps || []).slice(0, 12);
  const eventRows = (intl.events || []).slice(0, 8);
  previewOutput.innerHTML = `
    <div class="preview-note">
      <strong>OpenAI 狀態：</strong>${result.openai_used ? "有呼叫" : "未呼叫"}　
      <strong>Token：</strong>${escapeHtml(result.token_usage || "no_openai_call")}　
      <strong>耗時：</strong>${escapeHtml(result.elapsed_seconds || "--")} 秒
    </div>
    <div class="preview-grid">
      <section>
        <h3>股價與新鮮度</h3>
        ${simpleTable([
          ["目前價格", stock.close],
          ["資料日期", stock.latest_date],
          ["即時資料", stock.is_realtime_price ? "是" : "否"],
          ["即時來源", stock.realtime_source || stock.primary_price_source],
          ["即時時間", stock.realtime_time],
          ["成交量", stock.volume],
          ["20 日均線", stock.ma20],
          ["RSI14", stock.technical?.rsi14]
        ])}
      </section>
      <section>
        <h3>SCFI 與航線</h3>
        ${simpleTable([
          ["SCFI 綜合指數", freight.scfi_latest],
          ["SCFI 週變化", formatPct(freight.weekly_change)],
          ["連續週數", freight.scfi_streak_weeks],
          ["美西線", freight.us_west],
          ["美西週變化", formatPct(freight.us_west_weekly_change)],
          ["美東線", freight.us_east],
          ["美東週變化", formatPct(freight.us_east_weekly_change)],
          ["歐洲線", freight.europe],
          ["歐洲週變化", formatPct(freight.europe_weekly_change)],
          ["航線資料來源", freight.route_storage_label || freight.verified_route_source]
        ])}
      </section>
      <section>
        <h3>國際事件與油價</h3>
        ${simpleTable([
          ["整體風險", intl.overall_risk],
          ["信心分數", intl.confidence],
          ["WTI 日期", oil.wti?.date],
          ["WTI 價格", oil.wti?.close],
          ["WTI 5 日變化", formatPct(oil.wti?.change_5d_pct)],
          ["Brent 日期", oil.brent?.date],
          ["Brent 價格", oil.brent?.close],
          ["Brent 5 日變化", formatPct(oil.brent?.change_5d_pct)],
          ["市場環境", regime.market_regime],
          ["美股狀態", regime.us_market]
        ])}
      </section>
      <section>
        <h3>資料品質</h3>
        ${simpleTable([
          ["資料覆蓋率", result.local_scores?.revised_score?.data_coverage ?? result.local_scores?.data_coverage],
          ["真實性分數", result.truthfulness?.truthfulness_score],
          ["缺漏數", (result.data_missing || []).length],
          ["限制數", (result.data_limitations || []).length]
        ])}
      </section>
    </div>
    <section class="preview-list">
      <h3>國際事件摘要</h3>
      <ul>${eventRows.length ? eventRows.map((item) => `<li><strong>${escapeHtml(item.published_at || "時間不足")}</strong>｜${escapeHtml(item.title || "")}<br><span>${escapeHtml(item.source || "")}</span></li>`).join("") : "<li>目前沒有事件資料。</li>"}</ul>
    </section>
    <section class="preview-list">
      <h3>來源時間戳</h3>
      <ul>${sourceRows.length ? sourceRows.map((line) => `<li>${escapeHtml(line.replace(/^-\\s*/, ""))}</li>`).join("") : "<li>來源時間戳不足。</li>"}</ul>
    </section>
  `;
  previewPanel.classList.remove("hidden");
}

async function loadHistory() {
  try {
    const res = await fetch("/analysis-history");
    const json = await res.json();
    const rows = json.records || [];
    historyList.innerHTML = rows.length
      ? rows.map((row) => `<li><strong>${escapeHtml(row.symbol)} ${escapeHtml(row.mode)}</strong><br>${escapeHtml(row.timestamp || "")}<br>${escapeHtml(row.market_state || "--")} / ${escapeHtml(row.action || "--")} / ${escapeHtml(row.coverage_adjusted_score || "--")}/100 / 可信度 ${escapeHtml(row.truthfulness_score || "--")}</li>`).join("")
      : "<li>尚無紀錄。</li>";
  } catch {
    historyList.innerHTML = "<li>讀取歷史紀錄失敗。</li>";
  }
}

function renderMissingReasons(items) {
  missingReasonList.innerHTML = items.length
    ? items.map((item) => `<li><strong>${escapeHtml(item.category)}：${escapeHtml(item.status)}</strong><br>${escapeHtml(item.reason)}<br><span>${escapeHtml(item.how_to_fix)}</span></li>`).join("")
    : "<li>無明確缺漏原因。</li>";
}

function renderDataQuality(quality, truthfulness) {
  if (!quality) {
    dataQualityList.textContent = "尚未開始分析。";
    return;
  }
  const groups = [
    ["精確資料", quality.exact_data || []],
    ["爬取資料", quality.scraped_data || []],
    ["搜尋推論資料", quality.search_inferred_data || []],
    ["過期或可疑資料", quality.stale_or_suspicious_data || []],
    ["缺漏資料", quality.missing_data || []],
    ["衝突資料", quality.conflict_data || []]
  ];
  const truthBlock = truthfulness ? `
    <div class="quality-group">
      <strong>資料可信度</strong>
      <ul>
        <li>${escapeHtml(truthfulness.truthfulness_score)}/100</li>
        ${(truthfulness.warnings || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("")}
      </ul>
    </div>
  ` : "";
  dataQualityList.innerHTML = truthBlock + groups.map(([title, items]) => `
    <div class="quality-group">
      <strong>${escapeHtml(title)}</strong>
      <ul>${items.length ? items.map((item) => `<li>${escapeHtml(item)}</li>`).join("") : "<li>無</li>"}</ul>
    </div>
  `).join("");
}

function setLoading(isLoading, label = "分析中...") {
  analyzeButton.disabled = isLoading;
  previewButton.disabled = isLoading;
  analyzeButton.textContent = isLoading ? label : "立即分析";
  loadingBox.classList.toggle("hidden", !isLoading);
  if (isLoading) {
    startedAt = performance.now();
    elapsedTimer.textContent = "0.0";
    timerId = window.setInterval(() => {
      elapsedTimer.textContent = ((performance.now() - startedAt) / 1000).toFixed(1);
    }, 100);
  } else if (timerId) {
    window.clearInterval(timerId);
    timerId = null;
  }
}

function renderStatuses(status) {
  setStatus("#stockStatus", status.stock);
  setStatus("#institutionalStatus", status.institutional);
  setStatus("#freightStatus", status.freight);
  setStatus("#newsStatus", status.news);
}

function setStatus(selector, value = "--") {
  const el = document.querySelector(selector);
  const labelMap = {
    ok: "正常",
    partial: "部分取得",
    inferred_from_search: "搜尋推論",
    missing: "缺漏",
    error: "錯誤"
  };
  el.textContent = labelMap[value] || value || "--";
  el.className = value === "ok"
    ? "ok"
    : value === "partial" || value === "inferred_from_search"
      ? "partial"
      : value === "missing"
        ? "missing"
        : "";
}

function showNotice(text, tone) {
  noticeEl.textContent = text;
  noticeEl.className = `notice ${tone || ""}`;
  noticeEl.classList.remove("hidden");
}

function renderList(target, items, emptyText = "無") {
  target.innerHTML = items.length
    ? items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")
    : `<li>${escapeHtml(emptyText)}</li>`;
}

function renderSources(sources) {
  sourceList.innerHTML = sources.length
    ? sources.map((source) => `<li><a href="${escapeAttr(source.url || "#")}" target="_blank" rel="noreferrer">${escapeHtml(source.name || "source")}</a></li>`).join("")
    : "<li>尚無資料來源。</li>";
}

function renderMarkdown(markdown) {
  const lines = markdown.split("\n");
  let html = "";
  let inList = false;
  let sectionIndex = 0;
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (isMarkdownTable(lines, i)) {
      if (inList) html += "</ul>";
      inList = false;
      const parsed = renderMarkdownTable(lines, i);
      html += parsed.html;
      i = parsed.nextIndex - 1;
      continue;
    }
    if (line.startsWith("## ")) {
      if (inList) html += "</ul>";
      inList = false;
      const title = line.slice(3);
      sectionIndex += 1;
      const cls = sectionIndex <= 5 || title.includes("決策摘要") || title.includes("關鍵指標") || title.includes("盤中現況") || title.includes("今日操作建議") || title.includes("國際事件") ? " key-section" : "";
      html += `<h2 class="${cls.trim()}">${formatInline(title)}</h2>`;
    } else if (line.startsWith("### ")) {
      if (inList) html += "</ul>";
      inList = false;
      html += `<h3>${formatInline(line.slice(4))}</h3>`;
    } else if (line.startsWith("# ")) {
      if (inList) html += "</ul>";
      inList = false;
      html += `<h1>${formatInline(line.slice(2))}</h1>`;
    } else if (line.startsWith("- ") || /^\d+\.\s/.test(line)) {
      if (!inList) html += "<ul>";
      inList = true;
      const item = line.replace(/^\d+\.\s/, "").replace(/^- /, "");
      html += `<li>${formatInline(item)}</li>`;
    } else if (line.startsWith("> ")) {
      if (inList) html += "</ul>";
      inList = false;
      html += `<blockquote>${formatInline(line.slice(2))}</blockquote>`;
    } else if (!line.trim()) {
      if (inList) html += "</ul>";
      inList = false;
    } else {
      if (inList) html += "</ul>";
      inList = false;
      html += `<p>${formatInline(line)}</p>`;
    }
  }
  if (inList) html += "</ul>";
  reportOutput.innerHTML = html;
}

function isMarkdownTable(lines, index) {
  const header = lines[index] || "";
  const separator = lines[index + 1] || "";
  return header.trim().startsWith("|")
    && separator.trim().startsWith("|")
    && /\|\s*:?-{3,}:?\s*\|/.test(separator);
}

function renderMarkdownTable(lines, startIndex) {
  const tableLines = [];
  let index = startIndex;
  while (index < lines.length && lines[index].trim().startsWith("|")) {
    tableLines.push(lines[index]);
    index += 1;
  }
  const headerCells = splitMarkdownRow(tableLines[0]);
  const bodyLines = tableLines.slice(2);
  const thead = `<thead><tr>${headerCells.map((cell) => `<th>${formatInline(cell)}</th>`).join("")}</tr></thead>`;
  const tbody = `<tbody>${bodyLines.map((row) => {
    const cells = splitMarkdownRow(row);
    return `<tr>${cells.map((cell) => `<td>${formatInline(cell)}</td>`).join("")}</tr>`;
  }).join("")}</tbody>`;
  return { html: `<div class="table-wrap"><table>${thead}${tbody}</table></div>`, nextIndex: index };
}

function splitMarkdownRow(row) {
  return row.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());
}

function formatInline(value) {
  const escaped = escapeHtml(value);
  return escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}

function downloadHtmlReport() {
  if (!latestReport || !latestResult) return;
  const symbol = latestResult.symbol || document.querySelector("#symbolInput").value.trim() || "stock";
  const timestamp = latestResult.timestamp || new Date().toISOString();
  const filenameTime = timestamp.replaceAll(":", "-").replaceAll(".", "-").slice(0, 19);
  const html = buildStandaloneHtml(symbol, timestamp);
  const blob = new Blob([html], { type: "text/html;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `${symbol}_AI分析報告_${filenameTime}.html`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function buildStandaloneHtml(symbol, timestamp) {
  return `<!doctype html>
<html lang="zh-Hant">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(symbol)} AI 分析報告</title>
  <style>
    body { margin: 0; background: #f2efe8; color: #152128; font-family: "Microsoft JhengHei", "Noto Sans TC", Arial, sans-serif; line-height: 1.72; }
    main { max-width: 1120px; margin: 0 auto; padding: 28px; }
    header, section { background: #fffdf8; border: 1px solid #d8d0c2; margin-bottom: 16px; padding: 18px; }
    h1 { margin: 0 0 8px; font-size: 28px; }
    h2 { margin-top: 24px; border-top: 1px solid #d8d0c2; padding-top: 14px; font-size: 20px; }
    ul { padding-left: 22px; }
    a { color: #0f766e; }
    blockquote { margin: 12px 0; border-left: 5px solid #a16207; background: #fff6df; padding: 10px 12px; color: #594a35; }
    .table-wrap { overflow-x: auto; margin: 12px 0 18px; border: 1px solid #d8d0c2; background: #fff; }
    table { width: 100%; min-width: 680px; border-collapse: collapse; font-size: 14px; }
    th, td { border-bottom: 1px solid #d8d0c2; padding: 9px 10px; text-align: left; vertical-align: top; }
    th { color: #66717a; background: #fafbfc; font-weight: 800; }
    @media print { body { background: #fff; } main { padding: 0; } header, section { break-inside: avoid; } }
  </style>
</head>
<body>
  <main>
    <header>
      <h1>${escapeHtml(symbol)} 即時 AI 投資分析報告</h1>
      <p>分析時間：${escapeHtml(timestamp)}</p>
      <p>這不是投資建議，只是輔助決策；系統不會自動下單，也不會自動通知。</p>
    </header>
    ${summaryPanel.classList.contains("hidden") ? "" : summaryPanel.outerHTML}
    ${freshnessPanel.classList.contains("hidden") ? "" : freshnessPanel.outerHTML}
    <section>${reportOutput.innerHTML}</section>
    <section><h2>資料品質分層</h2>${dataQualityList.innerHTML}</section>
    <section><h2>資料缺漏</h2><ul>${missingList.innerHTML}</ul></section>
    <section><h2>資料限制</h2><ul>${limitationsList.innerHTML}</ul></section>
    <section><h2>缺漏原因</h2><ul>${missingReasonList.innerHTML}</ul></section>
    <section><h2>資料來源</h2><ul>${sourceList.innerHTML}</ul></section>
  </main>
</body>
</html>`;
}

function simpleTable(rows) {
  const body = rows.map(([label, value]) => `
    <tr>
      <th>${escapeHtml(label)}</th>
      <td>${escapeHtml(value === undefined || value === null || value === "" ? "資料不足" : value)}</td>
    </tr>
  `).join("");
  return `<div class="table-wrap compact-table"><table><tbody>${body}</tbody></table></div>`;
}

function formatPct(value) {
  if (value === undefined || value === null || value === "") return "資料不足";
  const number = Number(value);
  if (Number.isNaN(number)) return value;
  return `${number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function escapeAttr(value) {
  return escapeHtml(value);
}
