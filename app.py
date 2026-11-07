"""灵犀文脉（NAS 版）— Flask 后端 API + 静态前端。"""
import os
import json
import re
import traceback
from datetime import datetime

import yaml
from flask import Flask, jsonify, request, send_from_directory, abort

import db
import compiler

BASE = os.path.dirname(os.path.abspath(__file__))
STATIC = os.path.join(BASE, "static")
KIND_DIR = {"entity": "entities", "concept": "concepts", "synthesis": "syntheses",
            "source": "sources", "report": "reports"}

app = Flask(__name__, static_folder=None)
app.json.ensure_ascii = False


def ok(**payload):
    return jsonify({"ok": True, **payload})


def fail(msg, code=400):
    return jsonify({"ok": False, "error": str(msg)}), code


# ---------------- 静态前端 ----------------

@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(STATIC, filename)


# ---------------- 状态 ----------------

@app.get("/api/status")
def api_status():
    s = db.get_settings()
    with db.cursor(commit=False) as con:
        pages = con.run("SELECT count(*) FROM pages")[0][0]
        claims = con.run("SELECT count(*) FROM claims")[0][0]
        chunks = con.run("SELECT count(*) FROM chunks")[0][0]
        embedded = con.run("SELECT count(*) FROM chunks WHERE embedding IS NOT NULL")[0][0]
        kinds = con.run("SELECT kind, count(*) FROM pages GROUP BY kind ORDER BY 2 DESC")
        health = con.run(
            "SELECT status, count(*) FROM claims GROUP BY status ORDER BY 2 DESC")
        fresh = con.run(
            "SELECT freshness_level, count(*) FROM claims GROUP BY freshness_level")
        lowc = con.run("SELECT count(*) FROM claims WHERE confidence < :c",
                       c=float(s["low_confidence"]))[0][0]
        last = con.run(
            "SELECT id, started_at, finished_at, status, pages, claims, chunks, message "
            "FROM compile_runs ORDER BY id DESC LIMIT 1")
    vault_ok = os.path.isdir(s["vault_path"])
    return ok(status={
        "site_title": s["site_title"],
        "vault_path": s["vault_path"],
        "vault_exists": vault_ok,
        "pages": pages, "claims": claims, "chunks": chunks, "embedded": embedded,
        "kinds": {k: n for k, n in kinds},
        "claim_status": {k: n for k, n in health},
        "freshness": {k or "unknown": n for k, n in fresh},
        "low_confidence": lowc,
        "last_run": ({"id": last[0][0], "started_at": str(last[0][1]), "finished_at": str(last[0][2]),
                      "status": last[0][3], "pages": last[0][4], "claims": last[0][5],
                      "chunks": last[0][6], "message": last[0][7]} if last else None),
        "embedding": {"url": s["embed_url"], "model": s["embed_model"], "dim": s["embed_dim"]},
    })


# ---------------- 页面 ----------------

@app.get("/api/pages")
def api_pages():
    kind = request.args.get("kind")
    q = (request.args.get("q") or "").strip()
    limit = min(int(request.args.get("limit", 200)), 1000)
    offset = int(request.args.get("offset", 0))
    sql = ("SELECT p.id, p.kind, p.title, p.entity_type, p.aliases, p.path, p.updated_at, "
           "(SELECT count(*) FROM claims c WHERE c.page_id = p.id), "
           "(SELECT count(*) FROM chunks k WHERE k.page_id = p.id) FROM pages p WHERE 1=1")
    args = {}
    if kind:
        sql += " AND p.kind = :kind"
        args["kind"] = kind
    if q:
        sql += " AND (p.title ILIKE :q OR p.body ILIKE :q)"
        args["q"] = f"%{q}%"
    sql += " ORDER BY p.kind, p.title LIMIT :lim OFFSET :off"
    args.update(lim=limit, off=offset)
    with db.cursor(commit=False) as con:
        rows = con.run(sql, **args)
    cols = ["id", "kind", "title", "entity_type", "aliases", "path", "updated_at",
            "claims", "chunks"]
    return ok(pages=[dict(zip(cols, r)) for r in rows])


@app.get("/api/pages/<path:pid>")
def api_page(pid):
    with db.cursor(commit=False) as con:
        r = con.run("SELECT id,kind,title,path,entity_type,aliases,frontmatter,body,updated_at "
                    "FROM pages WHERE id = :id", id=pid)
        if not r:
            return fail("页面不存在", 404)
        row = r[0]
        claims = con.run(
            "SELECT claim_id,text,status,confidence,evidence_kinds,evidence_count,"
            "missing_evidence,freshness_level,updated_at,raw FROM claims "
            "WHERE page_id = :id ORDER BY confidence DESC", id=pid)
        backlinks = con.run(
            "SELECT src_page, rel FROM relations WHERE dst_page = :id LIMIT 50", id=pid)
    keys = ["id", "kind", "title", "path", "entity_type", "aliases", "frontmatter", "body", "updated_at"]
    page = dict(zip(keys, row))
    page["frontmatter"] = page["frontmatter"] or {}
    page["claims"] = [dict(zip(["id", "text", "status", "confidence", "evidence_kinds",
                                "evidence_count", "missing_evidence", "freshness_level",
                                "updated_at", "raw"], c)) for c in claims]
    page["backlinks"] = [{"from": b[0], "rel": b[1]} for b in backlinks]
    return ok(page=page)


@app.post("/api/pages")
def api_save_page():
    data = request.get_json(force=True) or {}
    s = db.get_settings()
    if not os.path.isdir(s["vault_path"]):
        return fail("vault 路径不存在，请先在设置里改对")
    kind = data.get("kind") or "concept"
    title = (data.get("title") or "").strip()
    body = data.get("body") or ""
    pid = (data.get("id") or "").strip()
    if not pid:
        pid = f"{kind}.{compiler.slugify(title)}"
    fm = {"id": pid, "kind": kind, "title": title or pid}
    if data.get("entityType"):
        fm["entityType"] = data["entityType"]
    if data.get("aliases"):
        fm["aliases"] = data["aliases"]
    if data.get("claims"):
        fm["claims"] = data["claims"]
    fm["updatedAt"] = datetime.now().strftime("%Y-%m-%d")

    sub = KIND_DIR.get(kind, "concepts")
    d = os.path.join(s["vault_path"], sub)
    os.makedirs(d, exist_ok=True)
    fname = compiler.slugify(pid.split(".", 1)[-1]) + ".md"
    path = os.path.join(d, fname)
    content = "---\n" + yaml.safe_dump(fm, allow_unicode=True, sort_keys=False) + "---\n\n" + body.strip() + "\n"
    open(path, "w", encoding="utf-8").write(content)
    if data.get("compile", True):
        compiler.compile_vault(s, embed_vectors=data.get("embed", True))
    return ok(id=pid, path=path)


@app.delete("/api/pages/<path:pid>")
def api_delete_page(pid):
    with db.cursor(commit=False) as con:
        r = con.run("SELECT path FROM pages WHERE id = :id", id=pid)
    if not r:
        return fail("页面不存在", 404)
    path = r[0][0]
    s = db.get_settings()
    vault = os.path.realpath(s["vault_path"])
    rp = os.path.realpath(path)
    if not rp.startswith(vault):
        return fail("拒绝删除 vault 之外的路径")
    if os.path.exists(rp):
        os.remove(rp)
    compiler.compile_vault(s, embed_vectors=False)
    return ok(deleted=pid)


# ---------------- 编译 ----------------

@app.post("/api/compile")
def api_compile():
    data = request.get_json(silent=True) or {}
    s = db.get_settings()
    try:
        stats = compiler.compile_vault(s, embed_vectors=bool(data.get("embed", True)))
        return ok(stats=stats)
    except Exception as e:
        traceback.print_exc()
        return fail(f"编译失败: {e}", 500)


@app.get("/api/runs")
def api_runs():
    with db.cursor(commit=False) as con:
        rows = con.run("SELECT id,started_at,finished_at,status,pages,claims,chunks,message "
                       "FROM compile_runs ORDER BY id DESC LIMIT 20")
    keys = ["id", "started_at", "finished_at", "status", "pages", "claims", "chunks", "message"]
    return ok(runs=[dict(zip(keys, [str(x) if x is not None else None for x in r])) for r in rows])


# ---------------- 检索 ----------------

@app.get("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    mode = request.args.get("mode") or db.get_settings()["default_mode"]
    limit = min(int(request.args.get("limit", 20)), 100)
    if not q:
        return ok(results=[], mode=mode)
    s = db.get_settings()
    results, seen = [], set()

    if mode in ("keyword", "hybrid"):
        with db.cursor(commit=False) as con:
            rows = con.run(
                """SELECT p.id, p.title, p.kind,
                          substring(p.body from greatest(1, position(:q in p.body) - 40) for 220) AS snippet,
                          ts_rank_cd(to_tsvector('simple', p.body), plainto_tsquery('simple', :q)) AS rank
                   FROM pages p
                   WHERE p.body ILIKE :like OR p.title ILIKE :like
                   ORDER BY rank DESC NULLS LAST, length(p.body) LIMIT :lim""",
                q=q, like=f"%{q}%", lim=limit)
        for r in rows:
            results.append({"page_id": r[0], "title": r[1], "kind": r[2],
                            "snippet": (r[3] or "").strip(), "score": float(r[4] or 0),
                            "source": "keyword"})
            seen.add(r[0])

    if mode in ("semantic", "hybrid"):
        try:
            vec = compiler.embed([q], s)[0]
            with db.cursor(commit=False) as con:
                rows = con.run(
                    """SELECT c.page_id, p.title, p.kind, c.heading, c.text,
                              1 - (c.embedding <=> :v::vector) AS score
                       FROM chunks c JOIN pages p ON p.id = c.page_id
                       WHERE c.embedding IS NOT NULL
                       ORDER BY c.embedding <=> :v::vector LIMIT :lim""",
                    v=db.vec_literal(vec), lim=limit)
            for r in rows:
                if r[0] in seen and mode == "hybrid":
                    continue
                results.append({"page_id": r[0], "title": r[1], "kind": r[2],
                                "snippet": (r[4] or "")[:220], "heading": r[3],
                                "score": round(float(r[5]), 4), "source": "semantic"})
                seen.add(r[0])
        except Exception as e:
            results.append({"page_id": None, "title": "语义检索不可用", "kind": "error",
                            "snippet": str(e), "score": 0, "source": "semantic"})

    if mode in ("claim", "hybrid"):
        with db.cursor(commit=False) as con:
            rows = con.run(
                """SELECT claim_id, page_id, text, status, confidence, evidence_kinds
                   FROM claims WHERE text ILIKE :like ORDER BY confidence DESC LIMIT :lim""",
                like=f"%{q}%", lim=limit)
        for r in rows:
            results.append({"page_id": r[1], "title": f"论断 {r[0]}", "kind": "claim",
                            "snippet": r[2], "score": float(r[4]), "source": "claim",
                            "claim": {"id": r[0], "status": r[3], "evidence": r[5]}})

    results.sort(key=lambda x: (-(x.get("score") or 0)))
    return ok(results=results[:limit], mode=mode, count=len(results))


@app.get("/api/claims")
def api_claims():
    status = request.args.get("status")
    page = request.args.get("page")
    limit = min(int(request.args.get("limit", 200)), 1000)
    sql = ("SELECT claim_id,page_id,text,status,confidence,evidence_kinds,evidence_count,"
           "missing_evidence,freshness_level FROM claims WHERE 1=1")
    args = {}
    if status:
        sql += " AND status = :st"; args["st"] = status
    if page:
        sql += " AND page_id = :pg"; args["pg"] = page
    sql += " ORDER BY confidence ASC LIMIT :lim"
    args["lim"] = limit
    with db.cursor(commit=False) as con:
        rows = con.run(sql, **args)
    keys = ["id", "page_id", "text", "status", "confidence", "evidence_kinds",
            "evidence_count", "missing_evidence", "freshness_level"]
    return ok(claims=[dict(zip(keys, r)) for r in rows])


# ---------------- 报告 ----------------

@app.get("/api/reports")
def api_reports():
    s = db.get_settings()
    d = os.path.join(s["vault_path"], "reports")
    items = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".md"):
                p = os.path.join(d, fn)
                items.append({"name": fn, "title": fn[:-3],
                              "bytes": os.path.getsize(p),
                              "mtime": datetime.fromtimestamp(os.path.getmtime(p)).strftime("%Y-%m-%d %H:%M")})
    return ok(reports=items)


@app.get("/api/reports/<name>")
def api_report(name):
    if not re.fullmatch(r"[A-Za-z0-9._-]+\.md", name):
        return fail("非法文件名")
    s = db.get_settings()
    p = os.path.join(s["vault_path"], "reports", name)
    if not os.path.exists(p):
        return fail("报告不存在", 404)
    return ok(name=name, content=open(p, encoding="utf-8").read())


# ---------------- 设置 ----------------

@app.get("/api/settings")
def api_get_settings():
    return ok(settings=db.get_settings())


@app.put("/api/settings")
def api_put_settings():
    patch = request.get_json(force=True) or {}
    unknown = [k for k in patch if k not in db.DEFAULT_SETTINGS]
    s = db.save_settings(patch)
    return ok(settings=s, ignored=unknown)


# ---------------- 初始化 ----------------

def bootstrap():
    db.init_schema()
    db.save_settings({})  # 幂等
    s = db.get_settings()
    v = s["vault_path"]
    for sub in ("entities", "concepts", "syntheses", "sources", "reports", ".wenmai", "_attachments"):
        os.makedirs(os.path.join(v, sub), exist_ok=True)
    for fn, text in (
        ("WIKI.md", "# 灵犀文脉\n\n本库由 NAS 上的「灵犀文脉」服务编译维护。\n"),
        ("AGENTS.md", "# 使用约定\n\n- 页面 frontmatter 里写 claims（含 evidence）才会进结构化论断层。\n- reports/ 由编译自动生成。\n"),
        ("index.md", "---\nid: concept.index\nkind: concept\ntitle: 索引\n---\n\n# 索引\n\n编译后自动列出所有页面。\n"),
    ):
        p = os.path.join(v, fn)
        if not os.path.exists(p):
            open(p, "w", encoding="utf-8").write(text)


if __name__ == "__main__":
    bootstrap()
    app.run(host="0.0.0.0", port=int(os.environ.get("WENMAI_PORT", "8800")), threaded=True)
