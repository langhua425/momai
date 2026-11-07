#!/usr/bin/env python3
"""索引流水线：扫描 → 递归解包 → 抽取文本 → 切块 → 嵌入 → 入库（PG + pgvector）。

用法:
  python ingest.py init
  python ingest.py add-source 名称 /路径
  python ingest.py scan [--limit N]
  python ingest.py index [--limit N] [--no-embed]
  python ingest.py status
"""
import os
import sys
import json
import hashlib
import argparse
import subprocess
import time
from datetime import datetime, timezone

import db
import extract
import archives
import mineru
import qdrant_store as qs
from schema_v2 import DDL, DEFAULT_SETTINGS_V2, LEGACY_DROP


# ---------------- 基础 ----------------

def init_schema(drop_legacy=False):
    """建表；drop_legacy=True 时才清理 v1（pages/claims/chunks）遗留表。
    注意：chunks 表不能在每次运行时重建，否则块数据会被清空。"""
    with db.cursor() as con:
        if drop_legacy:
            for stmt in LEGACY_DROP:
                con.run(stmt)
        for stmt in DDL:
            con.run(stmt)
    with db.cursor() as con:
        for k, v in DEFAULT_SETTINGS_V2.items():
            con.run(
                "INSERT INTO settings(key,value) VALUES (:k,:v::jsonb) ON CONFLICT (key) DO NOTHING",
                k=k, v=json.dumps(v, ensure_ascii=False))


def settings():
    out = dict(DEFAULT_SETTINGS_V2)
    with db.cursor(commit=False) as con:
        rows = con.run("SELECT key, value FROM settings")
    for k, v in rows:
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v          # v1 遗留的裸字符串，原样保留
        else:
            out[k] = v
    return out


def sha256_file(path, chunk=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------- 扫描 ----------------

def _ensure_move_tables():
    with db.cursor() as con:
        con.run("""CREATE TABLE IF NOT EXISTS file_move_candidates (
            id bigserial PRIMARY KEY,
            file_id bigint,
            old_path text,
            new_path text,
            name text,
            ext text,
            size bigint,
            mtime timestamptz,
            status text DEFAULT 'pending',   -- pending|resolved|expired
            note text,
            created_at timestamptz DEFAULT now()
        )""")
        con.run("CREATE INDEX IF NOT EXISTS idx_move_cand_status ON file_move_candidates(status)")


def _mark_missing(sid, root_abs, skip_ids, s):
    """真丢失的文件：状态改 missing、当前版块下线、Qdrant 删点（保留记录以便回溯）。

    必须现查库：移动识别会把老记录搬到新路径，用扫描前的旧快照会把它误判成缺失。
    """
    if not s.get("missing_detect_enabled", True):
        return 0
    import qdrant_store as _qs
    skip = set(skip_ids or ())
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, real_path, name FROM files
                          WHERE source_id=:sid AND coalesce(status,'') <> 'missing'
                            AND real_path IS NOT NULL""", sid=sid)
    gone = [(r[0], r[1], r[2]) for r in rows
            if r[0] not in skip and r[1]
            and os.path.abspath(r[1]).startswith(root_abs) and not os.path.exists(r[1])]
    if not gone:
        return 0
    n = 0
    for oid, rp, nm in gone:
        with db.cursor(commit=False) as con:
            rows = con.run("SELECT id FROM chunks WHERE file_id=:i AND is_current", i=oid)
            cur = con.run("SELECT status FROM files WHERE id=:i", i=oid)
        ids = [r[0] for r in rows]
        if ids:
            _qs.delete_ids(ids, s)
        with db.cursor() as con:
            con.run("UPDATE chunks SET is_current=false WHERE file_id=:i AND is_current", i=oid)
            con.run("""UPDATE files SET status='missing', error='磁盘上已不存在（可能被移动或删除）',
                       updated_at=now() WHERE id=:i""", i=oid)
        n += 1
    if n:
        print(f"  标记缺失 {n} 个文件（原状态已保留在 error 字段）")
    return n


def _reconcile_moves(sid, root, new_rows, s):
    """新路径出现 + 旧路径消失 + 同名同大小 → 判定为移动/改名，复用原记录。

    复用原 id 的意义：块、版本、实体关系、摘要、md 全部跟着走，不会因为改名而重新解析一遍。
    多个候选（同名同大小的多份）不猜，落到 file_move_candidates 待人工确认。
    """
    if not s.get("move_detect_enabled", True):
        return 0
    _ensure_move_tables()
    root_abs = os.path.abspath(root)
    with db.cursor(commit=False) as con:
        olds = con.run("""SELECT id, real_path, name, size, content_hash FROM files
                          WHERE source_id=:sid AND coalesce(status,'') <> 'missing'
                            AND real_path IS NOT NULL""", sid=sid)

    moved_ids = []
    missing = []
    for oid, rp, nm, sz, ch in olds:
        if not rp or not os.path.abspath(rp).startswith(root_abs) or os.path.exists(rp):
            continue
        missing.append((oid, rp, nm, sz or 0, ch))

    by_hash = {}
    by_szmt = {}
    for rec in missing:
        if rec[4]:
            by_hash.setdefault(rec[4], []).append(rec)
        by_szmt.setdefault((rec[3], rec[2]), []).append(rec)      # (size, name)

    moved = ambiguous = 0
    for (fid_new, p, fn, sz, mt, ext) in new_rows:
        cand = []
        if missing:
            try:
                h = sha256_file(p)
            except Exception:                                     # noqa: BLE001
                h = None
            if h and by_hash.get(h):
                cand = by_hash[h]
                # 哈希相同但多个候选时，用路径相似度再筛（同名优先）
                if len(cand) > 1:
                    same = [c for c in cand if c[2] == fn]
                    cand = same or cand
            if not cand:
                by_szmt2 = [c for c in by_szmt.get((sz or 0, fn), [])]
                if len(by_szmt2) == 1 and not by_szmt2[0][4]:
                    cand = by_szmt2          # 未入库过哈希的老文件，用 大小+同名 兜底
        if not cand:
            continue
        if len(cand) > 1:
            oid, oldp = cand[0][0], cand[0][1]
            with db.cursor() as con:
                con.run("""INSERT INTO file_move_candidates(file_id,old_path,new_path,name,ext,size,mtime,note)
                           VALUES (:f,:o,:n,:nm,:e,:sz,:mt,:note)""",
                        f=oid, o=oldp, n=p, nm=fn, e=ext, sz=sz, mt=mt,
                        note=f"{len(cand)} 个同名同大小候选，需人工确认")
            ambiguous += 1
            continue
        oid, oldp = cand[0][0], cand[0][1]
        with db.cursor(commit=False) as con:
            nchunk = con.run("SELECT count(*) FROM chunks WHERE file_id=:i AND is_current", i=oid)[0][0]
        with db.cursor() as con:
            con.run("DELETE FROM files WHERE id=:n", n=fid_new)     # 新记录尚未解析，直接丢弃
            con.run("""UPDATE files SET path=:p, real_path=:p, name=:n, ext=:e, size=:sz,
                       mtime=:mt, updated_at=now(),
                       error='已识别为移动/改名（原路径保留在 file_move_candidates）'
                       WHERE id=:i""", p=p, n=fn, e=ext, sz=sz, mt=mt, i=oid)
            con.run("""INSERT INTO file_move_candidates(file_id,old_path,new_path,name,ext,size,mtime,status)
                       VALUES (:f,:o,:np,:nm,:e,:sz,:mt,'resolved')""",
                    f=oid, o=oldp, np=p, nm=fn, e=ext, sz=sz, mt=mt)
        print(f"  识别移动: {fn}  ({oldp} → {p}) 复用 id={oid}，保留 {nchunk} 块")
        missing = [m for m in missing if m[0] != oid]
        moved_ids.append(oid)
        moved += 1

    _mark_missing(sid, root_abs, moved_ids, s)
    if moved or ambiguous:
        print(f"  移动识别：复用 {moved} 个；存疑 {ambiguous} 个（见 file_move_candidates）")
    return moved


def _upsert_one(sid, p, skip, max_bytes):
    """把单个磁盘文件 upsert 进 files（全量 walk 与增量事件共用这一段）。

    返回 {id, is_new, ok, ext, st}；读不到/该忽略时返回 None。
    """
    fn = os.path.basename(p)
    if fn.startswith("~$") or fn.startswith(".~lock") or fn.startswith("."):
        return None
    try:
        st = os.stat(p)
    except OSError:
        return None
    ext = os.path.splitext(fn)[1].lower()
    ok = ext not in skip and st.st_size <= max_bytes
    status = "pending" if ok else "skipped"
    with db.cursor() as con:
        r = con.run(
            """INSERT INTO files(source_id,path,real_path,name,ext,size,mtime,status,error)
               VALUES (:sid,:p,:rp,:n,:e,:sz,:mt,:st,:er)
               ON CONFLICT (path) DO UPDATE SET
                 size=EXCLUDED.size, mtime=EXCLUDED.mtime,
                 status=CASE WHEN files.size<>EXCLUDED.size THEN EXCLUDED.status
                             WHEN files.status IN ('pending','missing') THEN EXCLUDED.status
                             ELSE files.status END,
                 error=CASE WHEN files.size<>EXCLUDED.size THEN EXCLUDED.error ELSE files.error END,
                 updated_at=now()
               RETURNING id, (xmax = 0)""",
            sid=sid, p=p, rp=p, n=fn, e=ext, sz=st.st_size,
            mt=datetime.fromtimestamp(st.st_mtime, timezone.utc),
            st=status, er=(None if ok else ("扩展名跳过" if ext in skip else "超出大小上限")))
    return {"id": r[0][0] if r else None, "is_new": bool(r and r[0][1]),
            "ok": ok, "ext": ext, "st": st}


def _path_source_id(p, roots):
    """路径归属哪个源：最长前缀匹配，避免嵌套源互相抢文件。"""
    hits = [(len(r), sid) for sid, r in roots if p == r or p.startswith(r + os.sep)]
    return max(hits)[1] if hits else None


def scan_targets(paths):
    """增量扫描：只处理事件队列给的路径。

    与 `scan()` 的分工：这里**不 walk、不做 missing 全量判定**——
    删除判定需要「本轮见过的完整集合」，只有全量 walk 才做得到。
    事件负责及时性，全量负责一致性。
    """
    s = settings()
    skip = set(s["skip_exts"])
    max_bytes = int(s["max_file_mb"]) * 1024 * 1024
    with db.cursor(commit=False) as con:
        srcs = con.run("SELECT id, root_path FROM sources WHERE enabled")
    roots = [(sid, os.path.abspath(root)) for sid, root in srcs]
    r_dirs, r_files, r_exts, _ = _load_exclude_rules()
    _skip_prefix = [os.path.abspath(s.get("unpack_dir") or ""),
                    os.path.abspath(s.get("md_dir") or "")]
    seen = added = gone = skipped = 0
    for p in paths:
        if any(pp and p.startswith(pp) for pp in _skip_prefix):
            continue
        sid = _path_source_id(p, roots)
        if not sid:
            continue
        _hit = _rule_match(p, p, r_dirs, r_files, r_exts)
        if _hit:
            _mark_excluded(sid, p, _hit[1])
            skipped += 1
            continue
        if not os.path.exists(p):
            with db.cursor() as con:
                r = con.run("""UPDATE files SET status='missing',
                                 error='磁盘上已不存在（事件队列）', updated_at=now()
                               WHERE path=:p AND status<>'missing' RETURNING id""", p=p)
            if r:
                gone += 1
                with db.cursor() as con:
                    con.run("UPDATE chunks SET is_current=false WHERE file_id=:i AND is_current", i=r[0][0])
                    con.run("""UPDATE file_index_policies SET policy='never', reason_code='source_missing',
                                 reason_kind='policy', updated_at=now() WHERE file_id=:i""", i=r[0][0])
            continue
        info = _upsert_one(sid, p, skip, max_bytes)
        if not info:
            skipped += 1
            continue
        if info["ok"]:
            seen += 1
            added += 1 if info["is_new"] else 0
        else:
            skipped += 1
            _policy_sync(info["id"])      # 被跳过的也要有「为什么」，不能只在 index 里补
    return {"seen": seen, "added": added, "gone": gone, "skipped": skipped}


def drain_change_queue(limit=500):
    """认领一批未处理事件（按代际顺序），返回去重后的路径。"""
    with db.cursor() as con:
        rows = con.run("""
            UPDATE file_change_queue SET claimed_at=now()
            WHERE id IN (SELECT id FROM file_change_queue
                         WHERE processed_at IS NULL AND claimed_at IS NULL
                         ORDER BY generation, id LIMIT :l)
            RETURNING path""", l=limit)
    return sorted({r[0] for r in rows})


def finish_change_queue(paths):
    if not paths:
        return 0
    with db.cursor() as con:
        con.run("""UPDATE file_change_queue SET processed_at=now()
                   WHERE processed_at IS NULL AND path = ANY(:ps)""", ps=list(paths))
    return len(paths)


def _policy_sync(fid):
    """把 files 的最终状态投影到 file_index_policies（流水线每处理完一个文件调一次）。

    没有这一步，策略表只在回填时准过一次：新入库的文件永远没有记录（unclassified 涨），
    新失败的文件也进不了重试机制。
    """
    with db.cursor(commit=False) as con:
        row = con.run("""SELECT status, coalesce(parse_kind,''), coalesce(error,''),
                                coalesce(ext,''), coalesce(size,0)
                         FROM files WHERE id=:i""", i=fid)
    if not row:
        return
    status, kind, err, ext, size = row[0]

    def _put(policy, code, rkind, detail=None):
        with db.cursor() as con:
            con.run("""INSERT INTO file_index_policies
                         (file_id, policy, reason_code, reason_kind, reason_detail, attempts,
                          max_attempts, next_retry_at, updated_at)
                       VALUES (:i,:p,:c,:k,:d,0,4,NULL,now())
                       ON CONFLICT (file_id) DO UPDATE SET
                         policy=:p, reason_code=:c, reason_kind=:k, reason_detail=:d,
                         attempts=0, next_retry_at=NULL, updated_at=now()""",
                    i=fid, p=policy, c=code, k=rkind, d=detail)

    if status == "parsed":
        _put("indexed", "indexed_ok", "content", kind)
    elif status == "duplicate":
        _put("never", "duplicate_content", "content", err)
    elif status == "missing":
        _put("never", "source_missing", "policy", err or "磁盘上已不存在")
    else:
        # skipped / failed 交给重试机制分类（poison 一次判死，transient 排退避）
        import retry_policy
        detail = err or ("扩展名跳过" if status == "skipped" else "解析失败")
        if size and size > 50 * 1024 * 1024 and ext in (".xls", ".doc", ".ppt"):
            _put("never", "poison_oversize", "poison", f"体积异常 {size} 字节")
        else:
            retry_policy.schedule(fid, detail, status, ext)


def _mark_excluded(sid, p, rule_value):
    """把「按规则排除」的文件记一行：可解释、可恢复（删规则后再扫就回来）。"""
    fn = os.path.basename(p)
    ext = os.path.splitext(fn)[1].lower()
    try:
        st = os.stat(p)
        size, mt = st.st_size, datetime.fromtimestamp(st.st_mtime, timezone.utc)
    except OSError:
        return
    note = f"按排除规则跳过：{rule_value}"
    with db.cursor() as con:
        r = con.run(
            """INSERT INTO files(source_id,path,real_path,name,ext,size,mtime,status,error)
               VALUES (:sid,:p,:rp,:n,:e,:sz,:mt,'skipped',:er)
               ON CONFLICT (path) DO UPDATE SET status='skipped', error=:er, updated_at=now()
               RETURNING id""",
            sid=sid, p=p, rp=p, n=fn, e=ext, sz=size, mt=mt, er=note)
    if r:
        _policy_sync_fast(r[0][0], "never", "excluded_path", "policy", note)


def _policy_sync_fast(fid, policy, code, rkind, detail):
    with db.cursor() as con:
        con.run("""INSERT INTO file_index_policies
                     (file_id, policy, reason_code, reason_kind, reason_detail, attempts,
                      max_attempts, next_retry_at, updated_at)
                   VALUES (:i,:p,:c,:k,:d,0,4,NULL,now())
                   ON CONFLICT (file_id) DO UPDATE SET
                     policy=:p, reason_code=:c, reason_kind=:k, reason_detail=:d,
                     attempts=0, next_retry_at=NULL, updated_at=now()""",
                i=fid, p=policy, c=code, k=rkind, d=detail)


def _load_exclude_rules():
    """读用户排除规则 → (目录列表, 文件集合, 扩展名集合, id 映射)。"""
    try:
        with db.cursor(commit=False) as con:
            rows = con.run("SELECT id, kind, value FROM exclude_rules WHERE enabled")
    except Exception:
        return [], set(), set(), {}
    dirs, files, exts, by_value = [], set(), set(), {}
    for rid, kind, value in rows:
        if kind == "dir":
            dirs.append(os.path.abspath(value))
        elif kind == "file":
            files.add(os.path.abspath(value))
        elif kind == "ext":
            v = value if value.startswith(".") else "." + value
            exts.add(v.lower())
        by_value[value] = rid
    return dirs, files, exts, by_value


def _bump_hits(counter):
    """把本轮各规则命中数累加进 exclude_rules（一次一条 UPDATE，量很小）。"""
    if not counter:
        return
    with db.cursor() as con:
        for rid, n in counter.items():
            con.run("""UPDATE exclude_rules SET hits = hits + :n, last_hit_at = now()
                       WHERE id = :i""", n=n, i=rid)


def _rule_match(path, base, dirs, files, exts):
    """返回命中的规则 id（无命中返回 None）。dir 匹配整枝，file 精确，ext 按后缀。"""
    ap = os.path.abspath(path)
    if ap in files:
        return ("file", ap)
    if os.path.splitext(ap)[1].lower() in exts:
        return ("ext", os.path.splitext(ap)[1].lower())
    for d in dirs:
        if ap == d or ap.startswith(d + os.sep):
            return ("dir", d)
    return None


def scan(limit=0):
    s = settings()
    r_dirs, r_files, r_exts, r_by_value = _load_exclude_rules()
    rule_hits = {}
    skip = set(s["skip_exts"])
    max_bytes = int(s["max_file_mb"]) * 1024 * 1024
    with db.cursor(commit=False) as con:
        srcs = con.run("SELECT id, name, root_path, exclude FROM sources WHERE enabled")
    added = 0
    seen = 0        # 磁盘上扫到的文件总数（分母）
    indexable = 0   # 其中按规则应当入库的
    _skip_prefix = [os.path.abspath(s.get("unpack_dir") or ""),
                    os.path.abspath(s.get("md_dir") or "")]
    for sid, name, root, exclude in srcs:
        if not os.path.isdir(root):
            print(f"  ! 跳过（不存在）: {root}")
            continue
        new_rows = []          # 本轮新增的 (id, path, name, size, mtime, ext)
        ex = [e for e in (exclude or []) if e]
        for dirpath, dirnames, filenames in os.walk(root):
            _keep = []
            for _d in dirnames:
                _cand = os.path.join(dirpath, _d)
                if any(x in _cand for x in ex) or _d.startswith(".git"):
                    continue
                _hit = _rule_match(_cand, root, r_dirs, r_files, r_exts)
                if _hit:
                    _rid = r_by_value.get(_hit[1])
                    if _rid:
                        rule_hits[_rid] = rule_hits.get(_rid, 0) + 1
                    continue
                _keep.append(_d)
            dirnames[:] = _keep
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if any(pp and p.startswith(pp) for pp in _skip_prefix):
                    continue
                # Office 临时锁文件（~$xxx.docx）和隐藏文件一律不入库
                if fn.startswith("~$") or fn.startswith(".~lock") or fn.startswith("."):
                    continue
                _hit = _rule_match(p, root, r_dirs, r_files, r_exts)
                if _hit:
                    _rid = r_by_value.get(_hit[1])
                    if _rid:
                        rule_hits[_rid] = rule_hits.get(_rid, 0) + 1
                    _mark_excluded(sid, p, _hit[1])
                    continue
                info = _upsert_one(sid, p, skip, max_bytes)
                if not info:
                    continue
                seen += 1
                if info["ok"]:
                    indexable += 1
                if info["is_new"]:
                    added += 1
                    new_rows.append((info["id"], p, fn, info["st"].st_size,
                                     datetime.fromtimestamp(info["st"].st_mtime, timezone.utc),
                                     info["ext"]))
                if limit and added >= limit:
                    print(f"  达到上限 {limit}，停止扫描")
                    _reconcile_moves(sid, root, new_rows, s)
                    _save_scan_stats(seen, indexable, added)
                    return added
        _reconcile_moves(sid, root, new_rows, s)
    _bump_hits(rule_hits)
    print(f"  扫描到 {seen} 个文件（可入库 {indexable}），新增记录: {added}")
    _save_scan_stats(seen, indexable, added)
    return added


def _save_scan_stats(seen, indexable, added):
    """把磁盘总数写进 settings，供前端算 已入库/总数 的分母。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with db.cursor() as con:
        for k, v in (("disk_files_seen", seen), ("disk_files_indexable", indexable),
                     ("scan_at", now)):
            con.run("INSERT INTO settings(key,value) VALUES (:k,:v::jsonb) "
                    "ON CONFLICT (key) DO UPDATE SET value=:v::jsonb, updated_at=now()",
                    k=k, v=json.dumps(v, ensure_ascii=False))


# ---------------- 嵌入 ----------------

def meta_prefix(name, ext, mt, source_name="", section_path=""):
    """嵌入用的元数据前缀：把「文件身份」送进向量。

    为什么必须有：块向量只含正文，文件名/类型/年份/来源全都不在里面。
    实测按文件名检索 Recall@10 = 0.150，加前缀后 0.675 —— 不加前缀等于
    用户记得文件名也找不到这份文件。

    口径与 reembed.py 共用本函数，不要在别处另写一份。
    """
    year = ""
    try:
        year = str(mt.astimezone(timezone.utc).year) if mt else ""
    except Exception:                                            # noqa: BLE001
        year = ""
    kind = (ext or "").lstrip(".").upper() or "文档"
    bits = [f"《{name}》", kind]
    if year:
        bits.append(year)
    if source_name:
        bits.append(source_name)
    if section_path:
        bits.append(section_path)
    return " · ".join(b for b in bits if b) + "\n"


def _file_source_name(fid):
    try:
        with db.cursor(commit=False) as con:
            r = con.run("""SELECT coalesce(src.name,'') FROM files f
                           LEFT JOIN sources src ON src.id = f.source_id WHERE f.id=:i""", i=fid)
        return r[0][0] if r else ""
    except Exception:                                            # noqa: BLE001
        return ""


def embed_texts(texts, s):
    import urllib.request
    url, model = db.require_endpoint(s, "embed_url"), s["embed_model"]
    dim_expect = int(s["embed_dim"])
    batch = max(1, int(s["embed_batch"]))
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        req = urllib.request.Request(
            url, data=json.dumps({"model": model, "input": part}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read())
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        for d in items:
            vec = d["embedding"]
            if len(vec) != dim_expect:
                raise RuntimeError(
                    f"嵌入维度不匹配：接口返回 {len(vec)} 维，设置里是 {dim_expect} 维。"
                    f"请改设置或重建索引（chunks.embedding 固定 vector({dim_expect})）")
            out.append(vec)
    return out


# ---------------- 切块 ----------------

_HEAD_PATTERNS = [
    (r"^第[一二三四五六七八九十百零\d]+章", 1),
    (r"^第[一二三四五六七八九十百零\d]+节", 2),
    (r"^[一二三四五六七八九十]+、", 2),
    (r"^（[一二三四五六七八九十]+）", 3),
    (r"^\([一二三四五六七八九十]+\)", 3),
    (r"^\d+\.\d+(?=\s|\u4e00-\u9fff)", 3),
    (r"^\d+[、.]\s*\S", 3),
]


def _heading_level(block):
    """判断一段是否是标题，返回 (层级, 标题文本) 或 (0, None)。"""
    import re
    first = block.split("\n")[0].strip()
    if first.startswith("#"):
        lvl = len(first) - len(first.lstrip("#"))
        return lvl, first.lstrip("# ").strip()
    if len(first) > 60:          # 太长的不是标题
        return 0, None
    for pat, lvl in _HEAD_PATTERNS:
        if re.match(pat, first):
            return lvl, first
    return 0, None


def chunk_document(text, size=900, overlap=120):
    """切块并带章节路径（第三章 运营 > 第二节 绿化）。"""
    import re
    blocks = [b.strip() for b in re.split(r"\n{2,}", text) if b.strip()]
    chunks, cur, heading = [], "", ""
    stack = {}          # level -> 标题

    def section_path():
        if not stack:
            return heading or None
        return " > ".join(stack[k] for k in sorted(stack))

    for b in blocks:
        lvl, title = _heading_level(b)
        if title:
            for k in [k for k in stack if k >= lvl]:
                stack.pop(k, None)
            stack[lvl] = title
            heading = title
        if len(cur) + len(b) + 1 <= size:
            cur = (cur + "\n" + b).strip()
        else:
            if cur:
                chunks.append((heading, cur, section_path()))
            while len(b) > size:
                cut = b.rfind("。", 0, size)
                cut = cut + 1 if cut > size // 2 else size
                chunks.append((heading, b[:cut], section_path()))
                b = b[max(0, cut - overlap):]
            cur = b
    if cur:
        chunks.append((heading, cur, section_path()))
    return chunks


# ---------------- 解包（递归）----------------

def unpack_file(fid, logical_path, real_path, depth, s):
    if depth >= int(s["unpack_max_depth"]):
        return 0
    dest = os.path.join(s["unpack_dir"], str(fid))
    if os.path.isdir(dest) and os.listdir(dest):
        pass  # 复用已有解包结果
    else:
        res = archives.unpack(real_path, dest, int(s["unpack_max_bytes"]))
        with db.cursor() as con:
            con.run("""INSERT INTO archives(file_id,fmt,unpacked_dir,entries,bytes,depth,status,message)
                       VALUES (:f,:fmt,:d,:e,:b,:dp,:st,:m)""",
                    f=fid, fmt=res["fmt"], d=dest, e=res["entries"], b=res["bytes"],
                    dp=depth, st=("ok" if res["ok"] else "failed"), m=res["message"][:400])
        if not res["ok"]:
            return 0
    n = 0
    for rel in archives.walk_unpacked(dest):
        child_real = os.path.join(dest, rel)
        child_logical = f"{logical_path}#{rel}"
        try:
            st = os.stat(child_real)
        except OSError:
            continue
        with db.cursor() as con:
            con.run("""INSERT INTO files(source_id,path,real_path,name,ext,size,mtime,container_id,depth,status)
                       VALUES ((SELECT source_id FROM files WHERE id=:fid),:p,:rp,:n,:e,:sz,:mt,:cid,:dp,'pending')
                       ON CONFLICT (path) DO NOTHING""",
                    fid=fid, p=child_logical, rp=child_real,
                    n=os.path.basename(rel), e=os.path.splitext(rel)[1].lower(),
                    sz=st.st_size, mt=datetime.fromtimestamp(st.st_mtime, timezone.utc),
                    cid=fid, dp=depth + 1)
        n += 1
    return n


# ---------------- 并发互斥 ----------------

_LOCK_H = None


def acquire_lock(tag="ingest"):
    """拿文件锁，防止 autoscan 与手工命令同时跑导致重复插入。

    注意：必须可重入 —— autoscan 先 index() 再 ocr_batch()，
    同一进程第二次 flock 另一个 fd 会互相冲突（flock 按 fd 计），
    曾导致 OCR 永远拿不到锁、队列不动。
    """
    global _LOCK_H
    if _LOCK_H is not None:
        return True
    import fcntl
    s = settings()
    base = s.get("unpack_dir") or "/tmp"
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, ".wenmai.lock")
    fh = open(path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        print(f"  已有任务在跑（{tag} 拿不到锁），本次跳过")
        return False
    _LOCK_H = fh
    return True


# ---------------- 识别文本（md）留存 ----------------

def md_path_for(fid, real_path, s):
    """md 存到独立目录，镜像原目录结构：<md_dir>/<源名>/<相对路径>.md"""
    md_dir = s.get("md_dir") or "/tank/files/wenmai-md"
    with db.cursor(commit=False) as con:
        row = con.run("""SELECT coalesce(s.name,'others'), s.root_path, f.path, f.container_id
                         FROM files f LEFT JOIN sources s ON s.id=f.source_id
                         WHERE f.id=:i""", i=fid)
    if not row:
        return None
    src_name, root, logical, cid = row[0]
    if cid:      # 压缩包内的文件：单独归档目录，用 id 前缀防重名
        return os.path.join(md_dir, "_archives", src_name,
                            f"{fid}__{os.path.basename(logical)}.md")
    rel = logical
    if root and logical.startswith(root):
        rel = os.path.relpath(logical, root)
    safe = rel.replace("..", "_")
    safe = os.path.splitext(safe)[0] + ".md"
    return os.path.join(md_dir, src_name, safe)


def write_md(fid, text, s, meta_note=""):
    """把识别出的原文写成 md（幂等覆盖）。"""
    if not s.get("md_enabled", True) or not text:
        return None
    with db.cursor(commit=False) as con:
        rp = con.run("SELECT real_path FROM files WHERE id=:i", i=fid)
    out = md_path_for(fid, rp[0][0] if rp else None, s)
    if not out:
        return None
    try:
        os.makedirs(os.path.dirname(out), exist_ok=True)
        tmp = out + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            if meta_note:
                f.write(f"<!-- {meta_note} -->\n")
            f.write(text)
        os.replace(tmp, out)
        with db.cursor() as con:
            con.run("""CREATE TABLE IF NOT EXISTS md_files (
                         file_id bigint PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                         path text NOT NULL, bytes bigint, updated_at timestamptz DEFAULT now())""")
            con.run("""INSERT INTO md_files(file_id,path,bytes,updated_at) VALUES (:f,:p,:b,now())
                       ON CONFLICT (file_id) DO UPDATE SET path=:p, bytes=:b, updated_at=now()""",
                    f=fid, p=out, b=len(text.encode()))
        return out
    except PermissionError:
        # 目录属主不对（历史上用 root 跑过）；不影响入库，只丢 md
        print(f"  ! md 目录不可写，跳过落盘（cat /tank/files/wenmai-md 查属主）: {out}")
        return None
    except Exception as e:
        print(f"  ! 写 md 失败 {out}: {e}")
        return None


def _ensure_md_table():
    with db.cursor() as con:
        con.run("""CREATE TABLE IF NOT EXISTS md_files (
                     file_id bigint PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
                     path text NOT NULL, bytes bigint,
                     updated_at timestamptz DEFAULT now())""")


def md_export(limit=0):
    """为已解析但还没 md 的文件，用现有 chunks 拼出 md（回溯补齐）。"""
    s = settings()
    _ensure_md_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT f.id FROM files f
                          LEFT JOIN md_files m ON m.file_id=f.id
                          WHERE f.status='parsed' AND m.file_id IS NULL
                          LIMIT :l""", l=(limit or 10**9))
    n = 0
    for (fid,) in rows:
        with db.cursor(commit=False) as con:
            parts = con.run("""SELECT heading, content FROM chunks
                               WHERE file_id=:i AND is_current ORDER BY seq""", i=fid)
        if not parts:
            continue
        txt = "\n\n".join((f"## {h}\n{c}" if h else c) for h, c in parts)
        if write_md(fid, txt, s, meta_note=f"由已有块回溯生成 file_id={fid}"):
            n += 1
    print(f"  已导出 md: {n} 个文件")
    return n


# ---------------- 清理 ----------------

def clean(dry_run=False):
    """清临时文件：过期解包目录、孤儿 md、下载/转换残留。"""
    import shutil
    s = settings()
    _ensure_md_table()
    unpack_days = int(s.get("unpack_keep_days") or 0)
    md_days = int(s.get("md_keep_days") or 0)
    now = time.time()
    removed_dirs = freed = 0

    # 1) 过期解包目录（可按需从原压缩包重建）
    updir = s.get("unpack_dir") or ""
    if unpack_days and os.path.isdir(updir):
        for name in os.listdir(updir):
            p = os.path.join(updir, name)
            if not os.path.isdir(p):
                continue
            try:
                age_days = (now - os.path.getmtime(p)) / 86400
            except OSError:
                continue
            if age_days >= unpack_days:
                size = sum(os.path.getsize(os.path.join(r, f))
                           for r, _, fs in os.walk(p) for f in fs
                           if os.path.exists(os.path.join(r, f)))
                if not dry_run:
                    shutil.rmtree(p, ignore_errors=True)
                    with db.cursor() as con:
                        con.run("DELETE FROM archives WHERE unpacked_dir=:d", d=p)
                removed_dirs += 1
                freed += size
                print(f"  清理解包目录 {name}（{age_days:.0f} 天，{size/1048576:.1f} MB）")

    # 2) 孤儿 md（对应文件记录已不存在）
    with db.cursor(commit=False) as con:
        orph = con.run("""SELECT m.file_id, m.path FROM md_files m
                          LEFT JOIN files f ON f.id=m.file_id WHERE f.id IS NULL""")
    for fid, path in orph:
        if not dry_run:
            try:
                os.remove(path)
            except OSError:
                pass
            with db.cursor() as con:
                con.run("DELETE FROM md_files WHERE file_id=:f", f=fid)
        removed_dirs += 1
    if orph:
        print(f"  清理孤儿 md: {len(orph)} 个")

    # 3) 过期 md（可选，默认永久）
    if md_days:
        cutoff = now - md_days * 86400
        with db.cursor(commit=False) as con:
            rows = con.run("SELECT file_id, path FROM md_files")
        for fid, path in rows:
            try:
                if os.path.getmtime(path) < cutoff:
                    if not dry_run:
                        os.remove(path)
                        with db.cursor() as con:
                            con.run("DELETE FROM md_files WHERE file_id=:f", f=fid)
                    removed_dirs += 1
            except OSError:
                pass

    # 4) /tmp 里本系统的残留包
    import glob
    for pat in ("/tmp/wenmai_*.tgz", "/tmp/mcp_test*.py"):
        for p in glob.glob(pat):
            try:
                freed += os.path.getsize(p)
                if not dry_run:
                    os.remove(p)
            except OSError:
                pass

    print(f"  清理完成：{removed_dirs} 项，释放 {freed/1048576:.1f} MB"
          + ("（演练模式，未实际删除）" if dry_run else ""))
    return {"removed": removed_dirs, "freed_mb": round(freed / 1048576, 1)}


# ---------------- 主流程 ----------------

def index(limit=0, do_embed=True):
    if not acquire_lock("index"):
        return 0
    s = settings()
    skip = set(s["skip_exts"])
    max_bytes = int(s["max_file_mb"]) * 1024 * 1024
    # 排队优先级：新增/最近变更的优先，历史积压排后面
    with db.cursor(commit=False) as con:
        rows = con.run(
            """SELECT id,path,real_path,name,ext,size,container_id,depth,mtime,length(path)
               FROM files WHERE status='pending'
               ORDER BY coalesce(changed_at, created_at) DESC, depth, id
               LIMIT :lim""",
            lim=(limit or 10**9))
    done = unpacked = failed = dup = 0
    processed_ids = []
    for fid, lpath, rpath, name, ext, size, cid, depth, mt, plen in rows:
        processed_ids.append(fid)
        def _canon_rank(_mt, _plen, _id):
            """正本优先级：文件本身越旧越正、路径越短越正、入库越早越正。"""
            return (_mt.timestamp() if _mt else 0, _plen or len(lpath or ""), _id)
        # 归档 → 解包后继续递归（自身不解析）
        if archives.is_archive(name) or (ext in (".ofd",) and size and False):
            if archives.is_archive(name):
                n = unpack_file(fid, lpath, rpath, depth, s)
                unpacked += n
                with db.cursor() as con:
                    con.run("UPDATE files SET status=:st, parse_kind='archive', updated_at=now() WHERE id=:i",
                            st=("parsed" if n >= 0 else "failed"), i=fid)
                continue

        if ext in skip or (size or 0) > max_bytes or not rpath or not os.path.exists(rpath):
            with db.cursor() as con:
                con.run("UPDATE files SET status='skipped', error=:e, updated_at=now() WHERE id=:i",
                        e=("扩展名跳过" if ext in skip else "超出大小或文件缺失"), i=fid)
            continue

        # ---- 内容哈希 + 去重（同内容只解析一份，正本=最早入库的那份）----
        chash = None
        try:
            chash = sha256_file(rpath)
        except Exception as e:
            print(f"  ! 哈希失败 {name}: {e}")

        if chash and s.get("dedupe_enabled", True) and (size or 0) >= int(s.get("dedupe_min_bytes", 1024)):
            with db.cursor(commit=False) as con:
                peers = con.run(
                    """SELECT id, mtime, length(path) FROM files
                       WHERE content_hash=:h AND id<>:i AND dup_of IS NULL
                         AND status IN ('parsed','pdf-ocr') AND coalesce(text_bytes,0)>0""",
                    h=chash, i=fid)
            # 正本 = 最旧的那一份（不看处理顺序，避免"后来者当正本"）
            peers.sort(key=lambda r: _canon_rank(r[1], r[2], r[0]))
            if peers:
                pid, pmt, pplen = peers[0]
                if _canon_rank(mt, plen, fid) < _canon_rank(pmt, pplen, pid):
                    # 当前文件更旧 → 它才是正本；把已解析的副本降级并重新解析当前文件
                    with db.cursor() as con:
                        con.run("UPDATE chunks SET is_current=false WHERE file_id=:i AND is_current", i=pid)
                        con.run("UPDATE files SET dup_of=:n WHERE dup_of=:o", n=fid, o=pid)
                        con.run("""UPDATE files SET status='duplicate', dup_of=:n,
                                   error=:e, updated_at=now() WHERE id=:o""",
                                n=fid, e=f"内容重复，正本 id={fid}", o=pid)
                    print(f"  ↑ 修正正本: {name}（更旧）取代 id={pid}")
                else:
                    with db.cursor() as con:
                        # 副本必须退出检索：清掉它自己的当前块（可能是上一轮解析留下的）
                        con.run("UPDATE chunks SET is_current=false WHERE file_id=:i AND is_current", i=fid)
                        con.run("""UPDATE files SET content_hash=:h, status='duplicate', dup_of=:d,
                                   error=:e, updated_at=now() WHERE id=:i""",
                                h=chash, d=pid, e=f"内容重复，正本 id={pid}", i=fid)
                    dup += 1
                    print(f"  = 内容重复，跳过解析: {name} → 正本 {pid}")
                    continue

        if chash:
            with db.cursor() as con:
                con.run("""UPDATE files SET content_hash=:h, updated_at=now() WHERE id=:i""",
                        h=chash, i=fid)

        try:
            text, kind, note = extract.extract(rpath)
        except Exception as e:
            text, kind, note = "", "failed", str(e)

        if kind in ("pdf-scan",) or (kind == "failed" and ext == ".pdf"):
            with db.cursor() as con:
                con.run("UPDATE files SET status='failed', parse_kind=:k, error=:e, updated_at=now() WHERE id=:i",
                        k=kind, e=(note or "需要 OCR")[:300], i=fid)
                # 进 OCR 队列（幂等）；已 done 的不再入队
                con.run("""INSERT INTO ocr_queue(file_id,status) VALUES (:i,'pending')
                           ON CONFLICT (file_id) DO UPDATE SET status='pending'
                           WHERE ocr_queue.status <> 'done'""", i=fid)
            failed += 1
            continue

        if not text.strip():
            # 解析器报错 → failed（可重试/可排查）；确实无文本 → skipped
            _st = "failed" if kind == "failed" else "skipped"
            with db.cursor() as con:
                con.run("UPDATE files SET status=:st, parse_kind=:k, error=:e, updated_at=now() WHERE id=:i",
                        st=_st, k=kind, e=(note or "无文本")[:300], i=fid)
            continue

        chunks = chunk_document(text, int(s["chunk_size"]), int(s["chunk_overlap"]))
        # 口径：**嵌入正文，不加前缀**。文件名交给检索层的名字通道（_name_channel）。
        # 实测加前缀会让同文档各块向量趋同，段落检索 MRR 0.380→0.306，得不偿失。
        emb_texts = [c[1] for c in chunks]
        vecs = []
        if do_embed and chunks:
            try:
                vecs = embed_texts(emb_texts, s)
            except Exception as e:
                with db.cursor() as con:
                    con.run("UPDATE files SET status='failed', error=:e, updated_at=now() WHERE id=:i",
                            e=f"嵌入失败: {e}"[:300], i=fid)
                failed += 1
                print(f"  ! 嵌入失败 {name}: {e}")
                continue

        with db.cursor() as con:
            # 版本化：旧版块保留（is_current=false），新版 version+1，检索只用当前版
            retired = con.run("""UPDATE chunks SET is_current=false
                                 WHERE file_id=:i AND is_current RETURNING id""", i=fid)
            vrow = con.run("SELECT coalesce(max(version),0) FROM chunks WHERE file_id=:i", i=fid)
            ver = int(vrow[0][0]) + 1
            new_ids = []
            for i2, (h, c, sp) in enumerate(chunks):
                row = con.run("""INSERT INTO chunks(file_id,seq,heading,content,token_count,embedding,
                                             version,is_current,section_path,embed_text,embed_ver)
                           VALUES (:f,:s,:h,:c,:t,:e::vector,:v,true,:sp,:et,'content')
                           RETURNING id""",
                        f=fid, s=i2, h=h, c=c, t=len(c) // 2,
                        e=(db.vec_literal(vecs[i2]) if vecs else None), v=ver, sp=sp,
                        et=(emb_texts[i2][:4000] if emb_texts else None))
                if vecs:
                    new_ids.append((row[0][0], i2, h, c, sp, ver, fid, name, rpath, ext))
            con.run("""UPDATE files SET status='parsed', parse_kind=:k, text_bytes=:tb,
                       error=:e, version=:v, last_indexed_at=now(), updated_at=now() WHERE id=:i""",
                    k=kind, tb=len(text.encode()), e=(note or "")[:300], v=ver, i=fid)
        # 章节结构（纯 DB，毫秒级；失败不影响入库）
        try:
            import sections as _sec
            _sec.build(fid)
        except Exception as _e:                      # noqa: BLE001
            print(f"  ! 章节结构构建失败 {fid}: {_e}")
            con.run("""INSERT INTO file_versions(file_id,version,content_hash,size,mtime,text_bytes,chunk_count)
                       VALUES (:f,:v,:h,:sz,:mt,:tb,:cc)
                       ON CONFLICT (file_id,version) DO UPDATE SET indexed_at=now()""",
                    f=fid, v=ver, h=chash, sz=size, mt=None, tb=len(text.encode()), cc=len(chunks))
        # Qdrant 双写：先删退役点，再写当前版（失败不影响入库）
        if vecs:
            qs.delete_ids([r[0] for r in retired], s)
            pts = [{"id": int(r[0]),
                    "vector": vecs[r[1]],
                    "payload": {"file_id": int(r[6]), "seq": int(r[1]), "heading": r[2] or "",
                                "section_path": r[4] or "", "version": int(r[5]),
                                "name": r[7] or "", "path": r[8] or "", "ext": r[9] or "",
                                "is_current": True}}
                   for r in new_ids]
            qs.upsert(pts, s)
        write_md(fid, text, s, meta_note=f"{kind} {note}".strip())
        done += 1

    # 策略表必须随流水线走：否则新入库/新失败的文件的「为什么」永远没有记录
    for _fid in processed_ids:
        try:
            _policy_sync(_fid)
        except Exception as _e:
            print(f"  ! 策略同步失败 {_fid}: {_e}")
    print(f"  处理完成: {done} 个文件 | 解包新增: {unpacked} | 失败: {failed} | 内容重复跳过: {dup}")


def _ocr_ingest(fid, text, s, note="mineru OCR"):
    """把 OCR 文本切块 + 嵌入 + 版本化入库 + 写 md。返回块数。"""
    ch = chunk_document(text, int(s["chunk_size"]), int(s["chunk_overlap"]))
    # 先取文件身份再嵌入：OCR 路径原先顺序反了，导致这里永远加不上前缀
    frow0 = None
    with db.cursor(commit=False) as con:
        frow0 = con.run("SELECT name, real_path, ext, mtime FROM files WHERE id=:i", i=fid)
    fname, fpath, fext, f_mt = (frow0[0] if frow0 else ("", "", "", None))
    emb_texts = [c[1] for c in ch]        # 同口径：只嵌正文
    vecs = embed_texts(emb_texts, s)
    with db.cursor() as con:
        retired = con.run("""UPDATE chunks SET is_current=false
                             WHERE file_id=:i AND is_current RETURNING id""", i=fid)
        vrow = con.run("SELECT coalesce(max(version),0) FROM chunks WHERE file_id=:i", i=fid)
        ver = int(vrow[0][0]) + 1
        new_ids = []
        frow = con.run("SELECT name, real_path, ext FROM files WHERE id=:i", i=fid)
        fname, fpath, fext = frow[0] if frow else ("", "", "")
        for i2, (hd, c, sp) in enumerate(ch):
            row = con.run("""INSERT INTO chunks(file_id,seq,heading,content,token_count,embedding,
                                         version,is_current,section_path,embed_text,embed_ver)
                       VALUES (:f,:s,:h,:c,:t,:e::vector,:v,true,:sp,:et,'content')
                       RETURNING id""",
                    f=fid, s=i2, h=hd, c=c, t=len(c) // 2,
                    e=db.vec_literal(vecs[i2]), v=ver, sp=sp, et=emb_texts[i2][:4000])
            new_ids.append((row[0][0], i2, hd, c, sp, ver, fid, fname, fpath, fext))
        con.run("""UPDATE files SET status='parsed', parse_kind='pdf-ocr', text_bytes=:tb,
                   error=:en, version=:v, last_indexed_at=now(), updated_at=now()
                   WHERE id=:i""", tb=len(text.encode()), en=note[:200], v=ver, i=fid)
    try:
        import sections as _sec
        _sec.build(fid)
    except Exception as _e:                          # noqa: BLE001
        print(f"  ! 章节结构构建失败 {fid}: {_e}")
    qs.delete_ids([r[0] for r in retired], s)
    if vecs:
        qs.upsert([{"id": int(r[0]), "vector": vecs[r[1]],
                    "payload": {"file_id": int(r[6]), "seq": int(r[1]), "heading": r[2] or "",
                                "section_path": r[4] or "", "version": int(r[5]),
                                "name": r[7] or "", "path": r[8] or "", "ext": r[9] or "",
                                "is_current": True}}
                   for r in new_ids], s)
    write_md(fid, text, s, meta_note=note)
    return len(ch)


def ocr_batch(limit=2):
    """从 OCR 队列取任务：先查同内容缓存（命中则跳过 mineru），再调用 mineru。

    队列顺序：新入队优先（用户要求新增文件先处理）。
    """
    if not acquire_lock("ocr"):
        return 0
    s = settings()
    if not s.get("mineru_enabled", True):
        print("  MinerU 未启用，跳过 OCR")
        return 0
    import json as _json
    # 自愈：上一轮被 kill 的任务会永久卡在 running，启动时全部退回 pending
    with db.cursor() as con:
        n = con.run("UPDATE ocr_queue SET status='pending' WHERE status='running' RETURNING file_id")
    if n:
        print(f"  重置 {len(n)} 条上次被中断的 OCR 任务")
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT q.file_id, f.real_path, f.name, f.content_hash
                          FROM ocr_queue q JOIN files f ON f.id=q.file_id
                          WHERE q.status IN ('pending','failed') AND q.attempts < 3
                          ORDER BY q.enqueued_at DESC LIMIT :l""",
                        l=(limit if limit and limit > 0 else 10**9))
    if not rows:
        print("  OCR 队列为空")
        return 0
    done = 0
    for fid, rpath, name, chash in rows:
        with db.cursor() as con:
            con.run("UPDATE ocr_queue SET status='running' WHERE file_id=:i", i=fid)

        # ---- 缓存命中：同内容已经 OCR 过 → 直接复用 md ----
        cached_md = None
        if chash:
            with db.cursor(commit=False) as con:
                c = con.run("SELECT md_path FROM ocr_cache WHERE content_hash=:h", h=chash)
            if c and c[0][0] and os.path.exists(c[0][0]):
                try:
                    cached_md = open(c[0][0], encoding="utf-8").read()
                    print(f"  ♻ 缓存命中，跳过 OCR: {name[:34]}")
                except Exception:
                    cached_md = None

        try:
            if cached_md:
                n = _ocr_ingest(fid, cached_md, s, note="mineru OCR（缓存复用）")
                with db.cursor() as con:
                    con.run("""UPDATE ocr_queue SET status='done', finished_at=now(),
                               last_error=NULL WHERE file_id=:i""", i=fid)
                done += 1
                print(f"  OCR 完成（缓存）: {name[:34]} → {n} 块")
                continue

            import ocr_upgrade as _upt
            _tier = _upt.pick_tier(s, rpath)
            if _tier != (s.get("ocr_tier") or "standard"):
                print(f"    大文件自动降档: {_tier}")
            text, meta = mineru.parse_file(s["mineru_url"], rpath, file_name=name,
                                           ocr_mode="ocr", tier=_tier,
                                           poll_sec=int(s["mineru_poll_sec"]),
                                           timeout_sec=1800)
            if not text.strip():
                with db.cursor() as con:
                    con.run("UPDATE files SET error=:e, updated_at=now() WHERE id=:i",
                            e="OCR 无文本返回", i=fid)
                    con.run("""UPDATE ocr_queue SET status='failed', attempts=attempts+1,
                               last_error='OCR 无文本返回' WHERE file_id=:i""", i=fid)
                continue

            n = _ocr_ingest(fid, text, s)
            with db.cursor() as con:
                con.run("UPDATE files SET ocr_tier=:t WHERE id=:i", t=_tier, i=fid)
            try:
                import ocr_upgrade as _up
                _up.enqueue(fid, _tier, text, s, name=name)
            except Exception as _ue:               # noqa: BLE001
                print(f"    ! 入升级队列失败: {_ue}")
            md = md_path_for(fid, rpath, s)
            if chash and md and os.path.exists(md):
                with db.cursor() as con:
                    con.run("""INSERT INTO ocr_cache(content_hash,md_path,text_bytes)
                               VALUES (:h,:p,:b) ON CONFLICT (content_hash) DO UPDATE
                               SET md_path=:p, text_bytes=:b""",
                            h=chash, p=md, b=len(text.encode()))
            with db.cursor() as con:
                con.run("""UPDATE ocr_queue SET status='done', finished_at=now(),
                           last_error=NULL WHERE file_id=:i""", i=fid)
            done += 1
            print(f"  OCR 完成: {name[:34]} → {n} 块")
        except Exception as e:
            with db.cursor() as con:
                con.run("UPDATE files SET error=:e, updated_at=now() WHERE id=:i",
                        e=f"OCR失败: {e}"[:300], i=fid)
                con.run("""UPDATE ocr_queue SET status='failed', attempts=attempts+1,
                           last_error=:e WHERE file_id=:i""", e=str(e)[:300], i=fid)
            print(f"  ! OCR 失败 {name[:30]}: {e}")
    return done


def autoscan():
    """定时增量：扫描 → 新增/变更优先解析+嵌入 → 限量 OCR。

    间隔由 auto_scan_interval_min 控制；外部定时器可以更密（如 5 分钟），
    这里按“上次运行时间”决定是否真的执行。
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    s = settings()
    if not s.get("auto_scan_enabled"):
        print("  自动扫描已关闭")
        return
    last = s.get("auto_scan_last_run") or ""
    interval = int(s.get("auto_scan_interval_min") or 10)
    now = _dt.now(_tz.utc)
    if last:
        try:
            prev = _dt.fromisoformat(last)
            if (now - prev) < _td(minutes=interval - 1):
                print(f"  距上次运行不足 {interval} 分钟，跳过")
                return
        except Exception:
            pass

    print(f"===== 自动扫描 {now.astimezone().strftime('%Y-%m-%d %H:%M:%S')} =====")
    # ① 事件队列（秒级延迟）：inotify 抓到的变动路径，只处理这些，不 walk 全树
    inc = None
    if s.get("watch_queue_enabled", True):
        qpaths = drain_change_queue(500)
        if qpaths:
            inc = scan_targets(qpaths)
            finish_change_queue(qpaths)
            print(f"  增量事件 {len(qpaths)} 条：更新 {inc['seen']} / 新增 {inc['added']}"
                  f" / 删除 {inc['gone']} / 忽略 {inc['skipped']}")

    # ② 全量 walk 只按 full_scan_interval_min 跑 —— 它负责一致性（发现删除/移动），
    #    事件负责及时性；两者职责不同，不能只留一个
    full_every = int(s.get("full_scan_interval_min") or 30)
    last_full = s.get("full_scan_last_at") or ""
    do_full = True
    if last_full:
        try:
            do_full = (now - _dt.fromisoformat(last_full)) >= _td(minutes=full_every - 1)
        except Exception:
            do_full = True
    added = 0
    if do_full:
        added = scan(0)
        with db.cursor() as con:
            con.run("""INSERT INTO settings(key,value) VALUES ('full_scan_last_at',:v::jsonb)
                       ON CONFLICT (key) DO UPDATE SET value=:v::jsonb, updated_at=now()""",
                    v=json.dumps(now.isoformat(timespec="seconds")))
    else:
        print(f"  全量 walk 未到期（每 {full_every} 分钟一次），本轮只处理事件")
    # 注意：不能用 `x or 默认值` —— 0 本身就是合法值（不限），会被 or 吞掉
    def _limit(key, default):
        v = s.get(key)
        return default if v is None else int(v)

    if s.get("auto_index_on_scan", True):
        index(_limit("auto_index_limit", 0), True)
    if s.get("auto_ocr_enabled", True):
        ocr_batch(_limit("auto_ocr_limit", 0))
    with db.cursor() as con:
        con.run("""INSERT INTO settings(key,value) VALUES ('auto_scan_last_run',:v::jsonb)
                   ON CONFLICT (key) DO UPDATE SET value=:v::jsonb, updated_at=now()""",
                v=json.dumps(now.isoformat(timespec="seconds")))
    print(f"  完成：新增 {added} 个文件记录")


def status():
    with db.cursor(commit=False) as con:
        tot = con.run("SELECT count(*) FROM files")[0][0]
        by = con.run("SELECT status, count(*) FROM files GROUP BY status ORDER BY 2 DESC")
        ch = con.run("SELECT count(*), count(embedding) FROM chunks")[0]
        srcs = con.run("SELECT id,name,root_path,enabled FROM sources ORDER BY id")
        arc = con.run("SELECT count(*), coalesce(sum(entries),0) FROM archives")[0]
    print(f"  文件: {tot}   块: {ch[0]}（已向量化 {ch[1]}）   解包记录: {arc[0]}（条目 {arc[1]}）")
    print("  状态分布: " + " / ".join(f"{k}={v}" for k, v in by))
    print("  数据源:")
    for sid, name, root, en in srcs:
        print(f"    [{sid}] {name} → {root} {'(启用)' if en else '(停用)'}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["init", "add-source", "scan", "index", "status",
                                    "autoscan", "ocr", "ocr-upgrade", "clean", "md-export", "entities",
                    "summarize", "sections"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--sum-limit", type=int, default=6)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--no-embed", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="只演练不删除（clean 用）")
    ap.add_argument("--no-idle", action="store_true", help="不等空闲，立即抽取（entities 用）")
    a = ap.parse_args()
    # 只有 init 才做 v1 → v2 迁移；其余命令绝不能 DROP chunks（否则块数据被清空）
    if a.cmd == "init":
        init_schema(drop_legacy=True)
        print("  schema + 默认设置已就绪")
        status()
        return
    if a.cmd == "add-source":
        name, path = a.args[0], a.args[1]
        with db.cursor() as con:
            con.run("INSERT INTO sources(name,root_path) VALUES (:n,:p) ON CONFLICT (root_path) DO NOTHING",
                    n=name, p=path)
        print(f"  已添加数据源: {name} → {path}")
    elif a.cmd == "scan":
        scan(a.limit)
    elif a.cmd == "index":
        index(a.limit, not a.no_embed)
    elif a.cmd == "autoscan":
        autoscan()
    elif a.cmd == "ocr":
        ocr_batch(a.limit if a.limit is not None else 2)
    elif a.cmd == "clean":
        clean(dry_run=a.dry_run)
    elif a.cmd == "md-export":
        md_export(a.limit)
    elif a.cmd == "entities":
        import entities
        entities.run(limit=(a.limit or 5), idle_only=not getattr(a, "no_idle", False))
    elif a.cmd == "summarize":
        import summarize
        summarize.run(limit=(a.limit or 3), idle_only=not getattr(a, "no_idle", False))
    elif a.cmd == "ocr-upgrade":
        import ocr_upgrade
        ocr_upgrade.run(limit=(a.limit or 3))
    elif a.cmd == "sections":
        import sections
        if getattr(a, "rebuild", False):
            sections.rebuild_all(limit=a.limit)
        else:
            sections.build_missing(limit=(a.limit or 50))
        _sl = int(getattr(a, "sum_limit", 6) or 0)
        if _sl > 0:
            sections.summarize_pending(limit=_sl,
                                       idle_only=not getattr(a, "no_idle", False))
    elif a.cmd == "status":
        status()


if __name__ == "__main__":
    main()
