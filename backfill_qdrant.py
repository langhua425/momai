"""把 PG 里现有的 current 块补写进 Qdrant（一次性回填，可重复跑）。"""
import sys

sys.path.insert(0, "/opt/wenmai")

import db
import ingest
import qdrant_store as qs

BATCH = 200


def main():
    s = ingest.settings()
    if not qs.enabled(s):
        print("  Qdrant 未启用（qdrant_enabled / qdrant_url）")
        return
    qs.ensure_collection(s)
    print(f"  目标集合: {s.get('qdrant_collection')} @ {s.get('qdrant_url')}")

    total = pushed = 0
    last_id = 0
    while True:
        with db.cursor(commit=False) as con:
            rows = con.run("""
                SELECT c.id, c.seq, coalesce(c.heading,''), c.section_path, c.version,
                       c.file_id, f.name, f.real_path, f.ext, c.embedding::text
                FROM chunks c JOIN files f ON f.id=c.file_id
                WHERE c.is_current AND c.embedding IS NOT NULL AND c.id > :last
                ORDER BY c.id LIMIT :lim""", last=last_id, lim=BATCH)
        if not rows:
            break
        pts = []
        for (cid, seq, head, sp, ver, fid, name, path, ext, vec_txt) in rows:
            vec = [float(x) for x in vec_txt.strip("[]").split(",")]
            pts.append({
                "id": int(cid), "vector": vec,
                "payload": {"file_id": int(fid), "seq": int(seq), "heading": head or "",
                            "section_path": sp or "", "version": int(ver or 1),
                            "name": name or "", "path": path or "", "ext": ext or "",
                            "is_current": True},
            })
        pushed += qs.upsert(pts, s)
        total += len(rows)
        last_id = rows[-1][0]
        if total % 1000 < BATCH:
            print(f"  已处理 {total} 块（推送 {pushed}）", flush=True)
    print(f"  完成：扫描 {total} 块，推送 {pushed} 块")
    print("  Qdrant 状态:", qs.health(s))


if __name__ == "__main__":
    main()
