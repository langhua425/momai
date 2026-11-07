import db
"""Qdrant 双写：与 pgvector 并行保存同一批向量。

设计原则
- **PG 是主，Qdrant 是副本**：写入失败只记日志，绝不影响入库（PG 里永远有完整数据）。
- 只写 `is_current=true` 的块；版本切换时把旧块的点删掉，保证两边检索口径一致。
- 点 id 直接用 `chunks.id`（整数），天然幂等：重跑覆盖同一批点，不会重复。
"""
import json
import urllib.error
import urllib.request

COLLECTION = "wenmai_chunks"
DIM = 1024


def _base(s):
    return db.resolve_endpoint(s, "qdrant_url")


def enabled(s):
    return bool(s.get("qdrant_enabled")) and bool(_base(s))


def _req(url, method="GET", body=None, timeout=20):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def ensure_collection(s):
    base = _base(s)
    name = s.get("qdrant_collection") or COLLECTION
    try:
        _req(f"{base}/collections/{name}", timeout=10)
        return True
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    _req(f"{base}/collections/{name}", "PUT",
         {"vectors": {"size": int(s.get("embed_dim") or DIM), "distance": "Cosine"}},
         timeout=30)
    # 常用过滤字段建索引，避免全量扫描
    for field in ("file_id", "is_current", "ext"):
        try:
            _req(f"{base}/collections/{name}/index", "PUT",
                 {"field_name": field, "field_schema": "integer" if field == "file_id" else "keyword"})
        except Exception:                                   # noqa: BLE001
            pass
    return True


def upsert(points, s):
    """points: [{id, vector, payload}]"""
    if not points or not enabled(s):
        return 0
    base = _base(s)
    name = s.get("qdrant_collection") or COLLECTION
    try:
        ensure_collection(s)
        _req(f"{base}/collections/{name}/points?wait=false", "PUT", {"points": points}, timeout=60)
        return len(points)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! Qdrant 写入失败（不影响入库）: {e}")
        return 0


def delete_ids(ids, s):
    if not ids or not enabled(s):
        return 0
    base = _base(s)
    name = s.get("qdrant_collection") or COLLECTION
    try:
        _req(f"{base}/collections/{name}/points/delete?wait=false", "POST",
             {"points": [int(i) for i in ids]}, timeout=60)
        return len(ids)
    except Exception as e:                                   # noqa: BLE001
        print(f"  ! Qdrant 删除失败: {e}")
        return 0


def make_points(chunk_rows, s):
    """chunk_rows: [(id, seq, heading, content, section_path, version, file_id, name, real_path, ext)]"""
    out = []
    for (cid, seq, heading, content, sp, ver, fid, name, path, ext) in chunk_rows:
        vec = s.get("_vecs") or []
        out.append({
            "id": int(cid),
            "vector": vec,
            "payload": {
                "file_id": int(fid), "seq": int(seq), "heading": heading or "",
                "section_path": sp or "", "version": int(ver),
                "name": name or "", "path": path or "", "ext": ext or "",
                "is_current": True,
            },
        })
    return out


def health(s):
    base = _base(s)
    if not base:
        return {"ok": False, "error": "未配置 qdrant_url"}
    name = s.get("qdrant_collection") or COLLECTION
    try:
        d = _req(f"{base}/collections/{name}", timeout=8)
        res = (d.get("result") or {})
        return {"ok": True, "collection": name,
                "points": res.get("points_count"), "status": res.get("status"),
                "dim": (((res.get("config") or {}).get("params") or {}).get("vectors") or {}).get("size")}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}（集合可能还没建，入库时自动创建）"}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": str(e)}
