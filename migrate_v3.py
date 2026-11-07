#!/usr/bin/env python3
"""v2 → v3 迁移：版本化 / 去重 / 变更追踪。

幂等：全部 IF NOT EXISTS。可重复执行。
"""
import sys

sys.path.insert(0, "/opt/wenmai")
import db

STEPS = [
    # files：版本、去重指针、内容变更时间、最近索引时间
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS version int DEFAULT 1",
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS dup_of bigint REFERENCES files(id) ON DELETE SET NULL",
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS changed_at timestamptz",
    "ALTER TABLE files ADD COLUMN IF NOT EXISTS last_indexed_at timestamptz",
    # chunks：版本化，检索只用 is_current
    "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS version int DEFAULT 1",
    "ALTER TABLE chunks ADD COLUMN IF NOT EXISTS is_current boolean DEFAULT true",
    "ALTER TABLE chunks DROP CONSTRAINT IF EXISTS chunks_file_id_seq_key",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_chunks_file_seq_ver ON chunks(file_id, seq, version)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_current ON chunks(is_current)",
    "CREATE INDEX IF NOT EXISTS idx_files_dup ON files(dup_of)",
    "CREATE INDEX IF NOT EXISTS idx_files_changed ON files(changed_at DESC NULLS LAST)",
    # 版本审计表
    """CREATE TABLE IF NOT EXISTS file_versions (
        id bigserial PRIMARY KEY,
        file_id bigint REFERENCES files(id) ON DELETE CASCADE,
        version int NOT NULL,
        content_hash text,
        size bigint,
        mtime timestamptz,
        text_bytes int,
        chunk_count int,
        indexed_at timestamptz DEFAULT now(),
        UNIQUE (file_id, version)
    )""",
    # 旧数据补齐：已有块视为当前版
    "UPDATE chunks SET version=1 WHERE version IS NULL",
    "UPDATE chunks SET is_current=true WHERE is_current IS NULL",
    "UPDATE files SET version=1 WHERE version IS NULL",
]

with db.cursor() as con:
    for i, sql in enumerate(STEPS, 1):
        try:
            con.run(sql)
            print(f"  [{i}/{len(STEPS)}] ok  {sql.splitlines()[0][:64]}")
        except Exception as e:
            print(f"  [{i}/{len(STEPS)}] !!  {sql.splitlines()[0][:48]} → {e}")

with db.cursor(commit=False) as con:
    cols = con.run("""SELECT table_name, column_name FROM information_schema.columns
                      WHERE table_schema='public'
                        AND ((table_name='files' AND column_name IN ('version','dup_of','changed_at','last_indexed_at'))
                          OR (table_name='chunks' AND column_name IN ('version','is_current')))
                      ORDER BY 1,2""")
    print("\n  校验字段:")
    for t, c in cols:
        print(f"    {t}.{c}")
    n = con.run("SELECT count(*) FROM chunks WHERE is_current")[0][0]
    tot = con.run("SELECT count(*) FROM chunks")[0][0]
    print(f"  块: {tot} 总数 / {n} 当前版")
    fv = con.run("SELECT count(*) FROM file_versions")[0][0]
    print(f"  file_versions 行数: {fv}")
