#!/usr/bin/env python3
"""实测 mineru 是否能并行处理多个任务：同时提交 2 个，看墙钟时间"""
import sys, time, threading
sys.path.insert(0, "/opt/wenmai")
import db, mineru

s = {"mineru_url": "http://10.10.10.1:8004"}

with db.cursor(commit=False) as con:
    rows = con.run("""SELECT id, real_path, name, size FROM files
                      WHERE parse_kind='pdf-scan' AND size BETWEEN 1000000 AND 3000000
                      ORDER BY size LIMIT 2""")
print("样本:", [(r[2][:28], round(r[3]/1048576, 1)) for r in rows])

results = {}

def run(i, rpath, name):
    t0 = time.time()
    try:
        text, meta = mineru.parse_file(s["mineru_url"], rpath, file_name=name,
                                       ocr_mode="ocr", poll_sec=2, timeout_sec=900)
        results[i] = (round(time.time() - t0, 1), len(text), meta.get("parse", {}).get("duration_ms"))
    except Exception as e:
        results[i] = (round(time.time() - t0, 1), 0, f"err {e}")

t_all = time.time()
ts = [threading.Thread(target=run, args=(i, r[1], r[2])) for i, r in enumerate(rows)]
for t in ts:
    t.start()
for t in ts:
    t.join()

print("\n并发结果（墙钟 / 字数 / 服务端耗时ms）:")
for i, v in sorted(results.items()):
    print(f"  任务{i}: {v}")
print(f"两个任务总墙钟: {round(time.time() - t_all, 1)}s")
print("判定：总墙钟 ≈ 单个 → 服务端串行；总墙钟 ≈ 单个 → 并行加速生效")
