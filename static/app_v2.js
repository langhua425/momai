const $ = (s, r = document) => r.querySelector(s);
const el = (t, c, x) => { const e = document.createElement(t); if (c) e.className = c; if (x !== undefined) e.textContent = x; return e; };
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtN = (n) => n === null || n === undefined ? "-" : Number(n).toLocaleString("zh-CN");
const fmtSize = (n) => { n = Number(n || 0); const u = ["B", "KB", "MB", "GB"]; let i = 0; while (n >= 1024 && i < 3) { n /= 1024; i++; } return n.toFixed(i ? 1 : 0) + u[i]; };

async function api(path, opt = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opt });
  const j = await r.json().catch(() => ({ ok: false, error: "响应非 JSON" }));
  if (!j.ok) throw new Error(j.error || ("HTTP " + r.status));
  return j;
}

/* 页签 */
document.querySelectorAll("#nav button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("#nav button").forEach(x => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    b.classList.add("active");
    $("#tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "files") loadFiles();
    if (b.dataset.tab === "settings") loadSettings();
    if (b.dataset.tab === "overview") loadStatus();
    if (b.dataset.tab === "graph") loadEntities();
    if (b.dataset.tab === "chat") initChat();
    if (b.dataset.tab === "exclude") loadExclude();
  };
});

/* ---------- 处理进度（三段漏斗）---------- */
function bar(label, done, total, extra) {
  const pct = total > 0 ? Math.min(100, done / total * 100) : 0;
  const row = el("div", "prog-row");
  const head = el("div", "prog-head");
  head.append(el("span", "prog-label", label));
  head.append(el("span", "prog-num", `${fmtN(done)} / ${total > 0 ? fmtN(total) : "—"}` +
    (total > 0 ? `　${pct.toFixed(1)}%` : "")));
  const track = el("div", "prog-track");
  const fill = el("div", "prog-fill"); fill.style.width = pct + "%";
  if (pct >= 100) fill.classList.add("done");
  track.append(fill);
  row.append(head, track);
  if (extra) row.append(el("div", "prog-extra muted", extra));
  return row;
}

async function loadProgress() {
  const { progress: p } = await api("/api/progress");
  const box = $("#progress-box"); box.innerHTML = "";
  const disk = p.total_disk, idx = p.total;
  box.append(bar("已入库", p.ingested, disk,
    disk ? `磁盘文件 ${fmtN(disk)} 个${p.children ? `　+　解包子文件 ${fmtN(p.children)} 个` : ""}`
         : "还没统计过磁盘总数，点下面「重新统计磁盘总数」"));
  box.append(bar("已识别（解析 + OCR）", p.recognized, idx,
    `可识别文件 ${fmtN(idx)} 个（已排除跳过类型）　·　OCR 完成 ${fmtN(p.ocr_done)}，待 OCR ${fmtN(p.ocr_wait)}`));
  box.append(bar("已向量化", p.vectorized, idx,
    `文本块 ${fmtN(p.chunks)}，已嵌入 ${fmtN(p.chunks_embedded)}　·　向量化文件 / 已识别 = ${fmtN(p.vectorized)} / ${fmtN(p.recognized)}`));
  $("#progress-scan-at").textContent = p.scan_at ? `（上次统计：${p.scan_at.replace("T", " ").slice(0, 19)}）` : "";
}

$("#btn-count").onclick = async () => {
  $("#progress-msg").textContent = "统计中（会遍历所有数据源，大库较慢）…";
  try {
    await api("/api/scan", { method: "POST", body: JSON.stringify({}) });
    pollTask();
  } catch (e) { $("#progress-msg").textContent = "启动失败：" + e.message; }
};

/* ---------- 概览 ---------- */
async function loadStatus() {
  loadProgress().catch(() => {});
  const { status: s } = await api("/api/status");
  $("#site-title").textContent = s.site_title || "墨脉";
  document.title = (s.site_title || "墨脉") + " · 私有资料库";
  const cards = [
    ["文件", fmtN(s.files)], ["已解析", fmtN(s.by_status.parsed || 0)],
    ["待处理", fmtN(s.by_status.pending || 0)], ["失败/待 OCR", fmtN(s.by_status.failed || 0)],
    ["文本块", fmtN(s.chunks)], ["已向量化", fmtN(s.embedded)],
    ["解包条目", fmtN(s.archives.entries)], ["向量维度", (s.dims || []).join("/") || "-"],
  ];
  const g = $("#stat-grid"); g.innerHTML = "";
  cards.forEach(([k, v]) => { const c = el("div", "card"); c.append(el("div", "k", k), el("div", "v", v)); g.append(c); });

  const tb = $("#src-body"); tb.innerHTML = "";
  (s.sources || []).forEach(x => {
    const tr = el("tr");
    tr.append(el("td", "", x.id), el("td", "", x.name), el("td", "muted", x.root),
      el("td", "", x.enabled ? "启用" : "停用"));
    const td = el("td"); const b = el("button", "", "移除");
    b.onclick = async () => { if (confirm("移除数据源（不删文件）？")) { await api("/api/sources/" + x.id, { method: "DELETE" }); loadStatus(); } };
    td.append(b); tr.append(td); tb.append(tr);
  });

  const kd = $("#kind-stats"); kd.innerHTML = "";
  Object.entries(s.by_kind || {}).forEach(([k, v]) => {
    const line = el("div", "muted", `${k}: ${v}`); kd.append(line);
  });
  kd.append(el("div", "muted", `压缩包：${s.archives.count} 个 / ${fmtSize(s.archives.bytes)}`));

  $("#foot").textContent = `文件 ${fmtN(s.files)} · 块 ${fmtN(s.chunks)} · 嵌入端点 ${s.endpoints.embedding} · mineru ${s.endpoints.mineru}`;
  $("#task-badge").textContent = s.task.running ? ("运行中：" + s.task.kind) : (s.task.message || "空闲");
  $("#task-badge").className = "badge " + (s.task.running ? "warn" : (s.task.result === "error" ? "bad" : "ok"));

  // 类型下拉
  const sel = $("#ext-filter");
  const cur = sel.value;
  sel.innerHTML = '<option value="">全部类型</option>';
  Object.keys(s.by_ext || {}).forEach(e => { if (e && e !== "-") { const o = el("option", "", e); o.value = e; sel.append(o); } });
  sel.value = cur;
}

$("#btn-refresh").onclick = loadStatus;
$("#btn-scan").onclick = () => runTask("/api/scan", {}, "扫描");
$("#btn-index").onclick = () => runTask("/api/index", { embed: true }, "解析+嵌入");
$("#btn-ocr").onclick = () => runTask("/api/ocr", { limit: 5 }, "OCR");

async function runTask(path, body, label) {
  const msg = $("#op-msg");
  msg.textContent = label + " 已启动…";
  try {
    await api(path, { method: "POST", body: JSON.stringify(body) });
    pollTask();
  } catch (e) { msg.textContent = label + " 失败：" + e.message; }
}

let pollTimer = null;
function pollTask() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try {
      const { status: s } = await api("/api/status");
      $("#task-badge").textContent = s.task.running ? ("运行中：" + s.task.kind) : (s.task.message || "空闲");
      $("#op-msg").textContent = s.task.message + (s.task.started ? "（开始 " + s.task.started + "）" : "");
      if (!s.task.running) { clearInterval(pollTimer); pollTimer = null; loadStatus(); }
    } catch (e) { clearInterval(pollTimer); pollTimer = null; }
  }, 2500);
}

/* 数据源 */
$("#btn-add-src").onclick = async () => {
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify({ name: $("#src-name").value, root: $("#src-root").value }) });
    $("#src-name").value = ""; $("#src-root").value = "";
    loadStatus();
  } catch (e) { alert("添加失败：" + e.message); }
};

/* ---------- 检索 ---------- */
async function doSearch() {
  const q = $("#q").value.trim(); if (!q) return;
  const box = $("#results"); box.innerHTML = '<p class="muted">检索中…</p>';
  try {
    const resp = await api(`/api/search?q=${encodeURIComponent(q)}&mode=${$("#mode").value}&ext=${encodeURIComponent($("#ext-filter").value)}`);
    const { results, mode, count } = resp;
    box.innerHTML = `<p class="muted">模式 ${esc(mode)} · ${count} 条${resp.reranked ? " · 已重排" : " · 未重排"}${resp.error ? " · " + esc(resp.error) : ""}</p>`;
    if (!results.length) box.append(el("p", "muted", "没有命中"));
    results.forEach(r => {
      const d = el("div", "result");
      const h = el("div", "h");
      h.append(el("span", "title", r.name || "-"), el("span", "badge", r.source || ""));
      if (r.ext) h.append(el("span", "badge", r.ext));
      if (r.parse_kind) h.append(el("span", "badge", r.parse_kind));
      if (r.other_hits > 0) h.append(el("span", "badge", `+${r.other_hits} 块`));
      d.append(h);
      if (r.snippet) d.append(el("div", "snippet", r.snippet));
      const sc = [];
      if (r.rerank_score !== null && r.rerank_score !== undefined) sc.push(`重排 ${r.rerank_score}`);
      if (r.vscore !== undefined) sc.push(`语义 ${r.vscore}`);
      if (r.kscore !== undefined && r.kscore > 0) sc.push(`关键词 ${r.kscore}`);
      d.append(el("div", "meta",
        `${sc.length ? sc.join(" · ") + " · " : "得分 " + r.score + " · "}第 ${r.seq} 块` +
        `${r.section_path ? " · " + r.section_path : (r.heading ? " · " + r.heading : "")} · ${r.path || ""}`));
      if (r.file_id) {
        const rel = el("button", "ghost", "相关文档");
        rel.style.marginTop = "8px";
        rel.onclick = async ev => {
          ev.stopPropagation();
          rel.disabled = true; rel.textContent = "计算中…";
          try {
            const rr = await api(`/api/related?file_id=${r.file_id}&limit=6`);
            const wrap = el("div", "meta");
            wrap.style.marginTop = "8px";
            if (!rr.items.length) wrap.textContent = "没有相似文档";
            else wrap.innerHTML = "相似文档：" + rr.items.map(x =>
              `<span class="eg-chip" data-fid="${x.file_id}" title="${esc(x.path || "")}">` +
              `${esc(x.name)} <span class="n">${x.sim}</span></span>`).join("");
            wrap.querySelectorAll(".eg-chip").forEach(c =>
              c.onclick = e2 => { e2.stopPropagation(); showFile(+c.dataset.fid); });
            d.append(wrap);
            rel.remove();
          } catch (e) { rel.textContent = "失败"; rel.disabled = false; }
        };
        d.append(rel);
        d.style.cursor = "pointer"; d.onclick = () => showFile(r.file_id);
      }
      box.append(d);
    });
  } catch (e) { box.innerHTML = `<p class="badge bad">${esc(e.message)}</p>`; }
}
$("#btn-search").onclick = doSearch;
$("#q").onkeydown = e => { if (e.key === "Enter") doSearch(); };

/* ---------- 实体图谱 ---------- */
const TYPE_CLS = { org: "t-org", project: "t-project", money: "t-money", doc: "t-doc" };
const TYPE_CN = { org: "机构", project: "项目", money: "金额", doc: "文件", concept: "概念" };

async function loadEntities() {
  const q = ($("#ent-q") && $("#ent-q").value.trim()) || "";
  const box = $("#ent-list");
  box.innerHTML = `<li class="muted">加载中…</li>`;
  try {
    const r = await api(`/api/entities?limit=60&q=${encodeURIComponent(q)}`);
    $("#graph-count").textContent = `共 ${r.total} 个实体 · ${r.relations} 条关系`;
    box.innerHTML = "";
    if (!r.items.length) { box.innerHTML = `<li class="muted">没有实体，先跑「抽实体」</li>`; return; }
    r.items.forEach(e => {
      const li = el("li");
      li.innerHTML = `<div class="t">${esc(e.name)}</div>` +
        `<div class="m"><span class="eg-type ${TYPE_CLS[e.type] || ""}">${TYPE_CN[e.type] || e.type}</span>` +
        `提及 ${e.mentions} · 文件 ${e.files}</div>`;
      li.onclick = () => showEntity(e.id, li);
      box.append(li);
    });
  } catch (e) { box.innerHTML = `<li class="badge bad">${esc(e.message)}</li>`; }
}

async function showEntity(id, li) {
  document.querySelectorAll("#ent-list li").forEach(x => x.classList.remove("active"));
  if (li) li.classList.add("active");
  const box = $("#ent-detail");
  box.innerHTML = `<p class="muted">加载中…</p>`;
  try {
    const r = await api(`/api/entity/${id}`);
    const e = r.entity;
    const chip = (x, dir) => `<span class="eg-chip" data-eid="${x.id}">` +
      `<span class="eg-type ${TYPE_CLS[x.type] || ""}">${TYPE_CN[x.type] || x.type}</span>${esc(x.name)}</span>`;
    let html = `<h2>${esc(e.name)}</h2>` +
      `<p class="muted">类型 ${TYPE_CN[e.type] || e.type} · 提及 ${e.mentions} 次 · 出处 ${r.files.length} 个文件</p>`;
    if (r.out.length || r.inc.length) {
      html += `<h3>关系网络（${r.out.length + r.inc.length}）</h3>`;
      r.out.forEach(x => {
        html += `<div class="eg-rel"><span class="eg-name">${esc(e.name)}</span>` +
          `<span class="arrow">→</span><span class="rel">${esc(x.rel)}</span>` +
          `<span class="arrow">→</span>${chip(x)}</div>`;
      });
      r.inc.forEach(x => {
        html += `<div class="eg-rel">${chip(x)}<span class="arrow">→</span>` +
          `<span class="rel">${esc(x.rel)}</span><span class="arrow">→</span>` +
          `<span class="eg-name">${esc(e.name)}</span></div>`;
      });
    } else {
      html += `<p class="muted">这个实体暂时没有抽到关系（只有提及）。</p>`;
    }
    if (r.files.length) {
      html += `<h3>出处文件（${r.files.length}）</h3>`;
      html += r.files.map(f => {
        const nm = f.name || "";
        const tag = (f.ext && !nm.toLowerCase().endsWith(String(f.ext).toLowerCase())) ? f.ext : "";
        return `<span class="eg-chip" data-fid="${f.id}" title="${esc(f.path || "")}">${esc(nm)}` +
               (tag ? `<span class="n">${esc(tag)}</span>` : "") + `</span>`;
      }).join("");
    }
    box.innerHTML = html;
    box.querySelectorAll("[data-eid]").forEach(c => c.onclick = () => showEntity(+c.dataset.eid));
    box.querySelectorAll("[data-fid]").forEach(c => c.onclick = () => showFile(+c.dataset.fid));
  } catch (e) { box.innerHTML = `<p class="badge bad">${esc(e.message)}</p>`; }
}

$("#btn-ent-search").onclick = loadEntities;
$("#ent-q").onkeydown = e => { if (e.key === "Enter") loadEntities(); };

/* ---------- 文件 ---------- */
async function loadFiles() {
  const { files } = await api(`/api/files?status=${encodeURIComponent($("#file-status").value)}&q=${encodeURIComponent($("#file-q").value)}&limit=200`);
  const tb = $("#files-body"); tb.innerHTML = "";
  files.forEach(f => {
    const tr = el("tr");
    tr.append(el("td", "", f.id), el("td", "", f.name), el("td", "", f.ext || "-"),
      el("td", "", f.status), el("td", "", f.parse_kind || "-"),
      el("td", "", fmtSize(f.size)), el("td", "", fmtN(f.text_bytes)),
      el("td", "", f.chunks), el("td", "", f.depth));
    tr.style.cursor = "pointer";
    tr.onclick = () => showFile(f.id);
    tb.append(tr);
  });
}
$("#btn-files").onclick = loadFiles;

async function showFile(id) {
  document.querySelector('#nav button[data-tab="files"]').click();
  const { file } = await api("/api/files/" + id);
  const box = $("#file-detail"); box.style.display = "block";
  $("#fd-title").textContent = file.name;
  $("#fd-meta").textContent = `${file.parse_kind || "-"} · ${file.status} · ${fmtSize(file.size)} · 文本 ${fmtN(file.text_bytes)} 字节 · ${file.path}`;
  // 章节结构
  const sb = $("#fd-sections");
  if (sb) {
    sb.innerHTML = '<span class="muted">读取章节…</span>';
    api(`/api/sections?file_id=${id}`).then(r => {
      const list = r.sections || [];
      if (!list.length) {
        sb.innerHTML = '<span class="muted">没有章节结构</span>';
        return;
      }
      const withSum = list.filter(x => x.summary).length;
      sb.innerHTML = `<div class="h"><strong>章节</strong>` +
        `<span class="muted" style="font-size:12px;margin-left:8px">共 ${list.length} 节，已摘 ${withSum} 节</span>` +
        (withSum < list.length ? ` <button class="ghost" id="btn-mk-sections">生成章节摘要</button>` : "") +
        `</div>` + list.map(x =>
          `<div class="sec-item" data-seq="${x.start}">` +
            `<div class="sec-t"><span class="badge">§${x.index + 1}</span> ${esc(x.title)}` +
            `<span class="muted" style="font-size:11.5px;margin-left:8px">${x.chars} 字</span></div>` +
            (x.summary ? `<div class="sec-s">${esc(x.summary)}</div>` : '') +
          `</div>`).join("");
      const bb = $("#btn-mk-sections");
      if (bb) bb.onclick = async () => {
        bb.disabled = true; bb.textContent = "生成中…（每节约 10 秒）";
        try {
          const rr = await api("/api/sections", { method: "POST",
            body: JSON.stringify({ file_id: id, summarize: 6 }) });
          showFile(id);
        } catch (e) { bb.disabled = false; bb.textContent = "失败，重试"; }
      };
      sb.querySelectorAll(".sec-item").forEach(el => el.onclick = () => {
        const s = el.dataset.seq;
        const t = document.querySelector(`#fd-chunks [data-seq="${s}"]`);
        if (t) t.scrollIntoView({ behavior: "smooth", block: "center" });
      });
    }).catch(e => { sb.innerHTML = `<span class="badge bad">${esc(e.message)}</span>`; });
  }
  // 摘要块
  const box2 = $("#fd-summary");
  if (box2) {
    box2.innerHTML = '<span class="muted">读取摘要…</span>';
    api(`/api/summary?file_id=${id}`).then(r => {
      const sm = r.summary;
      if (sm) {
        box2.innerHTML = `<div class="h"><span class="badge claim">${esc(sm.doc_type || "摘要")}</span>` +
          `<span class="muted" style="font-size:12px">生成于 ${esc((sm.created_at || "").slice(0, 16))}</span></div>` +
          `<div class="snippet" style="margin-top:6px">${esc(sm.summary)}</div>` +
          (sm.keywords && sm.keywords.length
            ? `<div style="margin-top:8px">` + sm.keywords.map(k =>
                `<span class="eg-chip" style="cursor:default">${esc(k)}</span>`).join("") + `</div>`
            : "");
      } else {
        box2.innerHTML = '<span class="muted">还没有摘要</span> ' +
          '<button class="ghost" id="btn-mk-summary">生成摘要</button>';
        const b = $("#btn-mk-summary");
        if (b) b.onclick = async () => {
          b.disabled = true; b.textContent = "生成中…（约 10–30 秒）";
          try {
            const rr = await api("/api/summary", { method: "POST", body: JSON.stringify({ file_id: id }) });
            const s2 = rr.summary;
            box2.innerHTML = `<div class="h"><span class="badge claim">${esc(s2.doc_type || "摘要")}</span></div>` +
              `<div class="snippet" style="margin-top:6px">${esc(s2.summary)}</div>`;
          } catch (e) { b.disabled = false; b.textContent = "生成失败，重试"; }
        };
      }
    }).catch(e => { box2.innerHTML = `<span class="badge bad">${esc(e.message)}</span>`; });
  }
  const c = $("#fd-chunks"); c.innerHTML = "";
  (file.chunks || []).forEach(ch => {
    const d = el("div", "result");
    d.dataset.seq = ch.seq;
    d.append(el("div", "h", `第 ${ch.seq} 块${ch.heading ? " · " + ch.heading : ""}${ch.embedded ? " · 已向量化" : ""}`));
    d.append(el("div", "snippet", ch.content));
    c.append(d);
  });
  box.scrollIntoView({ behavior: "smooth" });
}

/* ---------- 设置 ---------- */
const ENDPOINT_FIELDS = [
  ["embed_url", "Embedding 端点", "text"],
  ["embed_model", "Embedding 模型名", "text"],
  ["embed_dim", "向量维度（改后需重建索引）", "number"],
  ["embed_batch", "嵌入批大小", "number"],
  ["mineru_url", "MinerU 端点", "text"],
  ["mineru_enabled", "启用 MinerU OCR", "bool"],
  ["mineru_poll_sec", "OCR 轮询秒数", "number"],
  ["rerank_url", "重排端点（llama.cpp /v1/rerank）", "text"],
  ["rerank_model", "重排模型名", "text"],
  ["rerank_enabled", "启用重排", "bool"],
  ["rerank_candidates", "重排候选条数", "number"],
  ["vllm_url", "vLLM 端点（OpenAI 兼容 /v1）", "text"],
  ["vllm_model", "vLLM 模型名", "text"],
  ["vllm_enabled", "启用 vLLM（实体抽取/摘要）", "bool"],
];
const PIPE_FIELDS = [
  ["chunk_size", "切块长度（字符）", "number"],
  ["chunk_overlap", "切块重叠", "number"],
  ["unpack_max_depth", "解包最大递归层数", "number"],
  ["unpack_dir", "解包落盘目录", "text"],
  ["max_file_mb", "单文件大小上限（MB）", "number"],
];

/* ---------- 扫描目录 ---------- */
async function loadScanSources() {
  const { sources } = await api("/api/sources");
  const tb = $("#scan-src-body"); tb.innerHTML = "";
  sources.forEach(s => {
    const tr = el("tr");
    // 启用
    const tdEn = el("td");
    const cb = el("input"); cb.type = "checkbox"; cb.checked = !!s.enabled;
    cb.onchange = async () => {
      await api("/api/sources/" + s.id, { method: "PUT", body: JSON.stringify({ enabled: cb.checked }) });
      $("#scan-msg").textContent = "已更新启用状态";
    };
    tdEn.append(cb);
    // 名称（可编辑）
    const tdName = el("td");
    const nameIn = el("input"); nameIn.value = s.name; nameIn.style.width = "130px";
    tdName.append(nameIn);
    // 路径
    const tdRoot = el("td", "muted", s.root);
    // 排除
    const tdEx = el("td");
    const exIn = el("input");
    exIn.value = (s.exclude || []).join(", ");
    exIn.placeholder = "临时,备份,.git";
    exIn.style.width = "100%";
    tdEx.append(exIn);
    // 操作
    const tdOp = el("td");
    const save = el("button", "", "保存");
    save.onclick = async () => {
      try {
        await api("/api/sources/" + s.id, { method: "PUT", body: JSON.stringify({
          name: nameIn.value, exclude: exIn.value }) });
        $("#scan-msg").textContent = `已保存「${nameIn.value}」`;
      } catch (e) { $("#scan-msg").textContent = "保存失败：" + e.message; }
    };
    const del = el("button", "", "删除");
    del.style.marginLeft = "6px";
    del.onclick = async () => {
      if (!confirm(`移除数据源「${s.name}」？（只移除索引配置，不动文件）`)) return;
      await api("/api/sources/" + s.id, { method: "DELETE" });
      loadScanSources(); loadStatus();
    };
    tdOp.append(save, del);
    tr.append(tdEn, tdName, tdRoot, tdEx, tdOp);
    tb.append(tr);
  });
  if (!sources.length) tb.append(el("tr", "", "还没有扫描目录，在下面添加"));

  const { settings } = await api("/api/settings");
  $("#scan-skip").value = (settings.skip_exts || []).join(", ");
}

$("#btn-scan-add").onclick = async () => {
  const msg = $("#scan-msg");
  try {
    await api("/api/sources", { method: "POST", body: JSON.stringify({
      name: $("#scan-name").value.trim(), root: $("#scan-root").value.trim() }) });
    // 若填了排除项，紧接着写进去
    const ex = $("#scan-exclude").value.trim();
    if (ex) {
      const { sources } = await api("/api/sources");
      const hit = sources.filter(s => s.root === $("#scan-root").value.trim()).pop();
      if (hit) await api("/api/sources/" + hit.id, { method: "PUT", body: JSON.stringify({ exclude: ex }) });
    }
    $("#scan-name").value = ""; $("#scan-root").value = ""; $("#scan-exclude").value = "";
    msg.textContent = "已添加 ✓";
    loadScanSources(); loadStatus();
  } catch (e) { msg.textContent = "添加失败：" + e.message; }
};

$("#btn-scan-save-global").onclick = async () => {
  const skip = $("#scan-skip").value.split(/[,，\n]/).map(x => x.trim()).filter(Boolean)
    .map(x => x.startsWith(".") ? x : "." + x);
  try {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ skip_exts: skip }) });
    $("#scan-global-msg").textContent = `已保存 ${skip.length} 个跳过扩展名`;
  } catch (e) { $("#scan-global-msg").textContent = "失败：" + e.message; }
};

$("#btn-scan-now").onclick = () => runTask("/api/scan", {}, "扫描");

let SETTINGS = null;
async function loadSettings() {
  const { settings } = await api("/api/settings");
  SETTINGS = settings;
  loadScanSources();
  loadMdStats();
  buildFields("#f-endpoints", ENDPOINT_FIELDS, settings);
  buildFields("#f-pipeline", PIPE_FIELDS, settings);
  $("#raw-settings").textContent = JSON.stringify(settings, null, 2);
}

function buildFields(sel, defs, s) {
  const box = $(sel); box.innerHTML = "";
  defs.forEach(([k, label, type]) => {
    const l = el("label", "", label);
    let i;
    if (type === "bool") {
      i = el("select");
      [["true", "是"], ["false", "否"]].forEach(([v, t]) => { const o = el("option", "", t); o.value = v; i.append(o); });
      i.value = String(!!s[k]);
    } else {
      i = el("input"); i.type = type; i.value = s[k] ?? "";
    }
    i.dataset.key = k; i.dataset.type = type;
    l.append(i); box.append(l);
  });
}

function collect(sel) {
  const out = {};
  document.querySelectorAll(sel + " [data-key]").forEach(i => {
    const t = i.dataset.type;
    out[i.dataset.key] = t === "number" ? Number(i.value) : t === "bool" ? i.value === "true" : i.value;
  });
  return out;
}

$("#btn-save-endpoints").onclick = async () => {
  try {
    const { settings } = await api("/api/settings", { method: "PUT", body: JSON.stringify(collect("#f-endpoints")) });
    SETTINGS = settings; $("#probe-msg").textContent = "已保存 ✓"; loadSettings();
  } catch (e) { $("#probe-msg").textContent = "失败：" + e.message; }
};
$("#btn-save-pipeline").onclick = async () => {
  try {
    const { settings } = await api("/api/settings", { method: "PUT", body: JSON.stringify(collect("#f-pipeline")) });
    SETTINGS = settings; $("#pipe-msg").textContent = "已保存 ✓"; loadSettings();
  } catch (e) { $("#pipe-msg").textContent = "失败：" + e.message; }
};

$("#btn-probe").onclick = async () => {
  const msg = $("#probe-msg"), d = $("#probe-detail");
  msg.textContent = "探测中…";
  try {
    const { probe } = await api("/api/probe", { method: "POST", body: "{}" });
    msg.textContent = "探测完成";
    d.innerHTML = "";
    const e1 = probe.embedding || {};
    d.append(el("div", "", `Embedding: ${e1.ok ? `返回 ${e1.dim} 维，设置 ${e1.expect} 维 ${e1.match ? "✓ 匹配" : "✗ 不匹配"}` : "✗ " + e1.error}`));
    const m1 = probe.mineru || {};
    d.append(el("div", "", `MinerU: ${m1.ok ? `✓ ${m1.version || ""}` : "✗ " + m1.error}`));
    const r1 = probe.rerank || {};
    d.append(el("div", "", `Rerank: ${r1.ok ? "✓ 可用" : "✗ " + r1.error}`));
    const v1 = probe.vllm || {};
    d.append(el("div", "", `vLLM: ${v1.ok ? "✓ 模型 " + (v1.models || []).join(", ") : "✗ " + v1.error}`));
  } catch (e) { msg.textContent = "探测失败：" + e.message; }
};

async function loadMdStats() {
  try {
    const { md } = await api("/api/md-stats");
    $("#md-stats").textContent =
      `md 留存：${md.enabled ? "开启" : "关闭"} · 已存 ${fmtN(md.files)} 个 / ${md.mb} MB · 目录 ${md.dir}` +
      `（不作为扫描目录）· 保留 ${md.keep_days ? md.keep_days + " 天" : "永久"} · 解包目录保留 ${md.unpack_keep_days} 天`;
  } catch (e) { /* ignore */ }
}

$("#btn-md-export").onclick = async () => {
  try { await api("/api/md-export", { method: "POST", body: "{}" });
        $("#clean-msg").textContent = "md 补齐已启动"; pollTask(); }
  catch (e) { $("#clean-msg").textContent = "失败：" + e.message; }
};
$("#btn-clean-dry").onclick = async () => {
  try { const r = await api("/api/clean", { method: "POST", body: JSON.stringify({ dry_run: true }) });
        $("#clean-msg").textContent = `演练：可清 ${r.result.removed} 项 / ${r.result.freed_mb} MB`; }
  catch (e) { $("#clean-msg").textContent = "失败：" + e.message; }
};
$("#btn-clean").onclick = async () => {
  try { await api("/api/clean", { method: "POST", body: "{}" });
        $("#clean-msg").textContent = "清理已启动"; pollTask(); }
  catch (e) { $("#clean-msg").textContent = "失败：" + e.message; }
};

$("#btn-reindex").onclick = async () => {
  if (!confirm("清空所有向量并重建？大库会跑很久。")) return;
  try { await api("/api/reindex", { method: "POST", body: "{}" }); $("#pipe-msg").textContent = "重建已启动"; pollTask(); }
  catch (e) { $("#pipe-msg").textContent = "失败：" + e.message; }
};

/* 启动 */
loadStatus();
setInterval(() => { if (!$("#tab-overview").classList.contains("active")) loadStatus(); }, 20000);

/* ---------- 对话与写稿（检索增强，带引用）---------- */
let CHAT_HIST = [];
let KINDS_LOADED = false;

async function initChat() {
  if (KINDS_LOADED) return;
  KINDS_LOADED = true;
  try {
    const r = await api("/api/draft_kinds");
    const sel = $("#chat-kind");
    sel.innerHTML = "";
    (r.kinds || []).forEach(k => sel.append(new Option(k.name, k.key)));
  } catch (e) { /* 拿不到文体清单不影响问答 */ }
}

function chatModeUI() {
  const draft = $("#chat-mode").value === "draft";
  $("#chat-kind").style.display = draft ? "" : "none";
  $("#chat-points").style.display = draft ? "" : "none";
  $("#chat-input").placeholder = draft
    ? "写稿主题，例如：2026 年上半年城市运营管理情况报告"
    : "问点什么，例如：静湖水质监测的频次是怎么规定的";
  $("#btn-chat").textContent = draft ? "起草" : "提问";
}

async function doChat() {
  const mode = $("#chat-mode").value;
  const txt = ($("#chat-input").value || "").trim();
  if (!txt) return;
  const out = $("#chat-out"), btn = $("#btn-chat");
  btn.disabled = true;
  const tip = el("p", "muted", mode === "draft"
    ? "正在检索资料并起草（约 30–90 秒，本地模型独占生成）…"
    : "正在检索并作答（约 10–30 秒）…");
  out.prepend(tip);
  try {
    let resp;
    if (mode === "draft") {
      resp = await api("/api/draft", { method: "POST", body: JSON.stringify({
        topic: txt, kind: $("#chat-kind").value, points: $("#chat-points").value }) });
    } else {
      resp = await api("/api/chat", { method: "POST", body: JSON.stringify({
        q: txt, history: CHAT_HIST.slice(-6) }) });
    }
    tip.remove();
    const card = el("div", "result");
    const head = el("div", "h");
    head.append(el("span", "title", mode === "draft" ? (resp.kind_name || "草稿") : "回答"));
    head.append(el("span", "badge", `证据 ${fmtN(resp.evidence_chars || 0)} 字`));
    card.append(head);
    const body = el("div");
    body.style.whiteSpace = "pre-wrap";
    body.style.lineHeight = "1.75";
    body.textContent = mode === "draft" ? (resp.draft || "") : (resp.answer || "");
    card.append(body);
    const refs = resp.refs || [];
    if (refs.length) {
      const wrap = el("div", "meta");
      wrap.style.marginTop = "10px";
      wrap.append(el("span", "muted", "引用："));
      refs.forEach(r => {
        const chip = el("span", "eg-chip", `[${r.n}] ${r.name}`);
        chip.title = r.path || "";
        chip.onclick = () => showFile(+r.file_id);
        wrap.append(chip);
      });
      card.append(wrap);
    }
    if (mode === "draft") {
      const cp = el("button", "ghost", "复制全文");
      cp.style.marginTop = "8px";
      cp.onclick = () => navigator.clipboard.writeText(body.textContent || "")
        .then(() => { cp.textContent = "已复制"; setTimeout(() => { cp.textContent = "复制全文"; }, 1500); });
      card.append(cp);
    }
    out.prepend(card);
    if (mode !== "draft") {
      CHAT_HIST.push({ role: "user", content: txt },
                     { role: "assistant", content: (resp.answer || "").slice(0, 1200) });
    }
    $("#chat-input").value = "";
  } catch (e) {
    tip.remove();
    out.prepend(el("p", "badge bad", "失败：" + e.message));
  } finally { btn.disabled = false; }
}

$("#btn-chat").onclick = doChat;
$("#chat-mode").onchange = chatModeUI;
$("#chat-input").onkeydown = e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); doChat(); } };

/* ---------- 排除规则（T1.3） ---------- */
const EX_LABEL = { dir: "目录", file: "文件", ext: "扩展名" };

async function loadExclude() {
  const body = $("#ex-body");
  body.innerHTML = "<tr><td colspan=8 class=muted>加载中…</td></tr>";
  try {
    const j = await api("/api/exclude_rules");
    if (!j.rules.length) {
      body.innerHTML = "<tr><td colspan=8 class=empty>还没有排除规则。上面填一个目录或扩展名即可。</td></tr>";
    } else {
      body.innerHTML = j.rules.map(r => `
        <tr>
          <td>${r.id}</td>
          <td>${EX_LABEL[r.kind] || r.kind}</td>
          <td class="path" title="${esc(r.value)}">${esc(r.value)}</td>
          <td>${esc(r.reason || "-")}</td>
          <td>${fmtN(r.hits)}</td>
          <td>${esc(r.last_hit || "-")}</td>
          <td><input type="checkbox" data-ex-toggle="${r.id}" ${r.enabled ? "checked" : ""}></td>
          <td><button class="mini danger" data-ex-del="${r.id}">删除</button></td>
        </tr>`).join("");
    }
    body.querySelectorAll("[data-ex-toggle]").forEach(cb => {
      cb.onchange = async () => {
        try {
          await api(`/api/exclude_rules/${cb.dataset.exToggle}`, {
            method: "PUT", body: JSON.stringify({ enabled: cb.checked }) });
        } catch (e) { alert("改启用状态失败：" + e.message); cb.checked = !cb.checked; }
      };
    });
    body.querySelectorAll("[data-ex-del]").forEach(btn => {
      btn.onclick = async () => {
        if (!confirm("删除这条规则？删掉后重新扫描，被排除的文件会回到库里。")) return;
        try {
          await api(`/api/exclude_rules/${btn.dataset.exDel}`, { method: "DELETE" });
          loadExclude();
        } catch (e) { alert("删除失败：" + e.message); }
      };
    });
  } catch (e) {
    body.innerHTML = `<tr><td colspan=8 class=muted>加载失败：${esc(e.message)}</td></tr>`;
  }
  loadIgnored();
}

async function addExcludeRule() {
  const kind = $("#ex-kind").value;
  const value = $("#ex-value").value.trim();
  const reason = $("#ex-reason").value.trim();
  if (!value) { alert("先把值填上"); return; }
  try {
    await api("/api/exclude_rules", { method: "POST",
      body: JSON.stringify({ kind, value, reason }) });
    $("#ex-value").value = ""; $("#ex-reason").value = "";
    loadExclude();
  } catch (e) { alert("添加失败：" + e.message); }
}

async function loadIgnored() {
  const gbox = $("#ign-groups"), body = $("#ign-body");
  body.innerHTML = "<tr><td colspan=4 class=muted>加载中…</td></tr>";
  try {
    const j = await api("/api/ignored?limit=100");
    gbox.innerHTML = j.groups.map(g =>
      `<span class="chip" title="${esc(g.reason_kind)}">${esc(g.reason_code)}
         <b>${fmtN(g.count)}</b><i>${g.mb} MB</i></span>`).join("") ||
      '<span class="muted">没有被忽略的文件</span>';
    body.innerHTML = j.files.map(f => `
      <tr>
        <td title="${esc(f.path)}">${esc(f.name)}</td>
        <td>${esc(f.reason_code)}</td>
        <td class="muted" title="${esc(f.detail)}">${esc((f.detail || "").slice(0, 60))}</td>
        <td>${fmtN(Math.round(f.kb))} KB</td>
      </tr>`).join("") || "<tr><td colspan=4 class=muted>无</td></tr>";
  } catch (e) {
    body.innerHTML = `<tr><td colspan=4 class=muted>加载失败：${esc(e.message)}</td></tr>`;
  }
}

$("#btn-ex-add").onclick = addExcludeRule;
$("#ex-value").addEventListener("keydown", e => { if (e.key === "Enter") addExcludeRule(); });
