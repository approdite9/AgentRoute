"""
Pydantic v2 结构化输出 Schema —— 取代脆弱的字符串 JSON 解析。

这些模型既用于 LLM 的 with_structured_output（json_mode），
也用于 render.parse_plan 的事后校验与归一化（温度去单位、补齐三餐、预算自动汇总）。
"""
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Literal


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
    visit_duration: int = Field(default=60, ge=15, le=480)
    ticket_price: float = Field(default=0.0, ge=0)
    category: str = ""
    description: str = ""
    location: dict = Field(default_factory=dict)


class Meal(BaseModel):
    type: Literal["breakfast", "lunch", "dinner"]
    name: str
    description: str = ""
    estimated_cost: float = Field(default=50.0, ge=0)


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
    hotel: Hotel = Field(default_factory=Hotel.model_construct)
    attractions: list[Attraction] = Field(default_factory=list)
    meals: list[Meal] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_three_meals(self):
        meal_types = {m.type for m in self.meals}
        for mtype in ["breakfast", "lunch", "dinner"]:
            if mtype not in meal_types:
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
    start_date: str
    end_date: str
    days: list[DayPlan] = Field(min_length=1)
    weather_info: list[WeatherInfo] = Field(default_factory=list)
    budget: Budget = Field(default_factory=Budget)
    overall_suggestions: str = ""
