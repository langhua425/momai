"""数据层：pg8000 连接 + 表结构 + 设置读写。纯 Python，无编译依赖。"""
import os
import json
import contextlib
import pg8000.native

DSN = dict(
    user=os.environ.get("WENMAI_DB_USER", "wenmai_app"),
    password=os.environ.get("WENMAI_DB_PASS", "wenmai_local_dev"),
    host=os.environ.get("WENMAI_DB_HOST", "127.0.0.1"),
    port=int(os.environ.get("WENMAI_DB_PORT", "5432")),
    database=os.environ.get("WENMAI_DB_NAME", "wenmai"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id            text PRIMARY KEY,
    kind          text NOT NULL DEFAULT 'concept',
    title         text,
    path          text NOT NULL,
    entity_type   text,
    aliases       text[] DEFAULT '{}',
    frontmatter   jsonb DEFAULT '{}'::jsonb,
    body          text DEFAULT '',
    content_hash  text,
    mtime         timestamptz,
    updated_at    timestamptz DEFAULT now(),
    compiled_at   timestamptz
);

CREATE TABLE IF NOT EXISTS claims (
    claim_id        text PRIMARY KEY,
    page_id         text REFERENCES pages(id) ON DELETE CASCADE,
    page_kind       text,
    entity_type     text,
    aliases         text[] DEFAULT '{}',
    text            text NOT NULL,
    status          text DEFAULT 'supported',
    confidence      real DEFAULT 0.5,
    evidence_kinds  text[] DEFAULT '{}',
    evidence_count  int DEFAULT 0,
    missing_evidence boolean DEFAULT false,
    freshness_level text DEFAULT 'unknown',
    updated_at      timestamptz,
    raw             jsonb DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS chunks (
    id        serial PRIMARY KEY,
    page_id   text REFERENCES pages(id) ON DELETE CASCADE,
    seq       int,
    heading   text,
    text      text NOT NULL,
    embedding vector(1024),
    UNIQUE (page_id, seq)
);

CREATE TABLE IF NOT EXISTS relations (
    id        serial PRIMARY KEY,
    src_page  text,
    dst_page  text,
    rel       text,
    UNIQUE (src_page, dst_page, rel)
);

CREATE TABLE IF NOT EXISTS settings (
    key        text PRIMARY KEY,
    value      jsonb NOT NULL,
    updated_at timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS compile_runs (
    id          serial PRIMARY KEY,
    started_at  timestamptz DEFAULT now(),
    finished_at timestamptz,
    status      text DEFAULT 'running',
    pages       int DEFAULT 0,
    claims      int DEFAULT 0,
    chunks      int DEFAULT 0,
    message     text
);

CREATE INDEX IF NOT EXISTS idx_pages_kind ON pages(kind);
CREATE INDEX IF NOT EXISTS idx_claims_page ON claims(page_id);
CREATE INDEX IF NOT EXISTS idx_claims_status ON claims(status);
CREATE INDEX IF NOT EXISTS idx_chunks_page ON chunks(page_id);
CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_pages_trgm ON pages USING gin (body gin_trgm_ops);
"""

DEFAULT_SETTINGS = {
    "site_title": "灵犀文脉",
    "vault_path": "/tank/files/wenmai/vault",
    "embed_url": "http://10.10.10.1:8002/v1/embeddings",
    "embed_model": "local",
    "embed_dim": 1024,
    "embed_batch": 8,
    "chunk_size": 800,
    "chunk_overlap": 100,
    "auto_compile": False,
    "fresh_days": 90,
    "aging_days": 270,
    "low_confidence": 0.5,
    "default_mode": "hybrid",
    "lan_only": True,
}


def connect():
    return pg8000.native.Connection(**DSN)


@contextlib.contextmanager
def cursor(commit=True):
    con = connect()
    try:
        yield con
        if commit:
            con.run("COMMIT")
    except Exception:
        with contextlib.suppress(Exception):
            con.run("ROLLBACK")
        raise
    finally:
        con.close()


def init_schema():
    with cursor() as con:
        for stmt in [s.strip() for s in SCHEMA.split(";") if s.strip()]:
            con.run(stmt)


def get_settings():
    out = dict(DEFAULT_SETTINGS)
    with cursor(commit=False) as con:
        rows = con.run("SELECT key, value FROM settings")
    for k, v in rows:
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except Exception:
                out[k] = v
        else:
            out[k] = v
    return out


def save_settings(patch: dict):
    with cursor() as con:
        for k, v in patch.items():
            if k not in DEFAULT_SETTINGS:
                continue
            con.run(
                "INSERT INTO settings(key, value) VALUES (:k, :v::jsonb) "
                "ON CONFLICT (key) DO UPDATE SET value = :v::jsonb, updated_at = now()",
                k=k, v=json.dumps(v, ensure_ascii=False),
            )
    return get_settings()


def vec_literal(vec):
    return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
