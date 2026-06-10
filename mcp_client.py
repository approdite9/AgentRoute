"""
MCP 客户端管理器 —— 单例模式，全局共享高德地图 MCP 连接。
"""
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_core.tools import BaseTool
from config import settings as CONFIG


class McpClientManager:
    """
    高德地图 MCP 客户端单例。
    这是单例模式
    保证整个程序永远只有一个 McpClientManager
    保证缓存只创建一次，不重复请求
    保证客户端不重复初始化

    职责：
      1. 管理与阿里百炼 MCP 服务器的连接
      2. 按领域（poi/weather/route）分发工具子集
      3. 缓存已加载工具，避免重复请求

    用法：
      manager = McpClientManager()
      poi_tools = await manager.get_tools_for("poi")
      route_tools = await manager.get_tools_for("route")
    """

    _instance: "McpClientManager | None" = None

    def __new__(cls) -> "McpClientManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._client: MultiServerMCPClient | None = None
        self._tools_cache: dict[str, list[BaseTool]] = {}
        self._initialized = True

    # ==================== 连接管理 ====================

    async def _get_client(self) -> MultiServerMCPClient:
        """懒加载 MCP 客户端。

        连接配置由 CONFIG.mcp_connection() 决定：设了 AMAP_API_KEY → 连高德官方 MCP
        （用你自己的配额，绕开 DashScope 托管 MCP 的免费日配额）；否则连 DashScope 托管 MCP。
        """
        if self._client is None:
            # 只记录提供方，绝不打印含 key 的连接 URL。
            print(f"[mcp] connecting via provider={CONFIG.mcp_provider}")
            self._client = MultiServerMCPClient({"amap-server": CONFIG.mcp_connection()})
        return self._client

    # ==================== 工具获取 ====================

    async def get_all_tools(self) -> list[BaseTool]:
        """获取 MCP 服务器暴露的全部工具"""
        if "all" not in self._tools_cache:
            client = await self._get_client()
            self._tools_cache["all"] = await client.get_tools()
            # for t in self._tools_cache["all"]:
            #     print(f"  ✓ {t.name}: {t.description[:60]}...")
        return self._tools_cache["all"]

    async def get_tools_for(self, domain: str) -> list[BaseTool]:
        """按领域获取工具子集"""
        all_tools = await self.get_all_tools()
        target_names = set(CONFIG.tool_domains.get(domain, []))
        return [t for t in all_tools if t.name in target_names]

    # ==================== 生命周期 ====================

    async def close(self):
        """关闭 MCP 连接（如有需要）"""
        self._client = None
        self._tools_cache.clear()

    @classmethod
    def reset(cls):
        """重置单例（测试用）"""
        cls._instance = None
