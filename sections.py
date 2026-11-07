"""章节结构 + 章节级摘要。

为什么要有这一层
- 一份 80 页的审计报告，整篇摘要只能给个大概；真正有用的是「第 3 章 费用核定」这一段讲了什么。
- 检索时命中一个块，如果知道它属于哪一章，能直接把整章上下文带出来。

两级成本
- 结构构建（纯 DB，毫秒级）：每次 index/OCR 入库后顺手建。
- 章节摘要（要调 8001）：走空闲门禁，一轮补几节，夜间窗口加速。

表 file_sections 对标 winclaw 的同名表：section_index / title / anchor /
start_chunk_index / end_chunk_index / content_chars / summary。
"""
import re
from datetime import datetime

import db
import ingest
from entities import _chat, is_idle

PROMPT = """你在为一篇公文做章节提要。只输出 JSON，不要解释。

输出格式：
{"summary": "1-3 句中文，讲清这一节说了什么、涉及什么金额/主体/结论"}

章节标题：__TITLE__
所属文档：__DOC__

本章节正文：
---
__TEXT__
---
只输出 JSON。"""

MAX_CHARS = 2500


def _ensure_table():
    with db.cursor() as con:
        con.run("""CREATE TABLE IF NOT EXISTS file_sections (
            id bigserial PRIMARY KEY,
            file_id bigint NOT NULL,
            version int,
            section_index int NOT NULL,
            title text,
            anchor int,
            start_chunk_index int,
            end_chunk_index int,
            content_chars int DEFAULT 0,
            summary text,
            created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(),
            UNIQUE (file_id, section_index)
        )""")
        con.run("CREATE INDEX IF NOT EXISTS idx_sections_file ON file_sections(file_id)")


# 中文公文常见标题形态：第三章 / 第二节 / 一、 / （一） / 1.1 / 四、
_CN_HEAD = re.compile(
    r"^\s*(?:第[一二三四五六七八九十百零0-9]{1,3}[章节篇条]"
    r"|[一二三四五六七八九十]{1,3}\s*[、.．]"
    r"|[（(][一二三四五六七八九十0-9]{1,3}[)）]"
    r"|\d{1,2}(?:\.\d{1,2}){1,3}\s+\S"
    r"|[一二三四五六七八九十]{1,3}\s+\S)"
)
_PSEUDO_CHARS = 3500      # 无标题时的切片粒度


def _valid_title(t):
    """标题得像标题：短、没有句号/分号、不是一句话。"""
    if not t:
        return False
    t = t.strip()
    if not (2 <= len(t) <= 30):
        return False
    if t.count("。") or t.count("；") or "。" in t:
        return False
    if t.endswith(("，", ",", "、", "：", ":", "…")):
        return False
    return True


def _title_of_chunk(head, path, content):
    """优先用切块时带的章节路径；否则从正文首行嗅探中文标题。"""
    if path and path.strip():
        cand = path.split(" > ")[-1].strip()
        if _valid_title(cand):
            return cand
    if head and _valid_title(head):
        return head.strip()
    first = ""
    for line in (content or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if first and len(first) <= 28 and _CN_HEAD.match(first) and "《" not in first:
        return first
    return None


def build(fid, s=None):
    """重建该文件的章节结构（幂等，不调模型）。

    分组优先级：切块带的 section_path > heading > 正文首行的中文标题 > 按长度切片。
    """
    _ensure_table()
    with db.cursor(commit=False) as con:
        ver = con.run("SELECT coalesce(version,1) FROM files WHERE id=:i", i=fid)
        rows = con.run("""SELECT seq, coalesce(heading,''), coalesce(section_path,''),
                                 content
                          FROM chunks WHERE file_id=:i AND is_current ORDER BY seq""", i=fid)
    if not rows:
        return 0
    version = int(ver[0][0]) if ver else 1

    groups = []          # [title, [seq...], chars]
    for seq, head, path, content in rows:
        title = _title_of_chunk(head, path, content)
        n = len(content or "")
        if not groups:
            groups.append([title or "正文", [], 0])
        elif title and title != groups[-1][0]:
            groups.append([title, [], 0])
        elif not title and groups[-1][2] + n > _PSEUDO_CHARS:
            # 无标题长文：按长度切，标题标明覆盖范围
            groups.append([None, [], 0])
        groups[-1][1].append(seq)
        groups[-1][2] += n

    # 无标题的伪章节补上可读标题：段号 + 开头一句（便于人眼定位）
    heads = {r[0]: (r[3] or "") for r in rows}

    def _snippet(text):
        """挑一行像人话的：≥8 个汉字、长度 8–22；纯数字/表格行直接跳过。"""
        for line in (text or "").splitlines():
            s = line.strip()
            if 8 <= len(s) <= 22 and len(re.findall(r"[\u4e00-\u9fff]", s)) >= 8:
                return s
        return ""

    for i, g in enumerate(groups):
        if g[0] is None:
            sn = _snippet(heads.get(g[1][0], ""))
            g[0] = f"第 {i + 1} 段" + (f"｜{sn}" if sn else "")

    with db.cursor() as con:
        for idx, (title, seqs, chars) in enumerate(groups):
            con.run("""INSERT INTO file_sections
                         (file_id, version, section_index, title, anchor,
                          start_chunk_index, end_chunk_index, content_chars, updated_at)
                       VALUES (:f,:v,:i,:t,:a,:s,:e,:c,now())
                       ON CONFLICT (file_id, section_index) DO UPDATE SET
                         version=EXCLUDED.version, title=EXCLUDED.title,
                         anchor=EXCLUDED.anchor, start_chunk_index=EXCLUDED.start_chunk_index,
                         end_chunk_index=EXCLUDED.end_chunk_index,
                         content_chars=EXCLUDED.content_chars, updated_at=now(),
                         -- 版本变了就丢掉旧摘要，等重生成
                         summary=CASE WHEN file_sections.version <> EXCLUDED.version
                                      THEN NULL ELSE file_sections.summary END""",
                    f=fid, v=version, i=idx, t=title[:200], a=seqs[0],
                    s=seqs[0], e=seqs[-1], c=chars)
        con.run("DELETE FROM file_sections WHERE file_id=:f AND section_index >= :n",
                f=fid, n=len(groups))
    return len(groups)


def get(fid):
    _ensure_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT section_index, title, anchor, start_chunk_index, end_chunk_index,
                                 content_chars, summary
                          FROM file_sections WHERE file_id=:f ORDER BY section_index""", f=fid)
    return [{"index": r[0], "title": r[1], "anchor": r[2], "start": r[3], "end": r[4],
             "chars": r[5], "summary": r[6]} for r in rows]


def _section_text(fid, start, end, cap=MAX_CHARS):
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT content FROM chunks
                          WHERE file_id=:f AND is_current AND seq BETWEEN :s AND :e
                          ORDER BY seq""", f=fid, s=start, e=end)
        nm = con.run("SELECT name FROM files WHERE id=:f", f=fid)
    text = "\n".join(r[0] or "" for r in rows)
    return (nm[0][0] if nm else ""), text[:cap]


def summarize_pending(limit=6, s=None, idle_only=True, file_id=None):
    """给还没摘要的章节补摘要（走 8001，遵守空闲门禁）。"""
    _ensure_table()
    s = s or ingest.settings()
    base = (s.get("vllm_url") or "").rstrip("/")
    model = s.get("vllm_model") or "local"
    hour = datetime.now().hour
    idle_check = False if (idle_only and 1 <= hour < 7) else idle_only
    if idle_check and not is_idle(base):
        print("  8001 正在忙，本轮不生成章节摘要")
        return 0

    with db.cursor(commit=False) as con:
        cond = "AND file_id = :f" if file_id else ""
        todo = con.run(f"""SELECT id, file_id, section_index, title, start_chunk_index, end_chunk_index
                           FROM file_sections
                           WHERE summary IS NULL AND content_chars >= 120 {cond}
                           ORDER BY file_id, section_index LIMIT :l""",
                       **({"f": file_id} if file_id else {}), l=limit)
    done = 0
    for sid, fid, idx, title, st, en in todo:
        if idle_check and not is_idle(base):
            print("  8001 转为忙，暂停")
            break
        name, text = _section_text(fid, st, en)
        if len(text.strip()) < 80:
            with db.cursor() as con:
                con.run("UPDATE file_sections SET summary='（正文过短，未生成摘要）' WHERE id=:i", i=sid)
            continue
        try:
            raw = _chat(base, model,
                        PROMPT.replace("__TITLE__", title or "").replace("__DOC__", name or "")
                              .replace("__TEXT__", text),
                        timeout=240)
        except Exception as e:                       # noqa: BLE001
            print(f"  ! 章节摘要失败 {fid}#{idx}: {e}")
            continue
        import re
        import json
        sm = None
        try:
            m = re.search(r"\{.*\}", raw or "", re.S)
            if m:
                sm = (json.loads(m.group(0)) or {}).get("summary")
        except Exception:                            # noqa: BLE001
            m = re.search(r'"summary"\s*:\s*"(.+?)"', raw or "", re.S)
            sm = m.group(1) if m else None
        if not sm:
            continue
        with db.cursor() as con:
            con.run("UPDATE file_sections SET summary=:s, updated_at=now() WHERE id=:i",
                    s=sm.strip()[:1200], i=sid)
        done += 1
        print(f"  {name[:26]:<26} §{idx} {title[:16]:<16} {sm[:48]}…")
    print(f"  本轮章节摘要 {done} 节")
    return done


def rebuild_all(limit=0):
    """全量重建章节结构（结构规则改过时用）。会清掉旧摘要，等重新生成。"""
    _ensure_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT DISTINCT f.id FROM files f
                          JOIN chunks c ON c.file_id=f.id AND c.is_current
                          ORDER BY f.id""" + (" LIMIT :l" if limit else ""),
                       **({"l": limit} if limit else {}))
    n_file = n_sec = 0
    for (fid,) in rows:
        with db.cursor() as con:
            con.run("UPDATE file_sections SET summary=NULL WHERE file_id=:f", f=fid)
        n_sec += build(fid)
        n_file += 1
    print(f"  重建 {n_file} 个文件 / {n_sec} 节")
    return n_file


def build_missing(limit=50):
    """给还没有章节结构的已解析文件补结构（纯 DB，不调模型）。"""
    _ensure_table()
    with db.cursor(commit=False) as con:
        rows = con.run("""SELECT DISTINCT f.id FROM files f
                          JOIN chunks c ON c.file_id=f.id AND c.is_current
                          WHERE NOT EXISTS (SELECT 1 FROM file_sections s WHERE s.file_id=f.id)
                          LIMIT :l""", l=limit)
    n = 0
    for (fid,) in rows:
        n += build(fid)
    print(f"  补建 {len(rows)} 个文件的章节结构（共 {n} 节）")
    return len(rows)
