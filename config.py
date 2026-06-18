"""
配置中心 —— 统一管理环境变量、LLM 实例、MCP 连接参数。
"""
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from langchain_community.chat_models.tongyi import ChatTongyi

# ========== 修复 langchain_community ChatTongyi 流式 tool_calls 的 KeyError ==========
# 上游 bug: subtract_client_response 访问 prev_function["name"] / ["arguments"]
# 前没有检查 key 是否存在。流式首个 tool_call chunk 可能不含这些 key。


def _patched_subtract(self, resp, prev_resp):
    import json

    resp_copy = json.loads(json.dumps(resp))
    message = resp_copy["output"]["choices"][0]["message"]
    prev_message = json.loads(json.dumps(prev_resp))["output"]["choices"][0]["message"]

    message["content"] = message["content"].replace(
        prev_message.get("content", "") or "", ""
    )

    if message.get("tool_calls") and prev_message.get("tool_calls"):
        for index, tool_call in enumerate(message["tool_calls"]):
            function = tool_call["function"]
            prev_function = prev_message["tool_calls"][index]["function"]

            if "name" in function and "name" in prev_function:
                function["name"] = function["name"].replace(prev_function["name"], "")
            if "arguments" in function and "arguments" in prev_function:
                function["arguments"] = function["arguments"].replace(
                    prev_function["arguments"], ""
                )

    return resp_copy


ChatTongyi.subtract_client_response = _patched_subtract
# ========== 修复结束 ==========


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    dashscope_api_key: str = ""  # API/worker 服务必须设置；Streamlit 仅调 FastAPI 可为空
    model_name: str = "deepseek-r1"
    temperature: float = 0.7
    max_tokens: int = 8192
    redis_url: str = "redis://localhost:6379"
    database_url: str = ""
    mcp_url: str = "https://dashscope.aliyuncs.com/api/v1/mcps/amap-maps/mcp"
    langchain_tracing_v2: bool = True
    langchain_project: str = "travel-agent-v1"
    langchain_api_key: str = ""
    rate_limit_per_minute: int = 60
    sentry_dsn: str = ""

    # Streamlit UI 通过该地址访问 FastAPI（提交规划 → SSE 流式 → 渲染）。
    # 本机开发用 localhost；docker-compose 里覆盖为 http://api:8000（见 environment）。
    api_base_url: str = "http://localhost:8000"

    # LangGraph Redis 检查点的存活时间（分钟）。HITL / 多轮会话的检查点写在 db0
    # （checkpoint:* / checkpoint_write:*），若不过期会随会话数无限增长。设默认 TTL
    # 后用完即自动回收；配合 refresh_on_read，活跃会话每次读取都会续期，不会被中途清掉。
    checkpoint_ttl_minutes: int = 24 * 60  # 24 小时

    # ===== MCP 连接 / 工具分发 =====
    # langchain_mcp_adapters 传输方式；阿里百炼 amap-maps 走 HTTP 流式。
    mcp_transport: str = "streamable_http"

    # ===== 高德官方 MCP（用自己的高德 Key，避开 DashScope 托管 MCP 的免费日配额）=====
    # 一旦 .env 里设了 AMAP_API_KEY，mcp_client 自动改连高德官方 MCP（消耗你自己的配额）；
    # 否则沿用上面的 DashScope 托管 MCP（DASHSCOPE_API_KEY 鉴权）。工具名两边一致，上层无需改。
    # 注：高德官方 MCP 用 URL 查询参数 ?key= 鉴权（其协议如此），key 来自 .env、不入日志。
    amap_api_key: str = ""
    amap_mcp_url: str = "https://mcp.amap.com/mcp"
    # 按领域分发 MCP 工具子集（工具名见高德地图 MCP 暴露的真实名称）。
    # 关键：各域只暴露「完成任务所必需的最小工具集」，从源头杜绝子 Agent 调用
    # 详情/周边/地理编码等附加工具而打爆每日配额（USER_DAILY_QUERY_OVER_LIMIT）。
    tool_domains: dict[str, list[str]] = {
        # 景点：maps_text_search 单次即可返回名称/地址/坐标/类别，足够规划用
        "poi": ["maps_text_search"],
        # 酒店：同样只给一次文本搜索，杜绝逐家详情/地理编码的额外调用
        "hotel": ["maps_text_search"],
        "weather": ["maps_weather"],
        "route": [
            "maps_direction_walking",
            "maps_direction_driving",
            "maps_direction_transit_integrated",
            "maps_direction_bicycling",
            "maps_distance",
        ],
        # 地理编码：synthesize 之后给缺坐标的景点/酒店补经纬度（地图打点用）。
        "geo": ["maps_geo"],
    }

    @field_validator("database_url", mode="before")
    @classmethod
    def fix_db_url(cls, v: str) -> str:
        """Railway 注入的 DATABASE_URL 前缀是 postgresql:// 或 postgres://，
        SQLAlchemy asyncpg 驱动需要 postgresql+asyncpg://，自动修正。"""
        if isinstance(v, str):
            if v.startswith("postgres://"):
                return v.replace("postgres://", "postgresql+asyncpg://", 1)
            if v.startswith("postgresql://"):
                return v.replace("postgresql://", "postgresql+asyncpg://", 1)
        return v

    def checkpoint_db_url(self) -> str:
        """LangGraph AsyncPostgresSaver 用的连接串（psycopg 格式，去掉 +asyncpg 驱动后缀）。

        复用业务库即可（检查点表与业务表共存于同一 Postgres）；未配置 DATABASE_URL 时
        返回空串，调用方据此降级为内存检查点。
        """
        url = self.database_url
        if not url:
            return ""
        return url.replace("postgresql+asyncpg://", "postgresql://", 1)

    @property
    def api_key(self) -> str:
        """MCP 鉴权所用 API Key（与 DashScope 相同）。"""
        return self.dashscope_api_key

    @property
    def mcp_provider(self) -> str:
        """当前 MCP 策略（仅用于日志，不含 key）。"""
        return "dashscope-primary+amap-fallback" if self.amap_api_key else "dashscope-only"

    def mcp_connection_primary(self) -> dict:
        """主路：DashScope 托管 amap-maps MCP（Bearer 鉴权）。其 maps_text_search **带经纬度**，
        地图打点依赖它；但有免费日配额。"""
        return {
            "transport": self.mcp_transport,
            "url": self.mcp_url,
            "headers": {"Authorization": f"Bearer {self.api_key}"},
        }

    def mcp_connection_fallback(self) -> dict | None:
        """回退路：高德官方 MCP（用你自己的 Key，?key= 鉴权）。仅在设了 AMAP_API_KEY 时启用。
        主路配额耗尽/失败时顶上，保证"内容拿得到"（注：高德官方 text_search 不返回坐标）。"""
        if not self.amap_api_key:
            return None
        sep = "&" if "?" in self.amap_mcp_url else "?"
        return {
            "transport": "streamable_http",
            "url": f"{self.amap_mcp_url}{sep}key={self.amap_api_key}",
        }

    def create_llm(self, *, streaming: bool = True) -> ChatTongyi:
        # streaming=False 用于结构化输出（with_structured_output）：流式下
        # ChatTongyi 的 tool_call args 会被拆散、组装不全，导致 Pydantic 校验缺字段。
        return ChatTongyi(
            model=self.model_name,
            api_key=self.dashscope_api_key,
            temperature=self.temperature,
            streaming=streaming,
        )


settings = Settings()
