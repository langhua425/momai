#!/usr/bin/env python3
"""墨脉 · 阶段 2 向量重建（T2.1 late-chunking-lite + T2.2 元数据前缀 + T2.4 章节向量）

一次重建同时产出三列，供 A/B 对比，**不要跑三遍**：

  embedding       现状（保留不动，作为对照与回退）
  embedding_meta  T2.2：块文本前拼「文档名 · 章节路径 · 类型 · 年份 · 来源」再嵌入
  embedding_late  T2.1：0.75×本块向量 + 0.25×上下文窗口向量（窗口=本块±2块）

为什么 late chunking 是"lite"版：真 late chunking 需要 token 级向量（整篇过模型再按 token 边界池化），
而 8002 的 llama.cpp server 只输出 pooled 向量。改成窗口池化再混合 —— 同样把邻块语境带进本块向量，
但不需要改引擎的 pooling 模式（那会破坏所有调用方）。差别要如实写在报告里。

T2.4 章节向量：用**最终选用的那列**按章节取均值，不额外调用嵌入服务。
默认用 embedding_late；可用 --section-from 指定。

用法（NAS）：
    venv/bin/python reembed.py --dry-run
    venv/bin/python reembed.py --batch-files 20
"""
import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import ingest

BATCH = 16               # 一次送嵌入的文本条数
WINDOW = 5               # 上下文窗口：本块 ± 2
W_CHUNK, W_WINDOW = 0.75, 0.25
MAX_WINDOW_CHARS = 3800  # 窗口拼接过长会被截断丢语境，宁可少拼


def embed_batch(texts, base):
    body = json.dumps({"input": texts, "model": "local"}).encode()
    req = urllib.request.Request(base, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return [d["embedding"] for d in json.loads(r.read())["data"]]


def blend(a, b, w=W_CHUNK):
    """向量混合后 L2 归一化 —— 不归一化会让混合结果模长漂移，余弦还算得过但内积失真。"""
    v = [x * w + y * (1 - w) for x, y in zip(a, b)]
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


def meta_prefix(row):
    """统一走 ingest.meta_prefix —— 两个实现各写一份，迟早口径漂移。"""
    name, ext, mt, sname, spath = row
    return ingest.meta_prefix(name, ext, mt, sname, spath)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--batch-files", type=int, default=20)
    ap.add_argument("--limit-files", type=int, default=0)
    ap.add_argument("--section-from", default="embedding_late")
    ap.add_argument("--skip-sections", action="store_true")
    a = ap.parse_args()

    s = ingest.settings()
    base = db.require_endpoint(s, "embed_url")

    with db.cursor(commit=False) as con:
        files = con.run("""
            SELECT f.id, f.name, coalesce(f.ext,''), f.mtime, coalesce(src.name,'')
            FROM files f JOIN sources src ON src.id = f.source_id
            WHERE f.status = 'parsed'
              AND EXISTS (SELECT 1 FROM chunks c WHERE c.file_id=f.id AND c.is_current)
            ORDER BY f.id""")
    if a.limit_files:
        files = files[:a.limit_files]
    print(f"待重建文件 {len(files)} 份")
    if a.dry_run:
        for fid, name, ext, _mt, _s in files[:8]:
            print(f"  {fid:6d} {name[:44]}")
        return

    t0 = time.time()
    done_chunks = done_files = 0
    for idx, (fid, name, ext, mt, sname) in enumerate(files, 1):
        with db.cursor(commit=False) as con:
            rows = con.run("""
                SELECT id, content, coalesce(section_path,'')
                FROM chunks WHERE file_id=:f AND is_current ORDER BY seq""", f=fid)
        if not rows:
            continue
        ids = [r[0] for r in rows]
        texts = [r[1] or "" for r in rows]

        # ① 元数据前缀版
        prefix = meta_prefix((name, ext, mt, sname, rows[0][2]))
        emb_texts = [prefix + t for t in texts]
        mvecs = []
        for i in range(0, len(emb_texts), BATCH):
            mvecs.extend(embed_batch(emb_texts[i:i + BATCH], base))

        # ② 窗口向量：滑窗拼接后整段嵌入
        win_texts, win_owner = [], []
        for i in range(0, len(texts), WINDOW):
            seg = texts[max(0, i - 1):i + WINDOW]
            joined = "\n".join(seg)[:MAX_WINDOW_CHARS]
            win_texts.append(prefix + joined)
            win_owner.extend([len(win_texts) - 1] * len(range(i, min(i + WINDOW, len(texts)))))
        wvecs = []
        for i in range(0, len(win_texts), BATCH):
            wvecs.extend(embed_batch(win_texts[i:i + BATCH], base))

        # ③ 混合
        late = [blend(mvecs[k], wvecs[win_owner[k]]) for k in range(len(mvecs))]

        with db.cursor() as con:
            for k, cid in enumerate(ids):
                con.run("""UPDATE chunks SET
                             embedding_v1 = coalesce(embedding_v1, embedding),
                             embedding_meta = :m::vector,
                             embedding_late = :l::vector,
                             embed_text = :t, embed_ver = 'v2'
                           WHERE id=:i""",
                        m=db.vec_literal(mvecs[k]), l=db.vec_literal(late[k]),
                        t=emb_texts[k][:4000], i=cid)
        done_chunks += len(ids)
        done_files += 1
        if idx % 10 == 0 or idx == len(files):
            el = time.time() - t0
            rate = done_chunks / el if el else 0
            print(f"  进度 {idx}/{len(files)} 文件 · {done_chunks} 块 · {rate:.1f} 块/s"
                  f" · 已用 {el/60:.1f} 分", flush=True)

    print(f"\n重建完成：{done_files} 文件 / {done_chunks} 块，耗时 {(time.time()-t0)/60:.1f} 分钟")

    if a.skip_sections:
        return
    # T2.4 章节向量：按章节取块向量均值
    col = a.section_from
    if not col.replace("_", "").isalnum():
        raise SystemExit("列名不合法")
    print(f"生成章节向量（源列 {col}）…")
    with db.cursor() as con:
        con.run(f"""
            INSERT INTO file_section_vectors (section_id, file_id, embedding, chunk_count, updated_at)
            SELECT s.id, s.file_id,
                   (avg(c.{col}))::vector, count(c.id), now()
            FROM file_sections s
            JOIN chunks c ON c.file_id = s.file_id AND c.is_current
                          AND c.seq BETWEEN s.start_chunk_index AND s.end_chunk_index
            WHERE c.{col} IS NOT NULL
            GROUP BY s.id, s.file_id
            ON CONFLICT (section_id) DO UPDATE SET
              embedding=EXCLUDED.embedding, chunk_count=EXCLUDED.chunk_count, updated_at=now()""")
        n = con.run("SELECT count(*) FROM file_section_vectors")[0][0]
    print(f"章节向量：{n} 条")


if __name__ == "__main__":
    main()
