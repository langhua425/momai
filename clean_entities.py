"""清理已入库的噪声实体（图号/图纸文件名/纯编号），连带关系与提及。"""
import re
import sys

sys.path.insert(0, "/opt/wenmai")

import db
from entities import _clean_entity_name

DRY = "--dry-run" in sys.argv


def main():
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, type, name, coalesce(mentions,0) FROM entities""")
    junk = [(r[0], r[1], r[2], r[3]) for r in rows if _clean_entity_name(r[2]) is None]
    print(f"  实体总数 {len(rows)}，判定噪声 {len(junk)}")
    for j in junk[:25]:
        print(f"    - [{j[1]}] {j[2]}")
    if len(junk) > 25:
        print(f"    … 其余 {len(junk) - 25} 条同类")
    if not junk:
        return
    if DRY:
        print("  演练模式，不删除")
        return
    ids = [j[0] for j in junk]
    with db.cursor() as con:
        r1 = con.run("""DELETE FROM relations WHERE src_id = ANY(:ids) OR dst_id = ANY(:ids)
                        RETURNING id""", ids=ids)
        r2 = con.run("""DELETE FROM entity_mentions WHERE entity_id = ANY(:ids) RETURNING entity_id""", ids=ids)
        r3 = con.run("""DELETE FROM entities WHERE id = ANY(:ids) RETURNING id""", ids=ids)
    print(f"  已删：关系 {len(r1)} 条、提及 {len(r2)} 条、实体 {len(r3)} 个")
    with db.cursor(commit=False) as con:
        print("  剩余:", con.run("SELECT count(*) FROM entities")[0][0], "实体 /",
              con.run("SELECT count(*) FROM relations")[0][0], "关系")


if __name__ == "__main__":
    main()
