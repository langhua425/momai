-- 006_entity_vec.sql —— 阶段 5 T5.2：实体向量 + 2-hop 扩展
--
-- 为什么给实体单独建向量，而不是复用块向量：
--   块向量回答「哪段文字像这个问题」，实体向量回答「这个查询指向图谱里哪个节点」。
--   前者是文本相似，后者是实体消解。混用会把「静湖」和「静湖西岸绿化带」当成同一件事。
--
-- 为什么只做 2-hop 而不是全图扩展：
--   1-hop 只能拿到直接关联，问「静湖水质」找不出「排口巡查」；
--   3-hop 会把整个图谱拖进来（491 条关系 × 平均度数，两跳就已覆盖大半），
--   噪声压过信号。2-hop 是「一层的邻居的邻居」——相关但不同主题的边界。

ALTER TABLE entities ADD COLUMN IF NOT EXISTS embedding vector(1024);
ALTER TABLE entities ADD COLUMN IF NOT EXISTS embed_ver text;

CREATE INDEX IF NOT EXISTS idx_entities_vec ON entities
    USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
CREATE INDEX IF NOT EXISTS idx_relations_src ON relations (src_id);
CREATE INDEX IF NOT EXISTS idx_relations_dst ON relations (dst_id);
CREATE INDEX IF NOT EXISTS idx_mentions_entity ON entity_mentions (entity_id);

GRANT SELECT, UPDATE ON entities TO wenmai_app;

COMMENT ON COLUMN entities.embedding IS '实体名+类型的向量，用于查询->实体消解；与块向量用途不同，不可混用';
