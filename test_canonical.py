#!/usr/bin/env python3
"""验证去重"正本"规则：新文件先处理时，旧文件仍应是正本。"""
import sys, os, time
sys.path.insert(0, "/opt/wenmai")
import db, ingest

D = "/tmp/wenmai_duptest"
os.makedirs(D, exist_ok=True)
A, B = os.path.join(D, "原件-旧.txt"), os.path.join(D, "副本-新.txt")
_p1 = "内容完全相同：城市运营费用审核 绿化养管补贴 120 万元。\n\n"
_p2 = "第二段：公交运行成本 25 万元，含车辆维护与油耗。\n\n"
_p3 = "补充条款：本协议经双方签字盖章后生效，未尽事宜另行协商解决。\n" * 40
body = _p1 + _p2 + _p3
open(A, "w").write(body)
open(B, "w").write(body)
# 把 A 的 mtime 设成一年前（A 是原件）
old = time.time() - 365 * 86400
os.utime(A, (old, old))

with db.cursor() as con:
    con.run("DELETE FROM sources WHERE root_path=:r", r=D)
    con.run("DELETE FROM files WHERE real_path LIKE :p", p=D + "/%")
    sid = con.run("""INSERT INTO sources(name,root_path,enabled,exclude)
                     VALUES ('去重正本测试',:r,true,'[]'::jsonb) RETURNING id""", r=D)[0][0]
print("源 id =", sid)

ingest.scan(0)
ingest.index(0, True)

with db.cursor(commit=False) as con:
    rows = con.run("""SELECT id, name, status, dup_of, mtime,
                        (SELECT count(*) FROM chunks c WHERE c.file_id=f.id AND c.is_current)
                      FROM files f WHERE real_path LIKE :p ORDER BY name""", p=D + "/%")
for r in rows:
    print(f"  {r[1]:<12} id={r[0]:<6} status={r[2]:<10} dup_of={r[3]} current块={r[5]}")

import search as S
res = S.search("城市运营费用审核 绿化养管补贴", mode="keyword", limit=5, do_rerank=False)
print(f"\n检索命中 {len(res['results'])} 条（同内容应只出现 1 条，且是原件路径）")
for x in res["results"]:
    print("  ", x["name"], "|", x["path"])

with db.cursor() as con:
    con.run("DELETE FROM sources WHERE id=:i", i=sid)
print("\n测试源已删除")
