"""检索编排：语义 + 关键词 双路召回 → RRF 融合 → 重排 → 只保留最新版块的命中。

关键纪律：
  1. 只检索 chunks.is_current = true（文件被改过时，旧版本块不参与结果）
  2. 语义与关键词各自独立召回，RRF 融合避免单路偏置
  3. 有 rerank 端点时对融合后的候选做重排，分数直接来自模型
"""
import db
import ingest
import segment as seg
import qdrant_store
import rerank as rr

CANDIDATES = 50      # 每路召回条数
RRF_K = 60


def _semantic(qv, ext, limit, s):
    try:
        vs = db.vec_literal(qv)
    except Exception:
        return []
    where = "c.embedding IS NOT NULL AND c.is_current"
    args = {"v": vs, "lim": limit}
    if ext:
        where += " AND f.ext = :ext"
        args["ext"] = ext
    with db.cursor(commit=False) as con:
        rows = con.run(f"""
            SELECT c.id, f.id, f.name, f.ext, f.path, f.parse_kind, c.seq, c.heading,
                   left(c.content, 600) AS snippet, c.content,
                   1 - (c.embedding <=> :v::vector) AS score, c.section_path
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE {where}
            ORDER BY c.embedding <=> :v::vector
            LIMIT :lim""", **args)
    out = []
    for r in rows:
        out.append({"chunk_id": r[0], "file_id": r[1], "name": r[2], "ext": r[3], "path": r[4],
                    "parse_kind": r[5], "seq": r[6], "heading": r[7], "snippet": r[8],
                    "content": r[9], "vscore": float(r[10]), "kscore": 0.0,
                    "section_path": r[11]})
    return out


def _qdrant_semantic(qv, ext, limit, s):
    """Qdrant 向量路：取 id 顺序后回 PG 补元数据。

    向量与 pgvector 同源（同 bge-m3 1024 维），所以它不是「另一类信号」，
    而是同一语义信号的第二个近似索引——两条 HNSW 的候选集合不完全相同，
    并集能提高召回覆盖率。权重按 0.6 计，避免语义双票压死关键词路。
    """
    if not (s.get("qdrant_recall_enabled", True) and qdrant_store.enabled(s)):
        return []
    try:
        body = {"query": list(map(float, qv)), "limit": int(limit), "with_payload": True}
        filt = {"must": [{"key": "is_current", "match": {"value": True}}]}
        if ext:
            filt["must"].append({"key": "ext", "match": {"value": ext}})
        body["filter"] = filt
        name = s.get("qdrant_collection") or "wenmai_chunks"
        d = qdrant_store._req(f"{qdrant_store._base(s)}/collections/{name}/points/query",
                              method="POST", body=body, timeout=12)
        pts = (((d.get("result") or {}).get("points")) or [])
    except Exception as e:                       # noqa: BLE001
        print(f"    Qdrant 召回失败（已忽略）: {e}")
        return []
    ids = [int(p.get("id")) for p in pts if p.get("id") is not None]
    if not ids:
        return []
    sc = {int(p.get("id")): float(p.get("score") or 0) for p in pts if p.get("id") is not None}
    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT c.id, f.id, f.name, f.ext, f.path, f.parse_kind, c.seq, c.heading,
                   left(c.content, 600) AS snippet, c.content, c.section_path
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE c.id = ANY(:ids::bigint[]) AND c.is_current""", ids=ids)
    by_id = {r[0]: r for r in rows}
    out = []
    for cid in ids:                              # 保持 Qdrant 的相似度顺序
        r = by_id.get(cid)
        if not r:
            continue
        out.append({"chunk_id": r[0], "file_id": r[1], "name": r[2], "ext": r[3], "path": r[4],
                    "parse_kind": r[5], "seq": r[6], "heading": r[7], "snippet": r[8],
                    "content": r[9], "vscore": sc.get(cid, 0.0), "kscore": 0.0,
                    "qscore": round(sc.get(cid, 0.0), 4), "section_path": r[10]})
    return out


def _name_channel(q, ext, limit):
    """文件名字段召回：解决「我记得有这么个文件」这个最高频场景。

    为什么单独一路而不是塞进 content：实测把文件名拼进块文本（meta 前缀）后，
    按名字找文件 0.237→0.562，但段落检索 MRR 0.380→0.306 —— 所有块共享同一段
    前缀会把同文档各块拉得互相靠近，块级区分度被抹平。字段匹配没有这个副作用。

    排序：整句 trgm 相似度优先，其次分词命中数。
    """
    kw = (q or "").strip()
    if len(kw) < 3:
        return []
    ts = seg.terms(q) or []
    params = {"q": kw, "l": limit, "kw": f"%{kw}%"}
    ext_sql = ""
    if ext:
        ext_sql = " AND f.ext = :ext"
        params["ext"] = ext
    # 每份文件只取一块（seq 最小），否则同一文件会占满名额
    sql = f"""
        WITH hit AS (
            SELECT f.id AS file_id,
                   greatest(similarity(f.name, :q), 0.6 * similarity(coalesce(f.path,''), :q)) AS sim,
                   (f.name ILIKE :kw) AS exact
            FROM files f
            WHERE f.status = 'parsed' AND (f.name ILIKE :kw OR coalesce(f.path,'') ILIKE :kw
                  OR similarity(f.name, :q) > 0.18){ext_sql}
            ORDER BY exact DESC, sim DESC
            LIMIT :l
        )
        SELECT c.id, c.file_id, f.name, f.ext, f.path, coalesce(f.parse_kind,''),
               c.seq, coalesce(c.heading,''), coalesce(c.section_path,''),
               coalesce(c.content,''), h.sim
        FROM hit h
        JOIN files f ON f.id = h.file_id
        JOIN chunks c ON c.file_id = h.file_id AND c.is_current
        WHERE c.seq = (SELECT min(c2.seq) FROM chunks c2
                        WHERE c2.file_id = h.file_id AND c2.is_current)
        ORDER BY h.exact DESC, h.sim DESC"""
    with db.cursor(commit=False) as con:
        rows = con.run(sql, **params)
    out = []
    for r in rows:
        out.append({"chunk_id": r[0], "id": r[0], "file_id": r[1], "name": r[2], "ext": r[3],
                    "path": r[4], "parse_kind": r[5], "seq": r[6], "heading": r[7],
                    "section_path": r[8], "snippet": (r[9] or "")[:600],
                    "content": (r[9] or ""), "vscore": 0.0,
                    "kscore": float(r[10] or 0), "khits": 0, "name_hit": True})
    return out


def _keyword(q, ext, limit):
    """按分词结果做 OR 匹配：命中词数优先，其次整句 trgm 相似度。

    整句 ILIKE 在自然语言查询上几乎必然召回 0，所以必须分词。
    """
    ts = seg.terms(q)
    if not ts:
        return []
    where = "c.is_current"
    args = {"kws": ts, "kw": q, "lim": limit}
    if ext:
        where += " AND f.ext = :ext"
        args["ext"] = ext
    with db.cursor(commit=False) as con:
        rows = con.run(f"""
            SELECT c.id, f.id, f.name, f.ext, f.path, f.parse_kind, c.seq, c.heading,
                   left(c.content, 600) AS snippet, c.content, c.section_path,
                   (SELECT count(*) FROM unnest(:kws::text[]) t
                     WHERE c.content ILIKE '%' || t || '%') AS hits,
                   similarity(c.content, :kw) AS sim
            FROM chunks c JOIN files f ON f.id = c.file_id
            WHERE {where}
              AND EXISTS (SELECT 1 FROM unnest(:kws::text[]) t
                           WHERE c.content ILIKE '%' || t || '%')
            ORDER BY hits DESC, sim DESC
            LIMIT :lim""", **args)
    out = []
    for r in rows:
        hits = int(r[11] or 0)
        sim = float(r[12] or 0)
        out.append({"chunk_id": r[0], "file_id": r[1], "name": r[2], "ext": r[3], "path": r[4],
                    "parse_kind": r[5], "seq": r[6], "heading": r[7], "snippet": r[8],
                    "content": r[9], "vscore": 0.0,
                    "kscore": round(hits + sim, 4), "khits": hits,
                    "section_path": r[10]})
    return out


def _rrf(lists, weights=None):
    """加权 RRF：score = Σ w/(k + rank)，按 chunk_id 合并。

    weights 与 lists 等长；缺省全 1.0。语义两路同源，Qdrant 路权重 0.6，
    避免「同一信号投两票」把关键词路的命中压掉。
    """
    if weights is None:
        weights = [1.0] * len(lists)
    fused, order = {}, []
    for w, lst in zip(weights, lists):
        for rank, item in enumerate(lst, 1):
            cid = item["chunk_id"]
            if cid not in fused:
                fused[cid] = {**item, "rrf": 0.0}
                order.append(cid)
            else:
                # 两路都命中时，取各自的较优分数（不能只留先到那一路的 0）
                f = fused[cid]
                f["vscore"] = max(f.get("vscore", 0.0), item.get("vscore", 0.0))
                f["kscore"] = max(f.get("kscore", 0.0), item.get("kscore", 0.0))
                f["khits"] = max(f.get("khits", 0) or 0, item.get("khits", 0) or 0)
                if not f.get("snippet") and item.get("snippet"):
                    f["snippet"] = item["snippet"]
            fused[cid]["rrf"] += w / (RRF_K + rank)
    for cid in order:
        fused[cid]["score"] = fused[cid]["rrf"]
    return [fused[cid] for cid in order]


def search(q, mode="hybrid", limit=20, ext="", do_rerank=None):
    s = ingest.settings()
    limit = max(1, min(int(limit or 20), 100))
    cand = max(CANDIDATES, limit)
    lists = []
    err = None

    weights = []
    routes = {}
    if mode in ("hybrid", "semantic"):
        try:
            qv = ingest.embed_texts([q], s)[0]
            pg_rows = _semantic(qv, ext, cand, s)
            lists.append(pg_rows); weights.append(1.0); routes["pg_vector"] = len(pg_rows)
            qd_rows = _qdrant_semantic(qv, ext, cand, s)
            if qd_rows:
                lists.append(qd_rows); weights.append(float(s.get("qdrant_recall_weight", 0.6) or 0.6))
                routes["qdrant_vector"] = len(qd_rows)
        except Exception as e:      # noqa: BLE001
            err = f"语义召回失败: {e}"
    if mode in ("hybrid", "keyword"):
        kw_rows = _keyword(q, ext, cand)
        lists.append(kw_rows); weights.append(1.0); routes["keyword"] = len(kw_rows)

        # 名字通道（独立一路）：正文向量不承担「文件身份」，避免块级区分度被前缀抹平
        if s.get("name_recall_enabled", True):
            try:
                nm_rows = _name_channel(q, ext, max(10, cand // 3))
                if nm_rows:
                    lists.append(nm_rows)
                    weights.append(float(s.get("name_recall_weight", 1.2) or 1.2))
                    routes["name_field"] = len(nm_rows)
            except Exception as e:                            # noqa: BLE001
                err = (err + "；" if err else "") + f"名字通道跳过: {e}"

    rows = _rrf(lists, weights) if len(lists) > 1 else (lists[0] if lists else [])
    rows.sort(key=lambda x: -x.get("score", 0))

    # 重排
    use_rr = s.get("rerank_enabled") if do_rerank is None else do_rerank
    if use_rr and rows:
        pool = rows[: max(int(s.get("rerank_candidates", 30) or 30), limit)]
        try:
            pairs = rr.rerank(s, q, [r["content"][:1200] for r in pool], top_n=len(pool))
            reranked = []
            for idx, score in pairs:
                if 0 <= idx < len(pool):
                    item = dict(pool[idx])
                    item["rerank_score"] = score
                    item["score"] = score
                    reranked.append(item)
            if reranked:
                rows = reranked
        except Exception as e:      # noqa: BLE001
            err = (err + "；" if err else "") + f"重排跳过: {e}"

    # 文档级聚合：同一文件只出一条（取分数最高的块），其余命中数并入 other_hits。
    # 原因：块级并列会让同一份报告在结果里出现 2–3 次，看起来像重复；而检索的目的是找文档。
    out = []
    seen_files = set()
    for r in rows:
        if len(out) >= limit:
            break
        if r["file_id"] in seen_files:
            for o in out:
                if o["file_id"] == r["file_id"]:
                    o["other_hits"] = o.get("other_hits", 0) + 1
                    break
            continue
        seen_files.add(r["file_id"])
        out.append({
            "file_id": r["file_id"], "name": r["name"], "ext": r["ext"], "path": r["path"],
            "parse_kind": r["parse_kind"], "seq": r["seq"], "heading": r["heading"],
            "section_path": r.get("section_path"),
            "snippet": (r.get("snippet") or "").strip(),
            "score": round(float(r.get("score", 0)), 4),
            "rerank_score": (round(float(r["rerank_score"]), 4) if "rerank_score" in r else None),
            "vscore": round(float(r.get("vscore", 0)), 4),
            "kscore": round(float(r.get("kscore", 0)), 4),
            "khits": r.get("khits", 0),
            "other_hits": 0,
            "source": "rerank" if "rerank_score" in r else ("hybrid" if len(lists) > 1 else mode),
        })
    return {"results": out, "count": len(out), "mode": mode,
            "reranked": any(x["rerank_score"] is not None for x in out),
            "routes": routes, "error": err}
