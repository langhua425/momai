const $ = (s, r = document) => r.querySelector(s);
const el = (tag, cls, txt) => {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (txt !== undefined) e.textContent = txt;
  return e;
};
const api = async (path, opt = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt });
  const j = await r.json().catch(() => ({ ok: false, error: "响应不是 JSON" }));
  if (!j.ok) throw new Error(j.error || `HTTP ${r.status}`);
  return j;
};
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (n) => (n === null || n === undefined ? "-" : Number(n).toLocaleString("zh-CN"));

/* ---------- 极简 markdown ---------- */
function md(src) {
  const lines = String(src || "").split("\n");
  let html = "", inCode = false, inUl = false, inTable = false, buf = [];
  const flushTable = () => {
    if (!inTable) return;
    const rows = buf.filter((r) => !/^\|?[\s:-]+\|/.test(r));
    const cells = (r) => r.replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    if (rows.length) {
      html += "<table><thead><tr>" + cells(rows[0]).map((c) => `<th>${inline(c)}</th>`).join("") +
        "</tr></thead><tbody>" +
        rows.slice(1).map((r) => "<tr>" + cells(r).map((c) => `<td>${inline(c)}</td>`).join("") + "</tr>").join("") +
        "</tbody></table>";
    }
    buf = []; inTable = false;
  };
  const inline = (t) => esc(t)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/\[\[([^\]]+)\]\]/g, '<span class="badge">$1</span>');
  for (const raw of lines) {
    const line = raw.replace(/\s+$/, "");
    if (line.startsWith("```")) {
      if (inCode) { html += "</pre>"; inCode = false; }
      else { flushTable(); html += "<pre>"; inCode = true; }
      continue;
    }
    if (inCode) { html += esc(line) + "\n"; continue; }
    if (/^\|.*\|/.test(line)) { inTable = true; buf.push(line); continue; }
    flushTable();
    if (/^#{1,6}\s/.test(line)) {
      const lv = line.match(/^#+/)[0].length;
      html += `<h${lv}>${inline(line.replace(/^#+\s*/, ""))}</h${lv}>`;
    } else if (/^\s*[-*+]\s/.test(line)) {
      if (!inUl) { html += "<ul>"; inUl = true; }
      html += `<li>${inline(line.replace(/^\s*[-*+]\s*/, ""))}</li>`;
    } else if (line.startsWith(">")) {
      html += `<blockquote>${inline(line.replace(/^>\s?/, ""))}</blockquote>`;
    } else if (line.trim() === "") {
      if (inUl) { html += "</ul>"; inUl = false; }
    } else {
      if (inUl) { html += "</ul>"; inUl = false; }
      html += `<p>${inline(line)}</p>`;
    }
  }
  if (inUl) html += "</ul>";
  if (inCode) html += "</pre>";
  flushTable();
  return html;
}

/* ---------- 页签 ---------- */
document.querySelectorAll("#nav button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("#nav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "pages" && !state.pages.length) loadPages();
    if (b.dataset.tab === "reports" && !state.reports.length) loadReports();
    if (b.dataset.tab === "claims") loadClaims();
    if (b.dataset.tab === "settings") loadSettings();
  };
});

const state = { pages: [], reports: [], status: null, settings: null };

/* ---------- 概览 ---------- */
async function loadStatus() {
  const { status } = await api("/api/status");
  state.status = status;
  $("#site-title").textContent = status.site_title || "灵犀文脉";
  document.title = (status.site_title || "灵犀文脉") + " · NAS";
  const cards = [
    ["页面", fmt(status.pages)], ["论断 claims", fmt(status.claims)],
    ["文本块 chunks", fmt(status.chunks)], ["已向量化", fmt(status.embedded)],
    ["低置信度", fmt(status.low_confidence)],
    ["vault", status.vault_exists ? "正常" : "路径不存在"],
  ];
  const g = $("#stat-grid"); g.innerHTML = "";
  cards.forEach(([k, v]) => {
    const c = el("div", "card");
    c.append(el("div", "k", k), el("div", "v", v));
    g.append(c);
  });
  const f = status.freshness || {}, s = status.claim_status || {};
  $("#health").innerHTML =
    `<p class="muted">状态分布：` +
    Object.entries(s).map(([k, v]) => `<span class="badge ${k === "supported" ? "ok" : k === "contested" ? "warn" : ""}">${esc(k)} ${v}</span>`).join(" ") +
    `</p><p class="muted">新鲜度：` +
    Object.entries(f).map(([k, v]) => `<span class="badge ${k === "fresh" ? "ok" : k === "stale" ? "bad" : ""}">${esc(k)} ${v}</span>`).join(" ") +
    `</p><p class="muted">嵌入端点：<code>${esc(status.embedding.url)}</code> · 模型 ${esc(status.embedding.model)} · ${status.embedding.dim} 维</p>`;
  $("#foot-status").textContent = `页面 ${status.pages} · 论断 ${status.claims} · ${status.last_run ? "上次编译 " + status.last_run.started_at.slice(0, 19) : "尚未编译"}`;
  loadRuns();
}

async function loadRuns() {
  const { runs } = await api("/api/runs");
  const tb = $("#runs-table tbody"); tb.innerHTML = "";
  runs.forEach((r) => {
    const tr = el("tr");
    [r.id, r.started_at?.slice(0, 19), r.finished_at?.slice(0, 19), r.status, r.pages, r.claims, r.chunks, r.message]
      .forEach((v) => tr.append(el("td", "", v ?? "-")));
    tb.append(tr);
  });
}

/* ---------- 页面 ---------- */
async function loadPages() {
  const q = $("#page-q").value.trim(), kind = $("#page-kind").value;
  const { pages } = await api(`/api/pages?q=${encodeURIComponent(q)}&kind=${encodeURIComponent(kind)}`);
  state.pages = pages;
  const ul = $("#page-list"); ul.innerHTML = "";
  pages.forEach((p) => {
    const li = el("li");
    li.append(el("div", "t", p.title || p.id),
      el("div", "m", `${p.kind} · 论断 ${p.claims} · 分块 ${p.chunks}`));
    li.onclick = () => { ul.querySelectorAll("li").forEach((x) => x.classList.remove("active")); li.classList.add("active"); showPage(p.id); };
    ul.append(li);
  });
  if (!pages.length) ul.append(el("li", "muted", "没有页面，去「设置」里新建一个"));
}

async function showPage(id) {
  const { page } = await api(`/api/pages/${encodeURIComponent(id)}`);
  const v = $("#page-view");
  v.innerHTML = "";
  const h = el("h1", "", page.title || page.id);
  v.append(h, el("p", "muted", `${page.kind}${page.entity_type ? " · " + page.entity_type : ""} · ${page.path}`));
  const editBtn = el("button", "", "编辑此页");
  editBtn.className = "primary";
  editBtn.style.cssText = "margin:6px 0 10px";
  editBtn.onclick = () => {
    document.querySelector('#nav button[data-tab="settings"]').click();
    loadSettings().then(() => {
      $("#ed-id").value = page.id;
      $("#ed-kind").value = page.kind || "concept";
      $("#ed-title").value = page.title || "";
      $("#ed-aliases").value = (page.aliases || []).join(", ");
      $("#ed-claims").value = (page.claims || []).map((c) =>
        `- id: ${c.id}\n  text: ${c.text}\n  status: ${c.status}\n  confidence: ${c.confidence}` +
        ((c.evidence_kinds || []).length ? `\n  evidence:\n` + c.evidence_kinds.map((k) => `    - kind: ${k}`).join("\n") : "")
      ).join("\n");
      $("#ed-body").value = page.body || "";
      $("#ed-msg").textContent = "已载入 " + page.id + "，改完点保存";
    });
  };
  v.append(editBtn);
  if (page.aliases?.length) v.append(el("p", "muted", "别名：" + page.aliases.join(" / ")));
  if (page.claims?.length) {
    const p = el("div", "panel");
    p.append(el("h2", "", `论断 ${page.claims.length}`));
    const tb = el("table", "tbl");
    tb.innerHTML = "<thead><tr><th>论断</th><th>状态</th><th>置信度</th><th>证据</th><th>新鲜度</th></tr></thead>";
    const body = el("tbody");
    page.claims.forEach((c) => {
      const tr = el("tr");
      const badge = el("span", "badge " + (c.status === "supported" ? "ok" : c.status === "contested" ? "warn" : ""), c.status);
      const td0 = el("td"); td0.append(document.createTextNode(c.text));
      const td1 = el("td"); td1.append(badge);
      tr.append(td0, td1, el("td", "", c.confidence), el("td", "", (c.evidence_kinds || []).join(", ") || "—"), el("td", "", c.freshness_level));
      body.append(tr);
    });
    tb.append(body); p.append(tb); v.append(p);
  }
  if (page.backlinks?.length) {
    v.append(el("p", "muted", "被引用：" + page.backlinks.map((b) => b.from).join(" / ")));
  }
  const body = el("div"); body.innerHTML = md(page.body); v.append(body);
}

$("#page-q").oninput = debounce(loadPages, 300);
$("#page-kind").onchange = loadPages;

/* ---------- 检索 ---------- */
async function doSearch() {
  const q = $("#search-q").value.trim(), mode = $("#search-mode").value;
  if (!q) return;
  const box = $("#search-results");
  box.innerHTML = '<p class="muted">检索中…</p>';
  try {
    const { results, mode: m, count } = await api(`/api/search?q=${encodeURIComponent(q)}&mode=${mode}`);
    box.innerHTML = `<p class="muted">模式 ${esc(m)} · ${count} 条</p>`;
    results.forEach((r) => {
      const d = el("div", "result");
      const h = el("div", "h");
      h.append(el("span", "title", r.title || r.page_id || "-"),
        el("span", "badge " + (r.source === "claim" ? "claim" : ""), r.source));
      d.append(h);
      if (r.snippet) d.append(el("div", "snippet", r.snippet));
      const meta = [`得分 ${r.score ?? "-"}`];
      if (r.heading) meta.push("章节 " + r.heading);
      if (r.page_id) meta.push("页面 " + r.page_id);
      if (r.claim) meta.push(`论断状态 ${r.claim.status}`);
      d.append(el("div", "meta", meta.join(" · ")));
      if (r.page_id && r.source !== "error") {
        d.style.cursor = "pointer";
        d.onclick = () => { document.querySelector('#nav button[data-tab="pages"]').click(); loadPages().then(() => showPage(r.page_id)); };
      }
      box.append(d);
    });
    if (!results.length) box.append(el("p", "muted", "没有命中"));
  } catch (e) {
    box.innerHTML = `<p class="badge bad">${esc(e.message)}</p>`;
  }
}
$("#btn-search").onclick = doSearch;
$("#search-q").onkeydown = (e) => { if (e.key === "Enter") doSearch(); };

/* ---------- 论断 ---------- */
async function loadClaims() {
  const status = $("#claim-status").value, q = $("#claim-q").value.trim().toLowerCase();
  const { claims } = await api(`/api/claims?status=${encodeURIComponent(status)}&limit=1000`);
  const tb = $("#claims-table tbody"); tb.innerHTML = "";
  claims.filter((c) => !q || (c.text || "").toLowerCase().includes(q)).forEach((c) => {
    const tr = el("tr");
    const td = el("td"); td.append(document.createTextNode(c.text));
    const b = el("span", "badge " + (c.status === "supported" ? "ok" : c.status === "contested" ? "warn" : (c.status === "refuted" ? "bad" : "")), c.status);
    const tds = el("td"); tds.append(b);
    tr.append(td, tds, el("td", "", c.confidence),
      el("td", "", (c.evidence_kinds || []).join(", ") || "缺证据"),
      el("td", "", c.freshness_level), el("td", "muted", c.page_id));
    tr.style.cursor = "pointer";
    tr.onclick = () => { document.querySelector('#nav button[data-tab="pages"]').click(); loadPages().then(() => showPage(c.page_id)); };
    tb.append(tr);
  });
}
$("#claim-status").onchange = loadClaims;
$("#claim-q").oninput = debounce(loadClaims, 250);

/* ---------- 看板 ---------- */
async function loadReports() {
  const { reports } = await api("/api/reports");
  state.reports = reports;
  const ul = $("#report-list"); ul.innerHTML = "";
  reports.forEach((r) => {
    const li = el("li");
    li.append(el("div", "t", r.title), el("div", "m", `${r.mtime} · ${(r.bytes / 1024).toFixed(1)} KB`));
    li.onclick = async () => {
      ul.querySelectorAll("li").forEach((x) => x.classList.remove("active")); li.classList.add("active");
      const { content } = await api("/api/reports/" + encodeURIComponent(r.name));
      $("#report-view").innerHTML = md(content);
    };
    ul.append(li);
  });
  if (!reports.length) ul.append(el("li", "muted", "还没有看板，先编译一次"));
}

/* ---------- 设置 ---------- */
const FIELD_META = {
  site_title: ["站点标题", "text"],
  vault_path: ["vault 路径", "text"],
  embed_url: ["嵌入端点", "text"],
  embed_model: ["嵌入模型名", "text"],
  embed_dim: ["向量维度", "number"],
  embed_batch: ["嵌入批大小", "number"],
  chunk_size: ["分块长度（字符）", "number"],
  chunk_overlap: ["分块重叠", "number"],
  auto_compile: ["保存页面后自动编译", "bool"],
  fresh_days: ["新鲜期（天）", "number"],
  aging_days: ["老化期（天）", "number"],
  low_confidence: ["低置信度阈值", "number"],
  default_mode: ["默认检索模式", "text"],
  lan_only: ["仅局域网访问", "bool"],
};

async function loadSettings() {
  const { settings } = await api("/api/settings");
  state.settings = settings;
  const box = $("#settings-fields"); box.innerHTML = "";
  Object.entries(FIELD_META).forEach(([k, [label, type]]) => {
    const l = el("label", "", label);
    let inp;
    if (type === "bool") {
      inp = el("select");
      ["true", "false"].forEach((v) => { const o = el("option", "", v === "true" ? "是" : "否"); o.value = v; inp.append(o); });
      inp.value = String(!!settings[k]);
    } else {
      inp = el("input");
      inp.type = type;
      inp.value = settings[k] ?? "";
    }
    inp.dataset.key = k;
    inp.dataset.type = type;
    l.append(inp); box.append(l);
  });
}

$("#settings-form").onsubmit = async (e) => {
  e.preventDefault();
  const patch = {};
  document.querySelectorAll("#settings-fields [data-key]").forEach((i) => {
    const t = i.dataset.type;
    patch[i.dataset.key] = t === "number" ? Number(i.value) : t === "bool" ? i.value === "true" : i.value;
  });
  try {
    const { settings } = await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
    state.settings = settings;
    $("#settings-msg").textContent = "已保存 ✓";
    loadStatus();
  } catch (e) { $("#settings-msg").textContent = "保存失败：" + e.message; }
};

/* ---------- 新建/编辑 ---------- */
$("#btn-ed-save").onclick = async () => {
  const msg = $("#ed-msg");
  let claims = [];
  const raw = $("#ed-claims").value.trim();
  if (raw) {
    try {
      claims = jsyamlLite(raw);
    } catch (e) { msg.textContent = "论断 YAML 解析失败：" + e.message; return; }
  }
  const payload = {
    id: $("#ed-id").value.trim() || undefined,
    kind: $("#ed-kind").value,
    title: $("#ed-title").value.trim(),
    aliases: $("#ed-aliases").value.split(",").map((s) => s.trim()).filter(Boolean),
    claims, body: $("#ed-body").value, compile: true, embed: true,
  };
  msg.textContent = "保存中…";
  try {
    const r = await api("/api/pages", { method: "POST", body: JSON.stringify(payload) });
    msg.textContent = `已保存并编译 ✓ ${r.id}`;
    state.pages = [];
    loadStatus();
  } catch (e) { msg.textContent = "失败：" + e.message; }
};
$("#btn-ed-clear").onclick = () => {
  ["#ed-id", "#ed-title", "#ed-aliases", "#ed-claims", "#ed-body"].forEach((s) => ($(s).value = ""));
  $("#ed-msg").textContent = "";
};
$("#btn-new-page").onclick = () => {
  document.querySelector('#nav button[data-tab="settings"]').click();
  loadSettings();
  $("#ed-title").focus();
};

/* 极简 YAML（仅支持本页 claims 用法：列表 + 键值 + 嵌套列表） */
function jsyamlLite(text) {
  const out = [];
  let cur = null, curEv = null, inEv = false;
  text.split("\n").forEach((line) => {
    if (!line.trim() || line.trim().startsWith("#")) return;
    const indent = line.length - line.trimStart().length;
    const t = line.trim().replace(/^-\s*/, "");
    const m = t.match(/^([\w\u4e00-\u9fff]+):\s*(.*)$/);
    if (t.startsWith("- ") || (line.trim().startsWith("-") && m)) {
      if (indent <= 2 || /^id:/.test(t)) {
        cur = {}; out.push(cur); curEv = null; inEv = false;
        const mm = t.match(/^([\w\u4e00-\u9fff]+):\s*(.*)$/);
        if (mm) cur[mm[1]] = parseVal(mm[2]);
        return;
      }
    }
    if (!m) return;
    const [, k, v] = m;
    const val = parseVal(v);
    if (cur && indent >= 4) {
      if (k === "kind" || k === "ref") { curEv = curEv || {}; curEv[k] = val; }
      if (k === "evidence") { cur.evidence = cur.evidence || []; curEv = {}; cur.evidence.push(curEv); }
      else if (curEv && (k === "kind" || k === "ref") && indent >= 6) { curEv[k] = val; }
      else if (!["evidence"].includes(k)) cur[k] = val;
    } else if (cur) {
      cur[k] = val;
    }
  });
  return out;
}
function parseVal(v) {
  const s = String(v || "").trim();
  if (!s) return "";
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  if (s === "true" || s === "false") return s === "true";
  return s.replace(/^["']|["']$/g, "");
}

/* ---------- 编译 ---------- */
$("#btn-compile").onclick = async () => {
  const b = $("#btn-compile");
  b.disabled = true; b.textContent = "编译中…";
  try {
    const { stats } = await api("/api/compile", { method: "POST", body: JSON.stringify({ embed: true }) });
    b.textContent = `完成 ${stats.pages}页/${stats.claims}论断/${stats.chunks}块`;
    state.pages = []; state.reports = [];
    await loadStatus();
    if ($("#tab-pages").classList.contains("active")) loadPages();
  } catch (e) {
    b.textContent = "编译失败";
    alert("编译失败：" + e.message);
  } finally {
    setTimeout(() => { b.disabled = false; b.textContent = "编译"; }, 2500);
  }
};

function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

/* ---------- 启动 ---------- */
loadStatus();
loadPages();
