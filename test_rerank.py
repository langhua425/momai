#!/usr/bin/env python3
"""验证重排链路：同一查询，对比 融合(RRF) 与 融合+重排 的排序差异"""
import sys
sys.path.insert(0, "/opt/wenmai")
import search as S
import ingest

s = ingest.settings()
print("配置:", {"rerank_url": s.get("rerank_url"), "model": s.get("rerank_model"),
                "enabled": s.get("rerank_enabled"), "cands": s.get("rerank_candidates")})

Q = "2021年度城市运营费用审核 绿化养管 补贴"
print(f"\n查询：{Q}")

for label, use in (("融合（不重排）", False), ("融合 + 重排", True)):
    r = S.search(Q, mode="hybrid", limit=5, do_rerank=use)
    print(f"\n===== {label} =====")
    if r.get("error"):
        print("  !!", r["error"])
    print(f"  reranked={r['reranked']}")
    for i, x in enumerate(r["results"], 1):
        rs = x["rerank_score"]
        print(f"  {i}. [{x['score']:>8}] rr={rs} v={x['vscore']} k={x['kscore']}  "
              f"{x['name'][:34]}  #{x['seq']}")
        print(f"      {(x['snippet'] or '')[:70].replace(chr(10), ' ')}")
