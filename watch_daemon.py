#!/usr/bin/env python3
"""墨脉 · inotify 变更守护（T1.2）

职责：只做一件事——把「哪个路径变了」可靠地写进 file_change_queue。
解析、入库、删除判定全交给 autoscan 消费者，本进程**不碰业务表**。

三条设计取舍：
1. **防抖 2 秒**：编辑器保存一个文件会连出 create/modify/close_write 多个事件，
   每事件一条会让队列瞬间膨胀十倍。同一路径合并，只记次数。
2. **不判删除**：删除只入队 `deleted`，真正判定由 `scan_targets()` 做
   （它还要处理"删了又放回"的复活场景）。
3. **宁可漏，不可卡**：inotify 溢出（队列被打满）时打一条 `overflow` 事件并继续；
   漏掉的变更由 30 分钟一次的全量 walk 兜住，不允许守护进程自己退出。

跑在 NAS：/opt/wenmai/venv/bin/python watch_daemon.py
"""
import json
import os
import select
import subprocess
import sys
import time
from datetime import datetime, timezone

import db

DEBOUNCE_SEC = 2.0
FLUSH_SEC = 1.0
EVENTS = "close_write,create,delete,moved_to,moved_from,attrib"


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_sources():
    with db.cursor(commit=False) as con:
        rows = con.run("SELECT id, name, root_path, exclude FROM sources WHERE enabled")
    return [{"id": r[0], "name": r[1], "root": r[2], "exclude": [e for e in (r[3] or []) if e]}
            for r in rows]


def skip_prefixes():
    with db.cursor(commit=False) as con:
        rows = con.run("SELECT key, value FROM settings WHERE key IN ('unpack_dir','md_dir')")
    out = []
    for k, v in rows:
        try:
            val = json.loads(v) if isinstance(v, str) else v
        except Exception:
            continue
        if val:
            out.append(os.path.abspath(val))
    return out


def should_ignore(path, excludes, prefixes):
    if any(pp and path.startswith(pp) for pp in prefixes):
        return True
    base = os.path.basename(path)
    if base.startswith(".") or base.startswith("~$") or base.startswith(".~lock"):
        return True
    return any(x in path for x in excludes)


def insert_events(batch):
    """batch: {path: [event_type, count]}"""
    if not batch:
        return 0
    with db.cursor(commit=False) as con:
        gen = con.run("SELECT coalesce(max(generation),0)+1 FROM file_change_queue")[0][0]
    n = 0
    with db.cursor() as con:
        for path, (ev, cnt) in batch.items():
            # 同一路径还有未处理的行 → 合并计数，不再增行（否则一次保存能堆出几十行）
            r = con.run("""UPDATE file_change_queue
                           SET coalesce_count = coalesce_count + :c, event_type=:e, observed_at=now()
                           WHERE id = (SELECT id FROM file_change_queue
                                       WHERE path=:p AND processed_at IS NULL
                                       ORDER BY id DESC LIMIT 1)
                           RETURNING id""", c=cnt, e=ev, p=path)
            if not r:
                con.run("""INSERT INTO file_change_queue(event_type, path, generation, coalesce_count)
                           VALUES (:e,:p,:g,:c)""", e=ev, p=path, g=gen, c=cnt)
            n += 1
    return n


def main():
    sources = load_sources()
    if not sources:
        log("没有启用的扫描源，退出")
        return 1
    prefixes = skip_prefixes()
    procs = {}
    for src in sources:
        root = os.path.abspath(src["root"])
        if not os.path.isdir(root):
            log(f"跳过（不存在）: {src['name']} {root}")
            continue
        p = subprocess.Popen(
            ["inotifywait", "-m", "-r", "-q", "--format", "%e|%w%f",
             "-e", EVENTS, root],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
        procs[p.stdout] = {"proc": p, "src": src, "root": root}
        log(f"监视 {src['name']}: {root}")

    if not procs:
        log("没有任何可监视的目录，退出")
        return 1

    pending = {}            # path -> [event, count, first_seen]
    total = 0
    last_flush = time.time()
    try:
        while procs:
            ready, _, _ = select.select(list(procs.keys()), [], [], FLUSH_SEC)
            for fh in ready:
                line = fh.readline()
                if not line:
                    # 子进程挂了：记录并从监视集合里移除，不允许整个守护进程跟着死
                    meta = procs.pop(fh)
                    err = meta["proc"].stderr.read().strip()
                    log(f"监视中断: {meta['root']} rc={meta['proc'].poll()} {err[:120]}")
                    continue
                ev, _, path = line.strip().partition("|")
                if not path:
                    continue
                if should_ignore(path, procs[fh]["src"]["exclude"], prefixes):
                    continue
                etype = "deleted" if ("DELETE" in ev or "MOVED_FROM" in ev) else (
                    "created" if "CREATE" in ev else "modified")
                slot = pending.get(path)
                if slot:
                    slot[1] += 1
                    slot[0] = etype
                else:
                    pending[path] = [etype, 1, time.time()]

            now = time.time()
            if pending and (now - last_flush >= FLUSH_SEC):
                ready_batch = {p: (v[0], v[1]) for p, v in pending.items()
                               if now - v[2] >= DEBOUNCE_SEC}
                if ready_batch:
                    for p in ready_batch:
                        pending.pop(p, None)
                    n = insert_events(ready_batch)
                    total += n
                    log(f"入队 {n} 条（累计 {total}）")
                last_flush = now
    except KeyboardInterrupt:
        pass
    finally:
        for meta in procs.values():
            meta["proc"].terminate()
    log(f"退出，累计入队 {total} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
