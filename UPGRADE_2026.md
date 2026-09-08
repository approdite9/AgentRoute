# AgentRoute 2026 升级方案

> **文档定位**：把 AgentRoute 从「传统微服务 + 可观测性」的 v1 企业化形态，升级到 **2026 年 AI 应用工程前沿形态**，并让业务价值贴合金融科技（成都博问金服）场景。 **适用版本基线**：Python 3.13 · LangGraph StateGraph · 高德 MCP（Streamable HTTP）· Redis 8.x · PostgreSQL 16 · Celery · FastAPI **编写日期**：2026-09-08

---

## 0. 一句话结论

AgentRoute 现有的 v1 规划（FastAPI + Celery + Redis 多库 + PG + Prometheus/Grafana/Flower + LangSmith + LangGraph Checkpointer）**基建层已经相当扎实**。它的短板不在「传统工程」，而在两处：

1. **技术栈**：缺 2026 AI 工程的 5 个前沿层 —— 评估回环、上下文/记忆工程、Guardrails、多模型网关、语义缓存 + OTel。
2. **业务**：定位是「旅行助手」，与金融科技公司业务零关联。架构骨架可复用，业务域需迁移。

本方案分两条主线：**A. 技术栈跟上 2026** ｜ **B. 业务贴合金融场景**。

---

## 1. 现状诊断

### 1.1 P0 复核结论：4 个致命 bug 均已在当前代码中修复 ✅

`project_planning.md` / `prompt_for_claude_code.md` 记录的 4 个 P0 是**改造前**诊断。 对当前源码逐一复核（2026-09-08）确认**已全部修复**，本次"先修"不涉及 P0：

| # | 文件 | 改造前问题 | 当前状态 |
| --- | --- | --- | --- |
| 1 | `agents/planner.py` | `from langchain.agents import create_agent`（不存在） | ✅ 已重构为薄封装层，无该导入 |
| 2 | `agents/specialist.py` | 同上错误导入 | ✅ 已是 `from langgraph.prebuilt import create_react_agent` |
| 3 | `prompts.py` | 假的 `[TOOL_CALL:xxx]` 语法 | ✅ 已改为标准 tool calling，源码零残留 |
| 4 | `app.py` | Streamlit 中 `asyncio.run()` 事件循环冲突 | ✅ 已重构为 FastAPI 后端 + httpx SSE 瘦客户端 |

> 复核方式：`compileall` 全量编译通过（COMPILE_OK）；`ripgrep` 确认上述坏模式仅存在于文档，源码已无残留。 因此重心转向下方主线 A 的前沿层补齐与主线 B 的业务迁移。

### 1.2 v1 规划已覆盖（无需重做）

- ✅ 并发/隔离：MCP 连接池、限流、会话隔离
- ✅ 可靠性：tenacity 重试、超时、熔断
- ✅ 持久化：PG + Redis Checkpointer（重启不丢状态）
- ✅ 可观测性：structlog + Sentry + Prometheus + Grafana + Flower + LangSmith
- ✅ 可扩展：FastAPI 解耦 UI、Celery 横向扩容
- ✅ HITL：LangGraph `interrupt()` 人工审核节点

---

## 2. 主线 A —— 技术栈跟上 2026（5 个前沿层）

对照 2026 年一线 AI 工程实践，按投入产出（ROI）排序补齐：

### A1. 评估回环（Evaluation Loop）🔴 最高优先级

**现状**：只有 `eval/evaluator.py` 占位 + LangSmith 追踪，无量化质量门禁。 **2026 做法**：LLM-as-a-judge 取代 BLEU/ROUGE，量化指标 + CI 门禁。

| 项 | 方案 |
| --- | --- |
| 框架 | **Ragas**（RAG 场景）+ **DeepEval**（Agent/断言场景） |
| 核心指标 | faithfulness ≥ 0.9 · answer relevancy ≥ 0.85 · context precision ≥ 0.8 |
| 落地 | 建 `eval/datasets/` 黄金测试集 → CI 跑评估 → **分数不达标 block 合并** |
| 监控 | 评估分数上报 Prometheus，Grafana 建「质量趋势」面板 |

```python
# eval/run_eval.py 骨架
from deepeval import evaluate
from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric
from deepeval.test_case import LLMTestCase

def build_cases(golden_set): ...
metrics = [AnswerRelevancyMetric(threshold=0.85), FaithfulnessMetric(threshold=0.9)]
results = evaluate(test_cases=build_cases(golden), metrics=metrics)
# CI: 任一指标未过阈值 → sys.exit(1)

```

### A2. 上下文工程 + Agent 记忆 🔴 高

**现状**：已无假 `[TOOL_CALL]`（历史问题，已修）；当前缺的是**记忆层**。 **2026 共识**：Agent 效果上限取决于「拿到的上下文质量」，不是模型本身。

- **短期记忆**：LangGraph Checkpointer（已有）承载单会话状态
- **长期记忆**：接入 **LangGraph Store**，存用户偏好 / 历史决策，跨会话可检索
- **上下文裁剪**：单轮 token 预算控制，动态选择注入哪些上下文（避免塞满窗口）
- **信息架构**：明确 Agent 能看哪些数据源、哪些知识库是最新的、何时检索什么

### A3. Guardrails / 防护栏 🔴 高（金融刚需）

**现状**：规划里仅把「prompt injection possible」标为 gap，未落地。 **2026 做法**：输入/输出双向校验。

| 层 | 方案 |
| --- | --- |
| 输入 | 提示注入检测、越权意图拦截 |
| 输出 | **NeMo Guardrails** 或 **Llama Guard** 做内容合规校验 |
| 数据 | **PII 脱敏**（金融场景硬要求）、敏感字段脱敏后再进日志 |

### A4. 多模型网关 + 成本治理 🟡 中

**现状**：单一 qwen3-max（dashscope），无兜底、无预算控制。 **2026 痛点**：LLMOps 最大缺口是「没在 LLM 基础设施里建成本控制」。

- **LiteLLM / AI Gateway**：统一多模型路由 + 故障转移（主模型挂了自动切备用）
- **成本治理**：按 token 计费预算、per-user/per-tenant 配额、超预算告警
- **供应商可移植性**：屏蔽厂商差异，随时换模型不改业务代码

### A5. 语义缓存 + OpenTelemetry 🟡 中

- **语义缓存**：现有 Redis 只做精确匹配缓存；升级为 **GPTCache 思路**，相似 query 命中缓存，显著降本
- **可观测性升级到 OTel**：行业在收敛到 OpenTelemetry 标准（与 n8n 2.37 的 OpenTelemetry 同方向），把 structlog/Prometheus/LangSmith 的追踪统一到 OTel trace

---

## 3. 主线 B —— 业务贴合金融科技

### 核心矛盾

博问金服是**金融科技公司**，「旅行助手」是纯练手 demo，业务价值为零。但其**架构骨架**（Multi-Agent 编排 + MCP 连接层 + LangGraph 状态机 + 企业基建 + 审计日志）**可直接迁移**。

### 路线 B-1（推荐）：抽象成「企业 Agent 平台底座」

把旅行助手降级为「参考实现之一」，沉淀可复用框架，长出真实业务 Agent：

| 可复用组件 | 迁移到的业务 Agent |
| --- | --- |
| MCP 连接层 + 连接池 + 熔断 | 内部数据源（DB / 知识库 / 风控 API） |
| LangGraph 编排 + HITL `interrupt()` | **合同续签自动化**（钉钉 + OA 审批，天然状态机 + 人工审核） |
| 评估 + 审计 + Checkpointer | **Boss 招聘预筛选 Agent**（已在做，套评估 + HITL + 审计） |
| 多模型网关 + Guardrails | 未来风控 / 尽调 / 智能客服 |

### 路线 B-2：直接换业务域

若只想让此单一项目「像金融业务」，把领域从旅行规划换成：

- **投研助手** / **合规文档问答（Agentic RAG）** / **智能客服**
- MCP 从高德地图 → 内部数据源；`audit_logs` 表已建好，正是 fintech 合规加分项

### 金融行业专属加分项（A/B 都该有）

1. **数据合规**：PII 脱敏、数据不出域（本就自托管 MCP，符合 data control）
2. **全链路审计 + 可解释性**：Agent 每步决策留痕，风控可回溯
3. **财务级可靠性**：幂等、对账、降级预案

---

## 4. 落地路线图（按阶段）

| 阶段 | 目标 | 关键动作 | 产出 |
| --- | --- | --- | --- |
| **P0（1-2 天）** | 让项目真正能跑 | 修 4 个致命 bug、工具真正被调用 | 可运行的 baseline |
| **P1（1 周）** | 质量可量化 | 接 Ragas/DeepEval + 黄金集 + CI 门禁 | 质量趋势面板 |
| **P2（1-2 周）** | 前沿层补齐 | 记忆层 + Guardrails + 多模型网关 | 2026 形态 Agent |
| **P3（2-3 周）** | 业务迁移 | 把架构套到 Boss/合同续签真实场景 | 公司可用的业务 Agent |
| **P4（持续）** | 金融合规 | PII 脱敏 + 全链路审计 + 降级预案 | 生产合规能力 |

### 推荐主路径

> **先修 P0 让它能跑 → 补评估回环（质量可量化）→ 架构迁移到 Boss/合同续签真实业务 → 加金融合规层**

这样既「技术栈跟上 2026」，又「从练手 demo 变成公司能用的东西」。

---

## 5. 新增依赖清单（相对现有 pyproject.toml）

```toml
# 评估
"ragas",
"deepeval",
# Guardrails
"nemoguardrails",      # 或 llama-guard 相关
"presidio-analyzer",   # PII 检测脱敏
"presidio-anonymizer",
# 多模型网关
"litellm",
# 语义缓存
"gptcache",
# 可观测性（OTel）
"opentelemetry-sdk",
"opentelemetry-instrumentation-fastapi",
"opentelemetry-exporter-otlp",

```

---

## 附录：2026 AI 工程趋势速查（本方案依据）

| 趋势 | 关键点 |
| --- | --- |
| Context Engineering | 取代「提示词工程」成为核心；Agent Memory + Sub-agents 标配 |
| MCP | 「AI 的 USB-C」，事实标准，Linux 基金会治理，多模型网关基础 |
| Agentic RAG | 2026 默认形态：LangGraph 编排 + LlamaIndex 检索 + Ragas/Phoenix/Langfuse 评估 |
| Evaluation | LLM-as-judge 取代 BLEU/ROUGE；faithfulness≥0.9 等生产阈值 |
| LLMOps | 市场高速增长，最大缺口是「没做成本控制」 |

