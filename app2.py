"""墨脉（MoMai）— 索引 / 检索 / 对话写稿 / 设置 后端。

存储：PG18 + pgvector(1024)
流水线：扫描 → 递归解包(ofd/zip/tar/gz/rar/7z) → 解析(pdf/docx/xlsx/pptx/ofd) → OCR(mineru) → 切块 → 嵌入
"""
import os
import json
import threading
import traceback
from datetime import datetime

from flask import Flask, jsonify, request, send_from_directory

import db
import ingest
import mineru
import archives
import assistant

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False

TASK = {"running": False, "kind": None, "started": None, "message": "空闲", "result": None}


def ok(**p):
    return jsonify({"ok": True, **p})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


@app.get("/")
def index():
    return send_from_directory(STATIC, "index_v2.html")


@app.get("/<path:fn>")
def static_files(fn):
    return send_from_directory(STATIC, fn)


# ---------------- 状态 ----------------

# ---------- MCP（streamable HTTP）：对外暴露 lingxi 工具 ----------
@app.route("/mcp/lingxi", methods=["POST", "GET", "OPTIONS"])
def mcp_lingxi():
    import mcp_lingxi as ml
    if request.method == "OPTIONS":
        r = app.response_class("", 204)
        r.headers["Access-Control-Allow-Origin"] = "*"
        r.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, Accept, Mcp-Session-Id"
        r.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        return r
    if request.method == "GET":
        # 仅用于探活；真正的调用走 POST JSON-RPC
        return jsonify({"server": ml.SERVER_NAME, "version": ml.SERVER_VERSION,
                        "protocol": ml.PROTOCOL, "tools": [t["name"] for t in ml.TOOLS],
                        "transport": "streamable-http (POST JSON-RPC)"})
    try:
        payload = request.get_json(force=True, silent=True)
        if payload is None:
            return jsonify({"jsonrpc": "2.0", "id": None,
                            "error": {"code": -32700, "message": "Parse error"}}), 400
        out = ml.dispatch(payload)
        if out is None:
            return app.response_class("", 202)
        resp = jsonify(out)
        resp.headers["Mcp-Session-Id"] = "wenmai-lingxi"
        return resp
    except Exception as e:                      # noqa: BLE001
        return jsonify({"jsonrpc": "2.0", "id": None,
                        "error": {"code": -32603, "message": f"Internal error: {e}"}}), 500


# ---------- 章节结构 ----------
@app.get("/api/sections")
def api_sections_get():
    fid = int(request.args.get("file_id") or 0)
    if not fid:
        return fail("需要 file_id")
    try:
        import sections
        return ok(file_id=fid, sections=sections.get(fid))
    except Exception as e:                       # noqa: BLE001
        return fail(f"读取章节失败: {e}", 500)


@app.post("/api/sections")
def api_sections_build():
    body = request.get_json(silent=True) or {}
    fid = int(body.get("file_id") or 0)
    if not fid:
        return fail("需要 file_id")
    n_sum = int(body.get("summarize") or 0)
    try:
        import sections
        n = sections.build(fid)
        if n_sum > 0:
            with db.cursor() as con:
                con.run("""UPDATE file_sections SET summary=NULL
                           WHERE file_id=:f AND section_index IN (
                             SELECT section_index FROM file_sections WHERE file_id=:f
                             ORDER BY section_index LIMIT :n)""", f=fid, n=n_sum)
            sections.summarize_pending(limit=n_sum, idle_only=False, file_id=fid)
        return ok(sections=sections.get(fid), count=n)
    except Exception as e:                       # noqa: BLE001
        return fail(f"构建章节失败: {e}", 500)


# ---------- 文档摘要 ----------
@app.get("/api/summary")
def api_summary_get():
    fid = int(request.args.get("file_id") or 0)
    if not fid:
        return fail("需要 file_id")
    try:
        import summarize
        r = summarize.get(fid)
        return ok(file_id=fid, summary=r)
    except Exception as e:                       # noqa: BLE001
        return fail(f"读取摘要失败: {e}", 500)


@app.post("/api/summary")
def api_summary_make():
    body = request.get_json(silent=True) or {}
    fid = int(body.get("file_id") or 0)
    if not fid:
        return fail("需要 file_id")
    try:
        import summarize
        r = summarize.summarize_file(fid, ingest.settings())
        if not r:
            return fail("生成失败（正文太短或模型没给出有效 JSON）", 500)
        return ok(summary=r)
    except Exception as e:                       # noqa: BLE001
        return fail(f"生成摘要失败: {e}", 500)


# ---------- 实体图谱 + 相关文档 ----------
@app.get("/api/entities")
def api_entities():
    q = (request.args.get("q") or "").strip()
    limit = max(1, min(int(request.args.get("limit") or 40), 200))
    try:
        with db.cursor(commit=False) as con:
            if q:
                rows = con.run("""SELECT e.id, e.type, e.name, coalesce(e.mentions,0),
                                         (SELECT count(DISTINCT m.file_id) FROM entity_mentions m
                                          WHERE m.entity_id=e.id)
                                  FROM entities e
                                  WHERE e.name ILIKE :q
                                  ORDER BY coalesce(e.mentions,0) DESC, e.id
                                  LIMIT :l""", q=f"%{q}%", l=limit)
            else:
                rows = con.run("""SELECT e.id, e.type, e.name, coalesce(e.mentions,0),
                                         (SELECT count(DISTINCT m.file_id) FROM entity_mentions m
                                          WHERE m.entity_id=e.id)
                                  FROM entities e
                                  ORDER BY coalesce(e.mentions,0) DESC, e.id
                                  LIMIT :l""", l=limit)
        items = [{"id": r[0], "type": r[1], "name": r[2], "mentions": r[3], "files": r[4]} for r in rows]
        with db.cursor(commit=False) as con:
            tot = con.run("SELECT count(*) FROM entities")[0][0]
            rel = con.run("SELECT count(*) FROM relations")[0][0]
        return ok(items=items, total=tot, relations=rel)
    except Exception as e:
        return fail(f"读取实体失败: {e}", 500)


@app.get("/api/entity/<int:eid>")
def api_entity_detail(eid):
    try:
        with db.cursor(commit=False) as con:
            row = con.run("""SELECT id,type,name,coalesce(mentions,0) FROM entities WHERE id=:i""", i=eid)
            if not row:
                return fail("实体不存在", 404)
            e = row[0]
            out = con.run("""SELECT DISTINCT r.rel, d.id, d.name, d.type
                             FROM relations r JOIN entities d ON d.id=r.dst_id
                             WHERE r.src_id=:i ORDER BY d.name LIMIT 100""", i=eid)
            inc = con.run("""SELECT DISTINCT r.rel, s.id, s.name, s.type
                             FROM relations r JOIN entities s ON s.id=r.src_id
                             WHERE r.dst_id=:i ORDER BY s.name LIMIT 100""", i=eid)
            files = con.run("""SELECT DISTINCT f.id, f.name, f.real_path, f.ext
                               FROM entity_mentions m JOIN files f ON f.id=m.file_id
                               WHERE m.entity_id=:i LIMIT 50""", i=eid)
        return ok(entity={"id": e[0], "type": e[1], "name": e[2], "mentions": e[3]},
                  out=[{"rel": r[0], "id": r[1], "name": r[2], "type": r[3]} for r in out],
                  inc=[{"rel": r[0], "id": r[1], "name": r[2], "type": r[3]} for r in inc],
                  files=[{"id": f[0], "name": f[1], "path": f[2], "ext": f[3]} for f in files])
    except Exception as e:
        return fail(f"读取实体详情失败: {e}", 500)


@app.get("/api/related")
def api_related():
    fid = int(request.args.get("file_id") or 0)
    limit = max(1, min(int(request.args.get("limit") or 8), 30))
    if not fid:
        return fail("需要 file_id")
    try:
        with db.cursor(commit=False) as con:
            rows = con.run("""
                WITH q AS (SELECT avg(embedding) AS v FROM chunks
                           WHERE file_id=:f AND is_current AND embedding IS NOT NULL),
                     me AS (SELECT content_hash AS h FROM files WHERE id=:f)
                SELECT f.id, f.name, f.real_path, f.ext,
                       max(1 - (c.embedding <=> (SELECT v FROM q))) AS sim,
                       count(*) AS hits
                FROM chunks c JOIN files f ON f.id=c.file_id
                WHERE c.is_current AND c.file_id <> :f AND c.embedding IS NOT NULL
                  AND (f.content_hash IS NULL OR f.content_hash <> (SELECT h FROM me))
                GROUP BY f.id, f.name, f.real_path, f.ext
                ORDER BY sim DESC LIMIT :l""", f=fid, l=limit)
        return ok(items=[{"file_id": r[0], "name": r[1], "path": r[2], "ext": r[3],
                          "sim": round(float(r[4]), 4), "hits": r[5]} for r in rows])
    except Exception as e:
        return fail(f"相关文档计算失败: {e}", 500)


@app.get("/api/status")
def api_status():
    s = ingest.settings()
    with db.cursor(commit=False) as con:
        files = con.run("SELECT count(*) FROM files")[0][0]
        by_status = dict(con.run("SELECT status, count(*) FROM files GROUP BY status"))
        chunks, emb = con.run("SELECT count(*), count(embedding) FROM chunks")[0]
        by_kind = dict(con.run("SELECT coalesce(parse_kind,'-'), count(*) FROM files GROUP BY 1"))
        by_ext = con.run("SELECT ext, count(*) FROM files GROUP BY ext ORDER BY 2 DESC LIMIT 8")
        arcs = con.run("SELECT count(*), coalesce(sum(entries),0), coalesce(sum(bytes),0) FROM archives")[0]
        srcs = con.run("SELECT id,name,root_path,enabled,exclude FROM sources ORDER BY id")
        dims = con.run("SELECT DISTINCT vector_dims(embedding) FROM chunks WHERE embedding IS NOT NULL")
    return ok(status={
        "site_title": s["site_title"],
        "files": files, "by_status": by_status, "chunks": chunks, "embedded": emb,
        "by_kind": by_kind, "by_ext": {k or "-": v for k, v in by_ext},
        "archives": {"count": arcs[0], "entries": int(arcs[1] or 0), "bytes": int(arcs[2] or 0)},
        "sources": [{"id": i, "name": n, "root": r, "enabled": e, "exclude": ex or []}
                    for i, n, r, e, ex in srcs],
        "dims": [d[0] for d in dims],
        "task": TASK,
        "endpoints": {"embedding": db.resolve_endpoint(s, "embed_url"),
                       "mineru": db.resolve_endpoint(s, "mineru_url"),
                       "vllm": db.resolve_endpoint(s, "vllm_url")},
    })


@app.get("/api/progress")
def api_progress():
    """三段漏斗：已入库 / 已识别 / 已向量化，分母 = 磁盘可入库文件总数。"""
    s = ingest.settings()
    with db.cursor(commit=False) as con:
        ingested = con.run("SELECT count(*) FROM files WHERE container_id IS NULL")[0][0]
        children = con.run("SELECT count(*) FROM files WHERE container_id IS NOT NULL")[0][0]
        recognized = con.run(
            """SELECT count(*) FROM files
               WHERE container_id IS NULL
                 AND (status='parsed' OR coalesce(text_bytes,0)>0)""")[0][0]
        vec_files = con.run(
            """SELECT count(DISTINCT c.file_id) FROM chunks c JOIN files f ON f.id=c.file_id
               WHERE c.embedding IS NOT NULL AND c.is_current AND f.container_id IS NULL""")[0][0]
        chunks, emb = con.run("SELECT count(*), count(embedding) FROM chunks")[0]
        ocr_done = con.run("SELECT count(*) FROM files WHERE parse_kind='pdf-ocr'")[0][0]
        ocr_wait = con.run(
            "SELECT count(*) FROM files WHERE status='failed' AND parse_kind='pdf-scan'")[0][0]
        indexable = con.run(
            "SELECT count(*) FROM files WHERE container_id IS NULL AND status<>'skipped'")[0][0]
    disk_seen = int(s.get("disk_files_seen") or 0)
    return ok(progress={
        "total_disk": disk_seen,
        "total": indexable,
        "disk_seen": disk_seen,
        "scan_at": s.get("scan_at") or "",
        "ingested": ingested,
        "children": children,
        "recognized": recognized,
        "vectorized": vec_files,
        "chunks": chunks,
        "chunks_embedded": emb,
        "ocr_done": ocr_done,
        "ocr_wait": ocr_wait,
    })


# ---------------- 数据源 ----------------

@app.get("/api/sources")
def api_sources():
    with db.cursor(commit=False) as con:
        rows = con.run("SELECT id,name,root_path,enabled,exclude,created_at FROM sources ORDER BY id")
    return ok(sources=[{"id": r[0], "name": r[1], "root": r[2], "enabled": r[3],
                        "exclude": r[4] or [], "created": str(r[5])} for r in rows])


@app.post("/api/sources")
def api_add_source():
    d = request.get_json(force=True) or {}
    name, root = (d.get("name") or "").strip(), (d.get("root") or "").strip()
    if not name or not root:
        return fail("name 和 root 都必填")
    if not os.path.exists(root):
        return fail(f"路径不存在: {root}")
    with db.cursor() as con:
        con.run("INSERT INTO sources(name,root_path) VALUES (:n,:p) ON CONFLICT (root_path) DO NOTHING", n=name, p=root)
    return ok(sources=api_sources().get_json()["sources"])


@app.put("/api/sources/<int:sid>")
def api_update_source(sid):
    d = request.get_json(force=True) or {}
    sets, args = [], {"i": sid}
    if "name" in d:
        sets.append("name=:n"); args["n"] = (d["name"] or "").strip() or "未命名"
    if "enabled" in d:
        sets.append("enabled=:en"); args["en"] = bool(d["enabled"])
    if "exclude" in d:
        ex = d["exclude"]
        if isinstance(ex, str):
            ex = [x.strip() for x in ex.replace("\n", ",").split(",") if x.strip()]
        sets.append("exclude=:ex::jsonb"); args["ex"] = json.dumps(ex, ensure_ascii=False)
    if not sets:
        return fail("没有可更新的字段")
    with db.cursor() as con:
        con.run(f"UPDATE sources SET {', '.join(sets)} WHERE id=:i", **args)
    return ok(updated=sid)


@app.delete("/api/sources/<int:sid>")
def api_del_source(sid):
    with db.cursor() as con:
        con.run("DELETE FROM sources WHERE id=:i", i=sid)
    return ok(deleted=sid)


# ---------------- 后台任务 ----------------

def _run(kind, fn):
    def wrapper():
        TASK.update(running=True, kind=kind, started=datetime.now().strftime("%H:%M:%S"), message="运行中", result=None)
        try:
            fn()
            TASK.update(message="完成", result="ok")
        except Exception as e:
            traceback.print_exc()
            TASK.update(message=f"失败: {e}", result="error")
        finally:
            TASK["running"] = False
    threading.Thread(target=wrapper, daemon=True).start()


@app.post("/api/scan")
def api_scan():
    if TASK["running"]:
        return fail("已有任务在跑，请稍后再试", 409)
    limit = int((request.get_json(silent=True) or {}).get("limit", 0))
    _run("scan", lambda: ingest.scan(limit))
    return ok(started="scan")


@app.post("/api/index")
def api_index():
    if TASK["running"]:
        return fail("已有任务在跑，请稍后再试", 409)
    d = request.get_json(silent=True) or {}
    _run("index", lambda: ingest.index(int(d.get("limit", 0)), bool(d.get("embed", True))))
    return ok(started="index")


@app.post("/api/ocr")
def api_ocr():
    """把 failed/pdf-scan 的文件批量送 mineru（可限条数）。"""
    if TASK["running"]:
        return fail("已有任务在跑，请稍后再试", 409)
    d = request.get_json(silent=True) or {}
    limit = int(d.get("limit", 5))
    _run("ocr", lambda: ingest.ocr_batch(limit))
    return ok(started="ocr")


def _legacy_ocr_body():
    """（保留旧实现做参考，不注册路由）"""
    if TASK["running"]:
        return fail("已有任务在跑，请稍后再试", 409)
    d = request.get_json(silent=True) or {}
    limit = int(d.get("limit", 5))

    def job():
        s = ingest.settings()
        with db.cursor(commit=False) as con:
            rows = con.run("""SELECT id,real_path,name FROM files
                              WHERE status='failed' AND parse_kind='pdf-scan'
                              ORDER BY size LIMIT :l""", l=limit)
        for fid, rpath, name in rows:
            try:
                text, meta = mineru.parse_file(s["mineru_url"], rpath, file_name=name,
                                               ocr_mode="ocr", poll_sec=int(s["mineru_poll_sec"]),
                                               timeout_sec=1800)
                if not text.strip():
                    continue
                ch = ingest.chunk_document(text, int(s["chunk_size"]), int(s["chunk_overlap"]))
                vecs = ingest.embed_texts([c[1] for c in ch], s)
                with db.cursor() as con:
                    con.run("DELETE FROM chunks WHERE file_id=:i", i=fid)
                    for i2, (hd, c) in enumerate(ch):
                        con.run("""INSERT INTO chunks(file_id,seq,heading,content,token_count,embedding)
                                   VALUES (:f,:s,:h,:c,:t,:e::vector)""",
                                f=fid, s=i2, h=hd, c=c, t=len(c) // 2, e=db.vec_literal(vecs[i2]))
                    con.run("""UPDATE files SET status='parsed', parse_kind='pdf-ocr',
                               text_bytes=:tb, error='mineru', updated_at=now() WHERE id=:i""",
                            tb=len(text.encode()), i=fid)
                TASK["message"] = f"OCR 完成 {name[:30]}"
            except Exception as e:
                with db.cursor() as con:
                    con.run("UPDATE files SET error=:e, updated_at=now() WHERE id=:i",
                            e=f"OCR失败: {e}"[:300], i=fid)
                TASK["message"] = f"OCR 失败 {name[:20]}: {e}"

    _run("ocr", job)
    return ok(started="ocr")


@app.get("/api/invariants")
def api_invariants():
    """不变量体检：给看门狗吃的**判定依据**，不是给人看的报表。

    设计原则：这里只返回**能判定健康/不健康的事实**（计数、时长、偏差），
    不含任何"解释"。解释和播报交给看门狗，服务端不猜阈值——
    阈值随规模变化，写死在服务里迟早要改两处。

    每一项都对应一类静默故障：
      policies_missing   有文件绕过策略表 → 报表从此不可信
      qdrant_drift       双写失步 → 检索少内容，界面完全正常
      embedding_broken   向量空/维数不对 → 静默漏检
      queue_stuck        worker 挂了 → 队列涨但没人处理
      stalled_*          夜间批跑失效
    """
    out = {}
    try:
        with db.cursor(commit=False) as con:
            out["files"] = int(con.run("SELECT count(*) FROM files")[0][0])
            out["policies"] = int(con.run("SELECT count(*) FROM file_index_policies")[0][0])
            out["unclassified"] = int(con.run("""
                SELECT count(*) FROM files f
                WHERE NOT EXISTS (SELECT 1 FROM file_index_policies p WHERE p.file_id=f.id)""")[0][0])
            out["chunks_current"] = int(con.run("SELECT count(*) FROM chunks WHERE is_current")[0][0])
            out["embedding_null"] = int(con.run("""
                SELECT count(*) FROM chunks WHERE is_current AND embedding IS NULL""")[0][0])
            out["embedding_dim_bad"] = int(con.run("""
                SELECT count(*) FROM chunks
                WHERE is_current AND embedding IS NOT NULL AND vector_dims(embedding) <> 1024""")[0][0])
            out["files_pending"] = int(con.run("""
                SELECT count(*) FROM files WHERE status IN ('pending','defer')""")[0][0])
            out["files_failed"] = int(con.run("""
                SELECT count(*) FROM files WHERE status='failed'""")[0][0])
            h = con.run("""
                SELECT count(*) FILTER (WHERE status='parsed'), count(*) FILTER (WHERE status='failed')
                FROM files WHERE updated_at > now() - interval '1 hour'""")[0]
            out["last_hour_parsed"], out["last_hour_failed"] = int(h[0] or 0), int(h[1] or 0)

            q = con.run("""
                SELECT count(*), coalesce(extract(epoch FROM now()-min(observed_at)),0)
                FROM file_change_queue WHERE processed_at IS NULL""")[0]
            out["change_queue_pending"], out["change_queue_oldest_sec"] = int(q[0]), int(q[1] or 0)

            o = con.run("""
                SELECT count(*), coalesce(extract(epoch FROM now()-min(enqueued_at)),0)
                FROM ocr_queue WHERE status IN ('pending','processing')""")[0]
            out["ocr_queue_pending"], out["ocr_queue_oldest_sec"] = int(o[0]), int(o[1] or 0)

            r = con.run("""
                SELECT count(*) FROM file_index_policies
                WHERE next_retry_at IS NOT NULL AND next_retry_at < now() - interval '2 hours'""")[0]
            out["retry_overdue"] = int(r[0])

            cov = con.run("""
                SELECT (SELECT count(*) FROM doc_summaries),
                       (SELECT count(DISTINCT file_id) FROM doc_summaries),
                       (SELECT count(*) FROM files WHERE status='parsed')""")[0]
            out["summaries"], out["summaries_files"], out["parsed_files"] = (
                int(cov[0] or 0), int(cov[1] or 0), int(cov[2] or 0))
            ent = con.run("SELECT count(*) FROM entities")[0][0]
            out["entities"] = int(ent)
    except Exception as e:
        return fail(f"不变量取数失败：{type(e).__name__}: {e}", 500)

    # Qdrant 点数：与 current 块比，差值是双写健康度
    s = ingest.settings()
    try:
        import qdrant_store as qs
    except Exception as e:                                   # noqa: BLE001
        qs = None
        out["qdrant_error"] = f"模块导入失败: {e}"[:200]
    out["qdrant_enabled"] = bool(qs and qs.enabled(s))
    if out["qdrant_enabled"]:
        try:
            hh = qs.health(s) or {}
            out["qdrant_points"] = int(hh.get("points") or hh.get("points_count") or 0)
            out["qdrant_drift"] = out["chunks_current"] - out["qdrant_points"]
            out["qdrant_drift_pct"] = round(
                100.0 * abs(out["qdrant_drift"]) / max(out["chunks_current"], 1), 2)
        except Exception as e:
            out["qdrant_error"] = f"{type(e).__name__}: {e}"[:200]
    out["server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return ok(invariants=out)


@app.get("/api/format-report")
def api_format_report():
    """格式体检：按「能不能解析」给每个扩展名定性 + 列出不支持但库里有文件的格式。

    纪律（踩过一次 500）：
    ① **不再硬引用常量名**。此前的实现直接读 `X.HTML_EXTS / X.MHTML_EXTS / X.RTF_EXTS /
       X.DBF_EXTS / X.ODF_EXTS`，这些在 extract.py 里根本不存在 → 接口 100% 500，
       看门狗连续报警三天都没人发现是代码问题而不是数据问题。
       现在统一走 `getattr(X, 名字, None)`，取不到就跳过，并回报实际用了哪些分组。
    ② **算不出来就回缓存**，不把 500 甩给调用方。缓存里带 `cached_at` 与 `stale:true`，
       看门狗据此区分「数据有问题」与「接口暂时不可用」。
    """
    # 缓存目录：优先环境变量，其次 md_dir 同级（都在 /tank/files 下，随备份一起走）
    _md = ""
    try:
        _md = (ingest.settings() or {}).get("md_dir") or ""
    except Exception:
        _md = ""
    cache_dir = os.environ.get("MOMAI_CACHE_DIR") or (
        os.path.join(os.path.dirname(_md), "wenmai-cache") if _md else "/tank/files/wenmai-cache")
    cache_path = os.path.join(cache_dir, "format_report.json")

    try:
        report = _compute_format_report()
        try:
            os.makedirs(cache_dir, exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False)
        except Exception as e:
            report["cache_write_error"] = str(e)[:200]
        return ok(report=report)
    except Exception as e:
        if os.path.exists(cache_path):
            try:
                cached = json.load(open(cache_path, encoding="utf-8"))
                cached["stale"] = True
                cached["stale_reason"] = f"{type(e).__name__}: {e}"[:300]
                cached["served_from_cache"] = True
                return ok(report=cached)
            except Exception:
                pass
        return fail(f"格式报表生成失败，且无可用缓存：{type(e).__name__}: {e}")


def _compute_format_report():
    import extract as X

    groups_used, missing = [], []
    supported = set()
    for name in ("TEXT_EXTS", "HTML_EXTS", "MHTML_EXTS", "RTF_EXTS", "DBF_EXTS",
                 "PDF_EXTS", "ODF_EXTS", "LEGACY_OFFICE", "OFFICE_EXTS", "IMAGE_EXTS"):
        val = getattr(X, name, None)
        if isinstance(val, (set, list, tuple)):
            supported |= set(val)
            groups_used.append(name)
        else:
            missing.append(name)
    dispatch = getattr(X, "OOXML_DISPATCH", None)
    if isinstance(dispatch, dict):
        supported |= set(dispatch)
        groups_used.append("OOXML_DISPATCH")
    supported |= {".ofd"}
    unsupportable = dict(getattr(X, "UNSUPPORTABLE", {}) or {})

    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT coalesce(ext,'-') AS ext, status,
                   coalesce(parse_kind,'-') AS kind, count(*) AS n,
                   coalesce(sum(size),0) AS bytes,
                   (array_agg(path ORDER BY path))[1] AS sample
            FROM files GROUP BY 1,2,3 ORDER BY n DESC""")
        pol = con.run("""SELECT policy, reason_code, count(*) FROM file_index_policies
                         GROUP BY 1,2 ORDER BY 3 DESC""")

    by_ext = {}
    for ext, status, kind, n, bts, sample in rows:
        e = by_ext.setdefault(ext, {"ext": ext, "files": 0, "mb": 0.0, "parsed": 0,
                                    "skipped": 0, "failed": 0, "pending": 0,
                                    "kinds": [], "sample": sample})
        e["files"] += int(n)
        e["mb"] = round(e["mb"] + float(bts or 0) / 1048576, 1)
        if status in ("parsed", "duplicate"):
            e["parsed"] += int(n)
        elif status == "skipped":
            e["skipped"] += int(n)
        elif status == "failed":
            e["failed"] += int(n)
        else:
            e["pending"] += int(n)
        if kind not in ("-", None) and kind not in e["kinds"]:
            e["kinds"].append(kind)

    parsed_ok = [e for e in by_ext.values() if e["parsed"] > 0]
    # 「不支持」的判据用实测结果，不是靠清单猜：解析成功过就是支持
    unsupported = [e for e in by_ext.values()
                   if e["parsed"] == 0 and (e["failed"] + e["skipped"]) > 0
                   and e["ext"] not in supported]

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "stale": False,
        "supported_ext_count": len(supported),
        "groups_used": groups_used,
        "groups_missing": missing,          # 显式暴露：常量改名/缺失会立刻看得见
        "parsed_exts": sorted((e["ext"] for e in parsed_ok)),
        "unsupported": sorted(
            ({"ext": e["ext"], "files": e["files"], "mb": e["mb"],
              "failed": e["failed"], "skipped": e["skipped"], "sample": e["sample"]}
             for e in unsupported), key=lambda x: -x["files"]),
        "by_ext": sorted(by_ext.values(), key=lambda x: -x["files"]),
        "policies": [{"policy": p[0], "reason_code": p[1], "count": int(p[2])} for p in pol],
        "unsupportable_hint": unsupportable,
        "note": "被排除的格式见 /api/policy-report 的 policy=never/out_of_scope_*",
    }


@app.get("/api/policy-report")
def api_policy_report():
    """按 file_index_policies 出报表：每个文件为什么进/不进库，按原因码聚合。

    与 /api/format-report 的区别：那个按扩展名猜"支持不支持"，
    这个直接读策略表——是**实际决定**的记录，不是推断。
    """
    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT p.policy, p.reason_code,
                   array_to_string(array_agg(DISTINCT coalesce(p.reason_kind,'-')), ','),
                   count(*) AS n, coalesce(sum(f.size),0) AS bytes,
                   (array_agg(f.ext ORDER BY f.size DESC))[1] AS top_ext,
                   (array_agg(f.path ORDER BY f.size DESC))[1:5] AS samples
            FROM file_index_policies p JOIN files f ON f.id = p.file_id
            GROUP BY 1,2 ORDER BY n DESC""")
        unclassified = con.run("""
            SELECT count(*) FROM files f LEFT JOIN file_index_policies p ON p.file_id=f.id
            WHERE p.file_id IS NULL""")[0][0]

    groups = []
    for policy, code, kind, n, bts, top_ext, samples in rows:
        groups.append({
            "policy": policy, "reason_code": code, "reason_kind": kind,
            "count": int(n), "mb": round(float(bts or 0) / 1048576, 1),
            "top_ext": top_ext, "samples": list(samples or []),
        })

    by_policy, by_code = {}, {}
    for g in groups:
        by_policy[g["policy"]] = by_policy.get(g["policy"], 0) + g["count"]
        by_code[g["reason_code"]] = by_code.get(g["reason_code"], 0) + g["count"]

    # 需要人看的三类：不支持格式 / 待 OCR 的图片 / 解析失败
    actionable = {
        "unsupported": [g for g in groups if g["reason_code"].startswith("unsupported")],
        "image_pending": [g for g in groups if g["reason_code"] == "image_needs_ocr"],
        "failed": [g for g in groups if g["policy"] == "defer"],
    }
    import hashlib
    sig = hashlib.sha256(
        ("|".join(f"{g['reason_code']}:{g['count']}" for g in sorted(groups, key=lambda x: x["reason_code"]))).encode()
    ).hexdigest()[:16]

    return ok(report={
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "signature": sig,                       # 内容指纹：报表没变就不重复推送
        "unclassified": int(unclassified),       # 必须为 0，否则说明有新文件绕过了策略表
        "groups": groups,
        "by_policy": by_policy,
        "by_reason": by_code,
        "actionable": actionable,
    })


# ---------------- 排除规则（T1.3） ----------------

@app.get("/api/exclude_rules")
def api_exclude_rules():
    """列出排除规则 + 命中计数。命中数：file/ext 规则=跳过文件数，dir 规则=整枝跳过次数。"""
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, kind, value, coalesce(reason,''), enabled, hits,
                                 to_char(last_hit_at,'YYYY-MM-DD HH24:MI'), created_at
                          FROM exclude_rules ORDER BY kind, id""")
    rules = [{"id": r[0], "kind": r[1], "value": r[2], "reason": r[3], "enabled": r[4],
              "hits": int(r[5] or 0), "last_hit": r[6],
              "created_at": r[7].strftime("%Y-%m-%d %H:%M") if r[7] else None} for r in rows]
    return ok(rules=rules,
              kinds=[{"kind": "dir", "label": "目录（整枝跳过）"},
                     {"kind": "file", "label": "单个文件"},
                     {"kind": "ext", "label": "扩展名"}])


@app.post("/api/exclude_rules")
def api_exclude_rule_add():
    d = request.get_json(force=True, silent=True) or {}
    kind = (d.get("kind") or "dir").strip()
    value = (d.get("value") or "").strip()
    reason = (d.get("reason") or "").strip() or None
    if kind not in ("dir", "file", "ext"):
        return fail("kind 只能是 dir / file / ext")
    if not value:
        return fail("value 不能为空")
    # 目录/文件必须存在：存一个不存在的路径等于给未来埋雷
    if kind == "dir" and not os.path.isdir(value):
        return fail(f"目录不存在：{value}")
    if kind == "file" and not os.path.isfile(value):
        return fail(f"文件不存在：{value}")
    if kind == "ext" and not value.startswith("."):
        value = "." + value
    try:
        with db.cursor() as con:
            r = con.run("""INSERT INTO exclude_rules(kind, value, reason)
                           VALUES (:k,:v,:r) ON CONFLICT (kind, value) DO NOTHING
                           RETURNING id""", k=kind, v=value, r=reason)
    except Exception as e:
        return fail(f"写入失败：{e}")
    if not r:
        return fail("这条规则已存在")
    return ok(id=r[0][0])


@app.put("/api/exclude_rules/<int:rid>")
def api_exclude_rule_update(rid):
    d = request.get_json(force=True, silent=True) or {}
    sets, params = [], {"i": rid}
    if "enabled" in d:
        sets.append("enabled=:e")
        params["e"] = bool(d["enabled"])
    if "reason" in d:
        sets.append("reason=:r")
        params["r"] = (d["reason"] or "").strip() or None
    if not sets:
        return fail("没有要改的字段")
    with db.cursor() as con:
        con.run(f"UPDATE exclude_rules SET {', '.join(sets)} WHERE id=:i", **params)
    return ok(changed=rid)


@app.delete("/api/exclude_rules/<int:rid>")
def api_exclude_rule_delete(rid):
    with db.cursor() as con:
        con.run("DELETE FROM exclude_rules WHERE id=:i", i=rid)
    return ok(deleted=rid)


@app.get("/api/ignored")
def api_ignored():
    """当前被忽略的文件：按原因码聚合 + 明细。数据来自策略表，不另存一份真相。"""
    limit = min(int(request.args.get("limit") or 100), 500)
    reason = (request.args.get("reason") or "").strip()
    where = "p.policy='never'"
    params = {}
    if reason:
        where += " AND p.reason_code=:r"
        params["r"] = reason
    with db.cursor(commit=False) as con:
        groups = con.run(f"""SELECT p.reason_code, coalesce(p.reason_kind,'-'), count(*),
                                    round(sum(coalesce(f.size,0))/1048576.0, 1)
                             FROM file_index_policies p JOIN files f ON f.id=p.file_id
                             WHERE {where} GROUP BY 1,2 ORDER BY 3 DESC""", **params)
        rows = con.run(f"""SELECT f.name, f.path, p.reason_code, coalesce(p.reason_detail,''),
                                  round(coalesce(f.size,0)/1024.0, 1)
                           FROM file_index_policies p JOIN files f ON f.id=p.file_id
                           WHERE {where} ORDER BY f.size DESC NULLS LAST LIMIT :l""",
                       l=limit, **params)
    return ok(groups=[{"reason_code": g[0], "reason_kind": g[1], "count": int(g[2]),
                       "mb": float(g[3] or 0)} for g in groups],
              files=[{"name": r[0], "path": r[1], "reason_code": r[2],
                      "detail": r[3], "kb": float(r[4] or 0)} for r in rows])


# ---------------- 检索 ----------------

@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    mode = request.args.get("mode") or "hybrid"
    limit = min(int(request.args.get("limit", 20)), 100)
    ext = (request.args.get("ext") or "").strip()
    if not q:
        return ok(results=[], mode=mode)
    import search as S
    try:
        r = S.search(q, mode=mode, limit=limit, ext=ext)
    except Exception as e:
        return fail(f"检索失败: {e}", 500)
    return ok(**r)


@app.get("/api/draft_kinds")
def api_draft_kinds():
    """文体清单，前端下拉用。"""
    return ok(kinds=[{"key": k, "name": v[0]} for k, v in assistant.KINDS.items()])


@app.post("/api/chat")
def api_chat():
    """对话问答：三路召回取证据 → 本地 8001 生成 → 带引用返回。

    非流式：8001 单次 7s 起步，前端用 loading 占位；上 SSE 留到链路稳定之后。
    """
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q:
        return fail("问题为空")
    mode = body.get("mode") or "hybrid"
    try:
        r = assistant.answer(q, history=body.get("history") or [], mode=mode)
    except Exception as e:                                    # noqa: BLE001
        traceback.print_exc()
        return fail(f"对话失败: {e}", 500)
    return ok(**r)


@app.post("/api/draft")
def api_draft():
    """写稿：按文体骨架 + 资料事实起草，缺要素输出【待补：xx】。"""
    body = request.get_json(silent=True) or {}
    try:
        r = assistant.draft(body.get("topic") or "",
                            kind=body.get("kind") or "report",
                            points=body.get("points") or "")
    except Exception as e:                                    # noqa: BLE001
        traceback.print_exc()
        return fail(f"写稿失败: {e}", 500)
    return ok(**r)


@app.get("/api/search_legacy")
def api_search_legacy():
    q = (request.args.get("q") or "").strip()
    mode = request.args.get("mode") or "hybrid"
    limit = min(int(request.args.get("limit", 20)), 100)
    ext = (request.args.get("ext") or "").strip()
    if not q:
        return ok(results=[], mode=mode)
    s = ingest.settings()
    results, seen = [], set()
    where = "c.embedding IS NOT NULL"
    args = {}
    if ext:
        where += " AND f.ext = :ext"
        args["ext"] = ext.lower()

    if mode in ("hybrid", "semantic"):
        try:
            qv = ingest.embed_texts([q], s)[0]
            with db.cursor(commit=False) as con:
                rows = con.run(f"""
                    SELECT f.id, f.name, f.ext, f.path, f.parse_kind, c.seq, c.heading,
                           left(c.content, 300) AS snippet,
                           1 - (c.embedding <=> :v::vector) AS score
                    FROM chunks c JOIN files f ON f.id = c.file_id
                    WHERE {where}
                    ORDER BY c.embedding <=> :v::vector LIMIT :lim""",
                    v=db.vec_literal(qv), lim=limit, **args)
            for r in rows:
                key = (r[0], r[5])
                if key in seen:
                    continue
                seen.add(key)
                results.append({"file_id": r[0], "name": r[1], "ext": r[2], "path": r[3],
                                "parse_kind": r[4], "seq": r[5], "heading": r[6],
                                "snippet": r[7], "score": round(float(r[8]), 4), "source": "semantic"})
        except Exception as e:
            results.append({"name": "语义检索不可用", "snippet": str(e), "source": "error", "score": 0})

    if mode in ("hybrid", "keyword"):
        with db.cursor(commit=False) as con:
            rows = con.run(f"""
                SELECT f.id, f.name, f.ext, f.path, f.parse_kind, c.seq, c.heading,
                       left(c.content, 300) AS snippet,
                       similarity(c.content, :kw) AS score
                FROM chunks c JOIN files f ON f.id = c.file_id
                WHERE c.content ILIKE :like {('AND f.ext = :ext' if ext else '')}
                ORDER BY score DESC LIMIT :lim""",
                kw=q, like=f"%{q}%", lim=limit, **args)
        for r in rows:
            key = (r[0], r[5])
            if key in seen:
                continue
            seen.add(key)
            results.append({"file_id": r[0], "name": r[1], "ext": r[2], "path": r[3],
                            "parse_kind": r[4], "seq": r[5], "heading": r[6],
                            "snippet": r[7], "score": round(float(r[8] or 0), 4), "source": "keyword"})
    results.sort(key=lambda x: -(x.get("score") or 0))
    return ok(results=results[:limit], mode=mode, count=len(results))


@app.get("/api/files")
def api_files():
    status = request.args.get("status")
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 100)), 500)
    sql = """SELECT f.id,f.name,f.ext,f.path,f.status,f.parse_kind,f.size,f.text_bytes,
                    (SELECT count(*) FROM chunks c WHERE c.file_id=f.id), f.depth, f.error
             FROM files f WHERE 1=1"""
    args = {}
    if status:
        sql += " AND f.status=:st"; args["st"] = status
    if q:
        sql += " AND (f.name ILIKE :q OR f.path ILIKE :q)"; args["q"] = f"%{q}%"
    sql += " ORDER BY f.id DESC LIMIT :lim"; args["lim"] = limit
    with db.cursor(commit=False) as con:
        rows = con.run(sql, **args)
    keys = ["id", "name", "ext", "path", "status", "parse_kind", "size", "text_bytes", "chunks", "depth", "error"]
    return ok(files=[dict(zip(keys, r)) for r in rows])


@app.get("/api/files/<int:fid>")
def api_file(fid):
    with db.cursor(commit=False) as con:
        r = con.run("""SELECT id,name,path,real_path,ext,size,status,parse_kind,text_bytes,
                              content_hash,container_id,depth,error,updated_at FROM files WHERE id=:i""", i=fid)
        if not r:
            return fail("文件不存在", 404)
        ch = con.run("""SELECT seq,heading,content,token_count,(embedding IS NOT NULL)
                        FROM chunks WHERE file_id=:i ORDER BY seq LIMIT 50""", i=fid)
    keys = ["id", "name", "path", "real_path", "ext", "size", "status", "parse_kind",
            "text_bytes", "content_hash", "container_id", "depth", "error", "updated_at"]
    d = dict(zip(keys, r[0]))
    d["updated_at"] = str(d["updated_at"])
    d["chunks"] = [{"seq": c[0], "heading": c[1], "content": c[2][:2000],
                    "tokens": c[3], "embedded": c[4]} for c in ch]
    return ok(file=d)


# ---------------- 设置 ----------------

@app.get("/api/settings")
def api_get_settings():
    return ok(settings=ingest.settings())


@app.put("/api/settings")
def api_put_settings():
    patch = request.get_json(force=True) or {}
    from schema_v2 import DEFAULT_SETTINGS_V2
    unknown = [k for k in patch if k not in DEFAULT_SETTINGS_V2]
    with db.cursor() as con:
        for k, v in patch.items():
            if k in DEFAULT_SETTINGS_V2:
                con.run("INSERT INTO settings(key,value) VALUES (:k,:v::jsonb) "
                        "ON CONFLICT (key) DO UPDATE SET value=:v::jsonb, updated_at=now()",
                        k=k, v=json.dumps(v, ensure_ascii=False))
    return ok(settings=ingest.settings(), ignored=unknown)


@app.post("/api/probe")
def api_probe():
    """探测三个接入点：embedding 维度 / mineru 健康 / vllm 模型列表。"""
    s = ingest.settings()
    out = {}
    # embedding
    try:
        v = ingest.embed_texts(["维度探测"], s)[0]
        out["embedding"] = {"ok": True, "dim": len(v), "expect": int(s["embed_dim"]),
                            "match": len(v) == int(s["embed_dim"])}
    except Exception as e:
        out["embedding"] = {"ok": False, "error": str(e)[:200]}
    # mineru
    try:
        import urllib.request
        with urllib.request.urlopen(s["mineru_url"].rstrip("/") + "/v1/health", timeout=8) as r:
            out["mineru"] = {"ok": True, **json.loads(r.read())}
    except Exception as e:
        out["mineru"] = {"ok": False, "error": str(e)[:200]}
    # rerank
    try:
        import rerank as RR
        out["rerank"] = RR.health(s)
    except Exception as e:
        out["rerank"] = {"ok": False, "error": str(e)[:200]}
    # qdrant（副本，失败不影响主库）
    try:
        import qdrant_store as qstore
        out["qdrant"] = qstore.health(s)
    except Exception as e:                       # noqa: BLE001
        out["qdrant"] = {"ok": False, "error": str(e)}
    # vllm
    try:
        import urllib.request
        with urllib.request.urlopen(db.require_endpoint(s, "vllm_url") + "/models", timeout=8) as r:
            d = json.loads(r.read())
            out["vllm"] = {"ok": True, "models": [m.get("id") for m in d.get("data", [])][:8]}
    except Exception as e:
        out["vllm"] = {"ok": False, "error": str(e)[:200]}
    return ok(probe=out)


@app.post("/api/clean")
def api_clean():
    if TASK["running"]:
        return fail("已有任务在跑", 409)
    dry = bool((request.get_json(silent=True) or {}).get("dry_run"))
    result = {}
    if dry:
        result = ingest.clean(dry_run=True)
        return ok(result=result)
    _run("clean", lambda: result.update(ingest.clean(dry_run=False) or {}))
    return ok(started="clean")


@app.post("/api/md-export")
def api_md_export():
    if TASK["running"]:
        return fail("已有任务在跑", 409)
    _run("md-export", lambda: ingest.md_export(0))
    return ok(started="md-export")


@app.get("/api/md-stats")
def api_md_stats():
    s = ingest.settings()
    try:
        with db.cursor(commit=False) as con:
            n, b = con.run("SELECT count(*), coalesce(sum(bytes),0) FROM md_files")[0]
    except Exception:
        n, b = 0, 0
    return ok(md={"enabled": s.get("md_enabled"), "dir": s.get("md_dir"),
                  "files": n, "mb": round(float(b or 0) / 1048576, 1),
                  "keep_days": s.get("md_keep_days"),
                  "unpack_keep_days": s.get("unpack_keep_days")})


@app.post("/api/reindex")
def api_reindex():
    """清空向量后重建（用于改维度/换模型）。"""
    if TASK["running"]:
        return fail("已有任务在跑", 409)

    def job():
        with db.cursor() as con:
            con.run("UPDATE chunks SET embedding=NULL")
        ingest.index(0, True)

    _run("reindex", job)
    return ok(started="reindex")


if __name__ == "__main__":
    ingest.init_schema()
    os.makedirs("/tank/files/wenmai/unpacked", exist_ok=True)
    app.run(host="0.0.0.0", port=int(os.environ.get("WENMAI_PORT", "8800")), threaded=True)
