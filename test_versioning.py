#!/usr/bin/env python3
"""端到端验证版本语义：改文件 → version+1 → 旧块失效 → 检索只命中最后一版"""
import sys, os, time, json
sys.path.insert(0, "/opt/wenmai")
import db, ingest

TESTDIR = "/tmp/wenmai_verttest"
os.makedirs(TESTDIR, exist_ok=True)
FP = os.path.join(TESTDIR, "版本测试.txt")

def latest_text(fid):
    with db.cursor(commit=False) as con:
        r = con.run("""SELECT version, count(*) FROM chunks
                       WHERE file_id=:f AND is_current GROUP BY 1""", f=fid)
        allv = con.run("""SELECT version, count(*) FROM chunks WHERE file_id=:f
                          GROUP BY 1 ORDER BY 1""", f=fid)
        hit = con.run("""SELECT content FROM chunks WHERE file_id=:f AND is_current
                         ORDER BY seq LIMIT 1""", f=fid)
    return {"current": r, "all_versions": allv, "first_chunk": (hit[0][0][:40] if hit else None)}

def run_index(stage):
    print(f"\n== {stage} ==")
    ingest.scan(0)
    ingest.index(0, True)

# 1) 建源
with db.cursor() as con:
    con.run("DELETE FROM sources WHERE root_path=:r", r=TESTDIR)
    sid = con.run("""INSERT INTO sources(name, root_path, enabled, exclude)
                     VALUES ('版本测试源', :r, true, '[]'::jsonb) RETURNING id""", r=TESTDIR)[0][0]
print("测试源 id =", sid)

# 2) 第一版
open(FP, "w").write("第一版内容：城市运营费用审核，绿化养管补贴 100 万元。\n\n第二段：公交运行成本核算 20 万元。")
run_index("写入第 1 版")
with db.cursor(commit=False) as con:
    fid, v1 = con.run("SELECT id, version FROM files WHERE real_path=:p", p=FP)[0]
print(f"file_id={fid} version={v1}")
print("  块状态:", latest_text(fid))

# 3) 改内容 → 第二版
time.sleep(1.2)
open(FP, "w").write("第二版（已修改）：城市运营费用审核调整为 120 万元，新增水体养管 30 万元。\n\n"
                    "第二段：公交运行成本核算 25 万元，含车辆维护。")
run_index("写入第 2 版")
with db.cursor(commit=False) as con:
    v2, ct = con.run("SELECT version, coalesce(changed_at, created_at) FROM files WHERE id=:i", i=fid)[0]
print(f"version={v2}")
print("  块状态:", latest_text(fid))

# 4) 检索：旧内容的关键词不应再命中
import search as S
for q in ("100 万元 绿化养管补贴", "120 万元 水体养管"):
    r = S.search(q, mode="keyword", limit=3, do_rerank=False)
    names = [(x["name"], x["snippet"][:46].replace("\n", " ")) for x in r["results"][:2]]
    print(f"\n  查询「{q}」→ {len(r['results'])} 条")
    for n, s in names:
        print(f"     {n} | {s}")

# 5) 清理
with db.cursor() as con:
    con.run("DELETE FROM sources WHERE id=:i", i=sid)
print("\n测试源已删除（数据保留在库里，方便回看）")
