-- 004_embed_v2.sql —— 阶段 2：向量方案 A/B 列 + 章节向量
--
-- 为什么保留多列而不是直接覆盖：
-- ① 三种方案（原样 / 元数据前缀 / 窗口混合）必须同语料同查询对比，覆盖就没法 A/B；
-- ② 万一新方案更差，可以一行 SQL 回退，不需要再跑一次 15 分钟的重建。
-- 存储代价：10235 块 × 3 × 4 KB ≈ 120 MB，相对 1.6 TB 余量可忽略。

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embed_text    text;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embed_ver     text;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_v1  vector(1024);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_meta vector(1024);
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS embedding_late vector(1024);

-- 章节级向量（T2.4）：按章节内块向量取均值，不需要额外调用嵌入服务
CREATE TABLE IF NOT EXISTS file_section_vectors (
    section_id  bigint PRIMARY KEY REFERENCES file_sections(id) ON DELETE CASCADE,
    file_id     bigint NOT NULL,
    embedding   vector(1024) NOT NULL,
    chunk_count int NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fsv_file ON file_section_vectors (file_id);
CREATE INDEX IF NOT EXISTS idx_fsv_hnsw ON file_section_vectors
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);

GRANT SELECT, INSERT, UPDATE, DELETE ON file_section_vectors TO wenmai_app;

COMMENT ON COLUMN chunks.embed_text IS '实际送去嵌入的文本（含元数据前缀）；content 仍是展示/关键词用的原文';
COMMENT ON COLUMN chunks.embedding_late IS '窗口混合：0.75×本块向量 + 0.25×上下文窗口向量';
