"""实体与关系抽取（走 8001 vLLM，OpenAI 兼容接口）。

只抽公文/合同类最有价值的四类实体：
  机构(org) / 项目(project) / 金额(money) / 法条·文件(doc)
关系限定在动词短语（如「签订」「委托」「审核」「拨付」）。
"""
import json
import re
import urllib.request
from datetime import datetime

import db

PROMPT = """你是政务文档信息抽取器。从下面的文档片段里抽取实体和关系，只输出 JSON，不要解释。

实体类型只用：org(机关/公司)、project(项目/工程)、money(金额，含单位)、doc(法规/文号/协议名称)
关系用简短动词：签订、委托、审核、拨付、负责、依据、属于
不要抽取：图号、图纸文件名、纯编号（如 S-L-14侧平石大样图.dwg）、目录页码、表格序号。

输出格式：
{"entities":[{"name":"...","type":"org"}],"relations":[{"src":"...","rel":"签订","dst":"..."}]}

文档片段：
---
__TEXT__
---
只输出 JSON。"""


# 图纸/照片类文件名 + 图号式编号：这些不是业务实体，抽进来只会污染图谱
_DROP_EXT = re.compile(r"\.(dwg|dxf|shp|shx|dbf|sbn|sbx|tif|tiff|jpg|jpeg|png|bmp|gif)$", re.I)
_STRIP_EXT = re.compile(r"\.(pdf|docx?|xlsx?|ofd|txt|md)$", re.I)
_CODE_PREFIX = re.compile(r"^[A-Za-z]{1,3}-[A-Za-z0-9]{1,3}-[0-9]{1,4}")   # S-L-14 / S-H-01
_PURE_CODE = re.compile(r"^[A-Z0-9][A-Z0-9\-_\.]{2,}$")


def _clean_entity_name(name):
    """返回可入库的名字；判断为噪声则返回 None。

    图纸/照片文件名、图号、纯编号 → 丢；公文类后缀 → 只剥后缀保留主体。
    """
    n = (name or "").strip().strip("：:，,。.")
    if not n:
        return None
    if "/" in n or "\\" in n:                     # 路径
        return None
    if _DROP_EXT.search(n):                         # 图纸/照片
        return None
    m = _STRIP_EXT.search(n)
    if m:                                           # 报告.pdf → 报告
        n = n[:m.start()].strip()
        if len(n) < 4:
            return None
    if _CODE_PREFIX.match(n):                       # 图号 S-L-14…
        return None
    if _PURE_CODE.match(n) and not re.search(r"[\u4e00-\u9fff]", n):
        return None                                 # 纯编号、无中文
    return n


def _is_filename_entity(name):
    return _clean_entity_name(name) is None


def _endpoint(base, path="/v1/chat/completions"):
    """容错：vllm_url 可能已经带了 /v1，避免拼成 /v1/v1/..."""
    b = (base or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    return b + path


def is_idle(base, timeout=5):
    """8001 是否空闲：所有 slot 都没在推理，且队列为空。"""
    b = (base or "").rstrip("/")
    if b.endswith("/v1"):
        b = b[:-3]
    try:
        with urllib.request.urlopen(b + "/slots", timeout=timeout) as r:
            slots = json.loads(r.read())
        if any(s.get("is_processing") for s in slots):
            return False
        with urllib.request.urlopen(b + "/metrics", timeout=timeout) as r:
            m = json.loads(r.read())
        live = m.get("live") or {}
        return (live.get("state") in (None, "idle")) and not live.get("queued")
    except Exception:
        return False      # 探测失败就当作忙，宁可不跑


def _chat(base, model, prompt, timeout=180):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 2000,
        "stream": False,
    }).encode()
    req = urllib.request.Request(_endpoint(base), data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read())
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    # 推理模型可能把内容全塞进 reasoning_content，content 为 None
    txt = msg.get("content") or msg.get("reasoning_content") or ""
    return txt


def _parse_json(txt):
    """容忍模型输出带 ```json 围栏或前后废话；空输出直接返回 None。"""
    if not txt or not isinstance(txt, str):
        return None
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        return None
    s = m.group(0)
    s = s.replace("```json", "").replace("```", "")
    try:
        return json.loads(s)
    except Exception:
        # 去掉尾逗号再试一次
        s2 = re.sub(r",\s*([}\]])", r"\1", s)
        try:
            return json.loads(s2)
        except Exception:
            return None


def extract_for_file(fid, s, max_chunks=6):
    base = (s.get("vllm_url") or "").rstrip("/")
    model = s.get("vllm_model") or "local"
    if not base:
        raise RuntimeError("未配置 vllm_url")
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT id, content FROM chunks
                          WHERE file_id=:f AND is_current
                          ORDER BY seq LIMIT :l""", f=fid, l=max_chunks)
    ent_n = rel_n = 0
    for cid, content in rows:
        try:
            # 不能用 .format()：prompt 里有 JSON 花括号会被当占位符
            raw = _chat(base, model, PROMPT.replace("__TEXT__", content[:2500]))
        except Exception as e:
            print(f"  ! LLM 调用失败: {e}")
            continue
        data = _parse_json(raw)
        if not data:
            continue
        names = {}
        for e in (data.get("entities") or [])[:40]:
            name = (e.get("name") or "").strip()
            typ = (e.get("type") or "concept").strip()
            if not name or len(name) > 120:
                continue
            clean = _clean_entity_name(name)
            if not clean:
                continue
            name = clean
            with db.cursor() as con:
                row = con.run("""INSERT INTO entities(name,type) VALUES (:n,:t)
                                 ON CONFLICT (name,type) DO UPDATE SET mentions=entities.mentions+1
                                 RETURNING id""", n=name, t=typ)
                eid = row[0][0]
                con.run("""INSERT INTO entity_mentions(entity_id,file_id,chunk_id)
                           VALUES (:e,:f,:c) ON CONFLICT DO NOTHING""", e=eid, f=fid, c=cid)
            names[name] = eid
            ent_n += 1
        for r in (data.get("relations") or [])[:40]:
            src, rel, dst = (r.get("src") or "").strip(), (r.get("rel") or "").strip(), (r.get("dst") or "").strip()
            if src in names and dst in names and rel:
                with db.cursor() as con:
                    con.run("""INSERT INTO relations(src_id,dst_id,rel,file_id)
                               VALUES (:a,:b,:r,:f) ON CONFLICT DO NOTHING""",
                            a=names[src], b=names[dst], r=rel, f=fid)
                rel_n += 1
    return ent_n, rel_n


def run(limit=5, s=None, idle_only=True):
    """全量抽取（排队式）：每处理一个分块前都确认 8001 空闲，忙就收工。"""
    import ingest
    import time
    s = s or ingest.settings()
    base = (s.get("vllm_url") or "").rstrip("/")
    # 夜间窗口（01:00-07:00）不判空闲：白天让路，夜里补进度
    hour = datetime.now().hour
    in_night = 1 <= hour < 7
    if idle_only and in_night:
        print(f"  夜间窗口（{hour} 点），不等空闲直接抽")
        idle_only_check = False
    else:
        idle_only_check = idle_only
    if idle_only_check and not is_idle(base):
        print("  8001 正在忙，本轮不抽取（不抢业务）")
        return 0, 0
    # 选材：只要公文/合同/报告类正文文件；.log/.htm 之类和空壳文件不进
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT f.id, f.name FROM files f
                          WHERE f.status='parsed'
                            AND coalesce(f.text_bytes,0) BETWEEN 2000 AND 600000
                            AND f.ext IN ('.pdf','.docx','.doc','.ofd','.xlsx','.xls','.txt')
                            AND NOT EXISTS (SELECT 1 FROM relations r WHERE r.file_id=f.id)
                          ORDER BY
                            CASE WHEN f.name ~ '(协议|合同|审计|报告|方案|办法|规定|请示|批复)' THEN 0 ELSE 1 END,
                            coalesce(f.mtime, f.changed_at, f.created_at) DESC
                          LIMIT :l""", l=limit)
    total_e = total_r = 0
    for fid, name in rows:
        if idle_only_check and not is_idle(base):
            print("  8001 转为忙，暂停抽取，剩余留到下一轮")
            break
        e, r = extract_for_file(fid, s)
        total_e += e
        total_r += r
        print(f"  {name[:38]:<38} 实体 {e:>3} 关系 {r:>3}")
        time.sleep(5)
    print(f"  合计：实体 {total_e}，关系 {total_r}")
    return total_e, total_r
