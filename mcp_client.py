"""
MCP 客户端管理器 —— 单例，管理「DashScope 托管 MCP（主）+ 高德官方 MCP（回退）」。

策略（应用户要求）：**优先 DashScope**（其 maps_text_search 带经纬度，地图能打点），
其配额耗尽/调用失败时，**透明回退到高德官方 MCP**（用用户自己的 Key/配额）补内容。
回退在「工具」层做：每个领域工具包成 _FallbackTool，子 Agent 只看到一个普通工具，
内部先打主路、失败/配额再打回退路——上层（specialist/graph）完全无感。
"""
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

from config import settings as CONFIG

# 主路返回这些字样视为"没拿到内容"（配额类）→ 触发回退。
_QUOTA_TOKENS = ("OVER_LIMIT", "QUOTA", "DAILY_QUERY", "USER_DAILY")


def _looks_like_quota(text: Any) -> bool:
    return any(tok in str(text).upper() for tok in _QUOTA_TOKENS)


class _FallbackTool(BaseTool):
    """包两个同名工具：先调 primary（DashScope），失败/配额则调 fallback（高德）。"""

    primary: Any = None
    fallback: Any = None

    def _run(self, *args, **kwargs):  # 只走异步
        raise NotImplementedError("use async")

    async def _arun(self, *args, **kwargs):
        try:
            res = await self.primary.ainvoke(kwargs)
            if _looks_like_quota(res):       # 配额错误以文本返回的情况
                raise RuntimeError(f"primary quota: {str(res)[:80]}")
            return res
        except Exception as exc:  # noqa: BLE001 —— 主路未拿到内容 → 回退高德
            print(f"[mcp] '{self.name}' primary failed -> fallback to amap: {str(exc)[:80]}")
            return await self.fallback.ainvoke(kwargs)


def _make_fallback_tool(primary: BaseTool, fallback: BaseTool) -> _FallbackTool:
    return _FallbackTool(
        name=primary.name,
        description=primary.description,
        args_schema=primary.args_schema,
        primary=primary,
        fallback=fallback,
    )


class McpClientManager:
    """DashScope 主 + 高德 回退 的 MCP 工具管理单例（懒加载、按 provider 缓存工具）。"""

    _instance: "McpClientManager | None" = None

    def __new__(cls) -> "McpClientManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._clients: dict[str, MultiServerMCPClient] = {}
        self._tools_cache: dict[str, list[BaseTool]] = {}  # provider -> tools
        self._initialized = True

    # ==================== 连接 ====================

    def _client(self, provider: str) -> MultiServerMCPClient:
        if provider not in self._clients:
            conn = (
                CONFIG.mcp_connection_primary() if provider == "primary"
                else CONFIG.mcp_connection_fallback()
            )
            if conn is None:
                raise RuntimeError("fallback (高德) 未配置：未设置 AMAP_API_KEY")
            print(f"[mcp] connecting provider={provider} ({CONFIG.mcp_provider})")
            self._clients[provider] = MultiServerMCPClient({"amap-server": conn})
        return self._clients[provider]

    def _fallback_enabled(self) -> bool:
        return CONFIG.mcp_connection_fallback() is not None

    async def _provider_tools(self, provider: str) -> list[BaseTool]:
        if provider not in self._tools_cache:
            self._tools_cache[provider] = await self._client(provider).get_tools()
        return self._tools_cache[provider]

    # ==================== 工具获取 ====================

    async def get_all_tools(self) -> list[BaseTool]:
        """全部工具（用主路，供 /health 探活；列工具不消耗查询配额）。"""
        return await self._provider_tools("primary")

    async def get_tools_for(self, domain: str) -> list[BaseTool]:
        """按领域取工具子集；启用回退时，每个工具包成 主→回退 的 _FallbackTool。"""
        names = set(CONFIG.tool_domains.get(domain, []))
        primary = {t.name: t for t in await self._provider_tools("primary")}

        if not self._fallback_enabled():
            return [t for n, t in primary.items() if n in names]

        fallback = {t.name: t for t in await self._provider_tools("fallback")}
        out: list[BaseTool] = []
        for n in names:
            p, f = primary.get(n), fallback.get(n)
            if p is not None and f is not None:
                out.append(_make_fallback_tool(p, f))
            elif p is not None:
                out.append(p)
            elif f is not None:
                out.append(f)
        return out

    # ==================== 生命周期 ====================

    async def close(self):
        self._clients.clear()
        self._tools_cache.clear()

    @classmethod
    def reset(cls):
        cls._instance = None
