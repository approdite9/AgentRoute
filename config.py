"""
配置中心 —— 统一管理环境变量、LLM 实例、MCP 连接参数。
"""
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

    dashscope_api_key: str
    model_name: str = "qwen3-max"
    temperature: float = 0.7
    max_tokens: int = 8192
    redis_url: str = "redis://localhost:6379"
    database_url: str = ""
    mcp_url: str = "https://dashscope.aliyuncs.com/api/v1/mcps/amap-maps/mcp"
    langchain_tracing_v2: bool = True
    langchain_project: str = "travel-agent-v1"
    langchain_api_key: str = ""
    rate_limit_per_minute: int = 10
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
    }

    @property
    def api_key(self) -> str:
        """MCP 鉴权所用 API Key（与 DashScope 相同）。"""
        return self.dashscope_api_key

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
