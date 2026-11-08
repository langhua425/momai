"""主动推荐（阶段 5 T5.3）—— 三条规则，不跑 LLM。

为什么规则优先：
  「要不要提醒用户看这份东西」是个高置信判断，规则能覆盖八成场景，成本为零；
  LLM 每次要读文件、要花钱、结论还会漂移，在这里性价比极差。

三条规则各自回答一个真实问题：
  new_related  —— 新入库的东西里，哪份跟我最近在看的是同一件事？
  doc_gap      —— 同一件事的配套文件是不是缺了一件？（公文体裁成对出现）
  stale_value  —— 哪份有价值的资料我一***没再看过？
"""
import hashlib
import sys

import db

# 公文配套对：前件出现而缺后件 → 提示可能缺件
# 只用成对关系确定的组合，不猜「应该有」的东西。
PAIRS = [
    ("请示", ("批复", "答复", "意见")),
    ("招标", ("中标", "合同", "成交")),
    ("方案", ("评审", "审查意见", "批复")),
    ("验收", ("结算", "决算", "审计")),
    ("预算", ("决算", "拨付", "支付")),
    ("测绘", ("成果", "验收")),
]


def _h(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:24]


def record_view(file_id, kind="detail", meta=None):
    with db.cursor() as con:
        con.run("""INSERT INTO file_views(file_id, kind, meta) VALUES (:f,:k,:m::jsonb)""",
                f=file_id, k=kind, m=(__import__("json").dumps(meta or {}, ensure_ascii=False)))


def _rule_new_related(days=14, limit=20):
    """新入库 + 与我最近看过的东西共享实体。"""
    sql = f"""
        WITH recent_views AS (
            SELECT file_id FROM file_views
             WHERE viewed_at > now() - interval '{days * 4} day'
             ORDER BY viewed_at DESC LIMIT 20
        ), my_entities AS (
            SELECT DISTINCT m.entity_id
              FROM entity_mentions m JOIN recent_views v ON v.file_id = m.file_id
        ), cand AS (
            SELECT f.id, f.name, f.path, count(DISTINCT m.entity_id) AS shared
              FROM files f
              JOIN entity_mentions m ON m.file_id = f.id
             WHERE f.created_at > now() - interval '{days} day'
               AND f.status = 'parsed'
               AND f.id NOT IN (SELECT file_id FROM recent_views)
               AND m.entity_id IN (SELECT entity_id FROM my_entities)
             GROUP BY f.id, f.name, f.path
        )
        SELECT id, name, path, shared FROM cand WHERE shared >= 2
        ORDER BY shared DESC, id DESC LIMIT {int(limit)}"""
    with db.cursor(commit=False) as con:
        return con.run(sql)


def _rule_doc_gap(limit=30):
    """同目录内出现「前件」但整目录找不到任何「后件」。"""
    out = []
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, name, path FROM files
                           WHERE status='parsed' AND path IS NOT NULL""")
    by_dir = {}
    for fid, name, path in rows:
        d = path.rsplit("/", 1)[0]
        by_dir.setdefault(d, []).append((fid, name))
    for d, items in by_dir.items():
        if len(items) < 2:
            continue
        for before, afters in PAIRS:
            has_before = [x for x in items if before in (x[1] or "")]
            if not has_before:
                continue
            has_after = [x for x in items if any(a in (x[1] or "") for a in afters)]
            if has_after:
                continue
            fid, name = has_before[0]
            out.append((fid, name, d, before, "/".join(afters)))
            if len(out) >= limit:
                return out
    return out


def _rule_stale_value(days=90, limit=5):
    """高频被提及、却长期没被打开过的文件。"""
    sql = f"""
        SELECT f.id, f.name, count(m.entity_id) AS mentions, max(v.viewed_at) AS last_seen
          FROM files f
          JOIN entity_mentions m ON m.file_id = f.id
          LEFT JOIN file_views v ON v.file_id = f.id
         WHERE f.status = 'parsed'
         GROUP BY f.id, f.name
        HAVING (max(v.viewed_at) IS NULL OR max(v.viewed_at) < now() - interval '{days} day')
           AND count(m.entity_id) >= 8
         ORDER BY 3 DESC LIMIT {int(limit)}"""
    with db.cursor(commit=False) as con:
        return con.run(sql)


def generate(dry_run=False):
    """跑三条规则并落库。返回 {kind: 新增条数}。"""
    made = {}
    rows = []

    for fid, name, path, shared in _rule_new_related():
        conf = min(0.9, 0.4 + 0.1 * shared)
        rows.append(("new_related", fid, f"新入库：{name}",
                     f"共享 {shared} 个实体 · 近 14 天入库",
                     conf, {"shared_entities": shared, "dir": (path or "").rsplit("/", 1)[0]}))

    for fid, name, d, before, after in _rule_doc_gap():
        rows.append(("doc_gap", fid, f"可能缺配套文件：{name}",
                     f"缺：{after} · 同目录已有「{before}」",
                     0.5, {"dir": d, "before": before, "after": after}))

    # 冷启动保护：一条浏览记录都没有时，「从没看过」对所有文件都成立，
    # 这条规则退化成「按提及数列前十」——满屏同质卡片，纯噪声。
    # 此时只留前 3 条并说明原因，等有浏览数据后自然恢复。
    with db.cursor(commit=False) as con:
        n_views = con.run("SELECT count(*) FROM file_views")[0][0]
    stale = _rule_stale_value()
    if n_views == 0:
        stale = stale[:3]
    for fid, name, mentions, last_seen in stale:
        why = (f"提及 {mentions} 次 · 系统尚无浏览记录，按重要度列出"
               if n_views == 0 else f"提及 {mentions} 次 · 90 天未打开")
        rows.append(("stale_value", fid, f"很久没看了：{name}", why,
                     0.3 if n_views == 0 else 0.4, {"mentions": int(mentions),
                                                    "cold_start": n_views == 0}))

    if dry_run:
        for r in rows:
            print(f"  [{r[0]:11s}] {r[3][:60]}")
        return {"dry": len(rows)}

    with db.cursor() as con:
        for kind, fid, title, reason, conf, payload in rows:
            # 身份 = 规则 + 目标文件。把理由文本算进哈希是个陷阱：
            # 措辞一改（冷启动→正常态）同一份文件就会被当成新推荐再插一遍。
            sh = _h(kind, fid)
            n = con.run("""INSERT INTO recommendations
                    (kind, target_file_id, title, reason, confidence, payload, source_hash)
                    VALUES (:k,:f,:t,:r,:c,:p::jsonb,:h)
                    -- 裸 ON CONFLICT：既要挡 source_hash，也要挡 uq_rec_open
                    -- （同一规则对同一文件已有未处理条目）；指定单列会漏掉后者
                    ON CONFLICT DO NOTHING RETURNING id""",
                        k=kind, f=fid, t=title, r=reason, c=conf,
                        p=__import__("json").dumps(payload, ensure_ascii=False), h=sh)
            if n:
                made[kind] = made.get(kind, 0) + 1
    return made


def list_open(limit=20, kind=None):
    sql = """SELECT r.id, r.kind, r.target_file_id, r.title, r.reason, r.confidence,
                    f.name, f.path
               FROM recommendations r JOIN files f ON f.id = r.target_file_id
              WHERE r.acted_at IS NULL AND r.dismissed_at IS NULL"""
    params = {"l": limit}
    if kind:
        sql += " AND r.kind = :k"
        params["k"] = kind
    sql += " ORDER BY r.confidence DESC, r.created_at DESC LIMIT :l"
    with db.cursor(commit=False) as con:
        return con.run(sql, **params)


def mark(rec_id, action="acted"):
    col = "acted_at" if action == "acted" else "dismissed_at"
    with db.cursor() as con:
        con.run(f"UPDATE recommendations SET {col}=now() WHERE id=:i", i=rec_id)


if __name__ == "__main__":
    print(generate(dry_run="--dry-run" in sys.argv))
