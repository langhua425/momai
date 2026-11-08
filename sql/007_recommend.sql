-- 007_recommend.sql —— 阶段 5 T5.3：主动推荐
--
-- 设计要点：规则优先，不跑 LLM。
--   「要不要推荐」是个高置信判断，规则能覆盖八成情况，成本却是零；
--   LLM 决策在这里性价比极差（每次要读文件、要花钱、还会漂移）。
--
-- 幂等靠 source_hash：同一 (规则, 目标, 理由骨架) 只算一条，
--   夜间重跑不会把用户的收件箱刷成一片重复卡片。

CREATE TABLE IF NOT EXISTS file_views (
    id        bigserial PRIMARY KEY,
    file_id   bigint REFERENCES files(id) ON DELETE CASCADE,
    kind      text DEFAULT 'detail',
    viewed_at timestamptz DEFAULT now(),
    meta      jsonb
);
CREATE INDEX IF NOT EXISTS idx_views_file_time ON file_views (file_id, viewed_at DESC);

CREATE TABLE IF NOT EXISTS recommendations (
    id            bigserial PRIMARY KEY,
    kind          text NOT NULL,              -- new_related / doc_gap / stale_value
    target_file_id bigint REFERENCES files(id) ON DELETE CASCADE,
    title         text,
    reason        text,
    confidence    real DEFAULT 0.5,
    payload       jsonb,
    source_hash   text NOT NULL UNIQUE,
    created_at    timestamptz DEFAULT now(),
    acted_at      timestamptz,
    dismissed_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_rec_open ON recommendations (created_at DESC)
    WHERE acted_at IS NULL AND dismissed_at IS NULL;
-- 同一规则对同一文件只保留一条未处理：身份是 (kind, 文件)，不是理由文本
CREATE UNIQUE INDEX IF NOT EXISTS uq_rec_open ON recommendations (kind, target_file_id)
    WHERE acted_at IS NULL AND dismissed_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_rec_kind ON recommendations (kind);

GRANT SELECT, INSERT, UPDATE, DELETE ON file_views, recommendations TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE file_views_id_seq, recommendations_id_seq TO wenmai_app;
