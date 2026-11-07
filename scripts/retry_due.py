#!/usr/bin/env python3
"""墨脉 · 到点重试 worker（T1.1）

每 15 分钟一次。做三件事，没有可做事就安静退出（不产生日志噪音）：

1. **对账**：把 files.status='failed' 但策略表里没记过的文件补登记（新失败进入机制）
2. **取到期**：defer 且 next_retry_at 已过、未超 max_attempts、非人工挂起的
3. **重跑**：置回 pending → 调 ingest.index()（复用既有解析链路，不重造）
   结果回写：成功 → indexed；被去重 → duplicate_content；仍失败 → 按错误分类排下一轮或判死

跑在 NAS：/opt/wenmai/venv/bin/python scripts/retry_due.py [--limit N] [--dry-run]
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import ingest
import retry_policy


def reconcile(limit=200):
    """新出现的 failed 文件补登记：否则它们永远在策略表里没有记录。"""
    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT f.id, f.name, f.ext, coalesce(f.error,'')
            FROM files f LEFT JOIN file_index_policies p ON p.file_id = f.id
            WHERE f.status = 'failed'
              AND (p.file_id IS NULL                       -- 从没登记过
                   OR f.updated_at > p.updated_at          -- 有新一次失败，旧判定已过期
                   OR p.policy = 'indexed')                -- 曾入库但现在坏了
            LIMIT :l""", l=limit)
    for fid, name, ext, err in rows:
        policy, code, delay = retry_policy.schedule(fid, err, "failed", ext)
        print(f"  + 补登记 {name[:40]} → {policy}/{code}"
              f"{' 下次 ' + str(delay // 60) + 'min' if delay else ''}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    n_new = reconcile()
    rows = retry_policy.due(a.limit)
    if not rows and not n_new:
        return 0                                    # 无事可做，完全静默

    print(f"到期重试 {len(rows)} 个（本轮补登记 {n_new} 个）")
    if a.dry_run:
        for fid, name, ext, err, att in rows:
            print(f"  {fid:6d} att={att} {name[:44]}  err={err[:40]}")
        return 0

    ids = [r[0] for r in rows]
    with db.cursor() as con:
        for fid in ids:
            con.run("UPDATE files SET status='pending', error=NULL, updated_at=now() WHERE id=:i", i=fid)

    # 复用既有链路。limit 留余量，避免被 autoscan 新加的 pending 挤掉
    ingest.index(limit=max(len(ids) + 20, 50))

    ok = dead = later = 0
    with db.cursor(commit=False) as con:
        for fid, name, ext, old_err, att, old_detail, fsize in rows:
            st = con.run("SELECT status, coalesce(error,''), parse_kind FROM files WHERE id=:i", i=fid)
            status, err, kind = st[0] if st else ("unknown", "", None)
            if status == "parsed":
                retry_policy.mark_ok(fid)
                ok += 1
            elif status == "skipped" and "重复" in (err or ""):
                with db.cursor() as c2:
                    c2.run("""UPDATE file_index_policies SET policy='never', reason_code='duplicate_content',
                              reason_kind='content', next_retry_at=NULL, updated_at=now()
                              WHERE file_id=:i""", i=fid)
                ok += 1
            else:
                # 重试会把 files.error 覆盖成一句流水账，真实原因要从策略表里取回来
                evidence = " ".join(x for x in (err, old_detail) if x)
                if fsize and fsize > 50 * 1024 * 1024 and ext in (".xls", ".doc", ".ppt"):
                    evidence = "超出合理体积，文件可能损坏: " + evidence
                    policy, code, delay = "never", "poison_oversize", None
                    with db.cursor() as c2:
                        c2.run("""UPDATE file_index_policies SET policy='never', reason_code=:c,
                                  reason_kind='poison', reason_detail=:d, next_retry_at=NULL,
                                  updated_at=now() WHERE file_id=:i""",
                               c="poison_oversize", d=evidence[:300], i=fid)
                    dead += 1
                    print(f"  ✗ {name[:36]} 判死 poison_oversize（{fsize/1048576:.0f} MB）")
                    continue
                policy, code, delay = retry_policy.schedule(fid, evidence or "重试后仍失败", status, ext)
                if delay:
                    later += 1
                    print(f"  ↻ {name[:36]} {code} → {delay // 60}min 后再试（第 {att + 1} 次）")
                else:
                    dead += 1
                    print(f"  ✗ {name[:36]} 判死 {code}")

    print(f"结果：成功 {ok} · 待重试 {later} · 判死 {dead}（本轮送入解析 {len(ids)} 个）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
