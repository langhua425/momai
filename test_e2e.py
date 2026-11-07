#!/usr/bin/env python3
"""端到端检索质量与延迟抽检（走 HTTP，跟 UI 完全同一条链路）"""
import json, time, urllib.parse, urllib.request

BASE = "http://127.0.0.1:8800"
QUERIES = [
    "2021年度城市运营费用审核 绿化养管 补贴",
    "静湖幸福河湖 岸线植被覆盖率",
    "智慧供水 二次供水 设施管理责任",
    "污水处理 定价成本监审",
]

def call(q, limit=5):
    url = f"{BASE}/api/search?q={urllib.parse.quote(q)}&limit={limit}"
    t0 = time.time()
    with urllib.request.urlopen(url, timeout=180) as r:
        d = json.loads(r.read())
    return d, round((time.time() - t0) * 1000)

for q in QUERIES:
    d, ms = call(q)
    print(f"\n■ {q}   [{ms} ms]  reranked={d.get('reranked')}  {d.get('count')} 条")
    if d.get("error"):
        print("   !!", d["error"])
    for i, x in enumerate(d["results"][:4], 1):
        print(f"  {i}. {x['name'][:38]:<38} rr={x['rerank_score']} k={x['kscore']:<6} v={x['vscore']}")
        if x.get("section_path"):
            print(f"      § {x['section_path'][:60]}")
