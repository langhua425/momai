# 墨脉 MoMai · 技术路线定版 v2（2026-11-07）

约束：NAS 8505（6 线程/61G）、PG18+pgvector、bge-m3 1024 维、mineru 4.1.7 CPU、
8001 本地 LLM 并发 1（单次约 7s）、8003 重排 n_ctx 8192。

## 三条平衡点原则

1. **LLM 密集工序一律绕开**：10,183 块若逐块让 LLM 写上下文行 = 约 20 小时/轮，且长期占用对话通道。
   改用 late chunking（bge-m3 原生 8k，一次前向）+ 元数据前缀（零 LLM 成本）。
2. **图谱不做全量 GraphRAG**：社区摘要需整库重算，5,000+ 文档下增量不可用；LightRAG 只做实体抽取。
   我们已有 262 实体 / 226 关系，走 2-hop 扩展，索引成本≈0。
3. **记忆不重造 18 张表**：Hermes holographic（双时态 + 来源权威 + 信任演化 + 墓碑 + as_of 查询）
   已是 Zep/Graphiti 同代语义，照抄 3 张表即可。

## 阶段 0 · 止损与可观测（0.5 天）

| 项 | 方案 | 外部依据 | 验收 |
|---|---|---|---|
| 0.1 ✅ | file_index_policies：policy/reason_code/reason_kind/detected_stage/attempts/next_retry_at | winclaw 同名字段；成熟文档流水线均保留"为什么没处理"的结构化原因 | 1,728 文件 0 条无记录（已完成） |
| 0.2 | /api/format-report 按 reason_code 聚合 + 飞书日报 | — | 报表可列出每类原因与数量 |
| 0.3 | 图片分诊：680 张 ocr_candidate 抽样 20 张实测再定阈值（≥300KB 且像扫描页才 OCR） | MinerU2.5-Pro 95.75%（OmniDocBench v1.6），比 PaddleOCR-VL 96.34% 仅差 0.6 分，不换引擎 | 每张图片有确定 policy |
| 0.4 | 夜间批跑提速：sum-limit 6→60、limit 4→20（仅 1–7 点） | 章节摘要积压 3,569 节：36/h→≈220/h，99h→16h | 一周内积压 <500 |

## 阶段 1 · 流水线健壮性（1 天）

| 项 | 方案 | 外部依据 |
|---|---|---|
| 1.1 | 退避重试：attempts/max_attempts/next_retry_at/retry_class；瞬时错误（超时/429/503）指数退避+抖动，非瞬时（格式不支持/空文本）直接进 dead-letter 不重试 | 文档流水线错误处理共识：poison pill 分类 + exponential backoff with jitter，capped |
| 1.2 | inotifywait → file_change_queue（防抖 2s、按代际分批认领），5 分钟全量 walk 降为兜底 | 事件驱动优于轮询；注意 inotify 对 NFS/SMB 无效——源在 NAS 本地 ZFS 可用，若将来挂网络盘必须回退轮询。NAS max_user_watches=507,183 足够，需 apt 装 inotify-tools |
| 1.3 | ignored_files / excluded_files（按路径/哈希/大小）+ 前端管理页 | 排除要可回滚，不能只在扫描参数里写子串 |

## 阶段 2 · 检索质量（1.5 天）

| 项 | 方案 | 外部依据 |
|---|---|---|
| 2.1 | late chunking：整篇过 bge-m3（8k），按块做 mean-pooling 得块向量 | arXiv 2504.19754 系统评估 late chunking vs contextual retrieval 的效果与效率 |
| 2.2 | 块文本前置元数据（标题路径/文档类型/日期/来源）再嵌入 | Anthropic contextual retrieval：失败检索 −49%，加重排 −67%；Unstructured 换更好的 prompt 在财务文档上 −84%（其 Anthropic 原版 prompt 为 −47%） |
| 2.2b | 若 A/B 仍不达标，仅对公文类文档用 LLM 生成上下文行（约 3,400 块，夜间跑 2 晚） | 同上；避免全库 1 万块 |
| 2.3 | HyDE 只做「三路全空」兜底，不做默认路径 | 实测：HyDE 在概念类胜出，但在**专有知识语料上失败**（LLM 猜不准术语）；我们的语料正是专有公文 |
| 2.4 | 章节级向量 file_section_vectors（late chunking 顺带产出） | 长文档按章检索；HiChunk(ACL 2026) 层级切分对证据稀疏型问答有增益 |
| 2.5 | 重排保持现状（bge-reranker-v2-m3 类） | 重排带来的增益通常大于把向量模型翻倍；已核：块 p95=900 字符、重排器 n_ctx=8192 → **无 512 截断风险** |

## 阶段 3 · 对话与写稿（1 天，已完成后端）

产品定位从"检索后台"升级为"资料库 + 对话 + 写稿"：既能被 agent 通过 MCP 调用，也有自己的网页对话界面。

| 项 | 方案 | 外部依据 | 状态 |
|---|---|---|---|
| 3.1 | `/api/chat`：三路召回取证据 → 上下文注入 → 生成 → 强制 `[n]` 引用 | Anthropic / Unstructured：先检索再生成、引用可回溯，是降幻觉的关键 | ✅ 实测 29 s / 8 引用 |
| 3.2 | `/api/draft`：6 种公文文体骨架（报告/请示/总结/方案/通知/函），资料没有的要素输出 `【待补：xx】` | 缺要素显式标缺，是防编造数字/文号的唯一可靠手段 | ✅ 实测 73 s / 5 处待补 |
| 3.3 | 前端对话页：问答/写稿双模、引用可点开原文、草稿一键复制 | — | 🟡 已部署，待视觉验收 |
| 3.4 | SSE 流式输出（首 token <2 s） | 8001 单次 7 s 起，流式是体验关键 | ⬜ |
| 3.5 | MCP 工具扩展 `momai_chat` / `momai_draft` | 让 agent 直接调用对话与写稿 | ⬜ |

设计纪律：**绝不裸生成**——没有证据就回答"资料中未找到"，不猜测、不用常识补。

## 阶段 4 · 记忆系统（2 天）

| 项 | 方案 | 外部依据 |
|---|---|---|
| 3.1 | 3 张表：memory_items（双时态 valid_from/valid_to/status + authority + confidence + scope_key）、memory_revisions、memory_evidence | Zep/Graphiti 双时态边模型（LongMemEval 90.2% / 104ms p50） |
| 3.2 | 写入管道：章节摘要/实体产物 + 用户显式「记住」 | Mem0 式 candidate→reconcile（提取候选事实并与既有事实对齐） |
| 3.3 | 第四路召回参与加权 RRF（权重 0.5） | 混合召回 + RRF 已是生产默认 |
| 3.4 | as_of 时间点检索（"去年这条规定是什么"） | 双时态的核心价值；Mem0 94.4% vs Zep 90.2% 的分差已入噪声区，真正差异在时间语义与成本 |

## 阶段 5 · 图谱与主动性（2.5 天）

| 项 | 方案 | 外部依据 |
|---|---|---|
| 4.1 | graphs / graph_subviews 多图多视图 + 实体向量 + 2-hop 扩展 | LightRAG 只做实体抽取不做社区摘要，索引成本显著低于 GraphRAG；GraphRAG 全量重建在 5,000+ 文档规模不可行 |
| 4.2 | recommendations：规则优先（新入库相关/同项目配套缺件/长期未访问）+ confidence + reason + acted_at | 主动性系统研究：是否需要 LLM 决策存疑，规则+反馈闭环成本更低 |
| 4.3 | work_brief_auto：夜间生成，source_hash 幂等 | 同窗口同内容不重生成 |

## 明确不做

| 不做 | 理由 |
|---|---|
| 全量 GraphRAG 社区摘要 | 增量重算代价不可接受 |
| LLM 逐块上下文行（全库） | 约 20 小时/轮，长期占 LLM 通道 |
| ColBERT 多向量 | 存储 ×10，收益接近已有重排 |
| 换 PaddleOCR-VL | 96.34% vs 95.75%，差 0.6 分不值一套新引擎 |
| 数字分身 / 邮件源 / 对话源 | 与"找资料"核心无关 |
| winclaw 式 18 张 memory 表 | Hermes 语义更强，重造浪费 |
