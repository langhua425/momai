"""重排（rerank）客户端：兼容 llama.cpp /reranking 与 Jina/Cohere /v1/rerank。

llama.cpp: POST /v1/rerank {model, query, documents, top_n} -> {results:[{index, relevance_score}]}
"""
import json
import db
import urllib.error
import urllib.request


class RerankError(RuntimeError):
    pass


def _post(url, payload, timeout=120):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def rerank(s, query, docs, top_n=None, timeout=120):
    """返回 [(原下标, 分数)]，按分数降序。端点不可用时抛 RerankError。"""
    if not s.get("rerank_enabled"):
        raise RerankError("重排未启用")
    base = db.resolve_endpoint(s, "rerank_url")
    if not base or not docs:
        raise RerankError("未配置 rerank_url（请在设置页填写或设 MOMAI_RERANK_URL），或没有候选")
    payload = {"model": s.get("rerank_model") or "reranker",
               "query": query, "documents": docs,
               "top_n": top_n or len(docs)}
    last = None
    for path in ("/v1/rerank", "/reranking", "/rerank"):
        try:
            d = _post(base + path, payload, timeout=timeout)
            rows = d.get("results") or d.get("data") or []
            if not rows:
                raise RerankError(f"{path} 返回空结果")
            out = []
            for r in rows:
                idx = r.get("index")
                score = r.get("relevance_score", r.get("score"))
                if idx is None or score is None:
                    continue
                out.append((int(idx), float(score)))
            if out:
                out.sort(key=lambda x: -x[1])
                return out
            raise RerankError(f"{path} 结果字段不识别")
        except urllib.error.HTTPError as e:
            last = f"{path} HTTP {e.code}"
            continue
        except Exception as e:      # noqa: BLE001
            last = f"{path} {type(e).__name__}: {e}"
            continue
    raise RerankError(last or "重排失败")


def health(s):
    try:
        base = db.resolve_endpoint(s, "rerank_url")
        if not base:
            return {"ok": False, "error": "未配置"}
        pairs = rerank(s, "测试查询", ["测试文档一", "完全不相关的内容"], top_n=2, timeout=20)
        return {"ok": True, "sample": pairs}
    except Exception as e:      # noqa: BLE001
        return {"ok": False, "error": str(e)[:200]}
