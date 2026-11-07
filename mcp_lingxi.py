"""MCP（streamable HTTP）端点：/mcp/lingxi

对外暴露两个工具，与原 WinClaw 灵犀文脉同口径，任何 MCP 客户端
（Hermes / Claude Code / Codex / winclaw）都能直接挂上来。

协议要点：
- POST JSON-RPC 2.0，单条或批量
- 响应 application/json（客户端 Accept 含 application/json 时；SSE 也兼容）
- method: initialize / notifications/* / tools/list / tools/call / ping
"""
import json

import db
import search as wm_search
import assistant as wm_assistant

SERVER_NAME = "wenmai-lingxi"
SERVER_VERSION = "1.0.0"
PROTOCOL = "2025-06-18"

TOOLS = [
    {
        "name": "lingxi_search",
        "description": "在文脉资料库（workhome 全库 + 压缩包内文件）做语义/关键词混合检索，"
                       "返回文件路径、命中片段、相关度得分。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "检索问题或关键词"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 8},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "default": "hybrid"},
                "ext": {"type": "string", "description": "限定扩展名，如 .pdf（可空）"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "lingxi_read_document",
        "description": "按 file_id 或路径读取文档全文（由已解析文本块拼回），用于检索命中后看上下文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer"},
                "path": {"type": "string"},
                "max_chars": {"type": "integer", "default": 8000, "maximum": 60000},
            },
        },
    },
    {
        "name": "momai_chat",
        "description": "对资料库提问：先多路召回证据，再由本地模型作答，答案带 [n] 引用。"
                       "适合「这份资料里 X 是怎么规定的」这类需要综合多份文件的问题。"
                       "注意：本地模型生成需 15–90 秒，比检索慢得多；只要事实不要解释时用 momai_search。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "要问的问题"},
                "history": {
                    "type": "array",
                    "description": "可选的历史消息（最近 6 条生效），用于多轮追问",
                    "items": {
                        "type": "object",
                        "properties": {"role": {"type": "string", "enum": ["user", "assistant"]},
                                       "content": {"type": "string"}},
                    },
                },
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "keyword"], "default": "hybrid"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "momai_draft",
        "description": "按公文文体起草稿件：只用资料库事实，缺的要素一律输出【待补：xx】而不编造。"
                       "文种 report=报告 request=请示 summary=总结 plan=方案 notice=通知 letter=函。"
                       "生成需 30–120 秒。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "主题"},
                "kind": {"type": "string",
                         "enum": ["report", "request", "summary", "plan", "notice", "letter"],
                         "default": "report"},
                "points": {"type": "string", "description": "必须覆盖的要点（可空）"},
            },
            "required": ["topic"],
        },
    },
]


def _fmt_search(res):
    if not res.get("results"):
        return f"未命中（模式 {res.get('mode')}）" + (f"，原因：{res['error']}" if res.get("error") else "")
    lines = [f"命中 {res['count']} 条（模式 {res['mode']}{'，已重排' if res.get('reranked') else ''}）", ""]
    for i, r in enumerate(res["results"], 1):
        lines.append(f"[{i}] 得分 {r['score']} · {r['name']} · {r['ext']}")
        lines.append(f"    路径: {r['path']}")
        lines.append(f"    file_id={r['file_id']} 块#{r['seq']}"
                     + (f" § {r['section_path']}" if r.get("section_path") else ""))
        snip = (r.get("snippet") or "").replace("\n", " ")[:400]
        lines.append(f"    片段: {snip}")
        lines.append("")
    return "\n".join(lines)


def _read_document(args):
    fid = args.get("file_id")
    path = args.get("path")
    cap = min(int(args.get("max_chars") or 8000), 60000)
    with db.cursor(commit=False) as con:
        if fid:
            rows = con.run("""SELECT f.id, f.name, f.real_path, f.ext, f.parse_kind,
                                     f.status, coalesce(f.text_bytes,0)
                              FROM files f WHERE f.id=:i""", i=int(fid))
        elif path:
            rows = con.run("""SELECT f.id, f.name, f.real_path, f.ext, f.parse_kind,
                                     f.status, coalesce(f.text_bytes,0)
                              FROM files f WHERE f.real_path=:p OR f.name=:p LIMIT 1""", p=path)
        else:
            return "需要 file_id 或 path"
        if not rows:
            return "未找到该文档"
        fid_, name, rp, ext, kind, status, tb = rows[0]
        chunks = con.run("""SELECT seq, coalesce(heading,''), content, coalesce(section_path,'')
                            FROM chunks WHERE file_id=:i AND is_current
                            ORDER BY seq""", i=fid_)
    if not chunks:
        return (f"{name}（id={fid_}，状态 {status}，解析类型 {kind}）当前没有可用文本块；"
                f"可能是扫描件待 OCR 或该格式尚未支持。")
    buf, used = [], 0
    for seq, head, content, sp in chunks:
        piece = (f"\n## [{seq}] {head}" if head else f"\n## [{seq}]") + \
                (f"  § {sp}" if sp else "") + "\n" + content
        if used + len(piece) > cap:
            buf.append(f"\n…（已达 {cap} 字上限，共 {len(chunks)} 块）")
            break
        buf.append(piece)
        used += len(piece)
    head = f"# {name}\n路径: {rp}\n解析: {kind} · 块数 {len(chunks)} · 文本 {tb} 字节\n"
    return head + "".join(buf)


def call_tool(name, args):
    if name in ("momai_search", "lingxi_search"):
        q = (args.get("query") or args.get("q") or "").strip()
        if not q:
            return "query 不能为空", True
        res = wm_search.search(q, mode=args.get("mode") or "hybrid",
                               limit=int(args.get("limit") or 8), ext=args.get("ext") or "")
        return _fmt_search(res), False
    if name in ("momai_read_document", "lingxi_read_document"):
        return _read_document(args), False
    if name == "momai_chat":
        q = (args.get("question") or "").strip()
        if not q:
            return "question 不能为空", True
        hist = args.get("history")
        if not isinstance(hist, list):
            hist = []
        try:
            r = wm_assistant.answer(q, history=hist, mode=args.get("mode") or "hybrid")
        except Exception as e:                                  # noqa: BLE001
            return f"生成失败：{type(e).__name__}: {e}", True
        if not r.get("refs"):
            return r.get("answer") or "资料中未找到相关内容。", False
        lines = [r.get("answer") or "", "", "---", "引用："]
        for ref in r["refs"]:
            sec = f" · {ref['section_path']}" if ref.get("section_path") else ""
            lines.append(f"[{ref['n']}] {ref['name']}{sec} — {ref.get('path')}")
        lines.append(f"（证据 {r.get('evidence_chars', 0)} 字）")
        return "\n".join(lines), False
    if name == "momai_draft":
        topic = (args.get("topic") or "").strip()
        if not topic:
            return "topic 不能为空", True
        kind = args.get("kind") or "report"
        if kind not in wm_assistant.KINDS:
            return f"未知文种 {kind}，可选：{', '.join(wm_assistant.KINDS)}", True
        try:
            r = wm_assistant.draft(topic, kind=kind, points=args.get("points") or "")
        except Exception as e:                                  # noqa: BLE001
            return f"起草失败：{type(e).__name__}: {e}", True
        body = r.get("draft") or ""
        n_missing = body.count("【待补")
        lines = [f"# {r.get('kind_name', kind)}：{topic}", "", body, ""]
        if r.get("refs"):
            lines.append("---")
            lines.append(f"引用 {len(r['refs'])} 份，待补要素 {n_missing} 处：")
            for ref in r["refs"]:
                lines.append(f"[{ref['n']}] {ref['name']} — {ref.get('path')}")
        return "\n".join(lines), False
    return f"未知工具: {name}", True


def handle(msg):
    """处理单条 JSON-RPC 消息，返回响应 dict（通知返回 None）。"""
    mid = msg.get("id")
    method = msg.get("method") or ""
    params = msg.get("params") or {}

    if method.startswith("notifications/"):
        return None

    if method == "initialize":
        result = {
            "protocolVersion": PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name = params.get("name") or ""
        args = params.get("arguments") or {}
        text, is_err = call_tool(name, args)
        result = {"content": [{"type": "text", "text": text}], "isError": is_err}
    elif method in ("resources/list", "prompts/list"):
        result = {"resources": []} if method == "resources/list" else {"prompts": []}
    else:
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"Method not found: {method}"}}

    return {"jsonrpc": "2.0", "id": mid, "result": result}


def dispatch(payload):
    if isinstance(payload, list):
        out = [r for r in (handle(m) for m in payload) if r is not None]
        return out or None
    return handle(payload)
