#!/usr/bin/env python3
"""验证分词后的关键词召回：对比「整句 ILIKE」与「按词 OR」"""
import sys
sys.path.insert(0, "/opt/wenmai")
import search as S, segment as seg, ingest, db

print("分词器:", seg.info())
s = ingest.settings()
Q = "2021年度城市运营费用审核 绿化养管 补贴"
print("查询:", Q)
print("分词结果:", seg.terms(Q))

# 旧法：整句 ILIKE
with db.cursor(commit=False) as con:
    n_old = con.run("""SELECT count(*) FROM chunks c
                       WHERE c.is_current AND c.content ILIKE :like""", like=f"%{Q}%")[0][0]
print(f"\n整句 ILIKE 命中块数: {n_old}")

# 新法：分词 OR
for mode in ("keyword", "hybrid"):
    r = S.search(Q, mode=mode, limit=5, do_rerank=False)
    print(f"\n===== mode={mode} =====")
    if r.get("error"):
        print("  !!", r["error"])
    for i, x in enumerate(r["results"], 1):
        print(f"  {i}. {x['name'][:36]:<36} k={x['kscore']:<6} 命中词={x.get('khits')} v={x['vscore']}")
        print(f"      {(x['snippet'] or '')[:60].replace(chr(10), ' ')}")
