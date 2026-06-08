"""
LangChain 回调 —— 把子 Agent 实际发起的 MCP 工具调用计入 Prometheus。

为什么用回调而不是在 mcp_client 里包装：
  所有 MCP 工具最终都由专业子 Agent（create_react_agent 的 ToolNode）执行，
  ToolNode 会按 LangChain 稳定的回调协议触发 on_tool_start/end/error。挂一个
  回调即可与工具的具体实现（StructuredTool / 适配器版本差异）解耦，统一计数
  成功与失败，且不漏任何调用路径（CLI / API / worker 都走同一条 invoke）。
"""
import structlog
from langchain_core.callbacks import BaseCallbackHandler

from monitoring.metrics import MCP_CALLS

logger = structlog.get_logger(__name__)


class MCPMetricsCallback(BaseCallbackHandler):
    """按 run_id 关联 start→end/error，给 MCP_CALLS 打 tool/status 标签。"""

    def __init__(self) -> None:
        # 同一 Agent 内工具调用基本串行，run_id 仍用于稳妥地配对名称。
        self._names: dict = {}

    def on_tool_start(self, serialized, input_str, *, run_id=None, **kwargs) -> None:
        name = (serialized or {}).get("name") or kwargs.get("name") or "unknown"
        self._names[run_id] = name

    def on_tool_end(self, output, *, run_id=None, **kwargs) -> None:
        name = self._names.pop(run_id, "unknown")
        MCP_CALLS.labels(tool=name, status="success").inc()

    def on_tool_error(self, error, *, run_id=None, **kwargs) -> None:
        name = self._names.pop(run_id, "unknown")
        MCP_CALLS.labels(tool=name, status="error").inc()
