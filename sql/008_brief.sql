-- 008_brief.sql —— 阶段 5 T5.4：工作简报
--
-- 简报是**派生数据**：同一批输入必须产出同一份简报。
-- source_hash 存输入的指纹，重跑时若指纹相同直接跳过——
-- 否则夜间任务重试会把同一份简报反复写入，日期一栏就会出现多份。
CREATE TABLE IF NOT EXISTS work_briefs (
    id          bigserial PRIMARY KEY,
    brief_date  date NOT NULL UNIQUE,
    content_md  text NOT NULL,
    data        jsonb,
    source_hash text NOT NULL,
    created_at  timestamptz DEFAULT now(),
    updated_at  timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_brief_date ON work_briefs (brief_date DESC);

GRANT SELECT, INSERT, UPDATE ON work_briefs TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE work_briefs_id_seq TO wenmai_app;
