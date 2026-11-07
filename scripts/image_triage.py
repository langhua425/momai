#!/usr/bin/env python3
"""墨脉 · 图片分诊（T0.3）

背景：680 张图片此前被 skip_exts 一刀切，全部静默跳过。实测后按证据分流：

| 判据 | 归类 | 依据 |
|---|---|---|
| 路径在压缩包解包目录（/zk/html、含 .zip#）且文件不存在 | never / source_missing | 解包目录已清理，记录是幽灵 |
| 路径含 竣工图/图纸/大样/断面/平面图 | never / drawing_image | A0/A1 扫描（≥6000px），文字极少，OCR 成本高收益低 |
| 宽 1500–4000 且 宽高比 0.65–0.78（A4 竖版） | ocr_candidate / ready_to_ocr | 实测 4 张：5s/张，产 333–1558 字 |
| 其余 | defer / manual_review | 需人看一眼 |

用法（在宿主跑，宿主有 PIL 且 /tank/files 已 NFS 挂到 /data2/matrix-media）：
    python3 image_triage.py --dry-run      # 只看分布
    python3 image_triage.py --apply        # 写库（同时把缺失文件标 missing）
"""
import argparse
import json
import os
import subprocess

NAS = "langhua@192.168.18.4"
MOUNT = "/data2/matrix-media"          # NAS /tank/files 的宿主编译挂载点
SQL = """{stmt}"""

DRAWING_KEYS = ("竣工图", "图纸", "大样", "断面", "平面图")


def psql(sql):
    """走 ssh + psql；语句只含固定串与整数，不含用户输入。"""
    cmd = ["ssh", "-o", "BatchMode=yes", NAS,
           f"sudo -n -u postgres psql -d wenmai -tAF'|' -c \"{sql}\""]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:300])
    return r.stdout


def load_pending():
    out = psql("select f.id, f.path, f.size, coalesce(f.ext,'') "
               "from file_index_policies p join files f on f.id=p.file_id "
               "where p.reason_code='image_needs_ocr' order by f.size desc;")
    rows = []
    for line in out.splitlines():
        parts = line.split("|")
        if len(parts) == 4:
            rows.append({"id": int(parts[0]), "path": parts[1],
                         "size": int(parts[2]), "ext": parts[3]})
    return rows


def classify(row):
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    p = row["path"]
    host_path = p.replace("/tank/files/", MOUNT + "/")
    if "/zk/html" in p or ".zip#" in p:
        if not os.path.exists(host_path):
            return "never", "source_missing", "解包目录已清理，文件不存在"
    if any(k in p for k in DRAWING_KEYS):
        return "never", "drawing_image", "图纸扫描件，文字少"
    if not os.path.exists(host_path):
        return "never", "source_missing", "文件不存在"
    try:
        with Image.open(host_path) as im:
            w, h = im.size
    except Exception as e:
        return "defer", "manual_review", f"打不开：{type(e).__name__}"
    ratio = w / h if h else 0
    if 1500 <= w <= 4000 and 0.65 <= ratio <= 0.78:
        return "ocr_candidate", "ready_to_ocr", f"{w}x{h}"
    if w >= 6000:
        return "never", "huge_image", f"{w}x{h} 超大图"
    return "defer", "manual_review", f"{w}x{h}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = load_pending()
    print(f"待分诊图片：{len(rows)}")
    buckets = {}
    updates = []
    for r in rows:
        policy, code, why = classify(r)
        buckets.setdefault(f"{policy}/{code}", []).append(r)
        updates.append((r["id"], policy, code, why))

    for k, v in sorted(buckets.items(), key=lambda x: -len(x[1])):
        mb = sum(i["size"] for i in v) / 1048576
        print(f"  {k:32s} {len(v):4d} 个  {mb:8.1f} MB")
        for i in v[:2]:
            print(f"      e.g. {i['path'].rsplit('/', 1)[-1][:44]}")

    if not a.apply:
        print("\n（演练模式，未写库；加 --apply 生效）")
        return

    stmts = ["BEGIN;"]
    for fid, policy, code, why in updates:
        detail = why.replace("'", "''")[:200]
        stmts.append(
            "UPDATE file_index_policies SET policy='%s', reason_code='%s', "
            "reason_kind='policy', reason_detail='%s', updated_at=now() WHERE file_id=%d;"
            % (policy, code, detail, fid))
        if code == "source_missing":
            stmts.append(
                "UPDATE files SET status='missing' WHERE id=%d AND status<>'missing';" % fid)
            stmts.append(
                "UPDATE chunks SET is_current=false WHERE file_id=%d AND is_current;" % fid)
    stmts.append("COMMIT;")
    path = "/tmp/image_triage.sql"
    open(path, "w", encoding="utf-8").write("\n".join(stmts))
    r = subprocess.run(["scp", "-q", path, f"{NAS}:/tmp/image_triage.sql"], capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("scp 失败: " + r.stderr[:200])
    r2 = subprocess.run(["ssh", "-o", "BatchMode=yes", NAS,
                         "sudo -n -u postgres psql -d wenmai -q -f /tmp/image_triage.sql"],
                        capture_output=True, text=True, timeout=300)
    print("\n写库结果:", (r2.stdout + r2.stderr).strip()[:300] or "OK")
    print(psql("select p.policy||'/'||p.reason_code, count(*) from file_index_policies p "
               "join files f on f.id=p.file_id where f.ext in ('.jpg','.jpeg','.png','.bmp','.tif','.tiff') "
               "group by 1 order by 2 desc;"))


if __name__ == "__main__":
    main()
