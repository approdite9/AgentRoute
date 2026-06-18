"""
Pydantic v2 结构化输出 Schema —— 取代脆弱的字符串 JSON 解析。

这些模型既用于 LLM 的 with_structured_output（json_mode），
也用于 render.parse_plan 的事后校验与归一化（温度去单位、补齐三餐、预算自动汇总）。
"""
import re

from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal


def normalize_hhmm(v) -> str:
    """容错归一时间为 "HH:MM"（24 小时制）。

    接受 "9:00"/"09:00"/"9点"/"18：30" 等；无法解析返回 ""。
    被 Attraction.start_time、DayPlan.arrival_time/departure_time、
    TransportLeg.depart_time/arrive_time 复用。
    """
    if v is None:
        return ""
    s = str(v).strip().replace("：", ":").replace("点", ":").rstrip(":")
    if not s:
        return ""
    m = re.match(r"^(\d{1,2})(?::(\d{1,2}))?$", s)
    if not m:
        return ""
    hh = int(m.group(1))
    mm = int(m.group(2) or 0)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{hh:02d}:{mm:02d}"


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
    # 计划到达/开始游览的时间，形如 "09:00"（24 小时制）。用于「活动时间轴」渲染与
    # iCal 导出；LLM 未给时由渲染层按「上一项结束 + 缓冲」顺推兜底，故默认留空。
    start_time: str = Field(default="", description="计划开始时间，如 09:00")
    visit_duration: int = Field(default=60, ge=15, le=480)
    ticket_price: float = Field(default=0.0, ge=0)
    category: str = ""
    description: str = ""
    location: dict = Field(default_factory=dict)
    # 时段锁定：True 表示该景点强绑定特定时段（看日落/夜场演出/需预约时段等），
    # 不可被「几何最近邻路线优化」按距离挪到其它时段；普通景点为 False，可自由重排。
    time_locked: bool = Field(default=False, description="是否强绑定时段（如看日落），True 则路线优化不挪动它")

    @field_validator("start_time", mode="before")
    @classmethod
    def _norm_start(cls, v):
        return normalize_hhmm(v)


class Meal(BaseModel):
    type: Literal["breakfast", "lunch", "dinner"]
    name: str
    description: str = ""
    estimated_cost: float = Field(default=50.0, ge=0)


class TransportLeg(BaseModel):
    """城际/往返交通段。

    一段把「出发地」与「目的地」连起来的交通（去程 / 回程 / 城际中转）。本轮仅用
    outbound（出发地→目的地）与 return（目的地→出发地）两段；kind 预留 inter_city，
    以后做多城市行程时只需往 TravelPlan.transports 里追加城际段，无需改模型。

    票务全部来自用户手填时间 + LLM 估算（无外部票务 API）：
      - depart_time / arrive_time：用户填的「出发地出发」与「到达目的地」时间（HH:MM）；
      - mode / duration / price_range：LLM 基于城市与距离的**预估**，纯参考、可留空。
    """
    kind: Literal["outbound", "return", "inter_city"] = "outbound"
    mode: str = Field(default="", description="交通方式，如 飞机/高铁/自驾（可由 LLM 估算）")
    from_city: str = ""
    to_city: str = ""
    date: str = ""
    depart_time: str = Field(default="", description="出发地出发时间，如 08:30")
    arrive_time: str = Field(default="", description="到达目的地时间，如 11:00")
    duration: str = Field(default="", description="预计耗时（文本），如 约2小时40分")
    price_range: str = Field(default="", description="预估票价区间（文本，参考），如 800-1200元")
    note: str = ""

    @field_validator("depart_time", "arrive_time", mode="before")
    @classmethod
    def _norm_times(cls, v):
        return normalize_hhmm(v)


class Hotel(BaseModel):
    name: str
    address: str = ""
    rating: str = ""
    # 价格区间（如 "300-500元"）—— 酒店子 Agent 会返回，渲染层优先展示它。
    # 此前 schema 缺该字段，prompt 里的 price_range 会被 model_validate 丢弃。
    price_range: str = Field(default="", description="价格区间，如 300-500元")
    estimated_cost: float = Field(default=300.0, ge=0)
    type: str = ""
    distance: str = ""
    location: dict = Field(default_factory=dict)


class DayPlan(BaseModel):
    date: str
    day_index: int = Field(ge=0)
    description: str = ""
    transportation: str = ""
    # 「半天逻辑」标记：到达日填 arrival_time（当天该时间之后才安排行程），
    # 离开日填 departure_time（当天该时间之前收尾，留出赶车/赶机的缓冲）。
    # 普通整日留空。渲染层据此在日头打「🛬 抵达 / 🛫 离程」标记并截断时间轴。
    arrival_time: str = Field(default="", description="抵达目的地时间（仅到达日），如 15:00")
    departure_time: str = Field(default="", description="返程出发时间（仅离开日），如 11:00")
    hotel: Hotel = Field(default_factory=Hotel.model_construct)
    attractions: list[Attraction] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)

    @field_validator("arrival_time", "departure_time", mode="before")
    @classmethod
    def _norm_daytimes(cls, v):
        return normalize_hhmm(v)

    @model_validator(mode="after")
    def ensure_three_meals(self):
        # 半天逻辑：到达日/离开日只补「时间窗口内」会真正吃到的那几餐，
        # 避免给晚上才落地的到达日硬塞早午餐、或给上午就离程的离开日硬塞晚餐。
        # 各餐的代表时刻（与渲染层 _MEAL_META 一致）：早 08:00 / 午 12:00 / 晚 18:00。
        meal_clock = {"breakfast": 8 * 60, "lunch": 12 * 60, "dinner": 18 * 60}
        arr = normalize_hhmm(self.arrival_time)
        dep = normalize_hhmm(self.departure_time)

        def _mins(t: str) -> int | None:
            if not t:
                return None
            hh, mm = t.split(":")
            return int(hh) * 60 + int(mm)

        arr_m, dep_m = _mins(arr), _mins(dep)

        meal_types = {m.type for m in self.meals}
        for mtype in ["breakfast", "lunch", "dinner"]:
            if mtype in meal_types:
                continue
            clock = meal_clock[mtype]
            # 到达日：该餐时刻明显早于抵达（提前 1h 容差）则当天吃不到，不补。
            if arr_m is not None and clock + 60 < arr_m:
                continue
            # 离开日：该餐时刻明显晚于返程（延后 1h 容差）则赶不上，不补。
            if dep_m is not None and clock - 60 > dep_m:
                continue
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
        components = [
            self.total_attractions,
            self.total_hotels,
            self.total_meals,
            self.total_transportation,
        ]
        computed = sum(components)
        # 1) total 未给（0）但有分项 → 自动汇总。
        if self.total == 0 and computed > 0:
            self.total = computed
        # 2) 四项分项都齐全（完整拆分）却与 total 不一致 → 以分项之和为准，
        #    确保渲染出的「总计」与上面四格之和自洽（修复 LLM 给出错误 total 的情况）。
        #    仅部分分项时不强改 total，沿用模型给出的整体估算。
        elif all(c > 0 for c in components) and abs(self.total - computed) > 0.01:
            self.total = computed
        return self


class TravelPlan(BaseModel):
    city: str
    # 出发地（用户常驻城市）。用于生成往返交通段；为空表示用户没填，则不渲染往返。
    origin_city: str = ""
    start_date: str
    end_date: str
    days: list[DayPlan] = Field(min_length=1)
    # 往返/城际交通段。本轮通常是 [去程, 回程] 两段；为空则页面不显示往返卡片。
    # 多城市行程以后会在中间插入若干 inter_city 段，模型已预留、无需改结构。
    transports: list[TransportLeg] = Field(default_factory=list)
    weather_info: list[WeatherInfo] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    overall_suggestions: str = ""

    @model_validator(mode="after")
    def reindex_days(self):
        # 按位置把 day_index 归一为 0-based，避免 LLM 偶尔用 1-based 导致前端标签
        # 显示成「Day 2 / Day 3」（2 天行程却从 2 起跳）。渲染层一律 day_index+1。
        for i, day in enumerate(self.days):
            day.day_index = i
        return self


# ==================== 规划前澄清提问（主动追问）====================

class ClarifyQuestion(BaseModel):
    """一条澄清问题：单选 / 多选 给候选项，开放题（text）不给选项。"""
    question: str
    kind: Literal["single", "multi", "text"] = "single"
    options: list[str] = Field(default_factory=list)


class ClarifyingQuestions(BaseModel):
    """LLM 结构化输出的澄清问题集合（规划前主动追问）。"""
    questions: list[ClarifyQuestion] = Field(default_factory=list)
