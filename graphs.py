"""多图多视图（阶段 5 T5.1）

「图」= 一个范围。范围用 scope 描述，查询时现算，不物化实体和边：
范围一改就得重建物化副本，而按 scope 现查在 500 实体规模只要几十毫秒。

scope 支持的键（都可不填）：
    path_prefix   文件路径前缀，如 "往来文件/2023"
    ext           ["pdf", "docx"]
    source_id     数据源 id
    date_from     文件时间下界 (YYYY-MM-DD)
    date_to       文件时间上界
    entity_types  ["org","project",...]  只收这些类型的实体

「视图」= 同一张图上的筛法（filters）：
    entity_types / min_mentions / rels（关系名白名单）/ max_nodes

关键在于**筛选发生在节点集上，边只保留两端都在节点集里的**——
否则筛完会出现一堆指向不存在节点的悬空边。
"""
import json

import db

SCOPE_KEYS = {"path_prefix", "ext", "source_id", "date_from", "date_to", "entity_types"}
FILTER_KEYS = {"entity_types", "min_mentions", "rels", "max_nodes"}


def _file_where(scope):
    """把 scope 翻成 files 表的 WHERE 片段。全部用参数绑定，不拼字符串。"""
    w, p = [], {}
    if scope.get("path_prefix"):
        w.append("f.real_path LIKE :pfx")
        p["pfx"] = f"%{scope['path_prefix']}%"
    if scope.get("ext"):
        exts = [e.lower().lstrip(".") for e in scope["ext"] if str(e).strip()]
        if exts:
            w.append("lower(ltrim(f.ext,'.')) = ANY(:exts)")
            p["exts"] = exts
    if scope.get("source_id"):
        w.append("f.source_id = :sid")
        p["sid"] = int(scope["source_id"])
    if scope.get("date_from"):
        w.append("f.mtime >= :df")
        p["df"] = scope["date_from"]
    if scope.get("date_to"):
        w.append("f.mtime <= :dt")
        p["dt"] = scope["date_to"]
    return (" AND " + " AND ".join(w)) if w else "", p


def _entity_where(scope, filters):
    """实体侧的附加条件（类型 / 最低提及）。"""
    w, p = [], {}
    types = filters.get("entity_types") or scope.get("entity_types")
    if types:
        w.append("e.type = ANY(:etypes)")
        p["etypes"] = [str(t) for t in types]
    if filters.get("min_mentions"):
        w.append("coalesce(e.mentions,0) >= :mm")
        p["mm"] = int(filters["min_mentions"])
    return (" AND " + " AND ".join(w)) if w else "", p


def _nodes(con, scope, filters):
    fw, fp = _file_where(scope)
    ew, ep = _entity_where(scope, filters)
    sql = f"""
        SELECT e.id, e.type, e.name, coalesce(e.mentions,0) AS mentions,
               (SELECT count(DISTINCT m2.file_id) FROM entity_mentions m2 WHERE m2.entity_id=e.id) AS files
          FROM entities e
         WHERE EXISTS (SELECT 1 FROM entity_mentions m JOIN files f ON f.id=m.file_id
                        WHERE m.entity_id=e.id{fw}){ew}
         ORDER BY mentions DESC, e.id
         LIMIT :lim
    """
    return con.run(sql, **fp, **ep, lim=int(filters.get("max_nodes") or 120))


def _edges(con, node_ids, filters):
    if not node_ids:
        return []
    w, p = "", {}
    if filters.get("rels"):
        w = " AND r.rel = ANY(:rels)"
        p["rels"] = [str(r) for r in filters["rels"]]
    return con.run(f"""
        SELECT r.src_id, r.dst_id, r.rel
          FROM relations r
         WHERE r.src_id = ANY(:ids) AND r.dst_id = ANY(:ids){w}
         LIMIT 1200
    """, ids=list(node_ids), **p)


def stats(scope, filters=None):
    filters = filters or {}
    with db.cursor(commit=False) as con:
        nodes = _nodes(con, scope, filters)
        edges = _edges(con, [n[0] for n in nodes], filters)
    return {"nodes": len(nodes), "edges": len(edges)}


def build(name, scope, note=None):
    scope = {k: v for k, v in (scope or {}).items() if k in SCOPE_KEYS and v not in (None, "", [])}
    st = stats(scope)
    with db.cursor() as con:
        r = con.run("""INSERT INTO graphs (name, scope, note, built_at)
                       VALUES (:n,:s::jsonb,:note,now())
                       ON CONFLICT (name) DO UPDATE
                         SET scope=EXCLUDED.scope, note=EXCLUDED.note, built_at=now()
                       RETURNING id""",
                    n=name, s=json.dumps(scope, ensure_ascii=False), note=note)
    return {"id": r[0][0], "name": name, "scope": scope, **st}


def list_graphs():
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT g.id, g.name, g.scope, g.note, g.built_at,
                                 (SELECT count(*) FROM graph_subviews v WHERE v.graph_id=g.id)
                            FROM graphs g ORDER BY g.id""")
    return [{"id": r[0], "name": r[1], "scope": r[2] or {}, "note": r[3],
             "built_at": str(r[4]), "subviews": r[5]} for r in rows]


def get(graph_id, subview=None):
    with db.cursor(commit=False) as con:
        g = con.run("SELECT id, name, scope, note FROM graphs WHERE id=:i", i=graph_id)
        if not g:
            return None
        scope = g[0][2] or {}
        filters = {}
        if subview:
            v = con.run("SELECT filters FROM graph_subviews WHERE graph_id=:i AND name=:n",
                        i=graph_id, n=subview)
            if not v:
                return {"error": f"视图不存在: {subview}"}
            filters = v[0][0] or {}
        nodes = _nodes(con, scope, filters)
        edges = _edges(con, [n[0] for n in nodes], filters)
        vs = con.run("SELECT id, name, filters FROM graph_subviews WHERE graph_id=:i ORDER BY id",
                     i=graph_id)
    return {
        "graph": {"id": g[0][0], "name": g[0][1], "scope": scope, "note": g[0][3]},
        "subview": subview, "filters": filters,
        "nodes": [{"id": n[0], "type": n[1], "name": n[2], "mentions": n[3], "files": n[4]}
                  for n in nodes],
        "edges": [{"src": e[0], "dst": e[1], "rel": e[2]} for e in edges],
        "subviews": [{"id": v[0], "name": v[1], "filters": v[2] or {}} for v in vs],
    }


def add_subview(graph_id, name, filters):
    filters = {k: v for k, v in (filters or {}).items() if k in FILTER_KEYS and v not in (None, "", [])}
    with db.cursor() as con:
        r = con.run("""INSERT INTO graph_subviews (graph_id, name, filters)
                       VALUES (:g,:n,:f::jsonb)
                       ON CONFLICT (graph_id, name) DO UPDATE SET filters=EXCLUDED.filters
                       RETURNING id""", g=graph_id, n=name, f=json.dumps(filters, ensure_ascii=False))
    return {"id": r[0][0], "name": name, "filters": filters}


def del_subview(graph_id, name):
    with db.cursor() as con:
        con.run("DELETE FROM graph_subviews WHERE graph_id=:g AND name=:n", g=graph_id, n=name)
