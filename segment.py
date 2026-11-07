"""查询分词：中文用 jieba，英文/数字保留原样；供关键词召回按词 OR 匹配。

整句 ILIKE 在自然语言查询上召回为 0，所以必须先分詞再匹配。
"""
import re

_STOP = set("的 了 和 与 及 或 在 是 有 对 从 到 为 以 被 把 让 就 都 也 而 之 其 这 那 个 中 上 下 年内 年度 关于 情况 相关 进行 以及 等 我 你 他 它 我们 你们 他们".split())

try:
    import jieba
    jieba.setLogLevel(60)
    _HAS_JIEBA = True
except Exception:      # noqa: BLE001
    _HAS_JIEBA = False


def _fallback(text):
    """无 jieba 时：中文按 2-gram 切，英文数字按词切。"""
    out = []
    for tok in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_.-]+", text):
        if re.match(r"^[\u4e00-\u9fff]+$", tok):
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
            if len(tok) <= 2:
                out.append(tok)
        else:
            out.append(tok)
    return out


def terms(query, max_terms=10, min_len=2):
    """把查询切成检索词（去停用词、去重、按长度降序）。"""
    q = (query or "").strip()
    if not q:
        return []
    raw = list(jieba.cut_for_search(q)) if _HAS_JIEBA else _fallback(q)
    seen, out = set(), []
    for t in raw:
        t = t.strip().lower()
        if not t or t in _STOP:
            continue
        if len(t) < min_len and not t.isdigit():
            continue
        if t in seen:
            continue
        seen.add(t)
        out.append(t)
    out.sort(key=lambda x: (-len(x), x))
    # 整句本身也作为一个词，命中时权重最高
    whole = q.lower()
    if len(whole) >= min_len and whole not in seen:
        out.insert(0, whole)
    return out[:max_terms]


def info():
    return {"jieba": _HAS_JIEBA, "version": getattr(__import__("jieba"), "__version__", "") if _HAS_JIEBA else ""}
