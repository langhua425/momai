"""墨脉 · 失败分类与退避（T1.1）

问题：解析失败只写 files.error 一句，然后就永久躺在那里。
既没有"再试几次"的机制，也不区分"等会儿会好"和"永远不会好"。

两条判据：
- **transient**（等会儿会好）：超时、连接失败、5xx、限流、锁冲突 → 指数退避重试
- **poison**（永远不会好）：加密/损坏/零字节/无文本层/格式无处理器 → 一次就判死，不再烧算力

退避阶梯：1h → 6h → 24h → 72h，用完 4 次判 poison_pill。
"""
import re

BACKOFF_SEC = [3600, 21600, 86400, 259200]
MAX_ATTEMPTS = 4

# 顺序重要：先判 poison，避免"unsupported 里含 timeout"这类误判
_POISON = [
    (r"password|encrypted|加密|需要口令", "poison_encrypted"),
    (r"损坏|corrupt|crc|not a zip|BadZipFile|头错误|truncated", "poison_corrupt"),
    (r"零字节|0 ?字节|empty file|文件为空", "poison_empty"),
    (r"无文本层|no text layer|没有文字|图片型", "poison_no_text"),
    (r"unsupported|不支持|无处理器|no handler|unknown format", "poison_unsupported"),
    (r"扩展名跳过", "poison_skipped_ext"),
    (r"超出大小|too large|max_file_mb", "poison_oversize"),
    (r"乱码|garbled|框线|图纸目录", "poison_garbled"),
]

_TRANSIENT = [
    (r"timeout|timed out|超时", "transient_timeout"),
    (r"connection|refused|reset by peer|网络|unreachable|EOF", "transient_network"),
    (r"\b(429|502|503|504)\b|too many requests|rate limit", "transient_ratelimit"),
    (r"locked|deadlock|序列化|could not obtain", "transient_lock"),
    (r"temporarily|retry|稍后", "transient_temp"),
]


def classify(err, status=None, ext=None):
    """返回 (kind, reason_code)。kind ∈ transient / poison。"""
    e = (err or "").strip()
    low = e.lower()
    for pat, code in _POISON:
        if re.search(pat, low, re.I):
            return "poison", code
    for pat, code in _TRANSIENT:
        if re.search(pat, low, re.I):
            return "transient", code
    # 没见过的错误：给一次机会，但记成 unknown，方便事后看日志补规则
    if status == "skipped":
        return "poison", "poison_skipped"
    return "transient", "transient_unknown"


def next_delay(attempts, code="", kind="transient"):
    """attempts 是已尝试次数（≥1）。返回秒数，超上限返回 None。

    未知错误（transient_unknown）只给 2 次机会：没见过的错通常有稳定根因，
    重试 4 次只是浪费 4 次算力，两次仍失败就交给人看日志补规则。
    """
    if kind == "poison":
        return None                     # 永不再试，不留阶梯
    cap = 2 if code == "transient_unknown" else MAX_ATTEMPTS
    if attempts >= cap:
        return None
    return BACKOFF_SEC[min(attempts - 1, len(BACKOFF_SEC) - 1)]


def schedule(fid, err, status=None, ext=None, policy_note=None):
    """把一次失败落到 file_index_policies：计数 + 下次时间，或直接判死。

    返回 (policy, reason_code, next_retry_seconds|None)。
    """
    import db

    kind, code = classify(err, status, ext)
    with db.cursor(commit=False) as con:
        row = con.run("""SELECT coalesce(attempts,0), coalesce(max_attempts,:mx)
                         FROM file_index_policies WHERE file_id=:i""",
                      mx=MAX_ATTEMPTS, i=fid)
    attempts = int(row[0][0]) if row else 0
    attempts += 1
    delay = next_delay(attempts, code, kind)

    if kind == "poison" or delay is None:
        # 判死：不再自动重试；人工可在设置页重置 attempts 复活
        new_policy, new_code = "never", ("poison_pill" if kind == "transient" else code)
        with db.cursor() as con:
            con.run("""INSERT INTO file_index_policies
                         (file_id, policy, reason_code, reason_kind, reason_detail, attempts,
                          max_attempts, next_retry_at, updated_at)
                       VALUES (:i,:p,:c,:k,:d,:a,:mx,NULL,now())
                       ON CONFLICT (file_id) DO UPDATE SET
                         policy=:p, reason_code=:c, reason_kind=:k, reason_detail=:d,
                         attempts=:a, max_attempts=:mx, next_retry_at=NULL, updated_at=now()""",
                    i=fid, p=new_policy, c=new_code, k="poison", d=(err or "")[:300],
                    a=attempts, mx=MAX_ATTEMPTS)
        return new_policy, new_code, None

    with db.cursor() as con:
        con.run("""INSERT INTO file_index_policies
                     (file_id, policy, reason_code, reason_kind, reason_detail, attempts,
                      max_attempts, next_retry_at, updated_at)
                   VALUES (:i,'defer',:c,:k,:d,:a,:mx, now() + (:sec || ' seconds')::interval, now())
                   ON CONFLICT (file_id) DO UPDATE SET
                     policy='defer', reason_code=:c, reason_kind=:k, reason_detail=:d,
                     attempts=:a, max_attempts=:mx,
                     next_retry_at=now() + (:sec || ' seconds')::interval, updated_at=now()""",
                i=fid, c=code, k="transient", d=(err or "")[:300],
                a=attempts, mx=MAX_ATTEMPTS, sec=int(delay))
    return "defer", code, delay


def mark_ok(fid, reason="indexed_ok"):
    import db
    with db.cursor() as con:
        con.run("""INSERT INTO file_index_policies
                     (file_id, policy, reason_code, reason_kind, reason_detail, attempts,
                      max_attempts, next_retry_at, updated_at)
                   VALUES (:i,'indexed',:r,'content','重试后成功',0,:mx,NULL,now())
                   ON CONFLICT (file_id) DO UPDATE SET
                     policy='indexed', reason_code=:r, reason_kind='content',
                     reason_detail='重试后成功', next_retry_at=NULL, updated_at=now()""",
                i=fid, r=reason, mx=MAX_ATTEMPTS)


def due(limit=20):
    """到期可重试的文件：defer + 到点 + 未超上限 + 不是人工挂起的。"""
    import db
    with db.cursor(commit=False) as con:
        return con.run("""
            SELECT p.file_id, f.name, f.ext, coalesce(f.error,''), coalesce(p.attempts,0),
                   coalesce(p.reason_detail,''), coalesce(f.size,0)
            FROM file_index_policies p JOIN files f ON f.id = p.file_id
            WHERE p.policy='defer'
              AND coalesce(p.next_retry_at, now() - interval '1 minute') <= now()
              AND coalesce(p.attempts,0) < coalesce(p.max_attempts,:mx)
              AND coalesce(p.reason_code,'') <> 'manual_review'
            ORDER BY p.next_retry_at NULLS FIRST, p.file_id
            LIMIT :l""", mx=MAX_ATTEMPTS, l=limit)
