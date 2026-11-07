"""MinerU 4.x 客户端：POST /v1/tasks → PUT 上传 → 轮询 → 取 markdown。

服务端为任务制：创建任务后返回每文件的签名上传链接，PUT 完即自动执行。
"""
import json
import time
import urllib.request
import urllib.error


class MinerUError(RuntimeError):
    pass


def _post(url, payload, headers=None, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _put_file(url, path, headers=None, timeout=600):
    size = 0
    with open(path, "rb") as f:
        data = f.read()
    size = len(data)
    req = urllib.request.Request(url, data=data, method="PUT",
                                headers={"Content-Type": "application/octet-stream", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        try:
            return json.loads(body) if body else {}, size
        except Exception:
            return {}, size


def parse_file(base_url, path, file_name=None, ocr_mode="auto", tier=None,
               poll_sec=5, timeout_sec=1800, output_formats=("default",)):
    """把一个文件交给 mineru 解析，返回 (markdown_text, meta)。"""
    base = base_url.rstrip("/")
    name = file_name or path.rsplit("/", 1)[-1]
    import os as _os
    payload = {"files": [{"file_name": name, "bytes": _os.path.getsize(path)}],
               "ocr_mode": ocr_mode, "output_formats": list(output_formats)}
    if tier:
        payload["tier"] = tier
    created = _post(f"{base}/v1/tasks", payload)
    task_id = created["task_id"]
    finfo = created["files"][0]
    file_id = finfo["file_id"]
    upload = finfo.get("upload") or {}
    if not upload.get("url"):
        raise MinerUError(f"未返回上传链接: {json.dumps(created)[:300]}")
    _put_file(upload["url"], path, upload.get("headers"))

    deadline = time.time() + timeout_sec
    status, detail = "unknown", None
    while time.time() < deadline:
        time.sleep(poll_sec)
        detail = _get(f"{base}/v1/tasks/{task_id}")
        f = None
        for x in detail.get("files", []):
            if x.get("file_id") == file_id:
                f = x
                break
        status = (f or {}).get("status", detail.get("status"))
        if status in ("completed", "failed", "canceled"):
            break
    if status != "completed":
        raise MinerUError(f"任务未完成: status={status} err={(f or {}).get('error')}")

    # 结果内容：result_url 通常返回 zip（markdown.md + middle_json.json + images/）
    text = ""
    result_url = (f or {}).get("result_url")
    if result_url:
        req = urllib.request.Request(result_url)
        with urllib.request.urlopen(req, timeout=180) as r:
            raw = r.read()
        if raw[:2] == b"PK":
            import io, zipfile
            try:
                z = zipfile.ZipFile(io.BytesIO(raw))
                names = z.namelist()
                md = [n for n in names if n.lower().endswith(".md")]
                if md:
                    text = z.read(md[0]).decode("utf-8", errors="replace")
                else:
                    js = [n for n in names if n.lower().endswith(".json") and "content" in n.lower()]
                    if js:
                        text = z.read(js[0]).decode("utf-8", errors="replace")
            except Exception as e:
                text = ""
                meta_zip_err = str(e)
        else:
            text = raw.decode("utf-8", errors="replace")
    if not text:
        try:
            content = _get(f"{base}/v1/files/{file_id}/content")
            text = content.get("markdown") or content.get("text") or json.dumps(content)[:2000]
        except Exception:
            pass
    meta = {"task_id": task_id, "file_id": file_id, "status": status,
            "parse": (f or {}).get("parse"), "result_url": result_url}
    return text, meta


def health(base_url):
    try:
        d = _get(f"{base_url.rstrip('/')}/v1/health", timeout=10)
        return True, d
    except Exception as e:
        return False, str(e)
