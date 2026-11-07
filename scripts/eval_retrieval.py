#!/usr/bin/env python3
"""墨脉 · 检索质量 A/B 评测（阶段 2）——冻结评测集版本

为什么冻结：每轮 `ORDER BY random()` 抽不同样本，n=40 时 Recall 会在 0.2–0.6 之间乱跳，
根本分不清是改动有效还是抽样噪声。所以第一次 `--make-set` 把评测集落盘，
之后所有 run 都读同一批 (chunk_id, file_id, query)。

判据（产品视角）：
- **文档级命中**：top-k 里有任一块来自同一 file_id 即算找对文档。
  纯块级判据会把「同一段话在别处有副本」误判成未命中，而用户要的是「找到那份文件」。
- **块级命中**：原块命中，或 top-k 某块正文包含该查询片段（防副本误判）。
- 指标：文档级/块级 Recall@10，MRR@10（按文档级排名）。

查询构造：块正文中间截 40 字，**避开开头**——T2.2 的元数据前缀加在开头，
用开头当查询等于自证。

用法（NAS）：
    venv/bin/python scripts/eval_retrieval.py --make-set --sample 60
    venv/bin/python scripts/eval_retrieval.py --column embedding    --tag baseline
    venv/bin/python scripts/eval_retrieval.py --column embedding_v2 --tag meta
"""
import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import ingest

K = 10
SET_PATH = "/tank/files/wenmai-eval/set.json"
NAME_SET_PATH = "/tank/files/wenmai-eval/set_name.json"
BASE = os.environ.get("MOMAI_BASE", "http://192.168.18.4:8800")


def good_query(q):
    """片段质量判据：否则会抽到「多模态、多模态…」或纯数字表，评测就没意义。"""
    if len(q) < 20:
        return False
    han = sum(1 for c in q if "\u4e00" <= c <= "\u9fff")
    if han / len(q) < 0.5:
        return False
    if len(set(q)) / len(q) < 0.30:
        return False
    return len({q[i:i + 2] for i in range(len(q) - 1)}) >= 12


def embed_one(text, base):
    body = json.dumps({"input": [text], "model": "local"}).encode()
    req = urllib.request.Request(base, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["data"][0]["embedding"]


def make_set(n, slice_len, column="embedding"):
    with db.cursor(commit=False) as con:
        rows = con.run(f"""
            SELECT id, file_id, content FROM chunks
            WHERE is_current AND {column} IS NOT NULL
              AND length(content) BETWEEN 200 AND 900
            ORDER BY random() LIMIT :lim""", lim=max(n * 20, 400))
    items, seen = [], set()
    for cid, fid, content in rows:
        if fid in seen:                    # 一份文件只出一条，避免整库被几个大文件支配
            continue
        mid = len(content) // 2
        start = max(0, mid - slice_len // 2)
        q = content[start:start + slice_len].strip()
        if not good_query(q):
            continue
        seen.add(fid)
        items.append({"chunk": cid, "file": fid, "query": q})
        if len(items) >= n:
            break
    os.makedirs(os.path.dirname(SET_PATH), exist_ok=True)
    with open(SET_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"评测集已冻结：{len(items)} 条（来自 {len(seen)} 份不同文件）→ {SET_PATH}")


def make_name_queries(limit):
    """按文件名检索的评测集：真实用户最常干的事就是「我记得有这么个文件」。

    这一路和 chunk-as-query 互补：后者测「记得一段话，找它出处」，
    前者测「记得文件名，找文件」——元数据前缀（T2.2）正是为后者设计的。
    """
    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT f.id, f.name FROM files f
            WHERE f.status='parsed' AND length(f.name) BETWEEN 6 AND 60
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.file_id=f.id AND c.is_current)
            ORDER BY random() LIMIT :lim""", lim=limit * 3)
    items = []
    for fid, name in rows:
        q = os.path.splitext(name)[0][:24].strip(" _-·")
        if len(q) < 4:
            continue
        items.append({"file": fid, "query": q, "chunk": None})
        if len(items) >= limit:
            break
    return items


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--make-set", action="store_true")
    ap.add_argument("--sample", type=int, default=60)
    ap.add_argument("--slice", type=int, default=40)
    ap.add_argument("--column", default="embedding")
    ap.add_argument("--tag", default="run")
    ap.add_argument("--mode", default="chunk", choices=["chunk", "name"])
    ap.add_argument("--exact", action="store_true",
                    help="关闭索引扫描（精确最近邻）。对比两列时必须开，否则一边 HNSW 一边全扫")
    ap.add_argument("--make-name-set", action="store_true")
    ap.add_argument("--via", default="db", choices=["db", "api"],
                    help="api = 走 /api/search 端到端（含 RRF/重排/名字通道）")
    ap.add_argument("--list", default="")
    a = ap.parse_args()

    if a.make_set:
        make_set(a.sample, a.slice)
        return
    if not a.column.replace("_", "").isalnum():
        raise SystemExit("列名不合法")
    if not os.path.exists(SET_PATH):
        raise SystemExit("评测集不存在，先跑 --make-set")

    if a.make_name_set:
        items = make_name_queries(80)
        with open(NAME_SET_PATH, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        print(f"文件名评测集已冻结：{len(items)} 条 → {NAME_SET_PATH}")
        return
    if a.mode == "name":
        if os.path.exists(NAME_SET_PATH):
            items = json.load(open(NAME_SET_PATH, encoding="utf-8"))
        else:
            items = make_name_queries(80)
    else:
        items = json.load(open(a.list or SET_PATH, encoding="utf-8"))
    s = ingest.settings()
    base = db.require_endpoint(s, "embed_url") if a.via == "db" else ""

    doc_hits = blk_hits = 0
    rr = 0.0
    misses = []
    per_item = []          # 逐条命中/排名，供配对检验（McNemar / bootstrap）
    t0 = time.time()
    for it in items:
        q = it["query"]
        # api 模式不需要本地算向量：查询由服务端自己嵌入
        qv = db.vec_literal(embed_one(q, base)) if a.via == "db" else None
        if a.via == "api":
            u = f"{BASE}/api/search?limit={K}&mode=hybrid&q=" + urllib.parse.quote(q)
            with urllib.request.urlopen(u, timeout=120) as r:
                res = json.loads(r.read()).get("results") or []
            top = [(x.get("chunk_id"), x.get("file_id"), x.get("snippet") or "") for x in res]
            doc_rank = next((i + 1 for i, (_t, fid, _c) in enumerate(top) if fid == it["file"]), None)
            blk_ok = any((it.get("chunk") and tid == it["chunk"]) or q in (tc or "")
                         for tid, _f, tc in top)
            per_item.append({"chunk": it["chunk"], "file": it["file"],
                             "doc_rank": doc_rank, "blk_hit": bool(blk_ok)})
            if doc_rank:
                doc_hits += 1
                rr += 1.0 / doc_rank
            if blk_ok:
                blk_hits += 1
            continue

        with db.cursor(commit=False) as con:
            if a.exact:
                con.run("SET LOCAL enable_indexscan = off")
                con.run("SET LOCAL enable_bitmapscan = off")
            top = con.run(f"""
                SELECT c.id, c.file_id, c.content FROM chunks c
                WHERE c.is_current ORDER BY c.{a.column} <=> :q::vector LIMIT :k""",
                q=qv, k=K)
        doc_rank = next((i + 1 for i, (_tid, fid, _c) in enumerate(top) if fid == it["file"]), None)
        blk_ok = any((it.get("chunk") and tid == it["chunk"]) or q in (tc or "")
                     for tid, _f, tc in top)
        per_item.append({"chunk": it["chunk"], "file": it["file"],
                         "doc_rank": doc_rank, "blk_hit": bool(blk_ok)})
        if doc_rank:
            doc_hits += 1
            rr += 1.0 / doc_rank
        if blk_ok:
            blk_hits += 1
        if not doc_rank and len(misses) < 6:
            misses.append({"q": q, "want_file": it["file"],
                           "top1_file": top[0][1] if top else None,
                           "top1_head": (top[0][2][:50] if top else "")})

    n = len(items)
    out = {"tag": a.tag, "column": a.column, "n": n, "k": K,
           "doc_recall@%d" % K: round(doc_hits / n, 4),
           "blk_recall@%d" % K: round(blk_hits / n, 4),
           "doc_mrr@%d" % K: round(rr / n, 4),
           "sec": round(time.time() - t0, 1), "misses": misses,
           "items": per_item}
    print(f"  [{a.tag}] 列={a.column} n={n}  "
          f"文档级 Recall@{K}={out['doc_recall@%d' % K]:.3f} "
          f"块级 Recall@{K}={out['blk_recall@%d' % K]:.3f} "
          f"MRR@{K}={out['doc_mrr@%d' % K]:.3f}  {out['sec']}s")
    for m in misses[:2]:
        print(f"    未命中：{m['q'][:30]}… → top1 是另一份文件")
    path = a.list or f"/tank/files/wenmai-eval/{a.tag}.json"
    try:
        json.dump(out, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        print(f"  写入 {path}")
    except Exception as e:
        print(f"  (未落盘: {e})")


if __name__ == "__main__":
    main()
