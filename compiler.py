"""编译流水线：vault(markdown) → pages/claims/chunks → claims.jsonl + digest + reports。

与 OpenClaw memory-wiki 同构：
  - 页面 frontmatter 里写 claims（含 evidence），才进结构化论断层
  - 产物：<vault>/.wenmai/claims.jsonl、agent-digest.json、log.jsonl
  - reports/ 下自动生成 claim-health / low-confidence / contradictions / open-questions /
    stale-pages / provenance-coverage 六份看板（human 块不覆盖）
"""
import os
import re
import json
import time
import hashlib
import urllib.request
from datetime import datetime, timezone, timedelta

import yaml

import db

KINDS = {"entity", "concept", "synthesis", "source", "report"}
HUMAN_START = "<!-- wenmai:human:start -->"
HUMAN_END = "<!-- wenmai:human:end -->"


# ---------------- 解析 ----------------

def split_frontmatter(text):
    if not text.startswith("---"):
        return {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        fm = yaml.safe_load(m.group(1)) or {}
    except Exception:
        fm = {}
    return (fm if isinstance(fm, dict) else {}), m.group(2)


def slugify(s):
    s = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", s.strip())
    return re.sub(r"-+", "-", s).strip("-").lower() or "untitled"


def page_id_of(fm, path, vault):
    if fm.get("id"):
        return str(fm["id"])
    rel = os.path.relpath(path, vault)
    parts = rel.split(os.sep)
    kind = fm.get("kind") or (parts[0].rstrip("s") if parts[0] in
                              ("entities", "concepts", "syntheses", "sources") else "concept")
    return f"{kind}.{slugify(os.path.splitext(parts[-1])[0])}"


def chunk_text(body, size=800, overlap=100):
    """按标题分段，再按长度切块，保留标题上下文。"""
    out, heading = [], ""
    for block in re.split(r"\n{2,}", body):
        b = block.strip()
        if not b:
            continue
        if b.startswith("#"):
            heading = b.lstrip("# ").strip()
            continue
        text = b
        while len(text) > size:
            cut = text.rfind("。", 0, size)
            cut = cut + 1 if cut > size // 2 else size
            out.append((heading, text[:cut]))
            text = text[max(0, cut - overlap):]
        if text.strip():
            out.append((heading, text))
    merged = []
    for h, t in out:
        if merged and len(merged[-1][1]) < size // 2:
            merged[-1] = (merged[-1][0], merged[-1][1] + "\n" + t)
        else:
            merged.append((h, t))
    return merged


# ---------------- 嵌入 ----------------

def embed(texts, settings):
    url = settings["embed_url"]
    model = settings["embed_model"]
    batch = max(1, int(settings.get("embed_batch", 8)))
    vecs = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        payload = json.dumps({"model": model, "input": part}).encode()
        req = urllib.request.Request(url, data=payload,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read())
        items = sorted(data["data"], key=lambda d: d.get("index", 0))
        vecs.extend([d["embedding"] for d in items])
    return vecs


# ---------------- 论断分析 ----------------

def freshness(updated, settings):
    if not updated:
        return "unknown"
    try:
        if isinstance(updated, str):
            updated = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
    except Exception:
        return "unknown"
    days = (datetime.now(timezone.utc) - updated).days
    if days <= settings["fresh_days"]:
        return "fresh"
    if days <= settings["aging_days"]:
        return "aging"
    return "stale"


def normalize_claim(raw, page, settings):
    ev = raw.get("evidence") or []
    if isinstance(ev, dict):
        ev = [ev]
    kinds = sorted({str(e.get("kind", "unknown")) for e in ev if isinstance(e, dict)})
    cid = raw.get("id") or f"claim.{page['id']}.{hashlib.sha1(str(raw.get('text','')).encode()).hexdigest()[:8]}"
    upd = raw.get("updatedAt") or raw.get("updated_at") or page["fm"].get("updatedAt")
    return {
        "claim_id": cid,
        "page_id": page["id"],
        "page_kind": page["kind"],
        "entity_type": page["fm"].get("entityType"),
        "aliases": page["aliases"],
        "text": str(raw.get("text", "")).strip(),
        "status": raw.get("status", "supported"),
        "confidence": float(raw.get("confidence", 0.5)),
        "evidence_kinds": kinds,
        "evidence_count": len(ev),
        "missing_evidence": len(ev) == 0,
        "freshness_level": freshness(upd, settings),
        "updated_at": upd,
        "raw": raw,
    }


# ---------------- 编译 ----------------

def scan_pages(vault):
    pages = []
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d != "reports"]
        for fn in filenames:
            if not fn.lower().endswith(".md"):
                continue
            p = os.path.join(dirpath, fn)
            if os.path.basename(p) in ("WIKI.md", "AGENTS.md") and os.path.dirname(p) == vault:
                continue
            try:
                raw = open(p, encoding="utf-8").read()
            except Exception:
                continue
            fm, body = split_frontmatter(raw)
            kind = fm.get("kind")
            if not kind:
                rel = os.path.relpath(p, vault).split(os.sep)[0]
                kind = {"entities": "entity", "concepts": "concept",
                        "syntheses": "synthesis", "sources": "source"}.get(rel, "concept")
            aliases = fm.get("aliases") or []
            pages.append({
                "id": page_id_of(fm, p, vault),
                "kind": kind if kind in KINDS else "concept",
                "title": fm.get("title") or os.path.splitext(os.path.basename(p))[0],
                "path": p,
                "fm": fm,
                "aliases": [str(a) for a in aliases] if isinstance(aliases, list) else [],
                "body": body,
                "hash": hashlib.sha256(raw.encode()).hexdigest(),
                "mtime": datetime.fromtimestamp(os.path.getmtime(p), timezone.utc),
            })
    return pages


def compile_vault(settings, embed_vectors=True):
    vault = settings["vault_path"]
    if not os.path.isdir(vault):
        raise RuntimeError(f"vault 路径不存在: {vault}")
    started = time.time()
    with db.cursor() as con:
        run = con.run("INSERT INTO compile_runs(status) VALUES ('running') RETURNING id")[0][0]

    pages = scan_pages(vault)
    claims, chunk_rows, rels = [], [], []
    seen_ids = set()

    for pg in pages:
        seen_ids.add(pg["id"])
        for raw in (pg["fm"].get("claims") or []):
            if isinstance(raw, dict) and str(raw.get("text", "")).strip():
                claims.append(normalize_claim(raw, pg, settings))
        for h, t in chunk_text(pg["body"], settings["chunk_size"], settings["chunk_overlap"]):
            chunk_rows.append({"page_id": pg["id"], "heading": h, "text": t})
        for link in re.findall(r"\[\[([^\]]+)\]\]", pg["body"]):
            dst = f"concept.{slugify(link)}"
            rels.append((pg["id"], dst, "wiki-link"))

    # 嵌入
    vecs = []
    if embed_vectors and chunk_rows:
        try:
            vecs = embed([c["text"] for c in chunk_rows], settings)
        except Exception as e:
            print(f"[warn] 嵌入失败，退化为纯关键词检索: {e}")
            vecs = []

    with db.cursor() as con:
        for pg in pages:
            con.run(
                """INSERT INTO pages(id,kind,title,path,entity_type,aliases,frontmatter,body,
                                    content_hash,mtime,updated_at,compiled_at)
                   VALUES (:id,:kind,:title,:path,:et,:al,:fm::jsonb,:body,:h,:mt,now(),now())
                   ON CONFLICT (id) DO UPDATE SET
                     kind=:kind, title=:title, path=:path, entity_type=:et, aliases=:al,
                     frontmatter=:fm::jsonb, body=:body, content_hash=:h, mtime=:mt,
                     updated_at=now(), compiled_at=now()""",
                id=pg["id"], kind=pg["kind"], title=pg["title"], path=pg["path"],
                et=pg["fm"].get("entityType"),
                al=pg["aliases"],
                fm=json.dumps(pg["fm"], ensure_ascii=False, default=str),
                body=pg["body"], h=pg["hash"], mt=pg["mtime"],
            )
        con.run("DELETE FROM pages WHERE NOT (id = ANY(:ids))", ids=list(seen_ids))
        con.run("DELETE FROM claims WHERE page_id = ANY(:ids)", ids=list(seen_ids))
        con.run("DELETE FROM chunks WHERE page_id = ANY(:ids)", ids=list(seen_ids))
        con.run("DELETE FROM relations")

        for c in claims:
            con.run(
                """INSERT INTO claims(claim_id,page_id,page_kind,entity_type,aliases,text,status,
                        confidence,evidence_kinds,evidence_count,missing_evidence,
                        freshness_level,updated_at,raw)
                   VALUES (:cid,:pid,:pk,:et,:al,:txt,:st,:cf,:ek,:ec,:me,:fl,:ua,:raw::jsonb)""",
                cid=c["claim_id"], pid=c["page_id"], pk=c["page_kind"], et=c["entity_type"],
                al=c["aliases"], txt=c["text"], st=c["status"], cf=c["confidence"],
                ek=c["evidence_kinds"], ec=c["evidence_count"], me=c["missing_evidence"],
                fl=c["freshness_level"],
                ua=(c["updated_at"] if isinstance(c["updated_at"], datetime) else None),
                raw=json.dumps(c["raw"], ensure_ascii=False, default=str),
            )
        for i, ch in enumerate(chunk_rows):
            con.run(
                "INSERT INTO chunks(page_id,seq,heading,text,embedding) VALUES (:p,:s,:h,:t,:e::vector)",
                p=ch["page_id"], s=i, h=ch["heading"], t=ch["text"],
                e=(db.vec_literal(vecs[i]) if vecs else None),
            )
        for s, d, r in set(rels):
            con.run("INSERT INTO relations(src_page,dst_page,rel) VALUES (:s,:d,:r) ON CONFLICT DO NOTHING",
                    s=s, d=d, r=r)

    digest = build_digest(pages, claims, chunk_rows, settings, vecs)
    write_artifacts(vault, claims, digest)
    write_reports(vault, pages, claims, settings)

    with db.cursor() as con:
        con.run("""UPDATE compile_runs SET finished_at=now(), status='ok', pages=:p, claims=:c,
                   chunks=:ch, message=:m WHERE id=:i""",
                p=len(pages), c=len(claims), ch=len(chunk_rows),
                m=f"{time.time()-started:.1f}s embed={'yes' if vecs else 'no'}", i=run)
    return {"pages": len(pages), "claims": len(claims), "chunks": len(chunk_rows),
            "embedded": bool(vecs), "seconds": round(time.time() - started, 1)}


def build_digest(pages, claims, chunks, settings, vecs):
    fresh = {"fresh": 0, "aging": 0, "stale": 0, "unknown": 0}
    for c in claims:
        fresh[c["freshness_level"]] = fresh.get(c["freshness_level"], 0) + 1
    low = [c for c in claims if c["confidence"] < settings["low_confidence"]]
    contested = [c for c in claims if c["status"] == "contested"]
    missing = [c for c in claims if c["missing_evidence"]]
    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "pageCount": len(pages),
        "claimCount": len(claims),
        "chunkCount": len(chunks),
        "embedded": bool(vecs),
        "claimHealth": {
            "freshness": fresh,
            "contested": len(contested),
            "lowConfidence": len(low),
            "missingEvidence": len(missing),
        },
        "kinds": {k: sum(1 for p in pages if p["kind"] == k) for k in sorted({p["kind"] for p in pages})},
    }


def write_artifacts(vault, claims, digest):
    d = os.path.join(vault, ".wenmai")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "claims.jsonl"), "w", encoding="utf-8") as f:
        for c in claims:
            f.write(json.dumps({
                "id": c["claim_id"], "pageId": c["page_id"], "pageKind": c["page_kind"],
                "entityType": c["entity_type"], "aliases": c["aliases"], "text": c["text"],
                "status": c["status"], "confidence": c["confidence"],
                "evidenceKinds": c["evidence_kinds"], "evidenceCount": c["evidence_count"],
                "missingEvidence": c["missing_evidence"],
                "freshnessLevel": c["freshness_level"],
            }, ensure_ascii=False) + "\n")
    with open(os.path.join(d, "agent-digest.json"), "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)
    with open(os.path.join(d, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": digest["generatedAt"], "pages": digest["pageCount"],
                            "claims": digest["claimCount"], "chunks": digest["chunkCount"]},
                           ensure_ascii=False) + "\n")


def _write_report(vault, name, title, generated_body):
    """写看板：保留 human 段不被覆盖。"""
    path = os.path.join(vault, "reports", name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    human = ""
    if os.path.exists(path):
        old = open(path, encoding="utf-8").read()
        m = re.search(re.escape(HUMAN_START) + r"(.*?)" + re.escape(HUMAN_END), old, re.S)
        if m:
            human = m.group(1)
    content = (
        f"# {title}\n\n"
        f"> 自动生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}，请勿手工改动生成块。\n\n"
        f"{generated_body.strip()}\n\n"
        f"{HUMAN_START}{human}{HUMAN_END}\n"
    )
    open(path, "w", encoding="utf-8").write(content)


def write_reports(vault, pages, claims, settings):
    low = [c for c in claims if c["confidence"] < settings["low_confidence"]]
    contested = [c for c in claims if c["status"] == "contested"]
    missing = [c for c in claims if c["missing_evidence"]]
    stale = [c for c in claims if c["freshness_level"] == "stale"]
    by_page = {}
    for c in claims:
        by_page.setdefault(c["page_id"], []).append(c)

    def table(rows, cols):
        if not rows:
            return "_（无）_"
        head = "| " + " | ".join(cols) + " |\n|" + "---|" * len(cols) + "\n"
        body = "".join("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |\n" for r in rows)
        return head + body

    _write_report(vault, "claim-health.md", "论断健康度",
                  f"总计 **{len(claims)}** 条论断 / **{len(pages)}** 个页面\n\n"
                  + table([{"状态": c["status"], "置信度": c["confidence"],
                            "新鲜度": c["freshness_level"], "页面": c["page_id"], "论断": c["text"][:60]}
                           for c in claims], ["状态", "置信度", "新鲜度", "页面", "论断"]))
    _write_report(vault, "low-confidence.md", "低置信度论断",
                  table([{"置信度": c["confidence"], "页面": c["page_id"], "论断": c["text"][:80]}
                         for c in sorted(low, key=lambda x: x["confidence"])],
                        ["置信度", "页面", "论断"]))
    _write_report(vault, "contradictions.md", "矛盾论断",
                  table([{"页面": c["page_id"], "论断": c["text"][:80], "证据": ",".join(c["evidence_kinds"])}
                         for c in contested], ["页面", "论断", "证据"]))
    _write_report(vault, "open-questions.md", "缺证据的论断",
                  table([{"页面": c["page_id"], "论断": c["text"][:80]}
                         for c in missing], ["页面", "论断"]))
    _write_report(vault, "stale-pages.md", "过期论断",
                  table([{"页面": c["page_id"], "更新于": str(c["updated_at"]), "论断": c["text"][:70]}
                         for c in stale], ["页面", "更新于", "论断"]))
    coverage = [{"页面": pid, "论断数": len(cs),
                 "有证据": sum(1 for c in cs if not c["missing_evidence"]),
                 "覆盖率": f"{100*sum(1 for c in cs if not c['missing_evidence'])/len(cs):.0f}%"}
                for pid, cs in sorted(by_page.items())]
    _write_report(vault, "provenance-coverage.md", "溯源覆盖率",
                  table(coverage, ["页面", "论断数", "有证据", "覆盖率"]))
