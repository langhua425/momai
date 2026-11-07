-- 003_exclude_rules.sql —— 用户可管理的排除规则（T1.3）
--
-- 之前「排除」只有 sources.exclude 的**子串匹配**：看不见、改不了、也无法解释
-- 为什么某个目录被跳过。这里把它升级成显式规则表 + 命中计数。
--
-- 三类规则：
--   dir  —— 目录前缀（整枝跳过，不逐文件建记录：一个大目录逐条建行没有意义）
--   file —— 精确文件路径（会被记录成 policy=never/filename，可随时恢复）
--   ext  —— 扩展名（如 .tmp/.log；与 settings.skip_exts 并存，规则表只管用户新增的）

CREATE TABLE IF NOT EXISTS exclude_rules (
    id          bigserial PRIMARY KEY,
    kind        text        NOT NULL CHECK (kind IN ('dir', 'file', 'ext')),
    value       text        NOT NULL,
    reason      text,
    enabled     boolean     NOT NULL DEFAULT true,
    hits        bigint      NOT NULL DEFAULT 0,   -- 累计跳过多少个文件
    last_hit_at timestamptz,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (kind, value)
);

CREATE INDEX IF NOT EXISTS idx_exclude_rules_enabled ON exclude_rules (enabled) WHERE enabled;

GRANT SELECT, INSERT, UPDATE, DELETE ON exclude_rules TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE exclude_rules_id_seq TO wenmai_app;

-- 被忽略文件的「原因码」统一收口：策略表里已有的不再重复定义
COMMENT ON TABLE exclude_rules IS '用户维护的排除规则；被忽略的文件清单由 file_index_policies + 本表联合得出';
