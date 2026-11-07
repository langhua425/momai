-- 阶段 0.1：文件处理策略表（对标 winclaw file_index_policies 的语义，按我们需要裁剪）
-- 目的：任何文件"为什么没进库"都必须有一条结构化记录，不允许静默跳过。
-- 幂等：IF NOT EXISTS；回填用 upsert，可重复执行。

CREATE TABLE IF NOT EXISTS file_index_policies (
    file_id          BIGINT PRIMARY KEY,
    policy           TEXT NOT NULL,          -- indexed | ocr_candidate | defer | never
    reason_code      TEXT NOT NULL,          -- 机器可读，报表按此聚合
    reason_kind      TEXT,                   -- format | size | policy | error | content
    reason_detail    TEXT,                   -- 人读的补充（原 error 文本）
    detected_stage   TEXT,                   -- scan | extract | ocr | index
    attempts         INT  NOT NULL DEFAULT 0,
    max_attempts     INT  NOT NULL DEFAULT 3,
    next_retry_at    TIMESTAMPTZ,
    next_cleanup_at  TIMESTAMPTZ,
    observed_mtime   TIMESTAMPTZ,
    observed_size    BIGINT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_fip_reason ON file_index_policies (reason_code);
CREATE INDEX IF NOT EXISTS idx_fip_policy ON file_index_policies (policy);

-- 回填：按 files 现状给每个文件一条策略记录
INSERT INTO file_index_policies
    (file_id, policy, reason_code, reason_kind, reason_detail, detected_stage,
     observed_mtime, observed_size)
SELECT f.id,
       CASE
         WHEN f.status = 'parsed'    THEN 'indexed'
         WHEN f.status = 'duplicate' THEN 'never'
         WHEN lower(coalesce(f.ext,'')) IN ('.jpg','.jpeg','.png','.bmp','.tif','.tiff','.gif')
              THEN 'ocr_candidate'
         WHEN lower(coalesce(f.ext,'')) IN ('.dwg','.dxf','.caj','.ceb','.shp','.mxd')
              THEN 'never'
         ELSE 'defer'
       END,
       CASE
         WHEN f.status = 'parsed'    THEN 'indexed_ok'
         WHEN f.status = 'duplicate' THEN 'duplicate_content'
         WHEN lower(coalesce(f.ext,'')) IN ('.jpg','.jpeg','.png','.bmp','.tif','.tiff')
              THEN 'image_needs_ocr'
         WHEN lower(coalesce(f.ext,'')) IN ('.gif') THEN 'junk_ext'
         WHEN lower(coalesce(f.ext,'')) IN ('.dwg','.dxf') THEN 'unsupported_cad'
         WHEN lower(coalesce(f.ext,'')) IN ('.caj','.ceb','.shp','.mxd') THEN 'unsupported_format'
         WHEN lower(coalesce(f.ext,'')) IN ('.db','.sqlite','.bak','.exe','.dll','.so','.tmp','.log~','.lnk','.lock','.swp','.dmp')
              THEN 'junk_ext'
         WHEN coalesce(f.error,'') LIKE '%扩展名%' THEN 'excluded_ext'
         WHEN coalesce(f.error,'') <> ''          THEN 'parse_failed'
         WHEN f.status = 'skipped'                THEN 'no_text_layer'
         ELSE 'unknown_not_indexed'
       END,
       CASE
         WHEN f.status IN ('parsed','duplicate') THEN 'content'
         WHEN lower(coalesce(f.ext,'')) IN ('.dwg','.dxf','.caj','.ceb','.shp','.mxd') THEN 'format'
         WHEN coalesce(f.error,'') <> '' THEN 'error'
         ELSE 'policy'
       END,
       nullif(left(coalesce(f.error,''), 400), ''),
       CASE
         WHEN f.status = 'parsed' THEN 'index'
         WHEN coalesce(f.ext,'') IN ('.pdf','.png','.jpg','.jpeg','.tif','.tiff','.bmp') THEN 'ocr'
         ELSE 'extract'
       END,
       f.mtime, f.size
FROM files f
ON CONFLICT (file_id) DO UPDATE SET
    policy = EXCLUDED.policy,
    reason_code = EXCLUDED.reason_code,
    reason_kind = EXCLUDED.reason_kind,
    reason_detail = EXCLUDED.reason_detail,
    detected_stage = EXCLUDED.detected_stage,
    observed_mtime = EXCLUDED.observed_mtime,
    observed_size = EXCLUDED.observed_size,
    updated_at = now();
