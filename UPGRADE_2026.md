# AgentRoute 2026 升级方案

> **文档定位**：把 AgentRoute 从「传统微服务 + 可观测性」的 v1 企业化形态，升级到 **2026 年 AI 应用工程前沿形态**，并让业务价值贴合金融科技（成都博问金服）场景。 **适用版本基线**：Python 3.13 · LangGraph StateGraph · 高德 MCP（Streamable HTTP）· Redis 8.x · PostgreSQL 16 · Celery · FastAPI **编写日期**：2026-09-08

---

## 0. 一句话结论

AgentRoute 现有的 v1 规划（FastAPI + Celery + Redis 多库 + PG + Prometheus/Grafana/Flower + LangSmith + LangGraph Checkpointer）**基建层已经相当扎实**。它的短板不在「传统工程」，而在两处：

1. **技术栈**：缺 2026 AI 工程的 5 个前沿层 —— 评估回环、上下文/记忆工程、Guardrails、多模型网关、语义缓存 + OTel。
2. **业务**：定位是「旅行助手」，与金融科技公司业务零关联。架构骨架可复用，业务域需迁移。

本方案分两条主线：**A. 技术栈跟上 2026** ｜ **B. 业务贴合金融场景**。

---

## 0.5 实施进展（滚动更新）

| 项 | 状态 | 说明 |
| --- | --- | --- |
| P0 致命 bug | ✅ 复核确认已修 | 4 个 P0 在当前代码均已修复（见 1.1） |
| 输入注入防护层 | ✅ 已落地 | `security.py`：检测/清洗/边界标记三层，接入 graph sanitize 节点 |
| **A1 评估回环** | ✅ **已落地，CI 全绿** | `evaluation/` 统一门禁 + LLM-as-judge + CI `--strict` 红线（2026-09-08） |
| **A2 上下文/记忆** | ✅ **已落地** | `memory/` 长期偏好画像（Redis db6，跨会话补全/回写）+ 接入 planner |
| A3 Guardrails | ⏳ 待做 | 输出校验 + PII 脱敏（金融刚需） |
| **A4 多模型网关** | ✅ **已落地** | `gateway/` 故障转移（主→备用模型链）+ per-user token 预算 + 指标 |
| A5 语义缓存 + OTel | ⏳ 待做 | GPTCache + OpenTelemetry |
| 主线 B 业务迁移 | ⏳ 待做 | 迁移到 Boss 招聘 / 合同续签 |

> GitHub：[https://github.com/approdite9/AgentRoute](https://github.com/approdite9/AgentRoute) （main 与 origin 同步，CI 全绿）

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

### A1. 评估回环（Evaluation Loop）✅ 已落地（2026-09-08，CI 全绿）

**改造前**：只有 `tests/eval/evaluator.py`（计划质量启发式）+ `rag/eval.py`（检索 recall/faithfulness 词面代理）+ LangSmith 追踪，**三者分散、无统一 CI 门禁**。 **已完成**：新增 `evaluation/` 统一门禁包，编排三层评估 + LLM-as-judge，并接入 CI 作为红线。

| 项 | 已落地实现 |
| --- | --- |
| 统一入口 | `python -m evaluation.run_gate [--strict] [--json]`，编排「计划质量 + RAG 检索 + LLM-as-judge」三层 |
| LLM-as-judge | `evaluation/llm_judge.py`：大模型打 answer relevancy / faithfulness 分（取代 BLEU/ROUGE） |
| 优雅降级 | 无 `DASHSCOPE_API_KEY` 时自动降级为词面代理，**仅报告、不参与门禁**（惰性 import 避免硬依赖 numpy），CI 免配额可跑 |
| 阈值治理 | `evaluation/thresholds.py`：`GATE`（CI 起步红线）/ `TARGET`（2026 目标 faithfulness≥0.9 等）分离，随质量提升逐步收敛 |
| 评测集 | `evaluation/datasets/judge_qa.json`，问答锚定 `rag/corpus/travel_notes.json` |
| CI 门禁 | `.github/workflows/ci.yml` 在 pytest 后跑 `run_gate --strict`，分数不达标 exit 1 阻断合并 |
| 测试 | `tests/test_eval_gate.py` 冒烟测试（阈值自洽 / 降级 / 门禁三路径），随套件在 CI 运行 |

**当前门禁基线**：completeness / preference_match / budget_consistency 三项均 PASS；LLM 判官后端在 CI 无 key 时为 lexical（仅报告）。配 `DASHSCOPE_API_KEY` 后升级为真实 LLM 打分并纳入门禁。

> 说明：最初方案设想用 Ragas/DeepEval 外部框架，实施时发现项目已有可复用的评估器，故改为**自研统一门禁编排 + 复用现有评估器**，减少重依赖、CI 免配额即可跑。后续可平滑接入 Ragas/DeepEval 作为 judge 后端。

### A2. 上下文工程 + Agent 记忆 ✅ 已落地（长期记忆画像）

**2026 共识**：Agent 效果上限取决于「拿到的上下文质量」，不是模型本身。 **已完成**：新增 `memory/` 包，实现跨会话的用户偏好长期记忆，并接入 planner。

| 项 | 已落地实现 |
| --- | --- |
| 长期记忆 Store | `memory/store.py`：Redis **db6**（与 checkpoint/cache/session/rate/pubsub/test 隔离），按 `user_memory:{user_id}` 存偏好画像 |
| 记忆字段 | preferences / transport / hotel_type / origin_city / party_type / budget_level（list 累积去重，标量取最新非空） |
| 规划前补全 | `merge_memory_into_state`：用历史画像补全用户**未填**字段，**不覆盖**本次明确输入 |
| 规划后回写 | `update_memory_from_state`：回写本次明确偏好、累积去重、递增 visit_count、空值不抹除历史 |
| 接入点 | `TripPlanner.invoke(..., user_id=...)`：传 user_id 才启用记忆，向后兼容；优雅降级（Redis 不可用退化内存兜底，不阻断规划） |
| 测试 | `tests/test_memory.py`：合并/回写/降级往返/契约字段，无需 Redis 即可跑 |

**短期记忆**：LangGraph Checkpointer（已有）承载单会话状态，与本长期记忆互补。

**后续可深化**（本次未做）：

- **上下文裁剪**：单轮 token 预算控制，动态选择注入哪些上下文（避免塞满窗口）
- **信息架构**：明确 Agent 能看哪些数据源、哪些知识库最新、何时检索什么
- 语义化长期记忆（存自然语言「用户画像摘要」供 LLM 参考，而不止结构化偏好）

### A3. Guardrails / 防护栏 🔴 高（金融刚需）

**现状**：规划里仅把「prompt injection possible」标为 gap，未落地。 **2026 做法**：输入/输出双向校验。

| 层 | 方案 |
| --- | --- |
| 输入 | 提示注入检测、越权意图拦截 |
| 输出 | **NeMo Guardrails** 或 **Llama Guard** 做内容合规校验 |
| 数据 | **PII 脱敏**（金融场景硬要求）、敏感字段脱敏后再进日志 |

### A4. 多模型网关 + 成本治理 ✅ 已落地

**2026 痛点**：LLMOps 最大缺口是「没在 LLM 基础设施里建成本控制」。
**已完成**：新增 `gateway/` 包，故障转移 + 成本治理，**不引入 litellm 重依赖、不改 create_llm 契约**。

| 项 | 已落地实现 |
| --- | --- |
| 故障转移 | `gateway/router.py::ainvoke_with_fallback`：按「主模型 + `FALLBACK_MODELS`」链逐个降级重试，全败才抛 `LLMGatewayError` |
| 模型覆盖 | `config.create_llm(model=...)` 支持指定模型（默认沿用 model_name，向后兼容）；`fallback_model_list()` 解析备用链 |
| 成本治理 | `gateway/budget.py`：token 估算（字符启发式，无 tiktoken 依赖）+ per-user 预算护栏 `check_budget` + 用量记账 `record_usage` |
| 预算配置 | `token_budget_per_user`（0=不限）；超预算拦截并计 `llm_budget_blocks_total` |
| 可观测 | 4 个新指标：`llm_calls_total` / `llm_fallbacks_total` / `llm_tokens_total` / `llm_budget_blocks_total` |
| 测试 | `tests/test_gateway.py`：主成功/切备用/全败/去重/预算护栏/记账，全程 mock LLM 无需 key |

**供应商可移植性**：故障转移链可跨 provider（只要 create_llm 能构造对应模型）；后续可平滑替换为 LiteLLM 作为统一后端而不改上层调用。

> 说明：沿用「复用现有工厂 + 薄封装」策略（同 A1）——网关是**可选增强层**，现有节点仍可直接调 `create_llm`，零侵入。

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
| **P1（1 周）** ✅ | 质量可量化（已完成） | 自研 `evaluation/` 统一门禁 + LLM-as-judge + CI --strict 红线 | CI 全绿，门禁生效 |
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

