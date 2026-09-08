"""长期记忆层（A2）—— 跨会话记住用户的旅行偏好画像。

对应 UPGRADE_2026.md 主线 A2「上下文工程 + Agent 记忆」：
- 短期记忆：LangGraph Checkpointer（已有）承载单会话状态。
- 长期记忆（本包）：按 user_id 持久化用户偏好画像，跨会话可检索，
  规划前自动补全用户未填字段，规划后回写本次明确提供的偏好。

设计沿用项目 Redis 风格（cache/client.py）：
- 用独立 db=6，与 checkpoint(db0/1)/cache(db2)/session(db3)/rate(db4)/pubsub(db5)/test(db15) 隔离。
- 优雅降级：Redis 不可用时退化为进程内内存字典，不阻断业务。
- 值以 JSON 存储。
"""
from memory.store import (
    MemoryStore,
    get_user_memory,
    save_user_memory,
    merge_memory_into_state,
    update_memory_from_state,
    LONG_TERM_PREF_FIELDS,
)

__all__ = [
    "MemoryStore",
    "get_user_memory",
    "save_user_memory",
    "merge_memory_into_state",
    "update_memory_from_state",
    "LONG_TERM_PREF_FIELDS",
]
