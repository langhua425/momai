"""归档解包：zip / tar / tar.gz / tgz / gz / rar / 7z，支持递归与来源链。

来源链：外层容器逻辑路径#内部相对路径（多层继续用 # 连接），
      同时记录 container_id / depth，便于按"来自哪个压缩包"过滤。
"""
import os
import shutil
import zipfile
import tarfile
import subprocess
import gzip

ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".tar.gz", ".rar", ".7z", ".bz2", ".xz", ".zst"}


def is_archive(name: str) -> bool:
    low = name.lower()
    if low.endswith(".tar.gz") or low.endswith(".tar.bz2") or low.endswith(".tar.xz") or low.endswith(".tar.zst"):
        return True
    return os.path.splitext(low)[1] in ARCHIVE_EXTS


def _safe_join(dest: str, member: str) -> str | None:
    target = os.path.normpath(os.path.join(dest, member))
    if not os.path.abspath(target).startswith(os.path.abspath(dest) + os.sep):
        return None  # 路径穿越
    return target


def unpack(path: str, dest: str, max_bytes: int = 20 * 1024**3) -> dict:
    """解包到 dest，返回 {ok, entries, bytes, fmt, message}。"""
    os.makedirs(dest, exist_ok=True)
    ext = os.path.splitext(path)[1].lower()
    low = path.lower()
    entries = 0
    total = 0
    try:
        if low.endswith((".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".tar.zst", ".tar")):
            with tarfile.open(path, "r:*") as tf:
                for m in tf.getmembers():
                    total += (m.size or 0)
                    if total > max_bytes:
                        return {"ok": False, "fmt": "tar", "entries": entries, "bytes": total,
                                "message": "超过解包上限"}
                    if not m.isfile():
                        continue
                    target = _safe_join(dest, m.name)
                    if not target:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    f = tf.extractfile(m)
                    if f:
                        with open(target, "wb") as out:
                            shutil.copyfileobj(f, out, 1024 * 512)
                        entries += 1
            return {"ok": True, "fmt": "tar", "entries": entries, "bytes": total, "message": ""}

        if ext == ".zip":
            with zipfile.ZipFile(path) as z:
                for info in z.infolist():
                    if info.is_dir():
                        continue
                    total += info.file_size
                    if total > max_bytes:
                        return {"ok": False, "fmt": "zip", "entries": entries, "bytes": total,
                                "message": "超过解包上限"}
                    target = _safe_join(dest, info.filename)
                    if not target:
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with z.open(info) as src, open(target, "wb") as out:
                        shutil.copyfileobj(src, out, 1024 * 512)
                    entries += 1
                # 处理 zip 内的中文名乱码（cp437 → gbk）
            return {"ok": True, "fmt": "zip", "entries": entries, "bytes": total, "message": ""}

        if ext == ".gz" and not low.endswith(".tar.gz"):
            base = os.path.basename(path)[:-3]
            target = _safe_join(dest, base)
            if not target:
                return {"ok": False, "fmt": "gz", "entries": 0, "bytes": 0, "message": "非法路径"}
            with gzip.open(path, "rb") as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out, 1024 * 512)
            return {"ok": True, "fmt": "gz", "entries": 1, "bytes": os.path.getsize(target), "message": ""}

        if ext in (".rar", ".7z", ".bz2", ".xz", ".zst"):
            exe = shutil.which("unar") or shutil.which("7z") or shutil.which("unrar")
            if not exe:
                return {"ok": False, "fmt": ext.lstrip("."), "entries": 0, "bytes": 0,
                        "message": "缺少 unar/7z/unrar"}
            cmd = [exe, "x" if "7z" in exe else "-o", dest, path] if "7z" in exe else \
                  ([exe, "-q", "-o", dest, path] if "unar" in exe else [exe, "x", path, dest + "/"])
            r = subprocess.run(cmd, capture_output=True, timeout=3600)
            for dirpath, _, files in os.walk(dest):
                for fn in files:
                    entries += 1
                    total += os.path.getsize(os.path.join(dirpath, fn))
            ok = r.returncode == 0
            return {"ok": ok, "fmt": ext.lstrip("."), "entries": entries, "bytes": total,
                    "message": "" if ok else (r.stderr.decode(errors="replace")[:200])}
    except Exception as e:
        return {"ok": False, "fmt": ext.lstrip("."), "entries": entries, "bytes": total, "message": str(e)}
    return {"ok": False, "fmt": ext.lstrip("."), "entries": 0, "bytes": 0, "message": "不支持的格式"}


def walk_unpacked(root: str):
    """遍历解包目录，返回相对路径列表（文件）。"""
    out = []
    for dirpath, _, files in os.walk(root):
        for fn in files:
            p = os.path.join(dirpath, fn)
            out.append(os.path.relpath(p, root))
    return out
