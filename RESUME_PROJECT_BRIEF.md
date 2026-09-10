# AgentRoute 简历项目描述（可直接复制）

> 基于项目**真实代码**撰写，量化指标为工程事实（可复现），不夸大未实现的能力（如真实预订）。 提供三种长度版本，按简历版式取用。

---

## 版本 A：一句话（用于简历项目标题行）

**AgentRoute — 企业级 Multi-Agent 智能行程规划平台** 基于 LangGraph 状态机编排的多智能体系统，集成 MCP 工具层、Agentic RAG、全链路可观测与 HITL 人审，具备生产级可靠性与合规能力。

---

## 版本 B：STAR 精简版（3-4 条要点，推荐主用）

**AgentRoute · 企业级 Multi-Agent 行程规划平台**（个人项目 / 主导开发） `Python 3.13` `LangGraph` `FastAPI` `Celery` `Redis` `PostgreSQL` `MCP` `RAG` `LLM-as-judge` `Docker`

- **架构**：设计并实现三层 Multi-Agent 编排——Planner 总控调度 Weather/POI/Hotel 三领域专家**并行执行**（LangGraph Fan-Out/Fan-In），Route 节点汇聚，经 RAG 内容增强后合成结构化行程；关键节点 best-effort 降级，单点失败不影响整体出稿。
- **工程可靠性**：基于 PostgreSQL + Redis 双写 Checkpointer 实现会话状态持久化（重启不丢状态）；tenacity 重试 + 超时 + 熔断，并对配额/参数类**永久错误快速失败**，避免无效重试成倍消耗 token 与第三方配额。
- **质量评估门禁**：搭建三层评估回环（计划质量 / RAG 检索 / LLM-as-judge），量化指标（recall@4、faithfulness、context precision 等）经 `--strict` 模式作为 CI 合并门禁——分数不达阈值即阻断合并；判官不可用时优雅降级为词面代理、仅报告不误判。
- **长期记忆 + 多模型网关**：实现跨会话用户偏好画像（按 user_id 持久化、规划前补全未填字段、列表容量上限防膨胀）；LLM 调用经网关做多模型故障转移 + per-user token 预算护栏与用量记账，指标上报 Prometheus。
- **可观测 & 人审**：接入 structlog + Prometheus + Grafana + Flower + Sentry + LangSmith 全链路追踪；通过 LangGraph `interrupt()` 实现人在回路（HITL）审核断点，自动化流程（Celery/CLI）透传不打断。
- **检索质量**：自研 Agentic RAG 管线（chunk→embedding→BM25→向量→rerank→query rewrite），内建 recall@k / precision@k / MRR / faithfulness 评估与多路消融基准，量化验证检索策略收益。
- **安全合规**：入口 prompt injection 清洗节点 + 子进程 sandbox 隔离 + 审计日志 + JWT 鉴权，面向企业/金融场景的安全底座。

---

## 版本 C：详细版（用于作品集 / 项目详情页）

### 项目概述

AgentRoute 是一套**可复用的企业级 Multi-Agent 平台底座**，以「智能旅行助手」为业务载体：用户用自然语言描述出行需求，系统自动编排多个领域智能体，调用高德地图 MCP 服务查询天气、搜索景点酒店、规划路线，并结合 RAG 知识检索，最终输出结构化行程方案（JSON + 可读文本 + 可下载 Markdown）。

### 我的职责与技术实现

**1. 多智能体编排（LangGraph StateGraph）**

- 设计三层嵌套编排：Planner 总控 → 领域专家 Agent → MCP 工具层。
- Weather/POI/Hotel 三个采集节点在同一 super-step **并行执行**，全部完成后 Fan-In 汇聚到 Route 节点；后续经 review（人审）→ RAG → synthesize → geocode → 路线几何优化。
- 支持**多轮修改**：复用同一 thread 的 Checkpointer，携带修改意见时直接重整合、跳过重复采集，节省 token。

**2. 生产级可靠性**

- PostgreSQL + Redis Checkpointer 持久化会话状态，服务重启不丢进度。
- tenacity 指数退避重试 + 超时 + 熔断；区分**瞬时错误**（重试）与**永久错误**（配额超限/非法参数/递归死循环——一次即止，快速失败）。
- MCP 连接层按领域分发最小工具集，主路 DashScope 托管 + 回退高德官方 Key，保障「内容一定拿得到」。

**3. Agentic RAG 检索管线**

- chunk → embedding → BM25 + 向量混合检索 → rerank → query rewrite 全流程。
- 内建检索质量评估（recall@k / precision@k / MRR / faithfulness）与消融基准（全量 vs 仅向量 vs 不重排），量化指导优化方向。

**4. 可观测性与人在回路**

- structlog 结构化日志 + Prometheus 指标 + Grafana 面板 + Flower（Celery 监控）+ Sentry 异常 + LangSmith Agent 追踪。
- LangGraph `interrupt()` 实现 HITL 审核断点，仅交互式流程启用；Celery/CLI 自动化流程透传不打断。

**4.5 质量评估回环（CI 门禁）**

- 统一评估门禁 `evaluation/run_gate.py` 编排三层：计划质量（completeness / preference_match / budget_consistency）、RAG 检索（recall@4 / precision@4 / MRR）、LLM-as-judge（answer relevancy / faithfulness）。
- 阈值集中在 `thresholds.py`（起步 GATE 值 + 2026 生产级 TARGET 值），`--strict` 模式任一门禁指标低于阈值即 `exit 1` 阻断 CI 合并；结果写盘供 Grafana「质量趋势」面板消费。
- 优雅降级：无 DashScope key 时 LLM 判官自动降为词面代理、仅报告不参与门禁；缺 numpy 等依赖时 RAG 层跳过而不阻断——保证「本应免配额可跑」的门禁在任何环境都能加载。

**4.6 长期记忆 + 多模型网关（成本治理）**

- 长期记忆 `memory/store.py`：按 user_id 持久化跨会话偏好画像（偏好/交通/酒店档次/出发城市/人群），规划前补全用户未填字段、规划后回写本次明确偏好；列表字段设容量上限（FIFO 淘汰最旧）防止无限膨胀；Redis 不可用时降级进程内存。
- 多模型网关 `gateway/`：LLM 调用经 `ainvoke_with_fallback` 按「主模型 + 备用模型链」故障转移，全败才抛错；调用前 `check_budget` 做 per-user token 预算护栏、调用后 `record_usage` 记账，故障转移与用量指标上报 Prometheus。已接入行程整合（synthesis）节点的兜底路径。

**5. 安全与合规**

- 入口 prompt injection 清洗节点（高风险注入置空 + 审计留痕）、sandbox 子进程隔离执行不可信能力、audit_logs 审计表、JWT 鉴权。

**6. 部署**

- FastAPI 后端解耦前端（Streamlit → SSE 流式）、Celery 横向扩容；Docker + docker-compose + nginx + Railway 一键部署。

### 技术栈

Python 3.13 · LangGraph · LangChain · FastAPI · Celery · Redis 8 · PostgreSQL 16 · Alembic · MCP(Streamable HTTP) · Agentic RAG · LLM-as-judge 评估门禁 · 多模型网关 · Prometheus / Grafana / Flower · Sentry · LangSmith · Docker

### 项目亮点（面试可展开）

- **为什么是 Multi-Agent 而非单 Agent**：领域专家职责隔离 + 并行采集降低端到端延迟 + 最小工具集杜绝越权调用。
- **状态如何持久化**：Checkpointer 双写 + TTL 续期策略（活跃会话读取即续期，避免无限增长）。
- **如何保证质量**：RAG 层量化评估 + 消融基准；Agent 级 LLM-as-judge 三层门禁已接入 CI（`--strict` 分数不达标即阻断合并）。
- **如何控成本 / 抗故障**：多模型网关做故障转移 + per-user token 预算护栏；长期记忆跨会话复用用户画像，减少重复澄清。

---

## ⚠️ 诚信提示（面试防雷）

- 项目**不含真实预订/支付**闭环，定位为「行程规划与生成」。被问到时如实说明，并可主动补充「预订闭环需接航司/支付真实 API，合规与成本较高，故聚焦于可复用的编排与工程底座」——这反而是加分项。
- 「评估回环」「长期记忆」「多模型网关」均已在代码中落地并通过验证（`compileall` 通过、`run_gate --strict` 门禁 PASS、核心逻辑隔离单测 ALL_PASS），可写为已完成。
- **关于 LLM-judge 具体分数**：当前环境未配 DashScope key，`llm_relevancy` 跑的是词面代理（0.30，是判官降级的固有局限、非真实质量）。简历/面试中**不要引用这个数字**；`faithfulness=1.0`、`recall@4=1.0` 是词面判官下的可用值。等配 key 用真 LLM 判官重跑后，再引用 relevancy 的真实分。
- 稳妥表述：说「搭建了 LLM-as-judge 评估门禁并接入 CI」（这是事实），而非报某个具体 relevancy 数字，除非已用真判官测得。

