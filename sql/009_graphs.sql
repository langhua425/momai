-- 009_graphs.sql —— 阶段 5 T5.1：多图 / 多视图
--
-- 为什么需要「多图」：全库 518 个实体、491 条关系的图没有可读性，
-- 也没法回答「这个项目里谁和谁有关系」。图应当有范围。
--
-- graphs      = 一个范围内的子图（范围是可保存的 scope，不是物化的副本）
-- graph_subviews = 同一张图上的筛法（只看机构 / 只看提及≥5 / 只看某类关系）
--
-- 刻意**不物化**实体和边：范围一变就得重建，而按 scope 现查只要几十毫秒。
-- 物化只有在查询代价真的高到不可接受时才值得。
CREATE TABLE IF NOT EXISTS graphs (
    id         bigserial PRIMARY KEY,
    name       text NOT NULL UNIQUE,
    scope      jsonb NOT NULL DEFAULT '{}'::jsonb,
    note       text,
    created_at timestamptz DEFAULT now(),
    built_at   timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS graph_subviews (
    id         bigserial PRIMARY KEY,
    graph_id   bigint NOT NULL REFERENCES graphs(id) ON DELETE CASCADE,
    name       text NOT NULL,
    filters    jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT now(),
    UNIQUE (graph_id, name)
);

CREATE INDEX IF NOT EXISTS idx_subview_graph ON graph_subviews (graph_id);

GRANT SELECT, INSERT, UPDATE, DELETE ON graphs TO wenmai_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON graph_subviews TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE graphs_id_seq TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE graph_subviews_id_seq TO wenmai_app;

-- 默认图：全库
INSERT INTO graphs (name, scope, note)
VALUES ('全库', '{}'::jsonb, '不设范围，all entities')
ON CONFLICT (name) DO NOTHING;
