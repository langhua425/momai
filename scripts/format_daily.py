#!/usr/bin/env python3
"""墨脉 · 格式缺口日报（每日 09:30，内容没变就不推送）

设计纪律（照抄 work_brief_auto 的 source_hash 思路）：
- 报表带 signature（各 reason_code 计数拼接的哈希）。signature 与上次相同 → 直接退出，不打扰人。
- 只有出现"需要人决策"的变化才推送：不支持格式、待 OCR 的图片、解析失败。
- 凭据只从环境变量/.env 读，脚本里不写任何 token。
- 任一环节失败都安静退出（返回码非 0 交给 systemd 记录），不猜数据。

用法：
    python3 format_daily.py            # 正常跑，需要推送才推
    python3 format_daily.py --force    # 无视 signature 强制推一次（调试用）
"""
import hashlib
import json
import os
import sys
import urllib.request

API = os.environ.get("MOMAI_API", "http://192.168.18.4:8800")
STATE = os.environ.get("MOMAI_STATE", os.path.expanduser("~/.cache/momai/format_daily.json"))
ENV_FILE = os.path.expanduser("~/.hermes/.env")


def load_env(path=ENV_FILE):
    """从 Hermes 的 .env 读飞书凭据（不打印）。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch_report():
    req = urllib.request.Request(API + "/api/policy-report", headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        d = json.loads(r.read())
    if not d.get("ok"):
        raise RuntimeError(f"接口返回失败: {d.get('error')}")
    return d["report"]


def compose(rep):
    by_code = rep.get("by_reason") or {}
    lines = [f"【墨脉 · 格式缺口日报】{rep.get('generated_at')}", ""]

    unsupported = [g for g in rep.get("groups", []) if g["reason_code"].startswith("unsupported")]
    images = [g for g in rep.get("groups", []) if g["reason_code"] == "image_needs_ocr"]
    failed = [g for g in rep.get("groups", []) if g["policy"] == "defer"]

    if unsupported:
        lines.append("① 不支持解析（需决定是否接新引擎）")
        for g in unsupported:
            lines.append(f"   · {g['reason_code']}：{g['count']} 个 / {g['mb']} MB（{g['top_ext']}）")
        lines.append("")

    if images:
        for g in images:
            lines.append(f"② 待分诊图片：{g['count']} 个 / {g['mb']} MB（{g['top_ext']}）")
        lines.append("")

    if failed:
        lines.append("③ 解析失败（待人工判断）")
        for g in failed:
            lines.append(f"   · {g['reason_code']}：{g['count']} 个（{g['top_ext']}）")
            for s in (g.get("samples") or [])[:2]:
                lines.append(f"       {s}")
        lines.append("")

    ok_n = by_code.get("indexed_ok", 0)
    dup_n = by_code.get("duplicate_content", 0)
    junk_n = by_code.get("junk_ext", 0)
    lines.append(f"已入库 {ok_n} · 内容重复 {dup_n} · 垃圾后缀 {junk_n}")
    if rep.get("unclassified"):
        lines.append(f"⚠ 未登记策略的文件：{rep['unclassified']} 个（说明有文件绕过了策略表，需排查）")
    return "\n".join(lines)


def push_feishu(text):
    app_id = os.environ.get("FEISHU_APP_ID")
    app_secret = os.environ.get("FEISHU_APP_SECRET")
    chat = os.environ.get("FEISHU_HOME_CHANNEL")
    if not (app_id and app_secret and chat):
        print("飞书凭据缺失，跳过推送", file=sys.stderr)
        return False
    body = json.dumps({"app_id": app_id, "app_secret": app_secret}).encode()
    req = urllib.request.Request(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        tok = json.loads(r.read()).get("tenant_access_token", "")
    if not tok:
        print("获取 tenant_access_token 失败", file=sys.stderr)
        return False
    payload = json.dumps({
        "receive_id": chat, "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False)}, ensure_ascii=False).encode()
    req2 = urllib.request.Request(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        data=payload, headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req2, timeout=15) as r:
        return json.loads(r.read()).get("code") == 0


def main():
    force = "--force" in sys.argv
    load_env()
    rep = fetch_report()
    sig = rep.get("signature")
    prev = {}
    if os.path.exists(STATE):
        try:
            prev = json.load(open(STATE, encoding="utf-8"))
        except Exception:
            prev = {}
    if not force and prev.get("signature") == sig:
        print(f"报表未变化（signature={sig}），不推送")
        return 0
    text = compose(rep)
    sent = push_feishu(text)
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump({"signature": sig, "pushed_at": rep.get("generated_at"), "sent": sent},
                  f, ensure_ascii=False)
    print(text if not sent else f"已推送（signature={sig}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
