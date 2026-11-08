-- 005_memory_v2.sql —— 阶段 4：双时态记忆
--
-- 为什么要双时态（而不是简单加个 updated_at）：
--   「静湖水质标准 2018 年是Ⅳ类，2024 年提到Ⅲ类」——这不是一条事实被改写，
--   而是两条事实各自在不同时间区间成立。只留最新值的系统，无法回答
--   「去年这条规定是什么」，而这类问题在公文/合规场景是刚需。
--
-- 两个时间轴必须分开，混在一起就废了：
--   valid_from / valid_to  = 事实在现实世界成立的时间（业务时间）
--   created_at / updated_at = 这条记录进系统/被修改的时间（系统时间）
--   例：2024 年的新标准，2026 年才归档 → valid_from=2024-01-01, created_at=2026-xx
--   只用 valid_* 会误判「当时系统里没有」，只用 created_* 会误判「2024 年就生效」。

ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_from  timestamptz;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS valid_to    timestamptz;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS status      text DEFAULT 'active';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS authority   text DEFAULT 'record';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS scope_key   text DEFAULT 'default';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS source_kind text DEFAULT 'manual';
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS superseded_by bigint;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS embedding   vector(1024);

-- 存量行补齐：没有 valid_from 视为「一直有效」（宁可多召回，不可漏）
UPDATE memory_items SET valid_from = coalesce(valid_from, created_at, now())
 WHERE valid_from IS NULL;

-- 冲突判定要按 key + scope 分组；旧 UNIQUE(key) 会挡住「同 key 不同 scope/时段的多条历史」
ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS memory_items_key_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_active_key
    ON memory_items (key, scope_key) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_memory_valid ON memory_items (valid_from, valid_to);
CREATE INDEX IF NOT EXISTS idx_memory_scope_status ON memory_items (scope_key, status);
CREATE INDEX IF NOT EXISTS idx_memory_trgm ON memory_items USING gin (content gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_memory_vec ON memory_items
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

-- 修订史：每次内容/有效期变化都留一条旧值，用于「谁在什么时候改了口径」
CREATE TABLE IF NOT EXISTS memory_revisions (
    id          bigserial PRIMARY KEY,
    memory_id   bigint REFERENCES memory_items(id) ON DELETE CASCADE,
    content     text,
    valid_from  timestamptz,
    valid_to    timestamptz,
    status      text,
    authority   text,
    confidence  real,
    changed_by  text DEFAULT 'system',
    reason      text,
    changed_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_memrev_memory ON memory_revisions (memory_id, changed_at DESC);

GRANT SELECT, INSERT, UPDATE, DELETE ON memory_items, memory_evidence, memory_revisions TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE memory_revisions_id_seq TO wenmai_app;

COMMENT ON COLUMN memory_items.valid_from IS '事实在现实世界开始成立的时间（业务时间），与 created_at 的记录时间轴分开';
COMMENT ON COLUMN memory_items.authority IS '来源权威：official(红头/正式文件) > record(台账记录) > summary(机器摘要) > user(口述)';
