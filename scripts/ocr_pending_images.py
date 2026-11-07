#!/usr/bin/env python3
"""墨脉 · 把分诊为 ready_to_ocr 的图片真正入库（T0.3b）

跑在 NAS（/opt/momai 或 /opt/wenmai 下，venv 里才有 pg8000）：
    venv/bin/python ocr_pending_images.py [--limit N] [--dry-run]

实测口径：A4 竖版扫描图 flash 档 5 s/张，产 333–1558 字；手机截图只产 28 字。
所以低于 MIN_CHARS 的判为 junk_image 直接收工，不进索引、不烧后续算力。
"""
import argparse
import os
import sys

# 脚本在 scripts/ 子目录里跑，但 db/ingest/mineru 在上一级
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db
import ingest
import mineru

MIN_CHARS = 80          # 低于此值认为没有可索引内容（截图/图标/纯图形）
TIER = "flash"          # 小图一律 flash；standard 在这类图上没有额外收益


def load_targets(limit):
    with db.cursor(commit=False) as con:
        rows = con.run("""
            SELECT f.id, f.real_path, f.name, f.content_hash
            FROM file_index_policies p JOIN files f ON f.id = p.file_id
            WHERE p.policy = 'ocr_candidate' AND p.reason_code = 'ready_to_ocr'
              AND f.status <> 'missing'
            ORDER BY f.size DESC LIMIT :l""", l=(limit or 10**9))
    return rows


def mark(fid, policy, code, detail=None):
    with db.cursor() as con:
        con.run("""UPDATE file_index_policies
                   SET policy=:p, reason_code=:c, reason_detail=:d, updated_at=now()
                   WHERE file_id=:i""", p=policy, c=code, d=detail, i=fid)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    s = ingest.settings()
    base = db.require_endpoint(s, "mineru_url")
    rows = load_targets(a.limit)
    print(f"待 OCR 图片：{len(rows)} 张（mineru={base}）")
    if a.dry_run:
        for fid, rpath, name, _ in rows[:10]:
            print(f"  {fid:6d} {name[:44]}")
        return 0

    done = skipped = failed = 0
    for fid, rpath, name, chash in rows:
        # 同内容已 OCR 过就复用，不重复烧算力
        cached = None
        if chash:
            with db.cursor(commit=False) as con:
                c = con.run("SELECT md_path FROM ocr_cache WHERE content_hash=:h", h=chash)
            if c and c[0][0]:
                try:
                    cached = open(c[0][0], encoding="utf-8").read()
                except Exception:
                    cached = None
        try:
            if cached:
                text = cached
            else:
                text, meta = mineru.parse_file(
                    base, rpath, file_name=name, ocr_mode="ocr", tier=TIER,
                    poll_sec=int(s.get("mineru_poll_sec") or 2), timeout_sec=1800)
            text = (text or "").strip()
            if len(text) < MIN_CHARS:
                mark(fid, "never", "junk_image", f"OCR 仅 {len(text)} 字")
                skipped += 1
                print(f"  ✗ 无内容（{len(text)} 字）{name[:34]}")
                continue
            n = ingest._ocr_ingest(fid, text, s, note="mineru OCR（图片分诊）")
            mark(fid, "indexed", "ocr_done", f"{len(text)} 字 / {n} 块")
            done += 1
            print(f"  ✓ {name[:34]} → {len(text)} 字 / {n} 块")
        except Exception as e:                                    # noqa: BLE001
            failed += 1
            mark(fid, "defer", "ocr_error", f"{type(e).__name__}: {e}"[:200])
            print(f"  ! {name[:34]} 失败：{type(e).__name__}: {str(e)[:80]}")

    print(f"\n完成 {done} · 无内容 {skipped} · 失败 {failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
