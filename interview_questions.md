# Travel-Agent v1 — Core Interview Questions & Model Answers

> **How to use:** Each question has a 30-second "hook" (what to say first) and a full answer with code references.  
> Questions are grouped by topic. Interviewers typically open with System Design, then drill into one area.

---

## Category 1 — System Design

### Q1: Walk me through the architecture of your travel agent system.

**30-second hook:**  
"It's a multi-agent pipeline built on LangGraph StateGraph, exposed via a FastAPI backend and Streamlit UI, with Redis handling caching, session storage, and streaming pub/sub, and Celery offloading the long-running planning tasks."

**Full answer:**

The system has four layers:

**Layer 1 — API / UI**: FastAPI handles REST requests and SSE streaming. Streamlit provides the interactive UI. Both sit behind Nginx for routing and rate limiting.

**Layer 2 — Task queue**: Planning takes 60–120 seconds. Rather than block an HTTP worker, FastAPI dispatches the work to a Celery task (`plan_trip_task`) and immediately returns a `task_id`. The client then opens a Server-Sent Events connection to stream results as they arrive.

**Layer 3 — Agent core**: A LangGraph `StateGraph` with 6 nodes: `weather → poi → hotel → route → review (interrupt) → synthesize`. Each node is independent, runs a domain-specific sub-agent against the Amap MCP tools, and writes its output to a shared `TripState`. The synthesis node collects all intermediate results and produces a final Pydantic-validated `TravelPlan`.

**Layer 4 — Infrastructure**: Redis serves 5 distinct roles on separate DBs: Celery broker (DB 0), Celery results (DB 1), response cache (DB 2, weather 6h TTL / POI 24h TTL), session store (DB 3), and pub/sub streaming channel (DB 5). PostgreSQL stores final plans and an audit log of every node execution.

**Why this design?** The key constraint is that LLM calls are slow and non-deterministic. The Celery+SSE pattern solves the HTTP timeout problem. The StateGraph gives us per-node observability, retry capability, and human-in-the-loop interruption — none of which you get from a simple `create_react_agent` loop.

---

### Q2: How does your system handle 50 concurrent users?

**30-second hook:**  
"Each concern is isolated: Streamlit sessions are isolated by `thread_id`, MCP connections are pooled, LLM calls go through Celery workers, and Redis rate limiting prevents any single user from starving others."

**Full answer:**

Five specific solutions:

**1. Streamlit session isolation:** Every user gets a `thread_id = str(uuid4())` stored in `st.session_state`. All LangGraph calls pass this as `{"configurable": {"thread_id": thread_id}}`. The Redis-backed `AsyncRedisSaver` checkpoint store uses `thread_id` as the isolation key — state from user A is completely invisible to user B.

**2. MCP connection pooling:** The `McpClientManager` singleton holds an `httpx.AsyncClient` with `Limits(max_connections=20, max_keepalive_connections=10)`. Without this, 50 concurrent users would each open a new HTTP connection to the Amap MCP server, exhausting the server's connection limit and causing cascading failures.

**3. Celery horizontal scaling:** The `plan_trip_task` is dispatched to a Celery queue. We run 2 worker containers, each with `concurrency=3`, giving 6 simultaneous planning jobs. Adding capacity is `docker compose scale worker=4`.

**4. Redis caching:** Weather data for the same city on the same date is cached for 6 hours. In practice, 30-40% of city lookups are repeat queries (Shanghai, Beijing, Chengdu are heavily requested). Cache hits return in < 10ms vs 8–12 seconds for a live MCP call.

**5. Rate limiting:** A Redis sliding-window rate limiter (`INCR ratelimit:{ip}:{minute}`, TTL 60s) caps each IP at 10 planning requests per minute. Excess requests get a 429 with a `Retry-After` header — this protects both the Celery queue and the DashScope API token budget.

**What breaks first at high load?** The DashScope API rate limit (tokens per minute). The mitigation is tenacity retry with exponential backoff — `wait_exponential(min=2, max=30)` — which gracefully absorbs burst traffic at the cost of slightly longer latency.

---

### Q3: Describe a single request's journey from browser to final answer.

**Full answer (trace the path):**

```
User clicks "开始规划" in Streamlit
  │
  ├─ Streamlit calls: POST /api/v1/trips
  │    Body: {city, dates, preferences, ...}
  │    FastAPI: rate limit check → pass
  │    FastAPI: INSERT TripPlan(status="pending") to PostgreSQL
  │    FastAPI: plan_trip_task.delay(state_dict) → Celery broker (Redis DB 0)
  │    FastAPI: return {"task_id": "abc-123"}  ← responds in < 50ms
  │
  ├─ Streamlit opens: GET /api/v1/trips/abc-123/stream (SSE)
  │    FastAPI: subscribe to Redis pub/sub channel "stream:abc-123"
  │    Celery worker picks up task:
  │      UPDATE TripPlan status="planning"
  │      Build LangGraph graph with thread_id="celery-abc-123"
  │      ┌─ weather_node:
  │      │    Check Redis cache "weather:北京:2026-06-01" → miss
  │      │    Call Amap MCP maps_weather(city="北京")
  │      │    Store result in Redis cache (TTL 6h)
  │      │    Publish: {"type":"tool_end","name":"weather"} → Redis channel
  │      │    structlog: {"event":"node_done","node":"weather","duration_ms":1240}
  │      ├─ poi_node → hotel_node → route_node (similar pattern)
  │      ├─ interrupt at review node → (in this flow, auto-confirm)
  │      └─ synthesis_node:
  │           llm.with_structured_output(TravelPlan).ainvoke(all_data)
  │           Publish token by token: {"type":"token","content":"..."} → Redis channel
  │           Publish: {"type":"done","plan":{...}} → Redis channel
  │      UPDATE TripPlan status="done", plan_json={...}
  │
  ├─ FastAPI SSE endpoint reads each published message:
  │    yields: "data: {\"type\":\"token\",\"content\":\"# 北京旅行计划\"}\n\n"
  │    Browser receives tokens → Streamlit updates UI in real time
  │
  └─ On "done" message: SSE closes, Streamlit renders final structured plan
```

Total time: ~60-90s from submit to final render. SSE keeps the browser updated throughout.

---

## Category 2 — LangGraph & Agent Architecture

### Q4: Why StateGraph over create_react_agent?

**30-second hook:**  
"create_react_agent is a preset — it gives you a working ReAct loop in 3 lines, but you have no control over the loop. StateGraph is the full API — you define every node, every edge, every routing decision explicitly."

**Full answer:**

Three concrete reasons I chose StateGraph:

**1. Deterministic routing.** The planning workflow has a fixed structure: always query weather first, then POI, then hotels, then routes. With `create_react_agent`, the LLM decides what tools to call and in what order — and it sometimes skips weather or calls hotel search before POI search. With `StateGraph`, I define `weather → poi → hotel → route → synthesize` as explicit edges. The LLM has no say in the order.

**2. Per-node error handling and retry.** If the MCP server returns a 429 during `poi_node`, I want to retry that specific node without restarting from weather. In StateGraph, I add a conditional edge: `route → {"retry": "poi", "continue": "synthesize", "error": "error_handler"}` based on `state["error"]` and `state["retry_count"]`. In a ReAct loop, there's no granular way to do this.

**3. Human-in-the-Loop.** I compile the graph with `interrupt_before=["review"]`. After the data-gathering nodes complete, the graph pauses, the Streamlit UI shows the draft to the user, and after confirmation, the graph resumes from the exact checkpoint. This requires checkpointing, which StateGraph provides natively via `MemorySaver`/`AsyncRedisSaver`. You cannot interrupt a ReAct loop mid-execution.

**Trade-off:** StateGraph is more code — ~200 lines vs ~20 lines for create_react_agent. For a single-tool chatbot, that overhead isn't worth it. For a multi-step pipeline with retry, observability, and HitL requirements, StateGraph is the right tool.

---

### Q5: How does multi-turn conversation work? User says "change day 2 to museum visits."

**Full answer:**

The mechanism is LangGraph's `MemorySaver` with `thread_id` isolation.

When the user first generates a plan, the graph runs with `thread_id = "abc-123"`. After completion, LangGraph has stored the full `TripState` (including `final_plan`) as a checkpoint in Redis, keyed by `thread_id`.

When the user submits "change day 2 to museum visits":
1. Streamlit sends `PATCH /api/v1/trips/{task_id}` with `{"instruction": "change day 2 to museum visits"}`
2. The API appends a new user message to the graph: `graph.aupdate_state(config, {"messages": [HumanMessage("change day 2...")]})`
3. The graph resumes from its last checkpoint (it has all the previous weather/POI/hotel data in state)
4. Instead of re-running all nodes, we route directly to `synthesis_node` with the modification instruction
5. `synthesis_node` receives: original `final_plan` JSON + the modification request
6. The synthesis prompt says: "Here is the existing plan: {plan_json}. The user wants: 'change day 2 to museum visits'. Make MINIMAL changes to the existing plan."

This approach saves ~60% of tokens vs re-planning from scratch, because we skip all the MCP tool calls and reuse cached data.

**Edge case — what if the modification requires new data?** For example, "add a day 4." In that case, I check the modification instruction for keywords (`再加一天`, `新增`, specific attraction names) and conditionally re-run `poi_node` and `hotel_node` before synthesis.

---

### Q6: How do you handle the LLM producing malformed JSON output?

**Full answer: Three-layer defense**

**Layer 1 — `with_structured_output(TravelPlan, method="json_mode")`**  
LangChain passes the Pydantic schema to the LLM as a JSON schema in the system prompt and calls the API with `response_format={"type": "json_object"}`. If the output fails Pydantic validation, LangChain automatically retries (up to 3 times by default) with an error message telling the LLM what was wrong.

**Layer 2 — Pydantic validators handle common LLM quirks**  
Even when the JSON parses, LLMs often violate soft constraints:
- Temperature as "25°C" instead of 25: `@field_validator("day_temp", mode="before")` strips the unit
- Missing meals: `@model_validator(mode="after") def ensure_three_meals` fills in defaults
- Budget total = 0: `@model_validator(mode="after") def compute_total` calculates it from components
- These validators mean a "mostly correct" response becomes fully valid without a retry

**Layer 3 — `_fallback_parse()` for irrecoverable failures**  
If 3 retries all fail, we fall through to a regex-based JSON extractor:
```python
def _fallback_parse(text: str) -> dict | None:
    match = re.search(r'\{[\s\S]*\}', text)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            return None
    return None
```
This handles the case where the LLM wraps the JSON in markdown code fences or adds prose before/after it. The result won't pass Pydantic validation but is better than a blank page — we display what we can and show a warning.

**Lesson learned:** The Qwen3-max model follows JSON schemas reliably (~95% first-attempt success in testing). The 5% failure rate is almost entirely due to temperature fields with units ("25°C") and missing budget totals — both now handled by validators.

---

## Category 3 — Redis & Caching

### Q7: You use Redis for 5 different purposes. Walk through each.

**Full answer:**

**DB 0 — Celery Broker:** The message queue for task dispatch. When FastAPI calls `plan_trip_task.delay()`, Celery serializes the task to a Redis list (`celery`). Workers do `BLPOP` to pick up tasks. I configure Redis as the broker (not RabbitMQ) because we're already running Redis and the task volume doesn't justify a separate message broker.

**DB 1 — Celery Result Backend:** Stores task results so `GET /api/v1/trips/{task_id}` can poll status. Key pattern: `celery-task-meta-{task_id}`. TTL is 24h.

**DB 2 — Response Cache:** MCP tool results are cached by semantic key:
- `weather:{city}:{date}` → 6h TTL (weather doesn't change intraday)
- `poi:{city}:{category}` → 24h TTL (POI list changes weekly at most)
- `route:{mode}:{origin}:{dest}` → 1h TTL (traffic changes, routes don't)

The `@cached(key_template, ttl)` decorator wraps any async function transparently. Cache hits log `"cache_hit": true` in structlog and increment a Prometheus counter.

**DB 3 — Session Store:** User preferences and `thread_id` history. When a user returns after a week, their past `thread_id` list is in Redis — they can resume a previous plan. TTL is 7 days for session data, 30 days for the user→threads mapping.

**DB 4 — Rate Limiting:** Sliding window implementation using `INCR` + `EXPIRE`. Key: `ratelimit:{client_ip}:{unix_minute}`. Each request increments the counter; if > 10, return 429. The 60-second TTL auto-expires the key each minute without needing a cleanup job.

**DB 5 — Pub/Sub Streaming:** The SSE streaming channel. The Celery worker publishes tokens as it receives them from the LLM stream: `redis.publish(f"stream:{task_id}", json.dumps({"type":"token","content":token}))`. The FastAPI SSE endpoint subscribes and forwards each message to the browser. This is ephemeral — no persistence, channel disappears when the task completes.

**Why separate DBs instead of key prefixes?** Isolation for different eviction policies. DB 2 (cache) uses `allkeys-lru` eviction — Redis can drop cache entries under memory pressure. DB 3 (sessions) must NOT be evicted. Separate DB numbers give you per-DB eviction control and make `FLUSHDB` during testing safe (only wipes the test DB).

---

### Q8: How would you handle Redis going down?

**Full answer:**

Redis failure affects different features differently, so the response is layered:

**Cache miss degrades gracefully:** If `cache_get()` throws `ConnectionError`, I catch it and return `None` — the system falls through to a live MCP call. Slightly slower, but functional. This is the most important case.

```python
async def cache_get(key: str) -> dict | None:
    try:
        r = await CacheClient.get()
        val = await r.get(key)
        return json.loads(val) if val else None
    except (redis.ConnectionError, redis.TimeoutError):
        logger.warning("cache_unavailable", key=key)
        return None  # graceful miss
```

**Celery broker failure is fatal for async path:** If Redis (broker) is down, `plan_trip_task.delay()` raises `redis.exceptions.ConnectionError`. I catch this in the FastAPI endpoint and fall back to synchronous execution: run the graph directly in an `asyncio.gather()` call. This blocks the HTTP worker for the duration but keeps the service up. In the response, I set `X-Execution-Mode: sync` so monitoring can detect the degradation.

**Session store failure:** LangGraph's `AsyncRedisSaver` fails to write checkpoints. The graph still completes — it just can't be resumed in a future session. We log the error and continue; the user gets their plan but loses multi-turn capability for that session.

**Detection:** The `/health` endpoint checks Redis with a 2-second timeout and returns `{"redis": false}` if it fails. Prometheus alerts on this within 30 seconds. Grafana page fires to on-call.

---

## Category 4 — MCP Integration

### Q9: What is MCP and why did you choose it over a direct REST API call?

**Full answer:**

MCP (Model Context Protocol) is an open standard published by Anthropic in 2024 for exposing external tools to AI agents. It defines a transport-agnostic protocol (HTTP Streamable, SSE, stdio) for an AI host to discover and call tools from any MCP server.

**Why MCP over direct REST:** Amap (高德地图) publishes an official MCP server at the DashScope endpoint. The MCP server exposes ~15 geographic tools with standardized JSON schemas. Using `langchain_mcp_adapters.MultiServerMCPClient`, I get all these tools as `BaseTool` instances automatically — no manual wrapper code for each tool, no manual schema definition.

```python
client = MultiServerMCPClient({"amap-server": {
    "transport": "http",
    "url": settings.mcp_url,
    "headers": {"Authorization": f"Bearer {settings.dashscope_api_key}"}
}})
tools = await client.get_tools()  # returns 15 BaseTool instances automatically
```

If I called the Amap REST API directly, I'd need to write 15 Python wrapper functions, each with its own parameter handling and response parsing. MCP gives me that for free.

**The emerging significance:** MCP is becoming the "USB standard for AI tools." In 2025, major platforms (GitHub, Slack, Linear, Figma) are publishing official MCP servers. An agent built on MCP can plug into any of these without code changes — just add a new server config.

---

### Q10: You mentioned a KeyError bug in the Qwen streaming tool calls. Explain it.

**Full answer:**

This is a real upstream bug in `langchain_community.chat_models.tongyi.ChatTongyi`.

**What happens:** When Qwen returns a tool call in streaming mode, the response is sent as multiple chunks. The first chunk only contains `tool_call[0].id` and `tool_call[0].index` — it does NOT yet contain `function.name` or `function.arguments` (those come in subsequent chunks).

The `subtract_client_response` method in `ChatTongyi` is designed to compute the delta between the current chunk and the previous chunk to build up the incremental output. It does:
```python
function["name"] = function["name"].replace(prev_function["name"], "")
```
But when `prev_function` doesn't have `"name"` yet (first chunk), this raises `KeyError: 'name'`.

**My fix (monkey patch in `config.py`):**
```python
def _patched_subtract(self, resp, prev_resp):
    # ... (existing logic) ...
    if "name" in function and "name" in prev_function:      # guard added
        function["name"] = function["name"].replace(prev_function["name"], "")
    if "arguments" in function and "arguments" in prev_function:  # guard added
        function["arguments"] = function["arguments"].replace(prev_function["arguments"], "")
    return resp_copy

ChatTongyi.subtract_client_response = _patched_subtract
```

**Why monkey patch instead of forking?** Because `langchain_community` releases frequently and I want to receive security patches. The monkey patch is applied at import time, is isolated to one method, and the fix is small enough to recheck on each `langchain_community` version bump. I documented it with a comment pointing to the upstream issue number.

**Status:** I submitted the fix as a PR to `langchain_community`. Until it merges, the monkey patch is the cleanest solution.

---

## Category 5 — Concurrency & Async

### Q11: Explain the Streamlit + asyncio event loop problem and your solution.

**Full answer:**

**The problem:** Streamlit runs all user sessions in a single thread with its own event loop (managed by Tornado). When Python encounters `asyncio.run(coro)`, it tries to create a NEW event loop and run it. But the Python rule is: only one event loop can run per thread. Since Tornado already has one running, `asyncio.run()` raises `RuntimeError: This event loop is already running`.

**Why it matters:** The LangGraph agent uses `async/await` throughout. `planner.stream()` is an async generator. Calling it from Streamlit's sync context requires bridging async → sync.

**My solution — two approaches, I use both depending on context:**

**Approach 1: `nest_asyncio` (for Streamlit direct path)**
```python
import nest_asyncio
nest_asyncio.apply()  # patches asyncio to allow nested loops

# Now this works:
loop = asyncio.get_event_loop()
result = loop.run_until_complete(coro)
```
`nest_asyncio` patches `asyncio.BaseEventLoop.run_until_complete` to allow re-entrant calls. It's safe for our use case because Streamlit handles each user request synchronously within a single session.

**Approach 2: Dedicated thread with new event loop (for Celery workers)**
```python
def run_async_in_thread(coro):
    result = None
    exception = None
    def target():
        nonlocal result, exception
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(coro)
        except Exception as e:
            exception = e
        finally:
            loop.close()
    t = threading.Thread(target=target, daemon=True)
    t.start()
    t.join()
    if exception:
        raise exception
    return result
```
Celery workers are sync by design. The graph needs async. This approach creates a clean event loop in a daemon thread — no nesting, no patching, full isolation.

**Why `nest_asyncio` and not `asyncio.run()` in a thread?** `asyncio.run()` creates and destroys a loop each call — fine for one-off calls, but creates overhead when streaming (thousands of loop create/destroy cycles). `nest_asyncio` reuses the existing loop.

---

### Q12: How do you prevent one user's slow LLM call from blocking other users?

**Full answer:**

This is fundamentally a concurrency architecture question, and the answer is: by design, slow calls don't block other users, because the architecture separates request handling from work execution.

**In the FastAPI path:** FastAPI uses `uvicorn` with async workers. `POST /api/v1/trips` dispatches to Celery and returns in < 50ms. No blocking. Multiple simultaneous `POST` requests are handled concurrently by uvicorn's async event loop with no mutual interference.

**In the Celery path:** Workers are separate processes. A 120-second planning job occupies one worker process/coroutine. With `concurrency=3` per worker and 2 worker containers, we have 6 simultaneous planning slots. User 7 waits in the Redis queue, but Users 1-6 are unaffected.

**In the Streamlit direct path (no Celery):** This is the weak point. Streamlit is fundamentally synchronous per-session. While `astream_events` uses `asyncio`, Streamlit's session handler holds the GIL during the Python execution phases. Two simultaneous Streamlit users with long-running tasks WILL experience some slowdown. The mitigation:
1. Use `@st.cache_resource` for the compiled graph (avoids re-building LangGraph)
2. Use the FastAPI+Celery path for all production traffic (Streamlit calls the API, not the agent directly)
3. `streamlit run --server.numWorkers=4` to allow more parallel sessions

**Real answer for production:** The Streamlit UI is a demo interface. Production traffic goes through the FastAPI REST API, which is fully non-blocking.

---

## Category 6 — Enterprise Patterns

### Q13: How do you track costs? LLM calls aren't free.

**Full answer:**

Three levels of cost tracking:

**Level 1 — LangSmith automatic tracking:** Every LangChain/LangGraph call is automatically traced when `LANGCHAIN_TRACING_V2=true`. LangSmith records input tokens, output tokens, and (for supported models) estimated cost in USD. You can see cost per run, per chain, per tool call in the LangSmith dashboard.

**Level 2 — Custom token tracking in state:** I add a `token_usage` field to `TripState`:
```python
class TripState(TypedDict):
    token_usage: dict  # {"prompt_tokens": 0, "completion_tokens": 0}
```
In each node, after the LLM call, I extract usage:
```python
response = await llm.ainvoke(messages)
usage = response.usage_metadata or {}
state["token_usage"]["prompt_tokens"] += usage.get("input_tokens", 0)
state["token_usage"]["completion_tokens"] += usage.get("output_tokens", 0)
```
This accumulates across nodes and is stored in PostgreSQL `trip_plans.token_usage`.

**Level 3 — Budget enforcement:** In `synthesis_node`, I pass `max_tokens=settings.max_tokens` to the LLM call. The synthesis prompt (with all weather/POI/hotel data) is the largest call — I profile it to stay under 6,000 output tokens. If a user requests a 10-day trip, I chunk the synthesis into day-by-day calls to stay within token limits.

**Cost optimization tricks:**
- Redis caching for weather/POI saves ~40% of MCP token calls on repeated cities
- Synthesis prompt uses the raw MCP JSON (compact) not prose summaries as input
- For the multi-turn modification flow, only re-run `synthesis_node` (cheapest) not all nodes

---

### Q14: How do you test an agent? You can't unit test LLM responses.

**Full answer:**

You're right that you can't assert exact LLM outputs — but you can test everything around them and evaluate outputs probabilistically.

**Unit tests (no LLM, no MCP):**
- Pydantic schema validation: temperature parsing, budget auto-computation, meal count enforcement — all deterministic, 100% coverage
- Cache layer: mock `aioredis`, test cache miss → call → cache set flow
- Graph structure: `graph.get_graph().nodes` returns expected node names; edges connect correctly
- Node logic without LLM: mock `llm.ainvoke` to return a fixed response; test state transformations

**Integration tests (real Redis, mocked MCP):**
- `McpClientManager.get_tools_for()` is mocked to return `[FakeTool("maps_weather")]`
- Run the full graph against these mocked tools
- Assert that `final_plan` is non-None, has the expected structure, Pydantic validates cleanly

**End-to-end tests (real LLM, real MCP — marked `@pytest.mark.slow`):**
- Only run in CI with real API keys
- Assert structure, not content: `len(plan["days"]) >= 2`, `plan["budget"]["total"] > 0`
- Run against a fixed test case set (5 cities, 2-day and 5-day trips)

**Evaluation (LangSmith):**
- `completeness_score`: structural check — all 3 meals, hotel, attractions per day
- `preference_match_score`: user said "历史文化" → fraction of attractions with historical category
- `budget_consistency_score`: sum of components == total
- Run before and after any prompt change; regression if score drops > 5 points

**What I DON'T test:** Whether the plan is "good" in a human sense. That requires human evaluators or a separate LLM-as-judge setup (which I have as a TODO). The automated tests are guardrails, not quality certification.

---

### Q15: How do you handle the case where the Amap MCP server is down?

**Full answer: Circuit breaker + graceful degradation**

First line: `tenacity` retry with exponential backoff catches transient failures (network hiccup, brief 503):
```python
@retry(
    retry=retry_if_exception_type((httpx.ConnectError, httpx.TimeoutException)),
    wait=wait_exponential(multiplier=1, min=2, max=30),
    stop=stop_after_attempt(3),
)
async def call_mcp_tool(tool, args):
    return await tool.ainvoke(args)
```

Second line: Each node catches `Exception` and sets `state["error"]`. The conditional edge from `route_node` routes to `error_handler` node after 2 retries. The error handler logs the failure and returns a partial state.

Third line: `synthesis_node` handles partial state. If `weather_data` is None, the synthesis prompt says "weather data unavailable — omit weather section." If `poi_data` is None, it generates a plan based on the city name alone (LLM knowledge). The plan will be lower quality but still useful — degraded, not broken.

Fourth line: `/health` endpoint detects MCP unavailability with a 5-second timeout on `get_tools_for("weather")`. Returns `{"mcp": false, "status": "degraded"}`. Prometheus alert fires if MCP is down for > 5 minutes. On-call can reroute to a backup MCP provider or activate a cache-only mode.

**Cache-only mode:** If all cached weather/POI data is warm (common for popular cities), users still get results from cache even with MCP down. This is transparent — they have no idea the live tool failed.

---

## Category 7 — Personal Depth Questions

### Q16: What was the hardest bug you debugged in this project?

**Suggested answer (adapt to your actual experience):**

"The hardest bug was the `KeyError: 'name'` in Qwen's streaming tool calls. The symptom was that streaming worked fine for text output but crashed immediately when any tool call was triggered. The stack trace pointed into `langchain_community` internals, which made it look like a version incompatibility.

The root cause took a while to find: Qwen sends streaming tool calls as multiple chunks where the first chunk only contains `index` and `id`, not `name` or `arguments`. The `subtract_client_response` method expected all three to be present in every chunk.

To find this, I added logging to serialize every raw API response chunk to a file, then replayed them manually through the `subtract_client_response` logic. Once I saw the first chunk had no `name` key, the fix was obvious — add `if 'name' in function and 'name' in prev_function` guards before accessing those keys.

What I learned: streaming APIs often have complex multi-part chunk behavior that differs from non-streaming. Always test tool calling in streaming mode specifically, not just in non-streaming mode."

---

### Q17: If you had two more weeks, what would you add?

**Suggested answer:**

Three things in priority order:

**1. Parallel node execution in LangGraph.** Currently `weather → poi → hotel` runs sequentially. Each MCP call takes 2-4 seconds. With LangGraph's `Send` API or `asyncio.gather`, I could run all three in parallel, cutting the data-gathering phase from ~10 seconds to ~4 seconds. The implementation is slightly complex because you need to handle partial state merges, but the latency win is significant.

**2. RAG for travel knowledge.** Right now the LLM relies entirely on its training data for things like visa requirements, seasonal recommendations, and cultural tips. I'd add a small vector store (ChromaDB or FAISS) with curated travel guides indexed by city. Before synthesis, a retrieval step fetches relevant documents and injects them into the synthesis prompt. This would dramatically improve accuracy for less-popular cities that are underrepresented in LLM training data.

**3. A/B testing for prompts.** The `PLANNER_AGENT_PROMPT` is critical and I've tuned it manually. I'd use LangSmith's dataset + evaluation framework to systematically test prompt variants: does adding few-shot examples improve completeness score? Does restructuring the JSON schema hint affect budget accuracy? This moves prompt engineering from intuition to measurement.

---

*End of Interview Questions*
