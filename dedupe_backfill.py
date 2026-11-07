"""存量去重：给缺 content_hash 的已解析文件补哈希，再把同内容的多份降级为副本。

为什么需要：入库去重只对新文件生效（`scan`→`index` 时算哈希、比对 peers）。
早期记录（content_hash 为空）永远比不到一起，于是同一份内容会有两条 current 记录，
检索结果里就会同名同分重复出现。

正本规则与入库一致：`_canon_rank(mtime 最早, 路径最短, id 最小)`。
副本必须彻底退出检索：`status='duplicate'` + `dup_of` + `chunks.is_current=false` + 删 Qdrant 点。

用法：
  python dedupe_backfill.py --dry-run
  python dedupe_backfill.py --apply
"""
import argparse
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import ingest
import qdrant_store


def sha256_path(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只演练）")
    ap.add_argument("--limit", type=int, default=0, help="最多处理多少个待补哈希文件")
    a = ap.parse_args()
    s = ingest.settings()

    # ---- 1. 补哈希 ----
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, real_path, path, name FROM files
                          WHERE content_hash IS NULL AND status='parsed'
                            AND real_path IS NOT NULL
                          ORDER BY id""")
    if a.limit:
        rows = rows[: a.limit]
    print(f"待补哈希: {len(rows)} 份")
    filled = 0
    for fid, rp, path, name in rows:
        try:
            if not rp or not os.path.exists(rp):
                print(f"  - 跳过 id={fid}（文件不存在）{name[:30]}")
                continue
            h = sha256_path(rp)
        except Exception as e:  # noqa: BLE001
            print(f"  - 哈希失败 id={fid}: {e}")
            continue
        if a.apply:
            with db.cursor() as con:
                con.run("UPDATE files SET content_hash=:h WHERE id=:i", h=h, i=fid)
        filled += 1
    print(f"{'已补' if a.apply else '可补'}哈希: {filled} 份")

    # ---- 2. 同内容分组 ----
    with db.cursor(commit=False) as con:
        groups = con.run("""
            SELECT content_hash, array_agg(id ORDER BY id) AS ids, count(*) AS n
            FROM files
            WHERE content_hash IS NOT NULL AND dup_of IS NULL
              AND status IN ('parsed','pdf-ocr')
            GROUP BY content_hash HAVING count(*) > 1
            ORDER BY count(*) DESC""")
    print(f"同内容分组: {len(groups)} 组")
    total_dup = 0
    for gh, ids, n in groups:
        with db.cursor(commit=False) as con:
            meta = con.run("""SELECT id, mtime, length(path) FROM files WHERE id = ANY(:ids::bigint[])""",
                           ids=list(ids))
        # 与 index() 里的 _canon_rank 同构：mtime 最早优先，其次路径短，最后 id 小
        def canon_rank(mt, plen, fid):
            newest = 9e18
            return (mt.timestamp() if mt else newest, plen or 0, fid)

        meta.sort(key=lambda r: canon_rank(r[1], r[2], r[0]))
        canon = meta[0][0]
        dups = [m[0] for m in meta[1:]]
        with db.cursor(commit=False) as con:
            names = con.run("""SELECT id, name FROM files WHERE id = ANY(:ids::bigint[])""", ids=list(ids))
        nm = {i: x for i, x in names}
        print(f"  组 {gh[:12]}：{n} 份 → 正本 id={canon}（{nm.get(canon,'')[:34]}），副本 {dups}")
        if not a.apply:
            continue
        for d in dups:
            with db.cursor() as con:
                con.run("UPDATE chunks SET is_current=false WHERE file_id=:i AND is_current", i=d)
                con.run("""UPDATE files SET status='duplicate', dup_of=:c,
                           error=:e, updated_at=now() WHERE id=:i""",
                        c=canon, e=f"内容重复，正本 id={canon}", i=d)
            # Qdrant 里对应的点要删掉，否则向量副本还在
            with db.cursor(commit=False) as con:
                cids = [r[0] for r in con.run("SELECT id FROM chunks WHERE file_id=:i", i=d)]
            if cids and s.get("qdrant_enabled", True):
                try:
                    for k in range(0, len(cids), 200):
                        qdrant_store.delete_ids(cids[k:k + 200], s)
                except Exception as e:  # noqa: BLE001
                    print(f"    ! Qdrant 删点失败 file={d}: {e}")
            total_dup += 1
    print(f"{'已降级' if a.apply else '可降级'}副本: {total_dup} 份")
    if not a.apply:
        print("演练结束。确认无误后加 --apply 实跑。")


if __name__ == "__main__":
    main()
