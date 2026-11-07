"""PG 索引层 schema（对齐 winclaw lingxi 的表语义，存储全部落 PG18 + pgvector）。

层级：sources → files（含压缩包内文件，带 container/depth 来源链）→ chunks(vector 1024)
     + entities/relations/entity_mentions（图谱）
     + memory_items/memory_evidence（记忆与证据）
     + archives（解包记录）/ jobs（任务队列）
"""

# v1（OpenClaw memory-wiki 那套）留下来的同名表，列结构不同，必须先删再建
LEGACY_DROP = [
    "DROP TABLE IF EXISTS chunks CASCADE",
    "DROP TABLE IF EXISTS claims CASCADE",
    "DROP TABLE IF EXISTS pages CASCADE",
    "DROP TABLE IF EXISTS relations CASCADE",
    "DROP TABLE IF EXISTS compile_runs CASCADE",
]

DDL = [
    """CREATE TABLE IF NOT EXISTS sources (
        id serial PRIMARY KEY,
        name text NOT NULL,
        root_path text NOT NULL UNIQUE,
        kind text DEFAULT 'dir',
        enabled boolean DEFAULT true,
        exclude jsonb DEFAULT '[]'::jsonb,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS files (
        id bigserial PRIMARY KEY,
        source_id int REFERENCES sources(id) ON DELETE SET NULL,
        path text NOT NULL,                 -- 逻辑路径（压缩包内：容器逻辑路径#内部路径）
        real_path text,                     -- 实体落盘路径
        name text NOT NULL,
        ext text,
        size bigint DEFAULT 0,
        mtime timestamptz,
        content_hash text,
        container_id bigint REFERENCES files(id) ON DELETE CASCADE,
        depth int DEFAULT 0,
        status text DEFAULT 'pending',       -- pending|parsed|ocr|failed|skipped|duplicate
        parse_kind text,
        text_bytes int DEFAULT 0,
        error text,
        version int DEFAULT 1,               -- 内容版本号，每次内容变化 +1
        dup_of bigint REFERENCES files(id) ON DELETE SET NULL,  -- 同内容副本指向正本
        changed_at timestamptz,              -- 最近一次内容变化时间（用于排队优先级）
        last_indexed_at timestamptz,
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now(),
        UNIQUE (path)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_files_hash ON files(content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_files_status ON files(status)",
    "CREATE INDEX IF NOT EXISTS idx_files_container ON files(container_id)",
    """CREATE TABLE IF NOT EXISTS chunks (
        id bigserial PRIMARY KEY,
        file_id bigint REFERENCES files(id) ON DELETE CASCADE,
        seq int NOT NULL,
        heading text,
        content text NOT NULL,
        token_count int,
        embedding vector(1024),
        section_path text,                   -- 章节路径：第三章 运营 > 第二节 绿化
        version int DEFAULT 1,               -- 属于第几版内容
        is_current boolean DEFAULT true,     -- 检索只用当前版
        UNIQUE (file_id, seq, version)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks USING gin (content gin_trgm_ops)",
    "CREATE INDEX IF NOT EXISTS idx_chunks_current ON chunks(is_current)",
    """CREATE UNIQUE INDEX IF NOT EXISTS uq_chunks_current_seq
        ON chunks(file_id, seq) WHERE is_current""",
    """CREATE TABLE IF NOT EXISTS ocr_queue (
        id bigserial PRIMARY KEY,
        file_id bigint UNIQUE REFERENCES files(id) ON DELETE CASCADE,
        status text DEFAULT 'pending',        -- pending|running|done|failed
        attempts int DEFAULT 0,
        last_error text,
        enqueued_at timestamptz DEFAULT now(),
        finished_at timestamptz
    )""",
    "CREATE INDEX IF NOT EXISTS idx_ocrq_status ON ocr_queue(status, enqueued_at DESC)",
    """CREATE TABLE IF NOT EXISTS ocr_cache (
        content_hash text PRIMARY KEY,
        md_path text,
        text_bytes int,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS md_files (
        file_id bigint PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
        path text NOT NULL,
        bytes bigint,
        updated_at timestamptz DEFAULT now()
    )""",
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
    "CREATE INDEX IF NOT EXISTS idx_chunks_vec ON chunks USING hnsw (embedding vector_cosine_ops)",
    """CREATE TABLE IF NOT EXISTS entities (
        id bigserial PRIMARY KEY,
        name text NOT NULL,
        type text DEFAULT 'concept',
        props jsonb DEFAULT '{}'::jsonb,
        mentions int DEFAULT 0,
        UNIQUE (name, type)
    )""",
    """CREATE TABLE IF NOT EXISTS relations (
        id bigserial PRIMARY KEY,
        src_id bigint REFERENCES entities(id) ON DELETE CASCADE,
        dst_id bigint REFERENCES entities(id) ON DELETE CASCADE,
        rel text NOT NULL,
        weight real DEFAULT 1,
        file_id bigint REFERENCES files(id) ON DELETE CASCADE,
        UNIQUE (src_id, dst_id, rel, file_id)
    )""",
    """CREATE TABLE IF NOT EXISTS entity_mentions (
        entity_id bigint REFERENCES entities(id) ON DELETE CASCADE,
        file_id bigint REFERENCES files(id) ON DELETE CASCADE,
        chunk_id bigint REFERENCES chunks(id) ON DELETE CASCADE,
        PRIMARY KEY (entity_id, chunk_id)
    )""",
    """CREATE TABLE IF NOT EXISTS memory_items (
        id bigserial PRIMARY KEY,
        key text NOT NULL UNIQUE,
        subject text,
        content text NOT NULL,
        kind text DEFAULT 'fact',
        confidence real DEFAULT 0.6,
        scope text DEFAULT 'default',
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS memory_evidence (
        id bigserial PRIMARY KEY,
        memory_id bigint REFERENCES memory_items(id) ON DELETE CASCADE,
        file_id bigint REFERENCES files(id) ON DELETE SET NULL,
        chunk_id bigint REFERENCES chunks(id) ON DELETE SET NULL,
        excerpt text,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS archives (
        id bigserial PRIMARY KEY,
        file_id bigint REFERENCES files(id) ON DELETE CASCADE,
        fmt text,
        unpacked_dir text,
        entries int DEFAULT 0,
        bytes bigint DEFAULT 0,
        depth int DEFAULT 0,
        status text DEFAULT 'ok',
        message text,
        created_at timestamptz DEFAULT now()
    )""",
    """CREATE TABLE IF NOT EXISTS jobs (
        id bigserial PRIMARY KEY,
        kind text NOT NULL,
        payload jsonb DEFAULT '{}'::jsonb,
        status text DEFAULT 'pending',       -- pending|running|done|failed
        attempts int DEFAULT 0,
        last_error text,
        created_at timestamptz DEFAULT now(),
        updated_at timestamptz DEFAULT now()
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)",
]

DEFAULT_SETTINGS_V2 = {
    # 站点
    "site_title": "墨脉",
    "workspace": "/tank/files/wenmai",
    # 模型接入点
    "embed_url": "http://10.10.10.1:8002/v1/embeddings",
    "embed_model": "local",
    "embed_dim": 1024,                 # bge-m3；改维度必须重建索引
    "embed_batch": 8,
    "mineru_url": "http://10.10.10.1:8004",
    "mineru_enabled": True,            # 扫描件/图片走 mineru OCR
    "mineru_poll_sec": 5,
    "vllm_url": "http://10.10.10.1:8001/v1",
    "vllm_model": "local",
    "vllm_enabled": False,             # 需要时用 vllm 做实体抽取/摘要
    # 切块
    "chunk_size": 900,
    "chunk_overlap": 120,
    # 解包
    "archive_exts": [".zip", ".tar", ".gz", ".tgz", ".tar.gz", ".rar", ".7z", ".ofd"],
    "unpack_max_depth": 3,
    "unpack_max_bytes": 20 * 1024**3,
    "unpack_dir": "/tank/files/wenmai/unpacked",
    "keep_unpacked": True,
    # 解析
    # 自动扫描（新增/变更文件自动入库 + 向量化）
    "auto_scan_enabled": True,
    "auto_scan_interval_min": 10,      # 定时器每 5 分钟醒一次，按此间隔判断是否该跑
    "auto_index_on_scan": True,        # 扫描后自动解析 + 嵌入
    "auto_index_limit": 0,             # 0 = 不限（纯排队，跑完为止）
    "auto_ocr_enabled": True,          # 扫描件自动送 mineru
    "auto_ocr_limit": 0,               # 0 = 不限（串行排队 OCR）
    "auto_scan_last_run": "",
    # 识别文本（markdown）留存：单独目录，镜像原结构，不作为扫描目录
    "md_enabled": True,
    "md_dir": "/tank/files/wenmai-md",
    "md_keep_days": 0,                 # 0 = 永久保留
    "unpack_keep_days": 14,            # 解包临时目录保留天数（可随时从原压缩包重建）
    # OCR 分档与升级
    "ocr_tier": "standard",              # 首次 OCR 用的档位（flash/basic/standard/advanced）
    "ocr_tier_fast": "flash",
    "ocr_tier_auto": True,           # 按文件大小自动选档：>2MB 用 flash
    "ocr_tier_auto_mb": 2,
    "ocr_tier_full": "standard",      # 4.1.7 在 CPU 模式不接受 advanced；升级只对"首跑 flash"的件有意义
    "ocr_upgrade_enabled": True,
    "ocr_upgrade_min_chars": 600,
    "ocr_upgrade_max_garbage": 0.12,
    "ocr_upgrade_min_cjk": 0.30,
    # 文件移动/缺失识别
    "move_detect_enabled": True,
    "missing_detect_enabled": True,
    # Qdrant 双写（PG 仍是主库，Qdrant 只做副本/加速）
    "qdrant_enabled": True,
    "qdrant_url": "http://127.0.0.1:6333",
    "qdrant_collection": "wenmai_chunks",
    "qdrant_recall_enabled": True,      # Qdrant 作为语义第二路参与 RRF（同源向量，权重下调）
    "qdrant_recall_weight": 0.6,
    # 实体/关系抽取（占 LLM 车道，默认关闭，按需 --limit 跑）
    "entity_extract_enabled": False,
    "entity_extract_limit": 3,
    # 重排（rerank）
    "rerank_url": "http://10.10.10.1:8003",
    "rerank_model": "reranker",
    "rerank_enabled": True,
    "rerank_candidates": 30,           # 融合后送进重排的候选条数
    # 增量/版本/去重
    "dedupe_enabled": True,            # 同内容文件只解析一份
    "dedupe_min_bytes": 1024,          # 小于此值不做去重（避免误伤）
    "keep_versions": True,             # 旧版本块保留（is_current=false），检索只用最新
    "parse_text_exts": [".txt", ".md", ".csv", ".json", ".xml", ".html", ".log"],
    "max_file_mb": 800,
    "skip_exts": [".exe", ".dll", ".so", ".node", ".jpg", ".jpeg", ".png", ".gif", ".bmp",
                  ".mp4", ".avi", ".rmvb", ".mp3", ".wav", ".dwg", ".db", ".sqlite",
                  ".dwl", ".dwl2", ".bak", ".tmp", ".temp", ".lnk", ".lock", ".swp"],
}
