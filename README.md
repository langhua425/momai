# 墨脉（MoMai）

**把散落在 NAS 里的公文、图纸、合同、审计报告，变成能被 AI 检索、问答、并直接起草成稿的私有资料体系。**

墨脉是一个自建、可被 AI 调用（MCP）的私有知识库。它做三件事：

| 能力 | 说明 | 关键实现 |
| --- | --- | --- |
| **索引** | 多格式解析 + OCR + 切块 + 向量化，自动扫、自动去重、识别改名与移动 | pdf/docx/xlsx/pptx/ofd/rtf/mhtml/dbf + 无后缀魔数嗅探；MinerU 版式 OCR；递归解包 zip/ofd/rar/7z |
| **检索** | 语义 + 关键词 + 第二向量库三路加权融合，重排后收敛到文档级 | pgvector(1024) + Qdrant 副本 + jieba 分词，加权 RRF(k=60) → rerank → 按 file_id 聚合 |
| **生成** | 对话问答与公文写稿，**每条结论带可回溯引用** | 检索取证据 → 本地 LLM（8001）→ 强制 `[n]` 标注；资料没有的要素输出 `【待补：xx】` |

对外同时提供 **MCP 端点**（`/mcp/momai`，兼容旧名 `/mcp/lingxi`），因此既能用网页问，也能被 Hermes / Claude 这类 agent 直接调用。

---

## 为什么不是"再装一个 RAG"

普通 RAG 只有"切块→向量→相似度"一条线，落在真实公文库上会踩三类坑，墨脉逐条对应解决：

| 真实问题 | 直接后果 | 墨脉做法 |
| --- | --- | --- |
| 文件改名/移动后被当成新文件 | 幽灵记录、重复块、检索指到不存在的路径 | `_reconcile_moves`：**内容哈希**为主判据，同名同大小仅作兜底，多候选不猜、进人工确认队列 |
| 同一份东西存了两份（原件 + 压缩包解压副本） | 结果里出现两条一模一样的命中 | 存量补哈希 + `canon_rank`（mtime 最早 > 路径最短 > id 最小）定正本，副本降级并下线块 |
| 同一份报告多个段落各自命中 | 一页里出现 2–3 条"同一份文件" | 按 `file_id` 聚合，其余命中并入 `other_hits`，前端显示 `+N 块` |

## 实测口径（可复现的关键数字）

| 指标 | 数值 |
| --- | --- |
| 文件总量 / 已解析 | 1,728 / 574 |
| 当前版块 / 章节 | 10,183 / 3,586 |
| 嵌入维度 | **1024**（bge-m3；对比同类方案常用的 512 维，中文长文语义更细） |
| 召回 | 语义 50 + 语义 50(Qdrant) + 关键词 50 → 加权 RRF → 重排取 30 |
| 检索链路 | 单次端到端 ≈ 1.5–3 s（含重排） |
| OCR（8.2 MB 扫描件） | flash **55 s** / standard **466 s**；核显 GPU 与加线程均无收益，唯一杠杆是档位 |
| 文件级策略 | 1,728 个文件 100% 有 `policy + reason_code`，**不存在"不知道怎么没入库"的文件** |

## 架构

```
                    ┌──────────── 网页（检索 / 图谱 / 文件 / 对话写稿 / 设置）
       浏览器 ──────┤
                    └──────────── MCP 端点 /mcp/momai （agent 调用）

  Flask (8800) ── 检索编排 search.py ── pgvector(1024) ─┐
      │                │                Qdrant 副本    ├─ 加权 RRF → 8003 rerank
      │                └──── jieba 关键词 ─────────────┘
      │
      ├── 生成层 assistant.py ── 本地 vLLM(8001) ── 带引用作答 / 公文骨架起草
      │
      └── 流水线 ingest.py
            扫描 → 递归解包 → 解析(libreoffice/pymupdf/ofd) → OCR(mineru 8004)
                 → 切块 → 嵌入(8002 bge-m3) → 章节结构 → 实体/关系 → 摘要
            策略表 file_index_policies：每个文件为什么进/不进库，都可解释、可复检

  PostgreSQL 18 + pgvector 0.8.1 （主库，双时态版本 / 去重 / 策略 / 队列）
  Qdrant 1.20.0 （副本 + 第二路召回）
```

## 技术选型与理由

- **PG 为主、Qdrant 为副**：业务数据（文件、版本、策略、队列、实体）本来就在 PG，向量放同一事务里最省心；Qdrant 只当"第二条 HNSW"，参与加权融合时权重压到 0.6——两库存的是同一份 bge-m3 向量，不是两类信号，等权会让语义双票把关键词命中压死。
- **不用全量 GraphRAG**：社区摘要需要整库重算，5,000+ 文档的增量不可行；只做实体抽取 + 2-hop 扩展（LightRAG 路线），索引成本≈0。
- **不用 LLM 逐块写上下文行**：全库 1 万块 × 7 s ≈ 20 小时/轮；改用 **late chunking**（bge-m3 原生 8k，一次前向产块向量）+ 元数据前缀，零 LLM 成本拿到近似的上下文增强。
- **OCR 保留 MinerU 而不追新**：OmniDocBench v1.6 上 PaddleOCR-VL 96.34% vs MinerU2.5-Pro 95.75%，0.6 分的差距不值一套新引擎与新的部署面。

## 快速开始

```bash
# 依赖：Python 3.14 venv、pg8000、jieba、Flask；PG18 + pgvector；Qdrant；mineru；bge-m3；reranker
cp -r . /opt/momai && cd /opt/momai
python3 -m venv venv && venv/bin/pip install flask pg8000 jieba requests

# 建表 + 加数据源
venv/bin/python ingest.py init
venv/bin/python ingest.py add-source 往来文件 /tank/files/往来文件

# 扫描 → 解析 → 嵌入（一条命令跑完整条流水线）
venv/bin/python ingest.py scan
venv/bin/python ingest.py index

# 起服务
/opt/momai/venv/bin/python app.py          # 默认 8800
```

### 配置：地址一律不在代码里

服务端点（嵌入 / OCR / 生成 / 重排 / Qdrant）**代码中没有任何默认地址**，按以下优先级解析：

| 优先级 | 来源 | 用途 |
| --- | --- | --- |
| 1 | 环境变量 `MOMAI_EMBED_URL` / `MOMAI_MINERU_URL` / `MOMAI_VLLM_URL` / `MOMAI_RERANK_URL` / `MOMAI_QDRANT_URL` | 容器/多机部署，见 [`.env.example`](.env.example) |
| 2 | 数据库 `settings` 表（网页「设置」页） | 单机部署最方便的入口 |
| 3 | 都没有 | 后端抛可读错误：`未配置 embed_url：请到「设置」页填写，或设置环境变量 MOMAI_EMBED_URL` |

其余业务参数（嵌入维度、切块大小、OCR 档位、重排候选数、生成超时、策略开关）都在 `settings` 表里。

## 目录

| 路径 | 作用 |
| --- | --- |
| `search.py` | 三路召回 + 加权 RRF + 文档级聚合 |
| `assistant.py` | 对话问答与公文写稿（证据约束 + 引用） |
| `ingest.py` | 扫描/解包/解析/切块/嵌入/队列全流程 |
| `sections.py` `summarize.py` `entities.py` | 章节结构、整篇摘要、实体关系抽取 |
| `ocr_upgrade.py` | OCR 档位升级候选（乱码率/正文量/公文类型三判据） |
| `dedupe_backfill.py` | 存量补哈希 + 副本降级 |
| `qdrant_store.py` | Qdrant 双写（失败只记日志，绝不影响入库） |
| `mcp_lingxi.py` | MCP 端点实现 |
| `sql/` | 可重复执行的表结构变更 |
| `TECH-ROUTE-v1.md` | 完整技术路线与阶段计划 |

## 路线图

```
阶段0 止损与可观测   策略表·原因码·图片分诊·夜间提速      ← 策略表已完成
阶段1 流水线健壮性   退避重试·变更队列·忽略管理
阶段2 检索质量       late chunking·元数据前缀·章节向量
阶段3 对话与写稿     问答·公文起草·引用回溯             ← 后端已接入
阶段4 记忆系统       双时态事实·第四路召回·as_of 时间旅行
阶段5 图谱与主动性   多图多视图·推荐流·工作简报
```

明确不做：全量 GraphRAG 社区摘要、全库 LLM 上下文行、ColBERT 多向量、换 OCR 引擎、数字分身/邮件源。

## 已知限制

- 生成依赖本地 8001（并发 1），一次问答 7 s 起、写稿更久；前端暂用非流式 loading。
- `.dwg/.dxf/.caj/.ceb` 无解析能力（392 个文件，已在策略表中显式标注 `unsupported_cad`）。
- 实体抽取与摘要积压需夜间空闲窗口跑（8001 白天被对话占用）。
- 扫描目录若挂在 NFS/SMB 上，`inotify` 变更队列失效，需回退轮询。

## 许可

[MIT](LICENSE) —— 可自由使用、修改、商用，保留版权声明即可。
