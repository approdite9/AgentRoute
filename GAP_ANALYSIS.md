# AgentRoute 功能差距分析报告

> **文档定位**：将 AgentRoute 与 2026 年业界主流「AI 旅行助手 / Travel Agent」方案做**功能维度**对标，识别差距、分级、并给出「求职简历向」的补齐优先级。 **对标来源**：COAX《We tested the 10 best AI travel agents》(2026)、Softblues《Multi-Agent AI Travel Assistant》案例、Springer《Multi-model orchestrated agent for live flight/hotel itinerary》(2026)、IJRASET《Comprehensive Multi-Agent Architecture for Intelligent Itinerary Generation》(2026)。 **编写日期**：2026-09-08 · **适用基线**：Python 3.13 · LangGraph StateGraph · 高德 MCP(Streamable HTTP) · Redis 8 · PostgreSQL 16 · Celery · FastAPI

---

## 0. 一句话结论

AgentRoute 在**工程基建维度已超过大多数业界 demo**（FastAPI + Celery + 多库 Redis + PG + Prometheus/Grafana/Flower + LangSmith + LangGraph Checkpointer + HITL + 审计合规）。真正的功能差距**不在工程能力**，而集中在**面向终端用户的旅行业务闭环**（真实预订、价格追踪、长期个性化记忆）。

因此简历定位不应与业界比「能不能订机票」，而应主打**「可复用的企业级 Multi-Agent 平台底座」**——这恰是那些「能订票」的 demo 所缺失的生产级工程能力。

---

## 1. AgentRoute 真实功能盘点（基于源码核查，非 README）

> README 为早期版本，实际代码已远超其描述。以下为 2026-09-08 逐目录核查结果。

| 层 | 已实现能力 | 源码证据 |
| --- | --- | --- |
| **多 Agent 编排** | Planner 总控 + Weather/POI/Hotel 三领域专家并行 Fan-Out/Fan-In + Route 汇聚 + RAG + Synthesize + Geocode + 路线优化；`clarify` 澄清节点、多轮修改复用 Checkpointer | `agents/graph.py`、`nodes.py`、`clarify.py`、`state.py` |
| **工具 / 数据接入** | 高德地图 MCP（15 工具：天气/POI/路线×4/地理编码/距离），Streamable HTTP；按领域最小工具集分发，主路 DashScope 托管 + 回退高德官方 Key | `mcp_client.py`、`config.py::tool_domains` |
| **Agentic RAG** | 完整管线：chunk → embedding → BM25 → 向量 → rerank → rewrite → 检索评估（recall@k/precision@k/MRR/faithfulness）；混合多路检索 + 消融基准 | `rag/`（12 模块）、`rag/eval.py` |
| **企业基建** | FastAPI 后端解耦 UI + Celery 横向扩容 + Redis 多库 + PostgreSQL 16 + Alembic 迁移 | `api/`、`tasks/`、`db/` |
| **可观测性** | structlog + Sentry + Prometheus + Grafana + Flower + LangSmith 全链路追踪 | `monitoring/` |
| **可靠性** | tenacity 重试、超时、熔断、瞬时/永久错误分流（快速失败省配额）、PG+Redis Checkpointer（重启不丢状态） | `nodes.py`、`config.py` |
| **HITL 人审** | LangGraph `interrupt()` 人工审核断点，仅交互式流程启用，Celery/CLI 自动化流程透传不打断 | `nodes.py::review_node`、`graph.py` |
| **安全 / 合规** | 输入侧 prompt injection 清洗节点、sandbox 子进程隔离、audit_logs 审计表、JWT 鉴权 | `security.py`、`sandbox.py`、`api/auth.py` |
| **质量门禁（雏形）** | `eval/` 已含 `llm_judge.py`、`run_gate.py`、`thresholds.py` + RAG 检索评估 | `eval/`、`rag/eval.py` |
| **部署** | Docker + docker-compose + nginx + Railway 配置齐全 | `Dockerfile`、`docker-compose.yml`、`nginx.conf`、`railway.toml` |

---

## 2. 业界主流旅行 Agent 功能标配（对标基准）

综合 4 篇来源的共识，主流生产级方案的功能标配如下（✅=AgentRoute 已有，⚠️=部分，❌=缺失）：

| 功能类别 | 业界标配 | AgentRoute | 说明 |
| --- | --- | --- | --- |
| 自然语言多轮对话 | ✅ | ✅ | 已有 clarify + 多轮修改 |
| 多 Agent 专家分工 | ✅ | ✅ | 已有且带并行编排，优于多数 demo |
| 实时数据接入 | ✅ | ✅ | 高德 MCP 天气/POI/路线 |
| 行程自动生成 | ✅ | ✅ | 结构化 JSON + Markdown 导出 |
| **真实预订闭环** | ✅ | ❌ | 航班/酒店实时查价→下单→支付→确认 |
| **价格追踪与提醒** | ✅ | ❌ | 票价波动监控、超预算告警 |
| **长期记忆 / 个性化** | ✅ | ✅ | 已落地 `memory/store.py`：按 user_id 跨会话偏好画像（Redis db6，含容量上限） |
| **行程中断 / 改签处理** | ✅ | ❌ | 航班延误自动重排、替代方案 |
| **多语言** | ✅ | ❌ | 仅中文 |
| 质量评估回环 | ✅ | ✅ | 已落地 `evaluation/`：三层门禁（计划/RAG/LLM-judge），`--strict` 阻断 CI |
| Guardrails 双向护栏 | ✅ | ⚠️ | 有输入清洗 + 审计，缺输出合规 + PII 脱敏 |
| 多模型网关 / 成本治理 | ⚠️ | ✅ | 已落地 `gateway/` 并接入 synthesis：故障转移 + per-user 预算护栏 |

---

## 3. 差距明细与分级

### 🔴 核心差距（与业界「能订」方案的本质区别）

**G1. 真实预订闭环 —— 无预订/支付**

- 现状：仅到「行程规划」，输出结构化方案，不落单。
- 业界：接 Google Flights/Hotels API + 支付网关，对话内完成 search→compare→book→pay→confirm。
- 评估：demo 项目**不建议硬做**（需接航司/支付真实 API，合规与成本极高）。简历中应诚实定位为「行程生成」而非「预订」，避免夸大。

**G2. 价格追踪与提醒 —— 仅静态预算汇总**

- 现状：`final_plan` 汇总门票/酒店/餐饮/交通费用，为一次性静态估算。
- 业界：票价波动监控、多币种、超预算实时告警。
- 落地成本：中（可复用 Celery 定时任务 + 已有 Redis）。

**G3. 长期记忆 / 个性化 —— 仅单会话状态**

- 现状：LangGraph Checkpointer 承载**单会话**状态，重启不丢；但无跨会话用户画像。
- 业界：跨会话偏好学习、旅行历史、persona 匹配（如 ARRIVAL 的旅行人格测验）。
- 落地成本：低-中（接 LangGraph Store，复用现有 PG）。**简历含金量高**。

### 🟡 中等差距（企业化 / 前沿工程标签）

**G4. 质量评估回环 —— 缺 Agent 级量化门禁**

- 现状：`rag/eval.py` 已有检索评估（recall/precision/MRR/faithfulness），`eval/` 有 gate 雏形。
- 缺口：缺 Ragas/DeepEval 对**整体 Agent 输出**的量化门禁 + CI 阻断合并。
- 落地成本：低（雏形已在，补齐即可）。**简历含金量最高**（2026 面试必问）。

**G5. Guardrails —— 缺输出侧合规 + PII 脱敏**

- 现状：有输入 prompt injection 清洗 + audit_logs。
- 缺口：输出侧 NeMo Guardrails / Llama Guard 合规校验、PII 脱敏（金融场景硬需求）。
- 落地成本：中。**与博问金服金融科技业务贴合**。

**G6. 行程中断处理** / **G7. 多语言** —— 均为中等差距，工作量偏大，简历性价比一般。

### 🟢 低优先差距

**G8. 多模型网关 + 成本治理**

- 现状：单一 qwen3-max（DashScope），无预算控制。
- 业界：LiteLLM/AI Gateway 统一路由 + 故障转移 + per-tenant 配额。
- 落地成本：中，可写「故障转移 + 成本治理」标签。

---

## 4. 简历向补齐优先级（含金量 ÷ 成本）

| 优先级 | 补齐项 | 简历含金量 | 落地成本 | 理由 |
| --- | --- | --- | --- | --- |
| 🥇 P1 | **G4 评估回环**（Ragas+DeepEval+CI 门禁） | ★★★★★ | 低 | 2026 AI 工程面试必问「怎么评估 Agent」，雏形已在 |
| 🥇 P1 | **G3 长期记忆**（LangGraph Store + 上下文裁剪） | ★★★★★ | 低-中 | 「Context Engineering + Agent Memory」是 2026 最热标签 |
| 🥈 P2 | **G5 Guardrails + PII 脱敏** | ★★★★ | 中 | 金融科技刚需，业务贴合博问金服 |
| 🥈 P2 | **G8 多模型网关**（LiteLLM） | ★★★ | 中 | 「故障转移 + per-tenant 成本治理」标签 |
| 🥉 P3 | G1 真实预订闭环 | ★★ | 高 | 合规/成本高，demo 不建议硬做 |

---

## 5. 简历定位建议

AgentRoute 最大的差异化**不是「旅行」本身**，而是：

> **以旅行助手为业务载体，完整实现了一套可复用的企业级 Multi-Agent 平台底座——LangGraph 状态机编排 + MCP 连接层 + Agentic RAG + 全链路可观测 + HITL 人审 + 审计合规。**

业界那些「能订机票」的 demo 反而工程基建薄弱；AgentRoute 的强项恰是它们所缺的**生产级工程能力**。简历应主打此点，而非与人比「能否订票」。这也呼应 `UPGRADE_2026.md` 的 B-1 路线：将旅行助手抽象为「企业 Agent 平台底座」。

---

## 附：对标来源清单

1. COAX, *We tested the 10 best AI travel agents: What actually worked?* (2026-05)
2. Softblues, *From Trip Planning Chaos to Booked in 15 Minutes — Multi-Agent Travel Assistant* (2026-01)
3. Springer, *A multi-model orchestrated agent for live flight and hotel itinerary generation* (2026-04)
4. IJRASET, *A Comprehensive Multi-Agent Architecture for Intelligent Itinerary Generation* (2026-05)

