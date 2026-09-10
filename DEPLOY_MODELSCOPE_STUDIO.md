# 部署到魔搭 Studio（作品集/演示级）

> 目标：把 AgentRoute 的**轻量版**（进程内直跑 LangGraph，无 FastAPI/Celery/Redis/Postgres） 发布为魔搭创空间（Studio）Streamlit 应用，得到一个可分享的公网链接。 免费 Studio 在无人访问时会休眠，有访问自动唤醒（冷启动几十秒）——演示/简历完全够用。

---

## 为什么不能直接发 `app.py`

现有 `app.py` 是**瘦客户端**：它靠 `httpx` 调 FastAPI（`/api/v1/trips`）+ Celery + Postgres 后端， 离开这条后端链跑不起来。所以我们新增了独立入口 `app_studio.py`： 它复用 `agents.planner.TripPlanner`，在**进程内直接跑 LangGraph 图**（`stream()`/`invoke()`）， 只依赖 DashScope API + 高德 MCP（出网即可），单进程运行，天然适配 Studio。

---

## 本次为部署新增的文件

| 文件 | 作用 |
| --- | --- |
| `app_studio.py` | 独立 Streamlit 入口（进程内跑图，无后端） |
| `requirements-studio.txt` | 轻量依赖（砍掉 celery/redis/postgres/flower/sentry 等） |
| `DEPLOY_MODELSCOPE_STUDIO.md` | 本文档 |

---

## ⚠️ 部署前必须确认的 4 个点

1. **Python 版本**：`pyproject.toml` 写的是 `requires-python>=3.13`，但魔搭镜像多为 **py312**。 Studio 环境若是 3.12，本项目代码本身不依赖 3.13 独有语法，通常可跑； 如遇报错，把 `pyproject.toml` 的 `>=3.13` 降为 `>=3.12`（**只在 Studio 用 requirements-studio.txt 时不影响**）。
2. **入口文件名**：魔搭 Streamlit Studio 创建时可指定「启动文件」。- 若平台允许自定义启动文件 → 填 `app_studio.py`。
- 若平台**强制** `app.py` → 见下方「附：入口改名方案」。
3. **API Key 用 Secret 注入，绝不进仓库**： 在 Studio「设置 → 环境变量 / Secret」里加：- `DASHSCOPE_API_KEY=<你的key>`（必需，LLM+embedding+高德MCP鉴权都用它）
- 可选：`AMAP_API_KEY=<高德官方key>`（用自己的高德配额，避开 DashScope 托管 MCP 免费日限）
- 可选：`RAG_EMBEDDER=dashscope`、`RAG_RERANKER=dashscope`（走线上向量/重排，检索质量更高； 不设则用离线 Hashing embedder + 词面重排，零配额但语义弱）
4. `.env`** 与备份文件不要提交**：确认 `.env`、`*.bak_agentic`、`__pycache__/` 不进 Studio 仓库 （见下方 `.gitignore` 建议）。

---

## 方式一：用官方 Skill 自动部署（推荐）

```bash
# 1. 安装 modelscope SDK 与部署 Skill
python -m pip install -U modelscope
modelscope skills add @ModelScope/ms-hub @ModelScope/ms-studio-deploy

# 2. 配置 Access Token（https://modelscope.cn/my/myaccesstoken）
export MODELSCOPE_API_KEY="你的token"      # Windows PowerShell: $env:MODELSCOPE_API_KEY="..."

# 3. 在支持 Agent Skills 的工具（Codex/Cursor/Claude Code）里，对着本项目目录说：

```

提示词示例：

```
用 ms-studio-deploy Skill 把当前目录发布为 ModelScope 中国站的 Streamlit 创空间。
入口文件是 app_studio.py，依赖用 requirements-studio.txt。
创空间取名 agentroute-travel，先设为私有。部署后检查状态和日志，
若失败就诊断日志、修复后重试，最后返回可用 URL。

```

---

## 方式二：手动创建（理解流程 / 无 Agent 工具时）

1. 打开 [https://modelscope.cn/studios](https://modelscope.cn/studios) → 「创建创空间」，登录。
2. 填写基本信息：- Studio 名：小写字母+数字+连字符，如 `agentroute-travel`
- SDK 类型：选 **Streamlit**
- 可见性：先 **私有**，验证通过后再公开
3. 把项目**用 git 推到 Studio 仓库的 master 分支**（Studio 详情页有 git 地址）， 或用 Files 页上传。**确保仓库根目录有启动文件 + **`requirements-studio.txt`。
4. 在部署设置里：- 启动文件填 `app_studio.py`（若可指定）
- 依赖文件指向 `requirements-studio.txt`（若平台只认 `requirements.txt`，把它复制/改名）
5. 在「环境变量 / Secret」里配置 `DASHSCOPE_API_KEY`（见上）。
6. 保存 → 等待状态变 `running` → 打开 `https://modelscope.cn/studios/<你的名字>/agentroute-travel`
7. 验证：填参数点「开始规划」，能看到节点进度（含「📚 检索攻略知识（自我反思）」）+ 最终行程。

---

## 附：入口改名方案（仅当平台强制 app.py）

不要覆盖现有 `app.py`（它是后端瘦客户端，本地全栈还要用）。两种做法二选一：

**做法 A（推荐）**：Studio 仓库里把现有 `app.py` 改名为 `app_fullstack.py`， 再把 `app_studio.py` 改名为 `app.py`。**只在 Studio 仓库这么做**，本地不动。

**做法 B**：新建一个只含部署所需文件的干净目录/分支，把 `app_studio.py` 作为 `app.py` 放进去。

---

## `.gitignore` 建议（避免敏感/冗余文件进 Studio 仓库）

```gitignore
.env
*.bak_agentic
__pycache__/
*.pyc
eval_report.json
.pytest_cache/

```

---

## 部署后如何验证「自我反思」在生效

规划几个不同城市（有语料的成都/长沙 vs 无语料的生僻城市）， 在 Studio 日志里应能看到 `rag` 节点的结构化日志字段： `confidence=` 与 `outcome=`（no_reflect / reflected_adopted / reflected_kept / over_budget）。 正常城市应多为 `no_reflect`（不触发重检、不增加等待），验证阈值合理。

> 注：Prometheus `/metrics` 端点在轻量版不暴露（那是 FastAPI 的活）， 但 `RAG_REFLECTIONS` / `RAG_CONFIDENCE` 指标对象仍会在进程内累加，日志字段已足够演示观测。

