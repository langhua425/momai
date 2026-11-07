"""墨脉 · 对话与写稿（检索增强，本地 8001）

设计纪律：
1. **绝不裸生成**。先走 search.search() 三路召回（pgvector + Qdrant + 关键词，加权 RRF + 重排），
   再把命中的**完整块正文**拼进上下文；模型只允许基于这些内容作答。
2. **引用可回溯**。每个证据块带编号和文件名，模型被要求逐条标注 [n]。
3. **缺就标缺**。写稿时资料里没有的要素一律输出【待补：xx】，禁止编造数字/文号/单位/日期。
4. **8001 并发=1**：这里加进程内锁，避免与后台批处理同时打入把两边都拖慢。

为什么不用流式：8001 单次约 7s 起步，写稿更久；先做非流式 + 前端 loading，
等界面稳定后再上 SSE，避免一开始就把链路搞复杂。
"""
import json
import threading
import urllib.request

import db
import ingest
import pg8000.native

_LOCK = threading.Lock()
MAX_CTX_CHARS = 7000      # 上下文预算：约 7k 字 ≈ 7k token，留足生成空间
REF_LIMIT = 8             # 取 8 份文档做证据

# 公文骨架：只给结构，不预设内容，避免模型自由发挥
KINDS = {
    "report":  ("工作报告", "一、基本情况\n二、主要做法与成效\n三、存在问题\n四、下一步工作安排"),
    "request": ("请示",     "一、事由\n二、政策与事实依据\n三、请示事项"),
    "summary": ("工作总结", "一、总体情况\n二、完成的主要工作\n三、经验与不足\n四、下一年度打算"),
    "plan":    ("工作方案", "一、工作目标\n二、重点任务\n三、实施步骤\n四、保障措施"),
    "notice":  ("通知",     "一、事项\n二、要求\n三、时间安排\n四、联系方式"),
    "letter":  ("函",       "一、来函缘由\n二、商洽事项\n三、回复意见"),
}

SYS_CHAT = (
    "你是「墨脉」，一个只依据本单位资料库作答的助手。铁律：\n"
    "1. 只能使用【资料】中的内容，不得引入资料之外的知识；\n"
    "2. 每条关键结论后标注引用编号，如 [1]；同一结论有多份来源时写 [1][3]；\n"
    "3. 资料不足以回答时，直接说“资料中未找到相关内容”，不要推测、不要用常识补；\n"
    "4. 金额、日期、单位名称、文号必须与原文完全一致，不得改写；\n"
    "5. 回答用简体中文，先给结论再给依据，长度控制在必要范围。"
)

SYS_DRAFT = (
    "你是公文写作助手，按给定骨架起草公文。铁律：\n"
    "1. 只能使用【资料】中的事实与表述，不得虚构；\n"
    "2. 资料中没有的要素（数字、单位、文号、日期、责任人、金额）一律写成【待补：具体项】；\n"
    "3. 需要引用的关键事实在句末标 [n]；\n"
    "4. 直接输出公文正文，不要解释你的写作过程，不要输出“以下是……”之类的话。"
)


def _endpoint(base):
    """settings 里的 vllm_url 常已带 /v1，拼 /chat/completions 前先剥掉重复段。"""
    b = (base or "").rstrip("/")
    for suf in ("/chat/completions",):
        if b.endswith(suf):
            b = b[: -len(suf)].rstrip("/")
    return b + "/chat/completions"


def _chat(base, model, messages, timeout=240, max_tokens=2600, temperature=0.2):
    body = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(_endpoint(base), data=body,
                                 headers={"Content-Type": "application/json"})
    with _LOCK:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read())
    msg = (d.get("choices") or [{}])[0].get("message") or {}
    # 推理模型可能把内容全塞进 reasoning_content，content 为 None
    return (msg.get("content") or msg.get("reasoning_content") or "").strip()


def _chunk_text(file_id, seq, fallback=""):
    """取块的完整正文：snippet 只有几百字，写稿需要更完整的上下文。"""
    try:
        con = pg8000.native.Connection(**db.DSN)
        try:
            rows = con.run(
                "select content from chunks where file_id=:f and seq=:s and is_current limit 1",
                f=file_id, s=seq)
            return (rows[0][0] if rows else "") or fallback
        finally:
            con.close()
    except Exception:
        return fallback


def _collect(q, s, limit=REF_LIMIT, mode="hybrid"):
    """召回并按序号拼上下文；返回 (context, refs)。"""
    import search as S
    r = S.search(q, mode=mode, limit=limit)
    refs, parts, used = [], [], 0
    for i, it in enumerate(r.get("results") or [], 1):
        if used >= MAX_CTX_CHARS:
            break
        body = _chunk_text(it.get("file_id"), it.get("seq"), it.get("snippet") or "")
        if not body:
            continue
        body = body.strip()
        if used + len(body) > MAX_CTX_CHARS:
            body = body[: max(0, MAX_CTX_CHARS - used)]
        head = f"[{i}] 《{it.get('name')}》"
        if it.get("section_path"):
            head += f" · {it['section_path']}"
        head += f"（路径：{it.get('path')}）"
        parts.append(f"{head}\n{body}")
        refs.append({
            "n": i, "file_id": it.get("file_id"), "name": it.get("name"),
            "path": it.get("path"), "section_path": it.get("section_path"),
            "score": it.get("score"), "snippet": (it.get("snippet") or "")[:200],
        })
        used += len(body)
    return "\n\n".join(parts), refs


def _chat_stream(base, model, messages, timeout=300, max_tokens=2600, temperature=0.2):
    """逐片产出正文，供 SSE 转发。

    两个坑：
    ① **不能在整个流期间持 _LOCK** —— 那会把并发请求全排到流结束；
       只有 ink 建连需要串行（8001 并发=1），拿到响应对象就放锁。
    ② 推理模型会把思考塞进 `reasoning_content`。正文有内容时**不转发**思考，
       否则用户会看到一堆内心独白；只有整轮都没有正文时才回退到思考内容。
    """
    body = json.dumps({
        "model": model, "messages": messages, "temperature": temperature,
        "max_tokens": max_tokens, "stream": True,
    }, ensure_ascii=False).encode()
    req = urllib.request.Request(_endpoint(base), data=body,
                                 headers={"Content-Type": "application/json"})
    with _LOCK:
        r = urllib.request.urlopen(req, timeout=timeout)
    think_buf = []
    emitted = False
    try:
        for raw in r:
            line = raw.decode("utf-8", "ignore").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                d = json.loads(data)
            except Exception:                                  # noqa: BLE001
                continue
            delta = (d.get("choices") or [{}])[0].get("delta") or {}
            piece = delta.get("content") or ""
            if piece:
                emitted = True
                yield piece
            elif delta.get("reasoning_content"):
                think_buf.append(delta["reasoning_content"])
    finally:
        try:
            r.close()
        except Exception:                                      # noqa: BLE001
            pass
    if not emitted and think_buf:
        yield "".join(think_buf)


def _build_msgs(question, ctx, history=None):
    """对话消息体：answer() 与流式接口共用，避免两处 prompt 漂移。"""
    msgs = [{"role": "system", "content": SYS_CHAT}]
    for h in (history or [])[-6:]:
        role = h.get("role")
        content = (h.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            msgs.append({"role": role, "content": content[:1500]})
    msgs.append({"role": "user", "content": f"【资料】\n{ctx}\n\n【问题】\n{question}"})
    return msgs


def answer(question, history=None, s=None, mode="hybrid"):
    """对话式问答：返回 {answer, refs, evidence_chars}。"""
    s = s or ingest.settings()
    q = (question or "").strip()
    if not q:
        raise ValueError("问题为空")
    ctx, refs = _collect(q, s, mode=mode)
    if not refs:
        return {"answer": "资料中未找到相关内容。可以换个说法，或先确认该文件是否已入库。",
                "refs": [], "evidence_chars": 0}
    msgs = _build_msgs(q, ctx, history)
    txt = _chat(db.require_endpoint(s, "vllm_url"), s.get("vllm_model") or "local", msgs,
                timeout=int(s.get("assistant_timeout", 300)),
                max_tokens=int(s.get("assistant_max_tokens", 2600)))
    return {"answer": txt or "模型未返回内容，请重试。", "refs": refs,
            "evidence_chars": len(ctx)}


def draft(topic, kind="report", points="", s=None, mode="hybrid"):
    """写稿：按文体骨架 + 资料事实起草，缺要素标【待补】。"""
    s = s or ingest.settings()
    topic = (topic or "").strip()
    if not topic:
        raise ValueError("主题为空")
    kind_name, skeleton = KINDS.get(kind, KINDS["report"])
    # 检索查询用主题 + 要点，命中更准
    query = topic + (" " + points[:200] if points else "")
    ctx, refs = _collect(query, s, limit=10, mode=mode)
    if not refs:
        return {"draft": "资料中未找到与该主题相关的内容，无法起草。", "refs": [],
                "kind": kind, "kind_name": kind_name, "evidence_chars": 0}
    user = (
        f"【文体】{kind_name}\n"
        f"【主题】{topic}\n"
        f"【必须覆盖的要点】{points or '（未指定，按骨架自行组织）'}\n"
        f"【建议骨架】\n{skeleton}\n\n"
        f"【资料】\n{ctx}\n\n"
        "请据此起草正文。资料里没有的信息写【待补：xx】。"
    )
    msgs = [{"role": "system", "content": SYS_DRAFT},
            {"role": "user", "content": user}]
    txt = _chat(db.require_endpoint(s, "vllm_url"), s.get("vllm_model") or "local", msgs,
                timeout=int(s.get("draft_timeout", 420)),
                max_tokens=int(s.get("draft_max_tokens", 3200)))
    return {"draft": txt or "模型未返回内容，请重试。", "refs": refs, "kind": kind,
            "kind_name": kind_name, "evidence_chars": len(ctx)}
