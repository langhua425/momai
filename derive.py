"""记忆自动派生（阶段 4 的 4.2b 补齐）

从已有整篇摘要里抽「可复用的事实」，进记忆库。

三条硬约束：

① **authority='summary'**。机器抽出来的东西永远不许顶掉公文里的事实
   （memory.remember 里权威性只降不升，这里只是把来源标清楚）。

② **只抽能独立成立的事实**。「本文件是某某备案表」这种是对文件的描述，
   不是事实；要抽的是「某工程竣工验收日期是 2019-11-06」这类可被追问、
   可被引用、且换一份文件仍然成立的陈述。

③ **按文件版本幂等**。同一份文件同一版本只派生一次；重跑不会把同一份
   摘要再喂一遍模型（既费时又会产生近似重复的事实）。
"""
import json
import re
import sys

import db
import ingest
import memory as M
import assistant as A

KINDS_OK = {"date", "money", "org", "standard", "quantity", "fact"}

PROMPT = """从下面这份文件的摘要中，抽取可以脱离本文件独立成立的事实。

规则：
1. 只抽「换个场合问起来仍然成立」的陈述。例如「某工程竣工验收日期为 2019 年 11 月 6 日」「静湖投放鲢鱼 20 万尾」「该单位法定代表人为某某」。
2. 不要抽「本文件包含…」「该文件为…备案表」这类对文件本身的描述。
3. 不要把推测写进 facts：摘要没说的一律不补。
4. 每条 fact 给一个稳定的 key（中文短语，同一件事在不同文件里应当用同一个 key，例如「静湖鱼苗投放数量」）。
5. 每条给 kind：date/money/org/standard/quantity/fact 之一。
6. 最多 4 条；没有可抽的就返回空数组。

只输出 JSON，不要任何解释：
{"facts":[{"key":"…","content":"…","kind":"…","valid_from":"YYYY-MM-DD 或省略"}]}

文件名：{name}
摘要：
{summary}
"""


def _json_out(txt):
    """模型偶尔会包 ```json，或前后带解释。只取第一个平衡的 {}。"""
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:                                          # noqa: BLE001
        return None


def _seen(file_id, version):
    with db.cursor(commit=False) as con:
        r = con.run("""SELECT count(*) FROM memory_evidence
                        WHERE file_id=:f AND excerpt = :tag""",
                    f=file_id, tag=f"derived:v{version}")
    return r[0][0] > 0


def _mark(file_id, version, memory_id, excerpt=""):
    with db.cursor() as con:
        con.run("""INSERT INTO memory_evidence(memory_id, file_id, chunk_id, excerpt)
                   VALUES (:m,:f,NULL,:e)""",
                m=memory_id, f=file_id, e=f"derived:v{version}")


def derive_one(file_id, name, summary, version, s):
    msgs = [{"role": "user", "content": PROMPT.replace("{name}", name or "")
                                   .replace("{summary}", (summary or "")[:4000])}]
    # 800 tokens 不够：本地模型先走思考再输出，预算会在 JSON 中途用完
    # （实测同一份摘要 800 截断、1600 完整）。宁可多给预算，也不要拿半截 JSON 去解析。
    base = db.require_endpoint(s, "vllm_url")
    model = s.get("vllm_model") or "local"
    data = None
    for budget in (1600, 2400):
        raw = A._chat(base, model, msgs, timeout=240, max_tokens=budget, temperature=0.1)
        data = _json_out(raw if isinstance(raw, str) else json.dumps(raw))
        if data and isinstance(data.get("facts"), list):
            break
        data = None
    if not data:
        return [], "模型未返回可解析的 JSON（两次预算均失败）"

    made = []
    for f in data["facts"][:4]:
        content = (f.get("content") or "").strip()
        if len(content) < 8 or len(content) > 300:
            continue
        kind = f.get("kind") if f.get("kind") in KINDS_OK else "fact"
        try:
            r = M.remember(content, key=(f.get("key") or "").strip() or None,
                           subject=name, kind=kind, authority="summary",
                           confidence=0.55, scope="default", source_kind="derived",
                           valid_from=(f.get("valid_from") or None),
                           file_id=file_id, reason=f"由 {name} 摘要派生",
                           changed_by="derive", embed=False)
            made.append(r)
        except Exception as e:                                 # noqa: BLE001
            made.append({"error": f"{type(e).__name__}: {e}", "content": content[:40]})
    _mark(file_id, version, made[0]["id"] if made and "id" in made[0] else None)
    return made, ""


def run(limit=20, dry=False):
    s = ingest.settings()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT ds.file_id, f.name, ds.summary, ds.version
                            FROM doc_summaries ds JOIN files f ON f.id = ds.file_id
                           WHERE ds.summary IS NOT NULL AND length(ds.summary) > 60
                           ORDER BY ds.created_at DESC""")
    todo = [(f, n, sm, v) for (f, n, sm, v) in rows if not _seen(f, v)][:limit]
    print(f"待派生 {len(todo)} 份（已派生的跳过）")
    stats = {"created": 0, "superseded": 0, "rejected": 0, "updated": 0, "fail": 0}
    for i, (fid, name, summary, ver) in enumerate(todo, 1):
        if dry:
            print(f"  [dry] {name[:40]}")
            continue
        try:
            made, err = derive_one(fid, name, summary, ver, s)
        except Exception as e:                                 # noqa: BLE001
            print(f"  {i}/{len(todo)} {name[:30]} 失败: {type(e).__name__}: {e}")
            stats["fail"] += 1
            continue
        acts = [m.get("action") for m in made if m.get("action")]
        for a in acts:
            stats[a] = stats.get(a, 0) + 1
        print(f"  {i}/{len(todo)} {name[:26]:28s} 抽出 {len(made)} 条 {acts} {err}")
    return stats


if __name__ == "__main__":
    n = 20
    for a in sys.argv[1:]:
        if a.isdigit():
            n = int(a)
    print(run(limit=n, dry="--dry-run" in sys.argv))
