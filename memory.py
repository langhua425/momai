"""双时态记忆（阶段 4）

三条纪律：

① **valid_* 与 created_* 是两条时间轴**。前者说事实在现实里成立的时间，
   后者说这条记录进系统的时间。归档晚于生效的公文极常见，混用会得出
   「2024 年的标准在 2024 年查不到」这种错答案。

② **冲突不覆盖，先废止**。同一 key 出现不同内容时，旧条 status='superseded'
   + valid_to 封口，新条另起一行；memory_revisions 留旧值。这样「去年这条
   规定是什么」仍然答得出来——覆盖式更新会把这个能力直接毁掉。

③ **权威性只降不升**。机器摘要（summary）绝不许覆盖正式文件（official）
   的同一 key，只能被 official 覆盖。否则一次 LLM 幻觉就会污染权威事实。
"""
import json
import time
from datetime import datetime, timezone

import db
import ingest

AUTH_RANK = {"user": 1, "summary": 2, "record": 3, "official": 4}


def _now():
    return datetime.now(timezone.utc)


def _settings():
    return ingest.settings()


def _embed(text, s):
    """记忆的向量可选：拿不到就退化为纯关键词/trgm，不影响可用性。"""
    try:
        import urllib.request
        url = db.require_endpoint(s, "embed_url")
        body = json.dumps({"input": [text], "model": "local"}).encode()
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())["data"][0]["embedding"]
    except Exception:                                          # noqa: BLE001
        return None


def _norm_key(key, subject):
    raw = (key or subject or "").strip()
    if not raw:
        raise ValueError("需要 key 或 subject")
    return raw[:200]


def remember(content, key=None, subject=None, kind="fact", authority="record",
             confidence=0.6, valid_from=None, valid_to=None, scope="default",
             source_kind="manual", file_id=None, chunk_id=None, excerpt=None,
             reason="", changed_by="user", embed=True):
    """写入一条记忆，并处理与既有事实的关系。

    返回 dict：action ∈ created / updated / superseded / rejected
    """
    content = (content or "").strip()
    if not content:
        raise ValueError("内容为空")
    k = _norm_key(key, subject)
    vf = valid_from or _now()
    s = _settings()
    vec = _embed(content, s) if embed else None

    with db.cursor() as con:
        rows = con.run("""SELECT id, content, authority, confidence, valid_from
                            FROM memory_items
                           WHERE key=:k AND scope_key=:sc AND status='active'
                           ORDER BY created_at DESC LIMIT 1""", k=k, sc=scope)
        cur = rows[0] if rows else None

        if cur is None:
            rid = con.run("""INSERT INTO memory_items
                  (key, subject, content, kind, confidence, scope, scope_key, authority,
                   valid_from, valid_to, status, source_kind, embedding)
                  VALUES (:k,:sj,:c,:kd,:cf,:sc,:sck,:au,:vf,:vt,'active',:sk,:e::vector)
                  RETURNING id""",
                k=k, sj=subject, c=content, kd=kind, cf=confidence, sc=scope, sck=scope,
                au=authority, vf=vf, vt=valid_to, sk=source_kind,
                e=(db.vec_literal(vec) if vec else None))
            mid = rid[0][0]
            action = "created"
        else:
            mid, old_content, old_auth, old_conf, old_vf = cur
            same = (old_content or "").strip() == content
            if same and old_vf and (old_vf.date() == vf.date()):
                con.run("""UPDATE memory_items SET confidence=greatest(confidence,:cf), updated_at=now()
                            WHERE id=:i""", cf=confidence, i=mid)
                action = "updated"
            else:
                # 权威性只降不升：低权威不许顶掉高权威
                if AUTH_RANK.get(authority, 0) < AUTH_RANK.get(old_auth, 0):
                    action = "rejected"
                    con.run("""INSERT INTO memory_evidence(memory_id,file_id,chunk_id,excerpt)
                               VALUES (:m,:f,:c,:e)""", m=mid, f=file_id, c=chunk_id,
                            e=(excerpt or content)[:1000])
                else:
                    con.run("""INSERT INTO memory_revisions
                          (memory_id, content, valid_from, valid_to, status, authority,
                           confidence, changed_by, reason)
                          VALUES (:m,:c,:vf,NULL,'superseded',:au,:cf,:by,:rs)""",
                            m=mid, c=old_content, vf=old_vf, au=old_auth,
                            cf=old_conf, by=changed_by, rs=(reason or "内容变更")[:300])
                    con.run("""UPDATE memory_items
                                  SET status='superseded', valid_to=:vt, updated_at=now()
                                WHERE id=:i""", vt=vf, i=mid)
                    nid = con.run("""INSERT INTO memory_items
                          (key, subject, content, kind, confidence, scope, scope_key, authority,
                           valid_from, valid_to, status, source_kind, superseded_by, embedding)
                          VALUES (:k,:sj,:c,:kd,:cf,:sc,:sck,:au,:vf,:vt2,'active',:sk,NULL,:e::vector)
                          RETURNING id""",
                        k=k, sj=subject, c=content, kd=kind, cf=confidence, sc=scope, sck=scope,
                        au=authority, vf=vf, vt2=valid_to, sk=source_kind,
                        e=(db.vec_literal(vec) if vec else None))
                    mid = nid[0][0]
                    action = "superseded"

        if file_id or chunk_id:
            con.run("""INSERT INTO memory_evidence(memory_id,file_id,chunk_id,excerpt)
                       VALUES (:m,:f,:c,:e)""", m=mid, f=file_id, c=chunk_id,
                    e=(excerpt or content)[:1000])
    return {"id": mid, "key": k, "action": action, "authority": authority,
            "valid_from": str(vf)}


def search(q, limit=10, scope="default", as_of=None, include_history=False):
    """按 as_of 取「当时有效」的记忆。

    as_of 语义：valid_from <= as_of 且 (valid_to IS NULL OR valid_to > as_of)。
    不给 as_of 时只看当前有效（status='active'）。
    """
    q = (q or "").strip()
    if not q:
        return []
    params = {"l": limit, "k": f"%{q}%", "sc": scope}
    if as_of:
        where = """scope_key=:sc AND valid_from <= :asof
                   AND (valid_to IS NULL OR valid_to > :asof)"""
        params["asof"] = as_of
        # 注意：as_of 查询**不过滤 status**——历史版本在那个时间窗口内就是当时的事实，
        # 用 status='active' 过滤会让「去年这条规定是什么」永远只剩最新一条。
    else:
        where = "scope_key=:sc AND status='active'"

    sql = f"""
        SELECT id, key, coalesce(subject,''), content, kind, authority, confidence,
               valid_from, valid_to, status,
               greatest(similarity(content, :k2), 0) AS sim
        FROM memory_items
        WHERE {where} AND (content ILIKE :k OR similarity(content, :k2) > 0.15)
        ORDER BY sim DESC, coalesce(valid_from, now()) DESC
        LIMIT :l"""
    params["k2"] = q
    with db.cursor(commit=False) as con:
        rows = con.run(sql, **params)
    return [{"id": r[0], "key": r[1], "subject": r[2], "content": r[3], "kind": r[4],
             "authority": r[5], "confidence": float(r[6] or 0),
             "valid_from": str(r[7] or ""), "valid_to": str(r[8] or ""),
             "status": r[9], "score": round(float(r[10] or 0), 4)} for r in rows]


def timeline(key=None, scope="default", limit=50):
    """一条 key 的完整历史，用于回答「什么时候改的口径」。"""
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, key, content, valid_from, valid_to, status, authority,
                                 superseded_by, created_at
                            FROM memory_items
                           WHERE scope_key=:sc AND (:k::text IS NULL OR key=:k::text)
                           ORDER BY key, valid_from""", sc=scope, k=key)
    return [{"id": r[0], "key": r[1], "content": r[2], "valid_from": str(r[3] or ""),
             "valid_to": str(r[4] or ""), "status": r[5], "authority": r[6],
             "superseded_by": r[7], "created_at": str(r[8] or "")} for r in rows]
