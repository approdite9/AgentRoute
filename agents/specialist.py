"""
专业子 Agent —— POI 搜索 / 天气查询 / 酒店推荐。
每个实例封装一个独立的 LangGraph Agent，只持有自己领域的 MCP 工具。
"""
from langgraph.prebuilt import create_react_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool

from monitoring.callbacks import MCPMetricsCallback


class SpecialistAgent:
    """
    领域专家智能体。

    用法:
        agent = SpecialistAgent(llm, "POI搜索专家", system_prompt, tools)
        await agent.build()
        result = await agent.invoke("搜索北京故宫")
    """

    # ReAct 循环硬上限（graph 超步数）。配合精简提示，正常 1 次搜索仅用 ~3 超步，
    # 远低于此；该上限只用于兜底，防止个别情况下模型反复调用工具打爆配额。
    DEFAULT_RECURSION_LIMIT = 8

    def __init__(
        self,
        llm: BaseChatModel,
        name: str,
        system_prompt: str,
        tools: list[BaseTool],
        recursion_limit: int = DEFAULT_RECURSION_LIMIT,
    ):
        self.llm = llm
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self.recursion_limit = recursion_limit
        self._agent = None

    async def build(self):
        """构建底层 LangGraph Agent"""
        if self._agent is None:
            self._agent = create_react_agent(
                model=self.llm,
                tools=self.tools,
                prompt=self.system_prompt,
            )

    async def invoke(self, user_input: str) -> str:
        """非流式调用"""
        await self.build()
        result = await self._agent.ainvoke(
            {"messages": [{"role": "user", "content": user_input}]},
            config={
                "recursion_limit": self.recursion_limit,
                # 每次调用挂一个独立回调实例，按 MCP 工具名 + 成败计入 Prometheus。
                "callbacks": [MCPMetricsCallback()],
            },
        )
        return result["messages"][-1].content

    async def stream(self, user_input: str):
        """
        流式调用，逐 token yield。

        用于内部被 Planner 调用时不需要流式，但保留能力。
        """
        await self.build()
        async for event in self._agent.astream_events(
            {"messages": [{"role": "user", "content": user_input}]},
            version="v2",
            config={
                "recursion_limit": self.recursion_limit,
                "callbacks": [MCPMetricsCallback()],
            },
        ):
            if event.get("event") == "on_chat_model_stream":
                content = event["data"]["chunk"].content
                if content:
                    yield content
