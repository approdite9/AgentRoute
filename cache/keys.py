"""缓存键构造 —— 统一 weather/poi/route 的 Redis key 命名规范。

与 client.cached 装饰器使用的模板保持一致：
    weather:{city}:{date}
    poi:{city}:{category}
    route:{mode}:{origin}:{destination}
"""


def weather_key(city: str, date: str) -> str:
    return f"weather:{city}:{date}"


def poi_key(city: str, category: str) -> str:
    return f"poi:{city}:{category}"


def route_key(origin: str, destination: str, mode: str) -> str:
    return f"route:{mode}:{origin}:{destination}"
