-- 002_file_change_queue.sql —— 文件变更事件队列（T1.2）
--
-- 为什么需要：每 5 分钟全量 walk 一遍目录比对 mtime/size，文件量一涨就成了瓶颈，
-- 而且从「文件被改」到「库里看到」最多要等 5 分钟。
-- inotify 事件入队 → 消费者按代际认领 → 只处理变动路径，延迟降到秒级。
--
-- 全量 walk 不取消：`_mark_missing` 的判定依赖「本轮见过的完整集合」，
-- 纯增量永远发现不了删除。所以职责拆开——事件管**及时性**，全量 walk 管**一致性**。

CREATE TABLE IF NOT EXISTS file_change_queue (
    id              bigserial PRIMARY KEY,
    event_type      text        NOT NULL,          -- created|modified|moved|deleted|attrib
    path            text        NOT NULL,
    source_id       bigint,
    generation      bigint      NOT NULL DEFAULT 1, -- 代际：同一批认领的编号
    observed_at     timestamptz NOT NULL DEFAULT now(),
    claimed_at      timestamptz,                   -- 被 worker 认领时间（防止重复处理）
    processed_at    timestamptz,                   -- 处理完成时间
    coalesce_count  int         NOT NULL DEFAULT 1,-- 防抖合并次数（同一路径 2s 内多次事件）
    note            text
);

-- 只索引未处理的行：队列绝大多数记录会被标记 processed，全量索引会白白膨胀
CREATE INDEX IF NOT EXISTS idx_fcq_pending
    ON file_change_queue (generation, id) WHERE processed_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_fcq_path_pending
    ON file_change_queue (path) WHERE processed_at IS NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON file_change_queue TO wenmai_app;
GRANT USAGE, SELECT ON SEQUENCE file_change_queue_id_seq TO wenmai_app;
