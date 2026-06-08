# Travel-Agent v1 Enterprise Upgrade — Project Planning

> **Role of this document:** Master blueprint for the planning agent.  
> **Environment confirmed:** Python 3.13, Redis 8.8.0 (local), Docker 29.x, `nest_asyncio`/`pydantic-settings`/`tenacity`/`prometheus_client` pre-installed.

---

## 1. Current State Diagnosis

### Fatal Bugs (project cannot run)

| # | File | Issue |
|---|------|-------|
| 1 | `agents/planner.py` | `from langchain.agents import create_agent` — this import does not exist; correct is `from langgraph.prebuilt import create_react_agent` |
| 2 | `agents/specialist.py` | Same wrong import |
| 3 | `prompts.py` | All prompts use fake `[TOOL_CALL:xxx]` syntax — not LangChain tool calling, sub-agents never actually call tools |
| 4 | `app.py:205` | `asyncio.run()` inside Streamlit raises `RuntimeError: This event loop is already running` |

### Structural Gaps (enterprise quality missing)

| Category | Gap |
|----------|-----|
| Concurrency | No connection pooling for MCP client; no rate limiting; no request isolation between Streamlit users |
| Reliability | Zero error handling, zero retry logic, zero timeout on any async call |
| Persistence | All state is in-memory; restart loses everything |
| Observability | No structured logging, no metrics, no distributed tracing |
| Scalability | Single-process Streamlit cannot scale horizontally |
| Security | API key in env only; no input validation; prompt injection possible |
| Testing | No tests of any kind |

---

## 2. Target Enterprise Architecture (v1)

```
┌──────────────────────────────────────────────────────────────────┐
│                        Nginx (Port 80/443)                        │
│              reverse proxy + rate limiting + SSL termination      │
└───────────────┬──────────────────────────┬───────────────────────┘
                │                          │
   ┌────────────▼──────────┐   ┌──────────▼────────────────────┐
   │   Streamlit UI        │   │   FastAPI Backend              │
   │   (Port 8501)         │   │   (Port 8000)                  │
   │                       │   │                                │
   │  · Session isolation  │   │  POST /api/v1/trips            │
   │  · SSE streaming      │   │  GET  /api/v1/trips/{id}       │
   │  · thread_id binding  │   │  WS   /api/v1/trips/{id}/stream│
   │  · nest_asyncio bridge│   │  GET  /health                  │
   │                       │   │  GET  /metrics (Prometheus)    │
   └────────────┬──────────┘   └──────────┬────────────────────┘
                │                          │
                └──────────┬───────────────┘
                           │
        ┌──────────────────▼────────────────────────┐
        │              Redis 8.x (Port 6379)         │
        │                                            │
        │  DB 0 — Celery broker (task queue)         │
        │  DB 1 — Celery result backend              │
        │  DB 2 — Response cache                     │
        │          weather:  TTL 6h                  │
        │          POI:      TTL 24h                 │
        │          routes:   TTL 1h                  │
        │  DB 3 — Session store                      │
        │          user prefs, thread_ids, history   │
        │  DB 4 — Rate limiting (sliding window)     │
        │  DB 5 — Pub/Sub streaming results to UI    │
        └──────────────────┬────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────┐
        │         Celery Workers (3 replicas)        │
        │                                            │
        │  Queue: trip.planning  (concurrency=2)     │
        │  Queue: trip.weather   (concurrency=4)     │
        │  Queue: trip.poi       (concurrency=4)     │
        │                                            │
        │  Each worker: LangGraph graph instance     │
        └──────────────────┬────────────────────────┘
                           │
        ┌──────────────────▼────────────────────────┐
        │        LangGraph StateGraph Core           │
        │                                            │
        │  TripState (TypedDict)                     │
        │    ├─ weather_node ──┐                     │
        │    ├─ poi_node ──────┼─→ route_node        │
        │    └─ hotel_node ───┘          │            │
        │                          interrupt()       │
        │                     (human review)         │
        │                          │                 │
        │                    synthesis_node          │
        │                          │                 │
        │                   Pydantic TravelPlan      │
        │                                            │
        │  MemorySaver → Redis AsyncCheckpointer     │
        └──────────┬───────────────────┬─────────────┘
                   │                   │
     ┌─────────────▼──────┐  ┌────────▼──────────────┐
     │   Amap MCP Server  │  │   PostgreSQL 16        │
     │   HTTP transport   │  │                        │
     │   + connection pool│  │   trip_plans           │
     │   + retry wrapper  │  │   user_sessions        │
     │   + circuit breaker│  │   audit_logs           │
     │   (via tenacity)   │  │   (async via asyncpg)  │
     └────────────────────┘  └───────────────────────┘
                   │
     ┌─────────────▼──────────────────────────────────┐
     │         Monitoring Stack (Docker)               │
     │                                                  │
     │   Prometheus (Port 9090) — metrics scraping     │
     │   Grafana    (Port 3000) — dashboards           │
     │   Flower     (Port 5555) — Celery task monitor  │
     │   LangSmith  (cloud)    — LLM trace + eval      │
     └──────────────────────────────────────────────────┘
```

---

## 3. New Tech Stack (additions over demo)

| Technology | Package | Purpose | Why enterprise-relevant |
|-----------|---------|---------|------------------------|
| **FastAPI** | `fastapi uvicorn[standard]` | REST + WebSocket API layer | Decouples UI from agent; enables mobile/third-party clients |
| **Celery** | `celery[redis]` | Async task queue | Handles long-running planning (~30–120s) without blocking HTTP |
| **Redis async client** | `redis[asyncio]` | Cache, session, rate limit, pub/sub | Single Redis for 5 distinct concerns; avoids extra infra |
| **PostgreSQL** | `asyncpg sqlalchemy[asyncio] alembic` | Persistent trip plans + audit log | Survives restarts; queryable history |
| **LangGraph** | `langgraph` | StateGraph orchestration | Industry standard for stateful agents as of 2025 |
| **LangSmith** | `langsmith` | Distributed LLM tracing + eval | Production observability for AI systems |
| **structlog** | `structlog` | Structured JSON logging | Machine-parseable logs; ELK/Grafana Loki compatible |
| **Sentry** | `sentry-sdk[fastapi]` | Error tracking + alerting | Catch runtime failures with full stack trace |
| **Prometheus** | `prometheus-fastapi-instrumentator` | HTTP metrics | Request latency, error rate, active tasks |
| **Flower** | `flower` | Celery monitoring UI | Task success rate, worker throughput |
| **nest_asyncio** | already installed | Streamlit + asyncio bridge | Fixes `RuntimeError: event loop already running` |
| **pydantic-settings** | already installed | Typed config management | 12-factor app config; env validation at startup |
| **tenacity** | already installed | Retry with backoff | Wrap MCP calls + LLM calls; handles transient failures |

---

## 4. Directory Structure (v1)

```
travel-agent/
├── docker-compose.yml          # Full stack orchestration
├── Dockerfile                  # App image
├── nginx.conf                  # Reverse proxy config
├── pyproject.toml              # Dependencies (replaces requirements.txt)
├── alembic.ini                 # DB migrations config
├── .env.example                # Config template
│
├── app/                        # Streamlit UI
│   └── app.py
│
├── api/                        # FastAPI backend
│   ├── main.py                 # App factory + router registration
│   ├── routers/
│   │   ├── trips.py            # Trip CRUD + stream endpoints
│   │   └── health.py           # /health + /metrics
│   ├── middleware/
│   │   ├── rate_limit.py       # Redis sliding-window rate limiter
│   │   └── request_id.py       # X-Request-ID injection
│   └── deps.py                 # FastAPI dependency injection
│
├── agents/                     # Core agent logic
│   ├── __init__.py
│   ├── state.py                # TripState TypedDict
│   ├── graph.py                # LangGraph StateGraph definition
│   ├── nodes.py                # Node functions (weather/poi/hotel/route/synthesize)
│   ├── specialist.py           # SpecialistAgent (fixed)
│   └── planner.py              # TripPlanner wrapper (simplified)
│
├── tasks/                      # Celery tasks
│   ├── __init__.py
│   ├── celery_app.py           # Celery app + queue config
│   └── trip_tasks.py           # plan_trip_task, weather_task, poi_task
│
├── cache/                      # Redis cache layer
│   ├── __init__.py
│   ├── client.py               # Async Redis client singleton
│   └── keys.py                 # Cache key builders + TTL constants
│
├── db/                         # Database layer
│   ├── __init__.py
│   ├── session.py              # AsyncEngine + session factory
│   ├── models.py               # SQLAlchemy ORM models
│   └── migrations/             # Alembic versions
│
├── schemas.py                  # Pydantic v2 data models (TravelPlan etc.)
├── config.py                   # pydantic-settings Config (replaces dataclass)
├── mcp_client.py               # McpClientManager (with pool + retry)
├── prompts.py                  # ReAct-style system prompts (fixed)
├── render.py                   # UI rendering helpers
│
├── monitoring/
│   ├── prometheus.yml          # Scrape config
│   └── grafana/
│       └── dashboards/
│           └── travel_agent.json
│
└── tests/
    ├── conftest.py
    ├── test_graph.py           # LangGraph node unit tests
    ├── test_schemas.py         # Pydantic model validation tests
    ├── test_cache.py           # Redis cache tests
    ├── test_api.py             # FastAPI endpoint tests
    └── eval/
        └── evaluator.py        # LangSmith evaluation suite
```

---

## 5. Docker Compose Full Specification

```yaml
# docker-compose.yml
version: "3.9"

services:

  redis:
    image: redis:8-alpine
    ports: ["6379:6379"]
    volumes: ["redis_data:/data"]
    command: redis-server --appendonly yes --maxmemory 512mb --maxmemory-policy allkeys-lru
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 5

  postgres:
    image: postgres:16-alpine
    environment:
      POSTGRES_DB: travel_agent
      POSTGRES_USER: travel
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    volumes: ["pg_data:/var/lib/postgresql/data"]
    ports: ["5432:5432"]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U travel"]
      interval: 10s
      timeout: 5s
      retries: 5

  api:
    build: .
    command: uvicorn api.main:app --host 0.0.0.0 --port 8000 --workers 2
    ports: ["8000:8000"]
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }
      postgres: { condition: service_healthy }
    volumes: ["./:/app"]

  streamlit:
    build: .
    command: streamlit run app/app.py --server.port=8501
    ports: ["8501:8501"]
    env_file: .env
    depends_on: [api, redis]
    volumes: ["./:/app"]

  worker:
    build: .
    command: >
      celery -A tasks.celery_app worker
      -Q trip.planning,trip.weather,trip.poi
      --concurrency=3
      --loglevel=info
      --without-heartbeat
    env_file: .env
    depends_on:
      redis: { condition: service_healthy }
      postgres: { condition: service_healthy }
    deploy:
      replicas: 2     # scale to 2 worker containers

  flower:
    build: .
    command: celery -A tasks.celery_app flower --port=5555
    ports: ["5555:5555"]
    env_file: .env
    depends_on: [worker]

  prometheus:
    image: prom/prometheus:latest
    ports: ["9090:9090"]
    volumes: ["./monitoring/prometheus.yml:/etc/prometheus/prometheus.yml"]

  grafana:
    image: grafana/grafana:latest
    ports: ["3000:3000"]
    environment:
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_PASSWORD}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./monitoring/grafana/dashboards:/etc/grafana/provisioning/dashboards
    depends_on: [prometheus]

  nginx:
    image: nginx:alpine
    ports: ["80:80"]
    volumes: ["./nginx.conf:/etc/nginx/nginx.conf:ro"]
    depends_on: [api, streamlit]

volumes:
  redis_data:
  pg_data:
  grafana_data:
```

---

## 6. Redis Data Architecture

### Database mapping

```
DB 0  Celery Broker     celery-task-meta-*
DB 1  Celery Results    _kombu/*, celery-task-meta-*
DB 2  Cache
        weather:{city}:{date}           TTL 6h   (JSON string)
        poi:{city}:{category}           TTL 24h  (JSON string)
        route:{origin}:{destination}    TTL 1h   (JSON string)
DB 3  Sessions
        session:{thread_id}             TTL 7d   (JSON: prefs, history summary)
        user:{user_id}:threads          TTL 30d  (SET of thread_ids)
DB 4  Rate Limiting
        ratelimit:{api_key}:{window}    TTL 60s  (counter, sliding window)
DB 5  Pub/Sub Channels
        stream:{task_id}                (no persistence, ephemeral)
```

### Cache usage pattern

```python
# cache/client.py
import redis.asyncio as aioredis
from functools import wraps

class CacheClient:
    _pool: aioredis.ConnectionPool | None = None

    @classmethod
    async def get_pool(cls) -> aioredis.ConnectionPool:
        if cls._pool is None:
            cls._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                db=2,
                max_connections=20,
                decode_responses=True,
            )
        return cls._pool

def cache_result(key_fn, ttl: int):
    """Decorator for async functions — check Redis before calling."""
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            pool = await CacheClient.get_pool()
            r = aioredis.Redis(connection_pool=pool)
            key = key_fn(*args, **kwargs)
            cached = await r.get(key)
            if cached:
                return json.loads(cached)
            result = await fn(*args, **kwargs)
            await r.setex(key, ttl, json.dumps(result))
            return result
        return wrapper
    return decorator

# Usage
@cache_result(
    key_fn=lambda city, date: f"weather:{city}:{date}",
    ttl=6 * 3600
)
async def fetch_weather(city: str, date: str) -> dict:
    # calls MCP tool
    ...
```

### Pub/Sub streaming pattern

```
Celery Worker                    FastAPI SSE endpoint          Browser
     │                                  │                          │
     │  publish("stream:{task_id}",     │                          │
     │          {"token": "...",        │                          │
     │           "type": "text"})  ───► │  GET /trips/{id}/stream  │
     │                                  │    subscribe(channel)    │
     │  publish({type: "tool_start",    │    yield SSE events  ──► │
     │           name: "weather"})  ──► │                          │
     │                                  │                          │
     │  publish({type: "done"})  ──────►│  close connection        │
```

---

## 7. PostgreSQL Schema

```sql
-- db/migrations/versions/001_initial.sql

CREATE TABLE trip_plans (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    thread_id   VARCHAR(64) NOT NULL,
    user_id     VARCHAR(64),
    city        VARCHAR(128) NOT NULL,
    start_date  DATE NOT NULL,
    end_date    DATE NOT NULL,
    preferences JSONB,
    plan_json   JSONB,
    status      VARCHAR(32) DEFAULT 'pending',   -- pending/planning/done/error
    error_msg   TEXT,
    token_usage JSONB,                            -- {prompt: int, completion: int, cost_usd: float}
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE audit_logs (
    id          BIGSERIAL PRIMARY KEY,
    trip_id     UUID REFERENCES trip_plans(id),
    event       VARCHAR(64) NOT NULL,             -- plan_started/tool_called/plan_done
    detail      JSONB,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX idx_trip_thread ON trip_plans(thread_id);
CREATE INDEX idx_trip_user   ON trip_plans(user_id);
CREATE INDEX idx_trip_status ON trip_plans(status, created_at DESC);
CREATE INDEX idx_audit_trip  ON audit_logs(trip_id, created_at);
```

---

## 8. Concurrency Solutions

### Problem 1 — Multiple Streamlit users share one Python process

**Root cause:** Streamlit runs all sessions in the same process; global singletons (McpClientManager, LLM instances) are shared.

**Solution:**
- `McpClientManager`: already a singleton — safe, the MCP client is stateless per-request
- LLM instances: create per-request (cheap — just a config object, not a connection)
- LangGraph graph: compile once at startup, `ainvoke` is stateless per call
- Session state: all mutable state stored in `st.session_state` (per-session) or Redis (cross-session)

```python
# config.py  — separate shared (singleton) from per-request resources
@st.cache_resource          # shared across all sessions
def get_compiled_graph():
    return build_graph()    # compile once, use many times

def get_llm():              # per-request — cheap object
    return ChatTongyi(model=settings.model_name, api_key=settings.api_key)
```

### Problem 2 — MCP HTTP connection exhaustion under load

**Root cause:** Each `McpClientManager.get_tools()` call opens an HTTP connection. 50 concurrent users = 50 connections to Amap MCP.

**Solution:** Connection pool via `httpx.AsyncClient` with limits:

```python
# mcp_client.py
import httpx

class McpClientManager:
    _http_client: httpx.AsyncClient | None = None

    @classmethod
    async def get_http_client(cls) -> httpx.AsyncClient:
        if cls._http_client is None:
            cls._http_client = httpx.AsyncClient(
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                    keepalive_expiry=30,
                ),
                timeout=httpx.Timeout(30.0),
            )
        return cls._http_client
```

### Problem 3 — LLM API rate limiting (Qwen DashScope)

**Root cause:** DashScope has per-minute token limits and per-second request limits.

**Solution:** Redis-based sliding-window rate limiter + tenacity retry:

```python
# tasks/trip_tasks.py
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type
import httpx

@retry(
    retry=retry_if_exception_type((httpx.HTTPStatusError, TimeoutError)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(4),
)
async def call_llm_with_retry(llm, messages):
    return await llm.ainvoke(messages)
```

### Problem 4 — Long-running planning tasks block HTTP workers

**Root cause:** A planning task can take 60–120s; blocking an HTTP worker for 120s under load kills throughput.

**Solution:** Celery async offload:

```
Client → POST /api/v1/trips → FastAPI returns {task_id} immediately (< 50ms)
Client → GET /api/v1/trips/{task_id}/stream → SSE endpoint subscribes to Redis pub/sub
Celery worker → runs planning → publishes tokens to Redis channel
SSE endpoint → streams tokens to client as Server-Sent Events
```

### Problem 5 — LangGraph graph state isolation between users

**Root cause:** If the same graph instance is reused with different `thread_id`, state from one user could bleed into another if checkpointer is misconfigured.

**Solution:** Always pass explicit config with thread_id; use Redis-backed checkpointer:

```python
# agents/graph.py
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

async def get_graph(redis_url: str):
    checkpointer = AsyncRedisSaver.from_conn_string(redis_url)
    return builder.compile(checkpointer=checkpointer)

# Each invocation — fully isolated by thread_id
config = {"configurable": {"thread_id": f"trip-{task_id}"}}
await graph.ainvoke(state, config=config)
```

### Problem 6 — Asyncio event loop in Streamlit

**Root cause:** Streamlit's main thread already has a running event loop; `asyncio.run()` raises `RuntimeError`.

**Solution:** `nest_asyncio` (already installed) + a `run_async` helper:

```python
# app/app.py
import nest_asyncio
nest_asyncio.apply()   # patch once at module load

import asyncio

def run_async(coro):
    """Run async coroutine from Streamlit's sync context."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(coro)
```

For production (Celery path), Streamlit only does polling (`GET /trips/{id}`) — no direct async needed.

---

## 9. FastAPI API Design

```
POST   /api/v1/trips                  Submit planning request → {task_id}
GET    /api/v1/trips/{task_id}        Poll task status + result
GET    /api/v1/trips/{task_id}/stream SSE: stream tokens as they are generated
PATCH  /api/v1/trips/{task_id}        Send modification instruction (multi-turn)
GET    /api/v1/trips/history?user_id= List past plans from PostgreSQL

GET    /health                        {status: ok, redis: ok, postgres: ok, mcp: ok}
GET    /metrics                       Prometheus metrics endpoint
```

---

## 10. Config Management (pydantic-settings)

```python
# config.py  — replaces current dataclass
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # LLM
    dashscope_api_key: str
    model_name: str = "qwen3-max"
    temperature: float = 0.7
    max_tokens: int = 8192

    # Redis
    redis_url: str = "redis://localhost:6379"

    # PostgreSQL
    database_url: str = "postgresql+asyncpg://travel:password@localhost/travel_agent"

    # MCP
    mcp_url: str = "https://dashscope.aliyuncs.com/api/v1/mcps/amap-maps/mcp"

    # LangSmith
    langchain_tracing_v2: bool = True
    langchain_project: str = "travel-agent-v1"
    langchain_api_key: str = ""

    # Rate limiting
    rate_limit_per_minute: int = 10

    # Sentry
    sentry_dsn: str = ""

settings = Settings()
```

---

## 11. Observability Stack

### Structured logging (structlog)

```python
import structlog

logger = structlog.get_logger()

# In every node:
logger.info(
    "node_complete",
    node="weather",
    city=state["city"],
    duration_ms=elapsed,
    cache_hit=was_cached,
    token_usage=usage,
)
```

### Prometheus metrics

```python
from prometheus_client import Counter, Histogram, Gauge

TRIPS_TOTAL = Counter("trips_total", "Total trip plans requested", ["status"])
PLANNING_DURATION = Histogram("planning_duration_seconds", "End-to-end planning time")
ACTIVE_TASKS = Gauge("active_celery_tasks", "Currently running Celery tasks")
TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP tool invocations", ["tool", "status"])
CACHE_HITS = Counter("cache_hits_total", "Redis cache hits", ["cache_type"])
```

### LangSmith evaluation

```python
# tests/eval/evaluator.py
from langsmith import evaluate

def completeness_score(run, example) -> dict:
    plan = run.outputs.get("final_plan", {})
    days = plan.get("days", [])
    score = sum(
        bool(d.get("hotel")) and len(d.get("meals", [])) == 3
        for d in days
    ) / max(len(days), 1)
    return {"key": "completeness", "score": score, "comment": f"{len(days)} days checked"}

def preference_match_score(run, example) -> dict:
    prefs = set(example.inputs.get("preferences", []))
    categories = {
        a["category"]
        for d in run.outputs.get("final_plan", {}).get("days", [])
        for a in d.get("attractions", [])
    }
    overlap = len(prefs & categories) / max(len(prefs), 1)
    return {"key": "preference_match", "score": overlap}

results = evaluate(
    lambda inputs: graph.invoke(inputs),
    data="travel-agent-eval-v1",
    evaluators=[completeness_score, preference_match_score],
    experiment_prefix="qwen3-max-stategraph",
)
```

---

## 12. Security Checklist

| Concern | Mitigation |
|---------|-----------|
| API key exposure | `pydantic-settings` reads from `.env`; `.env` in `.gitignore`; Docker secrets for prod |
| Prompt injection | Input sanitization: strip `\n`, `<`, `>` from user text fields; max length 500 chars |
| Rate limiting | Redis sliding window: 10 requests/min per session; return 429 with Retry-After header |
| DoS via long input | `max_tokens` budget enforced at LLM call level; Streamlit text_area max_chars=500 |
| Data leakage | Audit log records all inputs; PII fields (city, dates) are low-sensitivity in this domain |
| Dependency supply chain | `pip-audit` in CI; Dependabot alerts |

---

## 13. Sprint Breakdown

| Sprint | Duration | Deliverables | Priority |
|--------|----------|-------------|---------|
| **S0** — Environment | 0.5d | Docker compose up, `.env`, `pyproject.toml`, `pydantic-settings` Config | P0 |
| **S1** — Bug fixes | 1d | Fix all 4 fatal bugs; project runs end-to-end | P0 |
| **S2** — LangGraph StateGraph | 2d | `state.py`, `nodes.py`, `graph.py`; Redis-backed MemorySaver | P0 |
| **S3** — Pydantic + schemas | 1d | `schemas.py`; `with_structured_output`; 3-layer fallback | P1 |
| **S4** — Redis cache layer | 1d | `cache/client.py`; decorator; weather/POI/route caching | P1 |
| **S5** — FastAPI + Celery | 2d | `api/`, `tasks/`; SSE streaming; pub/sub bridge | P1 |
| **S6** — PostgreSQL + Alembic | 1d | `db/`; ORM models; migration; audit logging | P1 |
| **S7** — Observability | 1d | structlog, Prometheus metrics, LangSmith, Sentry | P2 |
| **S8** — HitL + multi-turn | 1d | `interrupt()` node; Streamlit review UI; thread_id multi-turn | P2 |
| **S9** — Testing + eval | 1d | pytest suite; LangSmith eval dataset; CI config | P2 |
| **S10** — Docker polish | 0.5d | Dockerfile; nginx.conf; Grafana dashboard | P3 |

**Total estimate: ~12 developer days**

---

## 14. Success Metrics

| Metric | Target |
|--------|--------|
| End-to-end planning latency (P95) | < 90 seconds |
| Plan completeness score (eval) | > 0.85 |
| Preference match score (eval) | > 0.75 |
| Cache hit rate (weather) | > 60% for repeated cities |
| API error rate | < 2% |
| Worker task success rate | > 95% |
| MCP connection pool exhaustion incidents | 0 per day |
