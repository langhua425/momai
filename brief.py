"""工作简报（阶段 5 T5.4）

只写**数字和事实**，不写「成效显著」「持续推进」这类填充句。
简报的价值在于「昨天有哪些变化需要我知道」，凑字数只会让人不再看它。

四块内容：
  ① 昨日变化   新入库 / 解析失败 / OCR 处理 / 重试
  ② 待处理     推荐（前 5）+ 缺口（需人决策）
  ③ 覆盖进度   摘要 / 实体 / 章节（这些是慢工，要看趋势）
  ④ 异常       不变量类问题（只有出问题才出现）

幂等：source_hash = 全部输入数据的指纹。输入不变 → 不重写，
避免夜间重试把同一天的简报刷成多份。
"""
import hashlib
import json
import sys
from datetime import date

import db


def _one(con, sql, **kw):
    r = con.run(sql, **kw)
    return r[0][0] if r else 0


def collect():
    """把简报要用的数字一次取全。"""
    d = {}
    with db.cursor(commit=False) as con:
        d["files_total"] = _one(con, "SELECT count(*) FROM files")
        d["parsed"] = _one(con, "SELECT count(*) FROM files WHERE status='parsed'")
        d["new_24h"] = _one(con, "SELECT count(*) FROM files WHERE created_at > now()-interval '24 hour'")
        d["indexed_24h"] = _one(con, "SELECT count(*) FROM files WHERE last_indexed_at > now()-interval '24 hour'")
        d["failed_24h"] = _one(con, "SELECT count(*) FROM files WHERE status='failed' AND updated_at > now()-interval '24 hour'")
        d["pending"] = _one(con, "SELECT count(*) FROM files WHERE status='pending'")
        d["failed_total"] = _one(con, "SELECT count(*) FROM files WHERE status='failed'")
        d["chunks"] = _one(con, "SELECT count(*) FROM chunks WHERE is_current")
        # chunks 表没有 created_at：块的「新增」只能借文件的 last_indexed_at 判断
        d["chunks_24h"] = _one(con, """SELECT count(*) FROM chunks c JOIN files f ON f.id=c.file_id
                                        WHERE c.is_current AND f.last_indexed_at > now()-interval '24 hour'""")
        d["summary_cov"] = _one(con, "SELECT count(DISTINCT file_id) FROM doc_summaries")
        d["entities"] = _one(con, "SELECT count(*) FROM entities")
        d["sections"] = _one(con, "SELECT count(*) FROM file_sections")
        d["memories"] = _one(con, "SELECT count(*) FROM memory_items WHERE status='active'")
        d["reco_open"] = _one(con, "SELECT count(*) FROM recommendations WHERE acted_at IS NULL AND dismissed_at IS NULL")
        # 只数「仍在待处理里」的新增：把已处理/已忽略的也算进来，
        # 会出现「待处理 13 条（近 24h 新增 20 条）」这种自相矛盾的数字
        d["reco_new"] = _one(con, """SELECT count(*) FROM recommendations
                                      WHERE acted_at IS NULL AND dismissed_at IS NULL
                                        AND created_at > now()-interval '24 hour'""")
        d["reco_top"] = con.run("""SELECT r.kind, r.title, r.reason FROM recommendations r
                                    WHERE r.acted_at IS NULL AND r.dismissed_at IS NULL
                                    ORDER BY r.confidence DESC, r.created_at DESC LIMIT 5""")
        d["gaps"] = con.run("""SELECT reason_code, count(*) FROM file_index_policies
                                WHERE reason_kind IN ('error','poison') GROUP BY 1 ORDER BY 2 DESC LIMIT 5""")
        d["ocr_pending"] = _one(con, "SELECT count(*) FROM ocr_queue WHERE status IN ('pending','running')")
        d["drift_note"] = con.run("""SELECT count(*) FROM file_index_policies
                                      WHERE policy='never' AND reason_kind='decision'""")
    return d


def render(d):
    L = []

    L += ["## 昨日变化", ""]
    L.append(f"- 新入库 **{d['new_24h']}** 份，索引完成 **{d['indexed_24h']}** 份，解析失败 **{d['failed_24h']}** 份")
    L.append(f"- 新增文本块 **{d['chunks_24h']:,}**（现共 {d['chunks']:,}）")
    if d["pending"]:
        L.append(f"- 待处理队列 **{d['pending']}** 份")
    L.append("")

    L += ["## 待处理", ""]
    if d["reco_open"]:
        L.append(f"推荐 **{d['reco_open']}** 条（近 24h 新增 {d['reco_new']} 条）：")
        for kind, title, reason in d["reco_top"]:
            L.append(f"  - {title} — {reason}")
    else:
        L.append("- 无待处理推荐")
    if d["gaps"]:
        L.append("")
        L.append("解析侧未解决：")
        for code, n in d["gaps"]:
            L.append(f"  - `{code}` **{n}** 份")
    if d["ocr_pending"]:
        L.append(f"- OCR 队列待处理 **{d['ocr_pending']}** 份")
    L.append("")

    L += ["## 覆盖进度", ""]
    tot = d["parsed"] or 1
    L.append(f"- 整篇摘要 {d['summary_cov']}/{d['parsed']}（{d['summary_cov'] * 100 // tot}%）")
    L.append(f"- 实体 {d['entities']} 个 · 章节 {d['sections']} 个 · 记忆 {d['memories']} 条")
    L.append("")

    L += ["## 备注", ""]
    L.append(f"- 库内共 {d['files_total']:,} 份文件，其中已解析 {d['parsed']:,} 份")
    if d["failed_total"]:
        L.append(f"- 累计解析失败 {d['failed_total']} 份（含 poison：超限/乱码，不再自动重试）")
    L.append("")
    return "\n".join(L)


def build(force=False):
    d = collect()
    # 指纹只取会影响正文的字段；reco_top 的顺序也算在内
    fp = json.dumps({k: v for k, v in d.items() if k != "reco_top"},
                    ensure_ascii=False, sort_keys=True, default=str)
    fp += "|" + "|".join(f"{r[0]}:{r[1]}" for r in d["reco_top"])
    h = hashlib.sha1(fp.encode()).hexdigest()[:24]
    md = render(d)
    today = date.today()

    with db.cursor() as con:
        row = con.run("SELECT source_hash FROM work_briefs WHERE brief_date=:d", d=today)
        if row and row[0][0] == h and not force:
            return {"skipped": True, "reason": "内容未变", "hash": h}
        con.run("""INSERT INTO work_briefs (brief_date, content_md, data, source_hash)
                   VALUES (:d,:md,:data::jsonb,:h)
                   ON CONFLICT (brief_date) DO UPDATE
                     SET content_md=EXCLUDED.content_md, data=EXCLUDED.data,
                         source_hash=EXCLUDED.source_hash, updated_at=now()""",
                d=today, md=md, data=json.dumps(d, ensure_ascii=False, default=str), h=h)
    return {"skipped": False, "hash": h, "chars": len(md), "date": str(today)}


def latest():
    with db.cursor(commit=False) as con:
        r = con.run("""SELECT brief_date, content_md, source_hash, created_at
                         FROM work_briefs ORDER BY brief_date DESC LIMIT 1""")
    if not r:
        return None
    return {"date": str(r[0][0]), "content_md": r[0][1], "hash": r[0][2],
            "created_at": str(r[0][3])}


if __name__ == "__main__":
    print(build(force="--force" in sys.argv))
