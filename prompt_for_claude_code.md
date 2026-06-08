# Claude Code Development Prompts — Travel-Agent v1 Enterprise

> **How to use:** Give each Sprint section to the Code Agent as a standalone prompt.  
> Each prompt is self-contained: it includes full context, exact file paths, required packages, and success criteria.  
> Run sprints in order — later sprints depend on earlier ones.

---

## SPRINT 0 — Environment Bootstrap

```
You are building an enterprise-grade Python agent project from an existing demo.
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT:
- Python 3.13, Redis 8.8.0 running on localhost:6379
- Docker 29.x available (no containers running yet)
- Pre-installed: nest_asyncio, pydantic v2, pydantic-settings, tenacity, prometheus_client, httpx

TASK: Set up the project foundation.

1. Install required packages (I have a conda virtual environment. To activate it, I use the command "conda activate Agent".use pip install, NOT conda):
   pip install fastapi "uvicorn[standard]" "celery[redis]" "redis[asyncio]" \
     asyncpg "sqlalchemy[asyncio]" alembic structlog "sentry-sdk[fastapi]" \
     langgraph langsmith langchain langchain-community \
     langchain-mcp-adapters "prometheus-fastapi-instrumentator" flower \
     langchain_community python-dotenv

2. Create pyproject.toml in the project root with all dependencies listed above
   (do NOT use requirements.txt — use pyproject.toml with [project.dependencies]).

3. Create .env.example with these variables (with placeholder values):
   DASHSCOPE_API_KEY=your_key_here
   MODEL_NAME=qwen3-max
   TEMPERATURE=0.7
   REDIS_URL=redis://localhost:6379
   DATABASE_URL=postgresql+asyncpg://travel:password@localhost/travel_agent
   MCP_URL=https://dashscope.aliyuncs.com/api/v1/mcps/amap-maps/mcp
   LANGCHAIN_TRACING_V2=true
   LANGCHAIN_PROJECT=travel-agent-v1
   LANGCHAIN_API_KEY=
   RATE_LIMIT_PER_MINUTE=10
   SENTRY_DSN=
   POSTGRES_PASSWORD=securepassword
   GRAFANA_PASSWORD=admin

4. Create .env by copying .env.example (do NOT commit real keys).

5. Create a .gitignore that ignores: .env, __pycache__, *.pyc, .venv, *.egg-info

6. Rewrite config.py to use pydantic-settings BaseSettings:
   - Class name: Settings
   - model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")
   - Fields: dashscope_api_key (str), model_name (str="qwen3-max"), temperature (float=0.7),
             max_tokens (int=8192), redis_url (str), database_url (str), mcp_url (str),
             langchain_tracing_v2 (bool=True), langchain_project (str), langchain_api_key (str=""),
             rate_limit_per_minute (int=10), sentry_dsn (str="")
   - Keep create_llm() method — it should use self.dashscope_api_key
   - Keep the ChatTongyi monkey patch for streaming KeyError bug (already in config.py)
   - Export: settings = Settings()
   - Remove: old CONFIG = Config() singleton

7. Create the directory structure:
   mkdir -p api/routers api/middleware agents cache db/migrations tasks monitoring/grafana/dashboards tests/eval app

SUCCESS CRITERIA:
- python -c "from config import settings; print(settings.model_name)" prints "qwen3-max"
- python -c "import redis.asyncio; import celery; import fastapi; import langgraph; print('OK')"
- .env.example exists and has all listed variables
```

---

## SPRINT 1 — Fix Fatal Bugs

```
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT:
The project has 4 fatal bugs preventing it from running. Fix ALL of them.

BUG 1 — Wrong import in agents/planner.py and agents/specialist.py
File: agents/planner.py, line 7
File: agents/specialist.py, line 2
Problem: `from langchain.agents import create_agent` does not exist.
Fix: Change to `from langgraph.prebuilt import create_react_agent`
     Change all calls from `create_agent(...)` to `create_react_agent(model=..., tools=..., prompt=...)`
     Note: create_react_agent signature is: create_react_agent(model, tools, *, prompt=None)

BUG 2 — Broken prompts in prompts.py
File: prompts.py
Problem: All prompts instruct the LLM to output fake [TOOL_CALL:xxx] strings.
  This is NOT how LangChain tool calling works. The LLM will literally output these strings
  instead of invoking tools via the ReAct protocol. The [TOOL_CALL:...] markers are filtered
  out in planner.py but the tools are never actually called.
Fix: Rewrite ALL 4 prompts (WEATHER_AGENT_PROMPT, ATTRACTION_AGENT_PROMPT,
     HOTEL_AGENT_PROMPT, PLANNER_AGENT_PROMPT) as proper ReAct system prompts.
  - Remove ALL [TOOL_CALL:xxx] instructions
  - Tell the LLM it has tools available and MUST use them (ReAct style)
  - For sub-agent prompts: concise role description + tool usage instruction
  - For PLANNER_AGENT_PROMPT: keep the detailed JSON output schema at the end
  - Do NOT tell the LLM about tool calling syntax — create_react_agent handles that

  Example of correct WEATHER_AGENT_PROMPT:
  """你是天气查询专家。根据用户指定的城市，使用你的工具查询实时天气信息。
  你必须调用工具获取数据，不得凭空编造天气信息。查询完成后，将结果以结构化文本返回。"""

BUG 3 — asyncio.run() in Streamlit (app.py:205)
File: app.py
Problem: asyncio.run(_collect()) raises RuntimeError in Streamlit.
Fix:
  1. At the top of app.py, add:
     import nest_asyncio
     nest_asyncio.apply()
  2. Replace asyncio.run(_collect()) with:
     loop = asyncio.get_event_loop()
     tokens = loop.run_until_complete(_collect())

BUG 4 — Config import mismatch after Sprint 0 rewrites config.py
File: all files that import from config
Problem: Old code does `from config import CONFIG`; new code exports `settings`.
Fix: Search all .py files for `from config import CONFIG` and replace with
     `from config import settings as CONFIG` OR update each file to use `settings` directly.
     Files to check: mcp_client.py, agents/planner.py, agents/specialist.py, app.py, Agent.py

AFTER FIXING ALL BUGS:
- Run: python Agent.py
  (it will try to call Qwen + MCP — if DASHSCOPE_API_KEY is real, it should produce output)
- Verify the app starts: streamlit run app.py --server.headless=true &
  (should start without RuntimeError)

SUCCESS CRITERIA:
- No ImportError on any module import
- prompts.py has zero occurrences of "[TOOL_CALL"
- app.py has "nest_asyncio.apply()" near the top
- agents/planner.py imports from "langgraph.prebuilt"
```

---

## SPRINT 2 — LangGraph StateGraph Core

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT:
Sprint 0 and Sprint 1 are complete. The project imports correctly and runs.
Now replace the simple create_react_agent wrapper with a proper LangGraph StateGraph.
This is the most important architectural change for enterprise/resume quality.

TASK A — Create agents/state.py
Create this file exactly:

from typing import TypedDict, Annotated, Any
from langgraph.graph.message import add_messages

class TripState(TypedDict):
    # Input fields (set at graph start)
    city: str
    start_date: str
    end_date: str
    preferences: list[str]
    hotel_type: str
    transport: list[str]
    extra: str

    # Conversation history
    messages: Annotated[list, add_messages]

    # Intermediate results (populated by nodes)
    weather_data: dict | None
    poi_data: list | None
    hotel_data: list | None
    route_data: list | None

    # Final output
    final_plan: dict | None

    # Error tracking
    error: str | None
    retry_count: int

TASK B — Create agents/nodes.py
Create node functions. Each is an async function taking TripState, returning a partial state dict.

Requirements:
- weather_node(state): call WeatherAgent.invoke() with city + dates; return {"weather_data": result}
- poi_node(state): call AttractionAgent.invoke() with city + preferences; return {"poi_data": result}
- hotel_node(state): call HotelAgent.invoke() with city + hotel_type; return {"hotel_data": result}
- route_node(state): call MCP route tools based on transport preferences; return {"route_data": result}
- synthesis_node(state): call planner LLM with all collected data; return {"final_plan": parsed_json}
- error_node(state): log error, return {"error": state["error"], "final_plan": None}

Each node must:
  1. Import settings from config, create LLM fresh (do not cache at module level)
  2. Wrap the main call in try/except; on exception set state["error"] and return
  3. Log start/end with structlog: logger.info("node_start", node="weather", city=state["city"])
  4. Use tenacity @retry on the actual LLM/MCP calls inside each node

TASK C — Create agents/graph.py
Build the StateGraph:

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver
from agents.state import TripState
from agents.nodes import weather_node, poi_node, hotel_node, route_node, synthesis_node, error_node

def should_continue(state: TripState) -> str:
    if state.get("error") and state.get("retry_count", 0) < 2:
        return "retry"
    elif state.get("error"):
        return "error"
    return "continue"

def build_graph(checkpointer=None) -> any:
    builder = StateGraph(TripState)

    builder.add_node("weather", weather_node)
    builder.add_node("poi", poi_node)
    builder.add_node("hotel", hotel_node)
    builder.add_node("route", route_node)
    builder.add_node("synthesize", synthesis_node)
    builder.add_node("error_handler", error_node)

    # Entry: run weather, poi, hotel in sequence (parallel requires Send API)
    builder.set_entry_point("weather")
    builder.add_edge("weather", "poi")
    builder.add_edge("poi", "hotel")
    builder.add_edge("hotel", "route")
    builder.add_conditional_edges(
        "route",
        should_continue,
        {"retry": "poi", "continue": "synthesize", "error": "error_handler"}
    )
    builder.add_edge("synthesize", END)
    builder.add_edge("error_handler", END)

    checkpointer = checkpointer or MemorySaver()
    return builder.compile(checkpointer=checkpointer)

# Module-level compiled graph (shared across Streamlit sessions)
graph = build_graph()

TASK D — Rewrite agents/planner.py
Replace TripPlanner with a thin wrapper around the StateGraph:

class TripPlanner:
    def __init__(self):
        from agents.graph import graph
        self.graph = graph

    def _make_config(self, thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def invoke(self, user_input: str, thread_id: str = "default") -> dict:
        state = _parse_user_input(user_input)  # extract city, dates, prefs from string
        result = await self.graph.ainvoke(state, config=self._make_config(thread_id))
        return result.get("final_plan") or {}

    async def stream(self, user_input: str, thread_id: str = "default"):
        state = _parse_user_input(user_input)
        config = self._make_config(thread_id)
        async for event in self.graph.astream_events(state, config=config, version="v2"):
            yield event

    def _parse_user_input(self, text: str) -> TripState:
        # simple parser — extract city (first word before "日游"), dates, keep rest as extra
        # returns a TripState dict with sensible defaults for missing fields
        ...

TASK E — Update app.py to use new TripPlanner
- get_planner() should return TripPlanner() (no LLM arg needed)
- st.session_state should store a thread_id (generate with str(uuid.uuid4()) on first run)
- Pass thread_id to planner.invoke() / planner.stream()

SUCCESS CRITERIA:
- python -c "from agents.graph import graph; print(graph.get_graph())"  — prints graph description
- agents/state.py, agents/nodes.py, agents/graph.py all exist and import without error
- TripState TypedDict has all 14 fields listed above
```

---

## SPRINT 3 — Pydantic v2 Structured Output

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-2 complete. LangGraph graph is running.
Replace fragile string JSON parsing with Pydantic v2 structured output.

TASK A — Create schemas.py in project root.
Define these models with full field validation:

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal

class WeatherInfo(BaseModel):
    date: str
    day_weather: str
    night_weather: str
    day_temp: float
    night_temp: float
    wind_direction: str
    wind_power: str

    @field_validator("day_temp", "night_temp", mode="before")
    @classmethod
    def strip_unit(cls, v):
        # handle "25°C" → 25.0
        if isinstance(v, str):
            return float(v.replace("°C", "").replace("℃", "").strip())
        return float(v)

class Attraction(BaseModel):
    name: str
    address: str = ""
    visit_duration: int = Field(default=60, ge=15, le=480)
    ticket_price: float = Field(default=0.0, ge=0)
    category: str = ""
    description: str = ""
    location: dict = Field(default_factory=dict)

class Meal(BaseModel):
    type: Literal["breakfast", "lunch", "dinner"]
    name: str
    description: str = ""
    estimated_cost: float = Field(default=50.0, ge=0)

class Hotel(BaseModel):
    name: str
    address: str = ""
    rating: str = ""
    estimated_cost: float = Field(default=300.0, ge=0)
    type: str = ""
    distance: str = ""
    location: dict = Field(default_factory=dict)

class DayPlan(BaseModel):
    date: str
    day_index: int = Field(ge=0)
    description: str = ""
    transportation: str = ""
    hotel: Hotel = Field(default_factory=Hotel.model_construct)
    attractions: list[Attraction] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_three_meals(self):
        meal_types = {m.type for m in self.meals}
        for mtype in ["breakfast", "lunch", "dinner"]:
            if mtype not in meal_types:
                self.meals.append(Meal(type=mtype, name="自选", estimated_cost=50))
        return self

class Budget(BaseModel):
    total_attractions: float = 0
    total_hotels: float = 0
    total_meals: float = 0
    total_transportation: float = 0
    total: float = 0

    @model_validator(mode="after")
    def compute_total(self):
        computed = self.total_attractions + self.total_hotels + self.total_meals + self.total_transportation
        if self.total == 0 and computed > 0:
            self.total = computed
        return self

class TravelPlan(BaseModel):
    city: str
    start_date: str
    end_date: str
    days: list[DayPlan] = Field(min_length=1)
    weather_info: list[WeatherInfo] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    overall_suggestions: str = ""

TASK B — Use with_structured_output in synthesis_node (agents/nodes.py)
In synthesis_node, change the LLM call to:

  from schemas import TravelPlan
  structured_llm = llm.with_structured_output(TravelPlan, method="json_mode")

  try:
      plan: TravelPlan = await structured_llm.ainvoke(synthesis_messages)
      return {"final_plan": plan.model_dump()}
  except Exception:
      # Fallback: try raw JSON extraction
      raw = await llm.ainvoke(synthesis_messages)
      return {"final_plan": _fallback_parse(raw.content)}

def _fallback_parse(text: str) -> dict | None:
    """Extract JSON from mixed text — tolerant parser."""
    import json, re
    match = re.search(r'\{[\s\S]*\}', text)
    if not match:
        return None
    try:
        return json.loads(match.group())
    except json.JSONDecodeError:
        return None

TASK C — Update render.py parse_plan()
Replace the current fragile implementation with:

def parse_plan(text: str | dict) -> dict | None:
    if isinstance(text, dict):
        return text
    try:
        from schemas import TravelPlan
        import json, re
        match = re.search(r'\{[\s\S]*\}', text)
        if not match:
            return None
        data = json.loads(match.group())
        plan = TravelPlan.model_validate(data)
        return plan.model_dump()
    except Exception:
        return None

SUCCESS CRITERIA:
- python -c "from schemas import TravelPlan; p = TravelPlan(city='北京', start_date='2026-06-01', end_date='2026-06-03', days=[...]); print(p.budget.total)"
- Temperature "25°C" input is parsed to float 25.0 by WeatherInfo validator
- Budget.total is auto-computed when set to 0
```

---

## SPRINT 4 — Redis Cache Layer

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-3 complete. Redis is running on localhost:6379.
Add a caching layer to avoid redundant MCP calls for weather/POI/routes.
Same city + same date → return cached result within TTL window.

TASK A — Create cache/client.py

import json
import redis.asyncio as aioredis
from functools import wraps
from config import settings

TTL_WEATHER = 6 * 3600     # 6 hours
TTL_POI     = 24 * 3600    # 24 hours
TTL_ROUTE   = 3600         # 1 hour

class CacheClient:
    _pool: aioredis.ConnectionPool | None = None

    @classmethod
    async def get(cls) -> aioredis.Redis:
        if cls._pool is None:
            cls._pool = aioredis.ConnectionPool.from_url(
                settings.redis_url,
                db=2,
                max_connections=20,
                decode_responses=True,
            )
        return aioredis.Redis(connection_pool=cls._pool)

async def cache_get(key: str) -> dict | None:
    r = await CacheClient.get()
    val = await r.get(key)
    return json.loads(val) if val else None

async def cache_set(key: str, value: dict, ttl: int) -> None:
    r = await CacheClient.get()
    await r.setex(key, ttl, json.dumps(value, ensure_ascii=False))

def cached(key_template: str, ttl: int):
    """
    Decorator for async node functions.
    key_template: string with {arg_name} placeholders matching function params.
    Example: @cached("weather:{city}:{date}", TTL_WEATHER)
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            import inspect
            sig = inspect.signature(fn)
            bound = sig.bind(*args, **kwargs)
            bound.apply_defaults()
            key = key_template.format(**bound.arguments)
            cached_val = await cache_get(key)
            if cached_val is not None:
                import structlog
                structlog.get_logger().info("cache_hit", key=key)
                return cached_val
            result = await fn(*args, **kwargs)
            if result is not None:
                await cache_set(key, result, ttl)
            return result
        return wrapper
    return decorator

TASK B — Create cache/keys.py

def weather_key(city: str, date: str) -> str:
    return f"weather:{city}:{date}"

def poi_key(city: str, category: str) -> str:
    return f"poi:{city}:{category}"

def route_key(origin: str, destination: str, mode: str) -> str:
    return f"route:{mode}:{origin}:{destination}"

TASK C — Apply caching in agents/nodes.py
Wrap the MCP tool calls inside weather_node and poi_node with the @cached decorator.
Do NOT cache hotel_node results (prices change frequently).
Do NOT cache synthesis_node (always fresh generation).

In weather_node, the actual MCP call should be extracted to a separate function:

@cached("weather:{city}:{date}", TTL_WEATHER)
async def _fetch_weather(city: str, date: str) -> dict:
    # call weather MCP tool here
    ...

TASK D — Add cache stats to /health endpoint (anticipating Sprint 5)
In cache/client.py, add:

async def get_cache_info() -> dict:
    r = await CacheClient.get()
    info = await r.info("stats")
    return {
        "keyspace_hits": info.get("keyspace_hits", 0),
        "keyspace_misses": info.get("keyspace_misses", 0),
    }

SUCCESS CRITERIA:
- python -c "import asyncio; from cache.client import cache_set, cache_get; asyncio.run(cache_set('test', {'a':1}, 60)); print(asyncio.run(cache_get('test')))"
  → prints {'a': 1}
- Running the same city query twice: second call completes instantly (< 100ms) vs first call
- redis-cli -n 2 keys '*' shows weather/poi keys after a planning run
```

---

## SPRINT 5 — FastAPI + Celery + SSE Streaming

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-4 complete. Redis cache working. 
Add enterprise API layer: decouple long-running planning from HTTP request lifecycle.

TASK A — Create tasks/celery_app.py

from celery import Celery
from config import settings

celery_app = Celery("travel_agent")
celery_app.config_from_object({
    "broker_url": settings.redis_url.replace("redis://", "redis://") + "/0",
    "result_backend": settings.redis_url + "/1",
    "task_serializer": "json",
    "result_serializer": "json",
    "accept_content": ["json"],
    "task_track_started": True,
    "task_time_limit": 180,          # 3 min hard limit
    "task_soft_time_limit": 150,     # 2.5 min soft limit
    "task_routes": {
        "tasks.trip_tasks.plan_trip": {"queue": "trip.planning"},
        "tasks.trip_tasks.fetch_weather": {"queue": "trip.weather"},
        "tasks.trip_tasks.fetch_poi": {"queue": "trip.poi"},
    },
})

TASK B — Create tasks/trip_tasks.py

import asyncio, json
import redis as sync_redis
from celery import shared_task
from config import settings
from agents.graph import build_graph

def _publish_to_channel(channel: str, data: dict):
    r = sync_redis.from_url(settings.redis_url, db=5)
    r.publish(channel, json.dumps(data, ensure_ascii=False))

@celery_app.task(bind=True, name="tasks.trip_tasks.plan_trip")
def plan_trip_task(self, state_dict: dict) -> dict:
    """
    Long-running planning task. Runs LangGraph graph.
    Publishes streaming tokens to Redis pub/sub channel: stream:{task_id}
    Returns final plan dict.
    """
    task_id = self.request.id
    channel = f"stream:{task_id}"

    async def _run():
        graph = build_graph()
        config = {"configurable": {"thread_id": f"celery-{task_id}"}}

        async for event in graph.astream_events(state_dict, config=config, version="v2"):
            kind = event.get("event", "")
            if kind == "on_chat_model_stream":
                token = event["data"]["chunk"].content
                if token:
                    _publish_to_channel(channel, {"type": "token", "content": token})
            elif kind == "on_tool_start":
                _publish_to_channel(channel, {"type": "tool_start", "name": event.get("name")})
            elif kind == "on_tool_end":
                _publish_to_channel(channel, {"type": "tool_end", "name": event.get("name")})

        # Get final state
        final = await graph.aget_state(config)
        return final.values.get("final_plan") or {}

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(_run())
        _publish_to_channel(channel, {"type": "done", "plan": result})
        return result
    except Exception as e:
        _publish_to_channel(channel, {"type": "error", "message": str(e)})
        raise
    finally:
        loop.close()

TASK C — Create api/main.py

from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from api.routers import trips, health

app = FastAPI(title="Travel Agent API", version="1.0.0")

Instrumentator().instrument(app).expose(app, endpoint="/metrics")

app.include_router(trips.router, prefix="/api/v1")
app.include_router(health.router)

TASK D — Create api/routers/trips.py

Key endpoints:

POST /api/v1/trips
  - Body: {city, start_date, end_date, preferences, hotel_type, transport, extra}
  - Dispatch plan_trip_task.delay(state_dict)
  - Return: {"task_id": task_id, "status": "pending"}

GET /api/v1/trips/{task_id}
  - Query Celery result backend for task status
  - Return: {"task_id": task_id, "status": "pending|running|done|error", "result": {...}}

GET /api/v1/trips/{task_id}/stream  (Server-Sent Events)
  - Subscribe to Redis pub/sub channel stream:{task_id}
  - Use redis.asyncio pubsub; yield SSE events until "done" or "error" received
  - Use fastapi.responses.StreamingResponse with media_type="text/event-stream"
  - Format each event: f"data: {json.dumps(msg)}\n\n"
  - Set headers: {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}

TASK E — Create api/routers/health.py

GET /health
  - Check Redis: await redis_client.ping()
  - Check MCP: attempt get_tools_for("weather") with 5s timeout
  - Return: {"status": "ok"|"degraded", "redis": bool, "mcp": bool, "timestamp": iso_string}

TASK F — Create api/middleware/rate_limit.py
Sliding window rate limiter using Redis:
  - Key: ratelimit:{client_ip}:{current_minute}
  - INCR the key; EXPIRE to 60s
  - If count > settings.rate_limit_per_minute: return 429 with Retry-After header

SUCCESS CRITERIA:
- uvicorn api.main:app --port 8000 starts without error
- curl http://localhost:8000/health returns {"status":"ok",...}
- curl http://localhost:8000/metrics returns Prometheus text format
- celery -A tasks.celery_app worker --loglevel=info starts without error
- curl -X POST http://localhost:8000/api/v1/trips -d '{"city":"北京",...}' returns {"task_id":"..."}
```

---

## SPRINT 6 — PostgreSQL + Audit Logging

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-5 complete. FastAPI + Celery running.
Add PostgreSQL persistence for trip plans and audit trail.
Start PostgreSQL via Docker: docker run -d --name travel-pg \
  -e POSTGRES_DB=travel_agent -e POSTGRES_USER=travel \
  -e POSTGRES_PASSWORD=securepassword -p 5432:5432 postgres:16-alpine

TASK A — Create db/session.py

from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from sqlalchemy.orm import DeclarativeBase
from config import settings

engine = create_async_engine(settings.database_url, pool_size=10, max_overflow=20)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

async def get_db() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session

TASK B — Create db/models.py

import uuid
from datetime import datetime
from sqlalchemy import String, DateTime, JSON, Text, ForeignKey, BigInteger
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from db.session import Base

class TripPlan(Base):
    __tablename__ = "trip_plans"

    id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    thread_id: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(64), index=True)
    city: Mapped[str] = mapped_column(String(128), nullable=False)
    start_date: Mapped[str] = mapped_column(String(16))
    end_date: Mapped[str] = mapped_column(String(16))
    preferences: Mapped[dict | None] = mapped_column(JSON)
    plan_json: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    error_msg: Mapped[str | None] = mapped_column(Text)
    token_usage: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow)

    audit_logs: Mapped[list["AuditLog"]] = relationship(back_populates="trip", cascade="all, delete-orphan")

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    trip_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), ForeignKey("trip_plans.id"), index=True)
    event: Mapped[str] = mapped_column(String(64))
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)

    trip: Mapped["TripPlan"] = relationship(back_populates="audit_logs")

TASK C — Initialize Alembic and create first migration
  1. Run: alembic init db/migrations
  2. Edit alembic.ini: set sqlalchemy.url = postgresql+asyncpg://travel:securepassword@localhost/travel_agent
  3. Edit db/migrations/env.py: import Base from db.session; set target_metadata = Base.metadata
  4. Run: alembic revision --autogenerate -m "initial schema"
  5. Run: alembic upgrade head

TASK D — Integrate DB writes into Celery tasks (tasks/trip_tasks.py)
  - Before dispatching: INSERT TripPlan(status="pending", ...)
  - Task start: UPDATE status="planning"
  - Task done: UPDATE status="done", plan_json=result
  - Task error: UPDATE status="error", error_msg=str(e)
  - After each node: INSERT AuditLog(event="node_complete", detail={node_name, duration_ms})

TASK E — Add GET /api/v1/trips/history to trips router
  - Query: SELECT * FROM trip_plans WHERE user_id=? ORDER BY created_at DESC LIMIT 20
  - Return list of {id, city, start_date, end_date, status, created_at}

SUCCESS CRITERIA:
- alembic upgrade head completes without error
- After one planning request, SELECT * FROM trip_plans; shows a row
- SELECT * FROM audit_logs; shows node_complete events
- GET /api/v1/trips/history returns a JSON array
```

---

## SPRINT 7 — Observability: structlog + Prometheus + LangSmith

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-6 complete. Full stack running.
Add production observability: structured logs, metrics, distributed tracing.

TASK A — Configure structlog globally
Create a new file: logging_config.py

import logging
import structlog

def configure_logging():
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.JSONRenderer(),   # machine-parseable JSON output
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )

Call configure_logging() at the top of:
  - api/main.py
  - tasks/celery_app.py
  - Agent.py (CLI entry)

In every agents/nodes.py function, add:
  import structlog
  logger = structlog.get_logger()
  logger.info("node_start", node="weather", city=state["city"])
  # ... at end:
  logger.info("node_done", node="weather", duration_ms=elapsed_ms, cache_hit=from_cache)

TASK B — Add Prometheus metrics
Create monitoring/metrics.py:

from prometheus_client import Counter, Histogram, Gauge

TRIPS_SUBMITTED   = Counter("trips_submitted_total", "Trip planning requests submitted")
TRIPS_COMPLETED   = Counter("trips_completed_total", "Trip plans completed", ["status"])  # status: done|error
PLANNING_DURATION = Histogram("planning_duration_seconds", "Total planning wall time",
                               buckets=[10, 30, 60, 90, 120, 180])
NODE_DURATION     = Histogram("node_duration_seconds", "Per-node execution time", ["node"])
MCP_CALLS         = Counter("mcp_calls_total", "MCP tool invocations", ["tool", "status"])
CACHE_HITS        = Counter("cache_hits_total", "Redis cache hits", ["cache"])
ACTIVE_TASKS      = Gauge("celery_active_tasks", "Currently executing Celery tasks")

Instrument:
- TRIPS_SUBMITTED.inc() when plan_trip_task is dispatched
- TRIPS_COMPLETED.labels(status="done").inc() when task completes
- PLANNING_DURATION.observe(elapsed) in the Celery task wrapper
- NODE_DURATION.labels(node=name).observe(elapsed) in each node
- MCP_CALLS.labels(tool=name, status="success"|"error").inc() in MCP wrapper
- CACHE_HITS.labels(cache="weather").inc() in cached decorator

TASK C — Enable LangSmith tracing
In config.py Settings, ensure these are set from .env:
  langchain_tracing_v2: bool = True
  langchain_project: str = "travel-agent-v1"
  langchain_api_key: str = ""

In logging_config.py configure_logging(), add:
  import os
  os.environ["LANGCHAIN_TRACING_V2"] = str(settings.langchain_tracing_v2).lower()
  os.environ["LANGCHAIN_PROJECT"] = settings.langchain_project
  if settings.langchain_api_key:
      os.environ["LANGCHAIN_API_KEY"] = settings.langchain_api_key

TASK D — Sentry error tracking
In api/main.py, after app creation:
  import sentry_sdk
  from sentry_sdk.integrations.fastapi import FastApiIntegration
  from sentry_sdk.integrations.celery import CeleryIntegration
  if settings.sentry_dsn:
      sentry_sdk.init(
          dsn=settings.sentry_dsn,
          integrations=[FastApiIntegration(), CeleryIntegration()],
          traces_sample_rate=0.1,
      )

TASK E — Start monitoring Docker services
Run these Docker commands (do NOT use docker-compose yet):
  docker run -d --name travel-prometheus \
    -p 9090:9090 \
    -v $(pwd)/monitoring/prometheus.yml:/etc/prometheus/prometheus.yml \
    prom/prometheus:latest

Create monitoring/prometheus.yml:
  global:
    scrape_interval: 15s
  scrape_configs:
    - job_name: travel_api
      static_configs:
        - targets: ['host.docker.internal:8000']
      metrics_path: /metrics

SUCCESS CRITERIA:
- All log output is JSON formatted (test by running Agent.py and checking stderr)
- curl http://localhost:9090/targets shows travel_api target as UP
- curl http://localhost:8000/metrics shows trips_submitted_total counter
- After one planning run: curl http://localhost:8000/metrics | grep trips_completed
```

---

## SPRINT 8 — Human-in-the-Loop + Multi-turn Conversation

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: Sprints 0-7 complete. Full observability in place.
Add two key UX/enterprise features: plan review before finalizing, and multi-turn conversation.

TASK A — Add interrupt() node to LangGraph graph
In agents/graph.py, add a review node between route and synthesize:

from langgraph.types import interrupt

def review_node(state: TripState) -> dict:
    """
    Interrupt graph execution here. Returns control to the caller.
    The caller (Streamlit UI) will display the draft and collect feedback.
    On resume, state["user_feedback"] will contain the feedback string.
    """
    draft_summary = {
        "city": state["city"],
        "weather": state.get("weather_data", {}),
        "poi_count": len(state.get("poi_data") or []),
        "hotel_count": len(state.get("hotel_data") or []),
    }
    feedback = interrupt({
        "type": "plan_review",
        "draft": draft_summary,
        "prompt": "数据收集完成。请确认继续生成完整计划，或输入修改意见。",
    })
    return {"user_feedback": feedback, "messages": [{"role": "user", "content": feedback}]}

Add to TripState TypedDict:
  user_feedback: str | None

In agents/graph.py build_graph():
  builder.add_node("review", review_node)
  # Change: route → review → synthesize (instead of route → synthesize directly)
  builder.add_edge("route", "review")
  builder.add_edge("review", "synthesize")
  # Compile with interrupt:
  return builder.compile(
      checkpointer=checkpointer,
      interrupt_before=["review"],   # pause before review node runs
  )

TASK B — Add HitL flow to Streamlit app.py
Modify the planning flow to be two-phase:

Phase 1 (initial run):
  1. Invoke graph — it will stop at interrupt_before=["review"]
  2. Get the interrupt value: state = graph.get_state(config); interrupt_val = state.values.get(...)
  3. Show draft summary to user in st.expander("数据收集预览")
  4. Show a text_input("修改意见（留空则直接生成）") and button "确认生成"

Phase 2 (resume after user input):
  1. User clicks confirm button
  2. feedback = st.session_state.user_feedback_input or "请继续生成完整计划"
  3. Resume: await graph.aupdate_state(config, {"user_feedback": feedback})
  4. Continue: async for event in graph.astream(..., config=config): ...
  5. Collect final plan and render as before

TASK C — Multi-turn plan modification
Add a "修改计划" section below the rendered plan:

  st.markdown("### 🔄 修改计划")
  modification = st.text_input("输入修改要求", placeholder="例如：把第二天改成去博物馆，减少购物")
  if st.button("应用修改") and modification:
      # Re-invoke graph with existing thread_id — LangGraph resumes from checkpoint
      config = {"configurable": {"thread_id": st.session_state.thread_id}}
      new_state = {"messages": [{"role": "user", "content": f"请修改行程：{modification}"}]}
      # Add a "modify" entry point that goes directly to synthesize with the feedback
      result = await graph.ainvoke(new_state, config=config)
      st.session_state.plan_data = result.get("final_plan")
      st.rerun()

TASK D — Update synthesis_node to be modification-aware
In agents/nodes.py synthesis_node:
  - Check if state["user_feedback"] is not None and not default
  - If yes: include the original plan JSON in the synthesis prompt as context
  - Prompt addition: "原始计划: {json.dumps(state['final_plan'])}. 用户修改要求: {feedback}. 请在原计划基础上做最小修改。"
  - This prevents full re-planning when only small changes are needed (token savings ~40%)

SUCCESS CRITERIA:
- First planning run pauses at interrupt; Streamlit shows draft summary
- Clicking confirm resumes and produces final plan
- In a second run with same thread_id and "修改" input, the second plan reflects the modification
- redis-cli -n 0 keys 'checkpoint:*' shows checkpoints being stored
```

---

## SPRINT 9 — Testing + Evaluation Suite

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: All feature sprints complete. Add tests and evaluation.

TASK A — Create tests/conftest.py with shared fixtures:
- pytest_plugins = ["anyio"]  (for async tests)
- fixture: redis_client — returns a test Redis client using DB 15 (isolated)
- fixture: test_settings — override settings to use test Redis DB
- fixture: sample_state — a TripState dict with city="北京", 2-day trip

TASK B — Create tests/test_schemas.py
Test all Pydantic validators:
- test_weather_strips_unit: WeatherInfo(day_temp="25°C") → day_temp == 25.0
- test_budget_auto_total: Budget with components but total=0 → total is computed correctly
- test_day_ensures_three_meals: DayPlan with 1 meal → meals list has 3 after validation
- test_travel_plan_requires_days: TravelPlan(days=[]) → ValidationError
- test_fallback_parse_extracts_json: render.parse_plan("some text {\"city\":\"北京\",\"days\":[...]} more text")

TASK C — Create tests/test_cache.py
- test_cache_set_get: set a key, get it back, assert equal
- test_cache_ttl: set key with TTL 1; sleep 2; assert get returns None
- test_cache_miss_returns_none: get nonexistent key returns None
- test_cached_decorator: apply @cached to a mock async fn; call twice; assert fn called once

TASK D — Create tests/test_api.py  (use httpx.AsyncClient + TestClient)
- test_health_returns_ok: GET /health → 200, status=="ok"
- test_submit_trip_returns_task_id: POST /api/v1/trips → 200, has "task_id"
- test_rate_limit: POST /api/v1/trips 11 times → 11th returns 429
- test_metrics_endpoint: GET /metrics → 200, contains "trips_submitted_total"

TASK E — Create tests/test_graph.py (mock MCP tools)
- Use unittest.mock.AsyncMock to mock McpClientManager.get_tools_for
- test_weather_node_caches: run weather_node twice with same city; second call is cache hit
- test_graph_reaches_end: run full graph with mocked nodes; final state has "final_plan"
- test_error_node_on_mcp_failure: mock MCP to raise exception; graph reaches error_handler node
- test_retry_increments_count: on failure, retry_count increments in state

TASK F — Create tests/eval/evaluator.py (LangSmith evaluation)
Implement three evaluators:
1. completeness_evaluator: score = (days with 3 meals + hotel) / total_days
2. preference_match_evaluator: score = |pref_set ∩ category_set| / |pref_set|
3. budget_consistency_evaluator: score = 1.0 if budget.total == sum of components else 0.0

Create a local test dataset (no LangSmith account needed for local run):
  TEST_CASES = [
    {
      "inputs": {"city": "北京", "start_date": "2026-06-01", "end_date": "2026-06-03",
                 "preferences": ["历史文化"], ...},
      "expected": {"min_days": 2, "required_categories": ["历史文化"]}
    },
    ...  # 5 test cases covering: short trip, long trip, beach, mountain, city break
  ]

SUCCESS CRITERIA:
- pytest tests/ -v → all tests pass (or skip if DASHSCOPE_API_KEY not set for integration tests)
- pytest tests/test_schemas.py → 100% pass
- pytest tests/test_cache.py → 100% pass (requires running Redis)
- Coverage report: pytest --cov=agents --cov=cache --cov=schemas --cov-report=term-missing
```

---

## SPRINT 10 — Docker Polish + Production Config

```
The operating environment is "conda activate Agent"
Working directory: /Users/richard/Desktop/agent_coding/travel-agent

CONTEXT: All code complete. Package everything for a clean demo and production readiness.

TASK A — Create Dockerfile (multi-stage)
Stage 1 (builder): install deps
Stage 2 (runtime): copy only necessary files, non-root user

FROM python:3.13-slim AS builder
WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir build && pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.13-slim AS runtime
RUN useradd -m -u 1000 appuser
WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels
COPY . .
USER appuser
EXPOSE 8000 8501

TASK B — Create nginx.conf

upstream api      { server api:8000; }
upstream streamlit{ server streamlit:8501; }

server {
    listen 80;
    location /api/  { proxy_pass http://api; proxy_set_header X-Real-IP $remote_addr; }
    location /stream { proxy_pass http://api; proxy_buffering off; proxy_read_timeout 300s; }
    location /       { proxy_pass http://streamlit; proxy_http_version 1.1;
                       proxy_set_header Upgrade $http_upgrade;
                       proxy_set_header Connection "upgrade"; }
}

TASK C — Create monitoring/grafana/dashboards/travel_agent.json
A Grafana dashboard JSON with 4 panels:
1. "Trips Submitted" — Counter rate (trips_submitted_total[5m])
2. "Planning Duration P95" — histogram_quantile(0.95, planning_duration_seconds)
3. "Active Celery Tasks" — Gauge (celery_active_tasks)
4. "Cache Hit Rate" — rate(cache_hits_total[5m]) / (rate(cache_hits_total[5m]) + rate(cache_misses_total[5m]))

TASK D — Create a Makefile for common ops:
make dev        → docker compose up -d redis postgres; uvicorn api.main:app --reload; streamlit run app/app.py
make worker     → celery -A tasks.celery_app worker -Q trip.planning,trip.weather,trip.poi --concurrency=3
make flower     → celery -A tasks.celery_app flower
make test       → pytest tests/ -v --cov=.
make migrate    → alembic upgrade head
make docker-up  → docker compose up -d --build
make docker-down→ docker compose down

TASK E — Create .env.example with all variables documented (with comments explaining each)

TASK F — Final integration test:
1. docker compose up -d (all services)
2. make migrate
3. curl http://localhost/health → {"status":"ok",...}
4. Post a real trip planning request via the Streamlit UI
5. Check Grafana at http://localhost:3000 — verify metrics appear
6. Check Flower at http://localhost:5555 — verify task completed

SUCCESS CRITERIA:
- docker compose up starts all 8 services without error
- All health checks pass: curl http://localhost/health
- Grafana dashboard shows live data after one planning run
- A full end-to-end trip plan is generated and visible in the Streamlit UI
- Trip plan is persisted in PostgreSQL: psql -h localhost -U travel travel_agent -c "SELECT city, status FROM trip_plans;"
```
