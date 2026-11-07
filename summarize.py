"""文档摘要：用 8001 给每份文档生成 3–5 句摘要 + 类型 + 关键词。

设计
- 只处理有正文块的文件；同一文件同一 version 只生成一次（换版本会重新生成）。
- 默认空闲才跑（和实体抽取同一套闸门），夜间 01:00–07:00 不等空闲。
- 结果存 `doc_summaries`，前端在文件详情里直接看。
"""
import json
from datetime import datetime

import db
import ingest
from entities import _chat, _endpoint, _parse_json, is_idle

PROMPT = """你是公文档案管理员。为下面的文档写摘要，只输出 JSON，不要解释。

输出格式：
{"doc_type":"审计报告|协议|请示|批复|通知|台账/表格|图纸说明|其他",
 "summary":"3-5 句中文，说明这份文件是什么、涉及哪一方、金额/期限/结论等关键事实",
 "keywords":["4-8 个关键词"]}

文档正文（可能被截断）：
---
__TEXT__
---
只输出 JSON。"""

MAX_CHARS = 6000


def _ensure_table():
    with db.cursor() as con:
        con.run("""CREATE TABLE IF NOT EXISTS doc_summaries (
            file_id   bigint PRIMARY KEY,
            version   int,
            doc_type  text,
            summary   text,
            keywords  text[],
            model     text,
            chars_in  int,
            created_at timestamptz DEFAULT now()
        )""")


def get(file_id):
    _ensure_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT version, doc_type, summary, keywords, created_at
                          FROM doc_summaries WHERE file_id=:f""", f=file_id)
    if not rows:
        return None
    v, t, s, kw, at = rows[0]
    return {"version": v, "doc_type": t, "summary": s, "keywords": kw or [],
            "created_at": at.isoformat() if at else None}


def _text_of(fid, cap=MAX_CHARS):
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT coalesce(heading,''), content FROM chunks
                          WHERE file_id=:f AND is_current ORDER BY seq""", f=fid)
        frow = con.run("SELECT name, coalesce(version,0) FROM files WHERE id=:f", f=fid)
    if not rows:
        return None, None, None
    name = frow[0][0] if frow else ""
    ver = int(frow[0][1]) if frow else 1
    buf = []
    used = 0
    for head, content in rows:
        piece = (f"\n## {head}\n" if head else "\n") + content
        if used + len(piece) > cap:
            buf.append(piece[: max(0, cap - used)])
            break
        buf.append(piece)
        used += len(piece)
    return name, ver, "".join(buf)


def summarize_file(fid, s):
    name, ver, text = _text_of(fid)
    if not text or len(text.strip()) < 80:
        return None
    base = db.require_endpoint(s, "vllm_url")
    model = s.get("vllm_model") or "local"
    raw = _chat(base, model, PROMPT.replace("__TEXT__", f"《{name}》\n{text}"), timeout=240)
    data = _parse_json(raw)
    if not data:
        return None
    summary = (data.get("summary") or "").strip()
    if not summary:
        return None
    kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()][:8]
    with db.cursor() as con:
        con.run("""INSERT INTO doc_summaries(file_id,version,doc_type,summary,keywords,model,chars_in,created_at)
                   VALUES (:f,:v,:t,:s,:k,:m,:c,now())
                   ON CONFLICT (file_id) DO UPDATE SET
                     version=:v, doc_type=:t, summary=:s, keywords=:k, model=:m,
                     chars_in=:c, created_at=now()""",
                f=fid, v=ver, t=(data.get("doc_type") or "其他")[:40], s=summary[:4000],
                k=kws, m=f"{model}@{base}", c=len(text))
    return {"file_id": fid, "name": name, "doc_type": data.get("doc_type"),
            "summary": summary, "keywords": kws}


def run(limit=3, s=None, idle_only=True):
    _ensure_table()
    s = s or ingest.settings()
    base = db.require_endpoint(s, "vllm_url")
    hour = datetime.now().hour
    in_night = 1 <= hour < 7
    if idle_only and in_night:
        print(f"  夜间窗口（{hour} 点），不等空闲")
        idle_check = False
    else:
        idle_check = idle_only
    if idle_check and not is_idle(base):
        print("  8001 正在忙，本轮不生成摘要（不抢业务）")
        return 0

    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT f.id, f.name FROM files f
            WHERE f.status='parsed' AND coalesce(f.text_bytes,0) > 3000
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.file_id=f.id AND c.is_current)
              AND NOT EXISTS (SELECT 1 FROM doc_summaries d
                              WHERE d.file_id=f.id AND d.version = coalesce(f.version,1))
            ORDER BY CASE WHEN f.name ~ '(协议|合同|审计|报告|请示|批复|办法|方案)' THEN 0 ELSE 1 END,
                     coalesce(f.mtime, f.changed_at, f.created_at) DESC
            LIMIT :l""", l=limit)

    done = 0
    for fid, name in rows:
        if idle_check and not is_idle(base):
            print("  8001 转为忙，暂停")
            break
        try:
            r = summarize_file(fid, s)
        except Exception as e:                      # noqa: BLE001
            print(f"  ! {name[:30]} 摘要失败: {e}")
            continue
        if r:
            done += 1
            print(f"  {name[:34]:<34} [{r['doc_type']}] {r['summary'][:60]}…")
    print(f"  本轮生成 {done} 篇摘要")
    return done
