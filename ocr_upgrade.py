"""OCR 分档与升级候选。

为什么
- mineru 有 4 档（flash / basic / standard / advanced），老扫描件一律用 standard 是浪费：
  有些文件 flash 就够（清晰印刷体），有些 standard 都不够（表格、手写、低清）。
- 正确做法：先便宜档跑一遍 → 按结果质量打分 → 只有判为不足的才升级重跑。

判据（任一命中即进升级队列）
- 正文太短：`chars < ocr_upgrade_min_chars`
- 乱码太多：`□`/`�` 占比 > `ocr_upgrade_max_garbage`
- 汉字太少：中文文档 `cjk_ratio < ocr_upgrade_min_cjk`（说明可能整页识别成拉丁乱码）

升级 = 用 full 档重跑，按版本化重新入库（version+1，旧块下线），并记录前后字数对比。
"""
import re

import db
import ingest
import mineru


def _ensure_table():
    with db.cursor() as con:
        con.run("ALTER TABLE files ADD COLUMN IF NOT EXISTS ocr_tier text")
        con.run("""CREATE TABLE IF NOT EXISTS ocr_upgrade_candidates (
            id bigserial PRIMARY KEY,
            file_id bigint NOT NULL,
            tier_from text,
            tier_to text,
            reason text,
            chars int,
            cjk_ratio numeric(5,3),
            garbage_ratio numeric(5,3),
            status text DEFAULT 'pending',     -- pending|done|skipped
            chars_after int,
            created_at timestamptz DEFAULT now(),
            done_at timestamptz,
            UNIQUE (file_id)
        )""")
        con.run("CREATE INDEX IF NOT EXISTS idx_ocr_up_status ON ocr_upgrade_candidates(status)")


def pick_tier(s, size_bytes):
    """按文件大小自动选首跑档位。

    实测（大文件）：flash 55–72 s / 5–6.8 万字 vs standard 466–500 s / 10–11.6 万字
    → 大文件用 flash，省 7–9 倍时间；小文件 standard 更快也更全。
    """
    if not s.get("ocr_tier_auto", True):
        return s.get("ocr_tier") or "standard"
    import os
    big = float(s.get("ocr_tier_auto_mb", 2)) * 1024 * 1024
    try:
        big = big if big > 0 else 2 * 1024 * 1024
    except Exception:                                # noqa: BLE001
        big = 2 * 1024 * 1024
    try:
        sz = os.path.getsize(size_bytes)
    except Exception:                                # noqa: BLE001
        return s.get("ocr_tier") or "standard"
    if sz > big:
        return s.get("ocr_tier_fast", "flash")
    return s.get("ocr_tier") or "standard"


def quality(text):
    n = max(len(text or ""), 1)
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    bad = sum(1 for ch in text if ch in "□\ufffd")
    return {
        "chars": len(text or ""),
        "cjk_ratio": round(cjk / n, 3),
        "garbage_ratio": round(bad / n, 3),
    }


# 图纸类：文字少是正常的，升级档位救不回来，别浪费算力
_DRAWING = re.compile(r"(详图|断面|平面图|大样|结构图|平面详图|布局|轴测|总平面|索引图|配筋|基础图|系统图)")


def is_drawing(name):
    return bool(_DRAWING.search(name or ""))


_NUMERIC_NAME = re.compile(r"^[0-9A-Za-z_\-. ]{1,12}$")     # 1003.pdf / J005.pdf / 5.pdf
_DOC_WORD = re.compile(r"(报告|协议|合同|审计|请示|批复|通知|办法|方案|规定|证明|记录|清单|台账|意见|函|纪要|说明|标准|规范)")


def judge(text, s, name="", tier_from=None):
    """是否值得升级重跑。

    只有三种情况才升：
    ① 乱码占比高（识别坏了，升级确实能救）；
    ② 几乎没抽到字（<300 字，等于解析失败）；
    ③ 明确是公文类文件，但字数明显不足（<1200 字）。
    图纸（含纯编号命名的图纸页）文字少属正常，直接放过——升级也不会多出内容。
    """
    if not s.get("ocr_upgrade_enabled", True):
        return False, ""
    q = quality(text)
    nm = name or ""
    if is_drawing(nm) or _NUMERIC_NAME.match(nm.rsplit(".", 1)[0]):
        return False, "图纸/编号件，文字少属正常"

    reasons = []
    if q["garbage_ratio"] > float(s.get("ocr_upgrade_max_garbage", 0.12)):
        reasons.append(f"乱码多({q['garbage_ratio']:.0%})")
    # flash 首跑的预期产量本来就低，门槛相应下调，避免全库都被判升级
    low = 500 if tier_from == s.get("ocr_tier_fast", "flash") else 300
    doc_ok = 600 if tier_from == s.get("ocr_tier_fast", "flash") else 1200
    if q["chars"] < low:
        reasons.append(f"几乎无正文({q['chars']}字)")
    elif _DOC_WORD.search(nm) and q["chars"] < doc_ok:
        reasons.append(f"公文类正文偏少({q['chars']}字)")
    return (bool(reasons), "、".join(reasons))


def enqueue(fid, tier_from, text, s, name=""):
    """OCR 入库后调用：质量不达标就进升级队列。"""
    need, reason = judge(text, s, name, tier_from=tier_from)
    if not need:
        return False
    _ensure_table()
    q = quality(text)
    with db.cursor() as con:
        con.run("""INSERT INTO ocr_upgrade_candidates
                     (file_id, tier_from, tier_to, reason, chars, cjk_ratio, garbage_ratio)
                   VALUES (:f,:tf,:tt,:r,:c,:cj,:gb)
                   ON CONFLICT (file_id) DO UPDATE SET
                     tier_from=EXCLUDED.tier_from, tier_to=EXCLUDED.tier_to,
                     reason=EXCLUDED.reason, chars=EXCLUDED.chars,
                     cjk_ratio=EXCLUDED.cjk_ratio, garbage_ratio=EXCLUDED.garbage_ratio,
                     status=CASE WHEN ocr_upgrade_candidates.status='done'
                                 THEN ocr_upgrade_candidates.status ELSE 'pending' END""",
                f=fid, tf=tier_from, tt=s.get("ocr_tier_full", "standard"),
                r=reason[:200], c=q["chars"], cj=q["cjk_ratio"], gb=q["garbage_ratio"])
    print(f"    → 入升级队列：{reason}")
    return True


def run(limit=3, s=None):
    """处理升级候选：full 档重跑，版本化重新入库。"""
    _ensure_table()
    s = s or ingest.settings()
    if not s.get("ocr_upgrade_enabled", True):
        print("  OCR 升级未启用")
        return 0
    tier_to = s.get("ocr_tier_full", "standard")
    with db.cursor(commit=False) as con:
        todo = con.run("""SELECT c.id, c.file_id, f.real_path, f.name, c.chars
                          FROM ocr_upgrade_candidates c JOIN files f ON f.id=c.file_id
                          WHERE c.status='pending' AND f.real_path IS NOT NULL
                            AND lower(coalesce(f.ext,'')) IN ('.pdf','.png','.jpg','.jpeg','.tif','.tiff','.bmp')
                            AND coalesce(c.tier_from,'') <> :to
                          ORDER BY c.chars ASC LIMIT :l""", l=limit, to=tier_to)
        con.run("""UPDATE ocr_upgrade_candidates q SET status='skipped',
                   reason=coalesce(q.reason,'')||' | 格式不支持 OCR 升级'
                   FROM files f
                   WHERE f.id=q.file_id AND q.status='pending'
                     AND lower(coalesce(f.ext,'')) NOT IN
                         ('.pdf','.png','.jpg','.jpeg','.tif','.tiff','.bmp')""")
    if not todo:
        print("  升级队列为空")
        return 0
    done = 0
    for cid, fid, rpath, name, chars_before in todo:
        print(f"  升级 {name[:34]}（原 {chars_before} 字，档位 {tier_to}）")
        try:
            text, meta = mineru.parse_file(s["mineru_url"], rpath, file_name=name,
                                           ocr_mode="ocr", tier=tier_to,
                                           poll_sec=int(s["mineru_poll_sec"]), timeout_sec=1800)
        except Exception as e:                       # noqa: BLE001
            # 4.1.7 在 CPU 模式下不接受 advanced（HTTP 400）：降一档重试一次
            if "400" in str(e) and tier_to != "standard":
                print(f"    档位 {tier_to} 不被接受，降级 standard 重试")
                try:
                    text, meta = mineru.parse_file(s["mineru_url"], rpath, file_name=name,
                                                   ocr_mode="ocr", tier="standard",
                                                   poll_sec=int(s["mineru_poll_sec"]), timeout_sec=1800)
                except Exception as e2:              # noqa: BLE001
                    print(f"    ! 失败: {e2}")
                    continue
            else:
                print(f"    ! 失败: {e}")
                continue
        if not text.strip():
            with db.cursor() as con:
                con.run("""UPDATE ocr_upgrade_candidates SET status='skipped',
                           reason=coalesce(reason,'')||' | 升级后仍无文本', done_at=now()
                           WHERE id=:i""", i=cid)
            continue
        # 硬闸：升级后正文不能明显变少（advanced 并不保证更全，实测有 5/11 反而更短）
        if len(text) < chars_before * 0.95:
            with db.cursor() as con:
                con.run("""UPDATE ocr_upgrade_candidates SET status='skipped',
                           reason=coalesce(reason,'')||' | 升级后正文更少('||:a||'<'||:b||')，保留原结果',
                           chars_after=:a, done_at=now() WHERE id=:i""",
                        a=len(text), b=chars_before, i=cid)
            print(f"    - 跳过：{chars_before} → {len(text)} 字（更少，保留原结果）")
            continue

        n = ingest._ocr_ingest(fid, text, s, note=f"mineru OCR 升级（{tier_to}）")
        with db.cursor() as con:
            con.run("""UPDATE files SET ocr_tier=:t, updated_at=now() WHERE id=:f""",
                    t=tier_to, f=fid)
            con.run("""UPDATE ocr_upgrade_candidates SET status='done',
                       chars_after=:c, done_at=now() WHERE id=:i""", c=len(text), i=cid)
        done += 1
        print(f"    ✓ {chars_before} → {len(text)} 字，{n} 块")
    print(f"  本轮升级 {done} 份")
    return done


def stats():
    _ensure_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT status, count(*) FROM ocr_upgrade_candidates GROUP BY 1""")
        gain = con.run("""SELECT round(avg(chars_after - chars)) , count(*)
                          FROM ocr_upgrade_candidates WHERE status='done' AND chars_after IS NOT NULL""")
    return {r[0]: r[1] for r in rows}, (gain[0][0], gain[0][1])
