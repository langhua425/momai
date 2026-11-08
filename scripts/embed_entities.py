"""给实体补向量（阶段 5 T5.2）

嵌入文本的构造：`名称 · 类型`。为什么带类型——
「静湖」是 doc 还是 project，语义上差别很大（一个是资料名，一个是建设项目），
只嵌名称会让「静湖水质」「静湖项目」这类查询在实体空间里糊成一团。

不加 props / 提及次数：那些是结构信号，嵌入里掺进去会污染语义。
"""
import json
import os
import sys
import urllib.request

# 脚本在 scripts/ 子目录，模块在主目录 —— 不在 sys.path 里就 import 不到 db/ingest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db  # noqa: E402
import ingest  # noqa: E402

BATCH = 32
TYPE_CN = {"doc": "文件", "org": "单位", "project": "项目", "money": "金额", "person": "人员"}


def embed_batch(texts, s):
    url = db.require_endpoint(s, "embed_url")
    body = json.dumps({"input": texts, "model": "local"}).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=180) as r:
        return [d["embedding"] for d in json.loads(r.read())["data"]]


def main(force=False):
    s = ingest.settings()
    with db.cursor(commit=False) as con:
        where = "" if force else "WHERE embedding IS NULL"
        rows = con.run(f"SELECT id, name, coalesce(type,'') FROM entities {where} ORDER BY id")
    if not rows:
        print("实体向量已齐，无事可做")
        return
    print(f"待嵌入 {len(rows)} 个实体")
    done = 0
    for i in range(0, len(rows), BATCH):
        chunk = rows[i:i + BATCH]
        texts = [f"{r[1]} · {TYPE_CN.get(r[2], r[2] or '实体')}" for r in chunk]
        try:
            vecs = embed_batch(texts, s)
        except Exception as e:                                 # noqa: BLE001
            print(f"  批次 {i} 失败: {type(e).__name__}: {e}")
            continue
        with db.cursor() as con:
            for r, v in zip(chunk, vecs):
                con.run("UPDATE entities SET embedding=:v::vector, embed_ver='name_type' WHERE id=:i",
                        v=db.vec_literal(v), i=r[0])
        done += len(chunk)
        print(f"  {done}/{len(rows)}")
    with db.cursor(commit=False) as con:
        n = con.run("SELECT count(*) FROM entities WHERE embedding IS NOT NULL")[0][0]
    print(f"完成：有向量的实体 {n}")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
