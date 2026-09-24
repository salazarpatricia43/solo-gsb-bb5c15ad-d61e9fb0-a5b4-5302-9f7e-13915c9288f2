"""Pydantic request/response models."""
from __future__ import annotations

from datetime import date, time
from typing import Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import DEFAULT_TOLERANCE, ELEMENTS, normalize_element


class Concentrate(BaseModel):
    """One stock concentrate bottle.

    Concentrations are in mg of the *element* per L of concentrate
    (water-soluble N/P/K, elemental for micros). ``available_volume`` and
    ``max_volume`` are in mL.
    """

    name: str = Field(..., min_length=1, examples=["A液-钙肥"])
    available_volume: float = Field(..., gt=0, description="可用体积 (mL)")
    max_volume: Optional[float] = Field(
        None,
        gt=0,
        description="本配方中单液使用量上限 (mL)，缺省取可用体积",
    )
    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="各元素浓度 mg/L，键如 N/P/K/Ca/Mg/Fe ...",
    )

    # Reject misspelled keys (e.g. "concentrates" inside a concentrate)
    # instead of silently dropping them.
    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_concentrations(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class Targets(BaseModel):
    """Target nutrient concentrations of the final solution (mg/L)."""

    concentrations: dict[str, float]

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_targets(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 目标不能为负")
            normalized[elem] = float(val)
        return normalized


class SourceWater(BaseModel):
    """Raw (source) water mineral baseline concentrations (mg/L).

    The dilution water is not necessarily distilled: it may already carry
    some of the tracked elements.  These concentrations contribute to the
    final solution in proportion to the *solved* dilution water volume,
    i.e. ``source_amount_e = source_concentration_e * w / 1000`` (mg).
    """

    concentrations: dict[str, float] = Field(
        default_factory=dict,
        description="原水各元素本底浓度 mg/L，键如 Ca/Mg/...",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrations")
    @classmethod
    def _validate_source(cls, v: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for key, val in v.items():
            elem = normalize_element(key)
            if elem is None:
                raise ValueError(
                    f"未知元素 '{key}'，支持: {', '.join(ELEMENTS)}"
                )
            if val < 0:
                raise ValueError(f"元素 {elem} 原水浓度不能为负")
            normalized[elem] = float(val)
        return normalized


class SolveRequest(BaseModel):
    target_volume_ml: float = Field(..., gt=0, description="目标配方总体积 (mL)")
    targets: Targets
    concentrates: list[Concentrate] = Field(..., min_length=1)
    source_water: Optional[SourceWater] = Field(
        None,
        description=(
            "原水矿物质基线（各元素浓度 mg/L）。可选，未传时按零处理；"
            "原水贡献随求得的稀释水量线性计入各元素实际量，"
            "不会按目标总体积预扣固定原水量。"
        ),
    )
    min_water_ml: float = Field(
        0,
        ge=0,
        description="稀释水最低用量 (mL)，用于强制保留稀释空间",
    )
    max_concentrate_fraction: float = Field(
        1.0,
        gt=0,
        le=1.0,
        description="浓缩液合计占总体积的最大比例 (0~1]",
    )
    tolerance: float = Field(
        DEFAULT_TOLERANCE,
        ge=0,
        le=1,
        description="可接受的最大相对偏差；超过则在 violations 中列出",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("concentrates")
    @classmethod
    def _unique_names(cls, v: list[Concentrate]) -> list[Concentrate]:
        names = [c.name for c in v]
        if len(names) != len(set(names)):
            raise ValueError("浓缩液名称不能重复")
        return v


class ElementResult(BaseModel):
    element: str
    target_concentration: float
    actual_concentration: float
    target_amount_mg: float
    actual_amount_mg: float
    concentrate_amount_mg: float = Field(
        ..., description="浓缩液合计贡献的该元素量 (mg)"
    )
    source_water_amount_mg: float = Field(
        ...,
        description=(
            "原水本底贡献的该元素量 (mg) = 原水浓度 × 求得稀释水量 ÷ 1000；"
            "未传原水基线时为 0"
        ),
    )
    relative_deviation: Optional[float]
    status: str  # ok | under | over | unconstrained


class Violation(BaseModel):
    code: str
    message: str
    element: Optional[str] = None
    limit: Optional[float] = None
    actual: Optional[float] = None


class SolveResult(BaseModel):
    feasible: bool
    optimal: bool
    max_relative_deviation: Optional[float]
    total_volume_ml: float
    water_volume_ml: float
    concentrate_total_ml: float
    allocations: list[dict[str, float | str]]
    elements: list[ElementResult]
    violations: list[Violation] = []
    message: str = ""


class SaveRecipeRequest(BaseModel):
    name: str = Field(..., min_length=1)
    description: str = ""
    note: str = ""
    request: SolveRequest

    model_config = ConfigDict(extra="forbid")


class RecipeSummary(BaseModel):
    id: int
    name: str
    description: str
    created_at: str
    latest_version: int


class VersionSummary(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str


class RecipeVersion(BaseModel):
    id: int
    recipe_id: int
    version: int
    note: str
    created_at: str
    request: dict
    result: dict


# --------------------------------------------------------------------------- #
# Irrigation planning
# --------------------------------------------------------------------------- #
# Planning over an unbounded range would make the response arbitrarily large;
# a full year + leap slack is far beyond any practical irrigation schedule.
MAX_PLAN_DAYS = 370


class IrrigationSessionInput(BaseModel):
    """One daily irrigation start in local wall-clock time."""

    start_time: time = Field(
        ...,
        description=(
            "本地开始时间 HH:MM 或 HH:MM:SS（按 timezone 解释，"
            "可落在前一日 00:00~次日 23:59 的普通一天内；持续到次日即跨午夜）"
        ),
    )
    duration_minutes: float = Field(..., gt=0, description="持续分钟数，必须为正")
    water_ml: float = Field(..., gt=0, description="本次目标水量 (mL)，必须为正")

    model_config = ConfigDict(extra="forbid")


class IrrigationScheduleBase(BaseModel):
    """Common validated daily-schedule fields (planning & forecasting)."""

    timezone: str = Field(
        ...,
        min_length=1,
        examples=["Asia/Shanghai"],
        description="IANA 时区名，如 Asia/Shanghai、America/New_York",
    )
    start_date: date = Field(..., description="起始日期（含，本地日历日）")
    end_date: date = Field(..., description="结束日期（含，本地日历日）")
    sessions: list[IrrigationSessionInput] = Field(
        ..., min_length=1, description="每日重复的灌溉开始时段（至少 1 个）"
    )
    suspended_dates: list[date] = Field(
        default_factory=list,
        description=(
            "本地停灌日期列表（按 timezone 解释的本地日历日）：列表中的日期"
            "不生成当日排定的灌溉批次，响应仍保留这些日期并标明停灌；"
            "不影响前一日本地日期开始、跨入该日的跨午夜批次；"
            "日期须落在 start_date~end_date 内且不可重复"
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        key = v.strip()
        if not key:
            raise ValueError("时区不能为空")
        try:
            ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
            raise ValueError(
                f"非法 IANA 时区 '{v}'，例如 Asia/Shanghai、"
                "Europe/London、America/New_York"
            ) from exc
        return key

    @field_validator("sessions")
    @classmethod
    def _unique_sessions(cls, v: list[IrrigationSessionInput]) -> list[IrrigationSessionInput]:
        seen: set[time] = set()
        for s in v:
            if s.start_time in seen:
                raise ValueError(
                    f"每日开始时间 {s.start_time.strftime('%H:%M:%S')} 重复"
                )
            seen.add(s.start_time)
        return v

    @field_validator("suspended_dates")
    @classmethod
    def _unique_suspended_dates(cls, v: list[date]) -> list[date]:
        seen: set[date] = set()
        for d in v:
            if d in seen:
                raise ValueError(f"停灌日期 {d.isoformat()} 重复")
            seen.add(d)
        return v

    @model_validator(mode="after")
    def _validate_range(self) -> "IrrigationScheduleBase":
        if self.end_date < self.start_date:
            raise ValueError(
                "end_date 不能早于 start_date"
                f"（{self.start_date.isoformat()} > "
                f"{self.end_date.isoformat()}）"
            )
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_PLAN_DAYS:
            raise ValueError(
                f"日期范围最多 {MAX_PLAN_DAYS} 天，当前 {span} 天"
            )
        out_of_range = [
            d for d in self.suspended_dates
            if d < self.start_date or d > self.end_date
        ]
        if out_of_range:
            bad = ", ".join(d.isoformat() for d in sorted(out_of_range))
            raise ValueError(
                "停灌日期必须落在 "
                f"{self.start_date.isoformat()}~{self.end_date.isoformat()} "
                f"范围内：{bad}"
            )
        return self


class IrrigationPlanRequest(IrrigationScheduleBase):
    daily_capacity_ml: float = Field(
        ..., ge=0, description="设备每日供水容量 (mL)，非负；0 表示当日不可供水"
    )


class PlanSessionRef(BaseModel):
    """A constituent session occurrence inside a (possibly merged) batch."""

    day: str
    requested_time: str
    local_start: str
    dst_event: Optional[str] = None
    dst_note: Optional[str] = None
    water_ml: float


class IrrigationBatch(BaseModel):
    start_utc: str = Field(..., description="批次 UTC 开始时间 RFC3339")
    end_utc: str = Field(..., description="批次 UTC 结束时间 RFC3339")
    local_start: str = Field(..., description="本地显示开始时间（含 UTC 偏移）")
    local_end: str = Field(..., description="本地显示结束时间（含 UTC 偏移）")
    duration_seconds: int
    water_ml: float = Field(..., description="合并后总水量；超容也不截断")
    merged: bool
    batch_count_before_merge: int
    dst_event: Optional[str] = Field(
        None, description="gap=夏令时缺失本地时间（已顺延）；duplicate=重复本地时间"
    )
    dst_note: Optional[str] = None
    crosses_midnight: bool
    sources: list[PlanSessionRef]


class CapacityViolation(BaseModel):
    date: str
    planned_water_ml: float
    daily_capacity_ml: float
    shortfall_ml: float = Field(..., description="明确缺口 = 计划水量 - 日容量")
    message: str


class IrrigationDayResult(BaseModel):
    date: str
    suspended: bool = Field(
        False, description="当日是否为停灌日：停灌日不生成当日排定批次，水量为 0"
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    daily_capacity_ml: float
    within_capacity: bool
    shortfall_ml: float
    dst_events: list[str]


class IrrigationPlanResult(BaseModel):
    timezone: str
    start_date: str
    end_date: str
    days: int
    suspended_dates: list[str] = Field(
        default_factory=list,
        description="请求中的停灌日期（原样保留，已按日期升序排列）；这些日期不排定批次",
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    merge_gap_seconds: int
    planned_water_ml: float
    daily_capacity_ml: float
    capacity_ok: bool
    capacity_violations: list[CapacityViolation]
    day_results: list[IrrigationDayResult]
    batches: list[IrrigationBatch]


# --------------------------------------------------------------------------- #
# Concentrate consumption forecast
# --------------------------------------------------------------------------- #
class StockLevel(BaseModel):
    """Current on-hand volume of one concentrate (mL)."""

    concentrate: str = Field(
        ..., min_length=1, description="配方版本求解结果中的浓缩液名称"
    )
    stock_ml: float = Field(..., ge=0, description="当前库存 (mL)，不能为负")

    model_config = ConfigDict(extra="forbid")


class ConsumptionForecastRequest(IrrigationScheduleBase):
    """Irrigation schedule + current stock levels for one recipe version."""

    stock: list[StockLevel] = Field(
        ..., min_length=1, description="各浓缩液当前库存；名称须与配方求解结果一致"
    )

    @field_validator("stock")
    @classmethod
    def _unique_stock(cls, v: list[StockLevel]) -> list[StockLevel]:
        names = [s.concentrate for s in v]
        if len(names) != len(set(names)):
            raise ValueError("库存浓缩液名称不能重复")
        return v


class ForecastLiquidUse(BaseModel):
    """Per-concentrate usage / gap in one batch, or on one day."""

    concentrate: str
    required_ml: float = Field(..., description="按比例应消耗 (mL)")
    consumed_ml: float = Field(
        ..., description="实际扣减 (mL)；库存不足时仅扣到 0，不会出现负库存"
    )
    shortfall_ml: float = Field(..., description="本批缺口 = 应消耗 - 实际扣减")


class ForecastBatch(BaseModel):
    """One actual (post-merge) irrigation batch with concentrate draw-down."""

    index: int = Field(..., description="按执行顺序（UTC 开始时间）的批次序号，从 1 起")
    start_utc: str
    local_start: str
    local_end: str
    attributed_date: str = Field(
        ...,
        description=(
            "归属本地日：取批次本地开始日；跨午夜/合并批次整体只计入这一天，"
            "不重复扣减"
        ),
    )
    water_ml: float
    merged: bool
    batch_count_before_merge: int
    crosses_midnight: bool
    sufficient: bool = Field(..., description="本批所有浓缩液库存是否都够")
    liquids: list[ForecastLiquidUse]


class ForecastDayLiquid(BaseModel):
    concentrate: str
    required_ml: float
    consumed_ml: float
    shortfall_ml: float
    remaining_ml: float = Field(..., description="当日结束后的剩余库存（≥ 0）")


class ForecastDay(BaseModel):
    date: str
    suspended: bool = Field(
        False, description="当日是否为停灌日：停灌日不排定批次、不扣减库存"
    )
    batches: int = Field(..., description="归属本日的实际（合并后）批次数")
    water_ml: float
    sufficient: bool
    liquids: list[ForecastDayLiquid]


class ForecastLiquidSummary(BaseModel):
    concentrate: str
    initial_stock_ml: float
    mix_ml_per_l: float = Field(
        ..., description="每升灌溉水需该浓缩液 mL（配方分配体积 / 目标体积）"
    )
    total_required_ml: float
    total_consumed_ml: float
    total_shortfall_ml: float
    remaining_ml: float
    first_shortfall_date: Optional[str] = None


class ConsumptionForecastResult(BaseModel):
    recipe_id: int
    version: int
    recipe_feasible: bool = Field(
        ..., description="对应配方版本求解结果是否在容差内可行"
    )
    warning: Optional[str] = Field(
        None, description="配方版本不可行时的提示；可行时为 null"
    )
    timezone: str
    start_date: str
    end_date: str
    days: int
    suspended_dates: list[str] = Field(
        default_factory=list,
        description="请求中的停灌日期（原样保留，已按日期升序排列）；这些日期不排定批次、不扣减库存",
    )
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    sufficient: bool = Field(..., description="整个计划周期内库存是否始终充足")
    first_shortfall_date: Optional[str] = Field(
        None, description="首个出现任一浓缩液缺口的本地日期；全程充足为 null"
    )
    liquids: list[ForecastLiquidSummary]
    day_results: list[ForecastDay]
    batches: list[ForecastBatch]


# --------------------------------------------------------------------------- #
# Whole-package replenishment procurement planning
# --------------------------------------------------------------------------- #
class QuoteInput(BaseModel):
    """One supplier quote: concentrate sold in fixed whole packages."""

    concentrate: str = Field(
        ..., min_length=1, description="报价对应的浓缩液名称（须与配方版本一致）"
    )
    package_ml: float = Field(
        ...,
        gt=0,
        strict=True,
        description=(
            "整包装容量 (mL/包)，必须为有限正数（数字，不接受 \"1000\" 这类"
            "数字字符串）；采购量只能是该容量的非负整数倍"
        ),
    )
    price: float = Field(
        ...,
        gt=0,
        strict=True,
        description="整包单价（同一货币单位），必须为有限正数（数字）",
    )
    arrival_date: date = Field(
        ...,
        description=(
            "到货本地日期（按 timezone 解释）；该货物在当日**首个批次前**入库，"
            "当日全部批次（含本地 00:00 开始的批次）都可使用"
        ),
    )
    max_packages: Optional[int] = Field(
        None,
        ge=0,
        description="最多可购包数（非负整数）；缺省（null）表示不限量",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("package_ml", "price")
    @classmethod
    def _finite_positive(cls, v: float) -> float:
        # gt rejects <= 0, but NaN slips through comparisons; reject NaN/Inf
        # explicitly as illegal quotes.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("必须为有限正数")
        return float(v)


class ProcurementPlanRequest(IrrigationScheduleBase):
    """Irrigation schedule + current stock + supplier quotes for a version."""

    stock: list[StockLevel] = Field(
        ..., min_length=1, description="各浓缩液当前库存；名称须与配方求解结果一一对应"
    )
    quotes: list[QuoteInput] = Field(
        default_factory=list,
        description=(
            "补货报价列表（可为空：库存本身已足够时返回全 0 采购）；"
            "同一浓缩液可有多个报价，按整包出售、按到货日入库"
        ),
    )

    @field_validator("stock")
    @classmethod
    def _unique_stock(cls, v: list[StockLevel]) -> list[StockLevel]:
        names = [s.concentrate for s in v]
        if len(names) != len(set(names)):
            raise ValueError("库存浓缩液名称不能重复")
        return v


class ProcurementQuoteResult(BaseModel):
    """Purchased package count of one quote in the chosen procurement plan."""

    index: int = Field(..., description="报价序号，从 1 起（按请求顺序）")
    concentrate: str
    package_ml: float = Field(..., description="整包装容量 (mL/包)")
    price: float = Field(..., description="整包单价")
    arrival_date: str = Field(..., description="到货本地日期")
    max_packages: Optional[int] = Field(
        ..., description="最多可购包数；不限量为 null"
    )
    packages: Optional[int] = Field(
        ...,
        description=(
            "本报价采购包数（非负整数）；整体无解时不生成采购单，恒为 null"
        ),
    )
    volume_ml: Optional[float] = Field(
        ..., description="本报价补货体积 = package_ml × packages；无解时为 null"
    )
    cost: Optional[float] = Field(
        ..., description="本报价金额 = price × packages；无解时为 null"
    )


class ProcurementInbound(BaseModel):
    """One quote's packages entering stock at one batch."""

    quote_index: int
    concentrate: str
    packages: int
    volume_ml: float


class ProcurementBatchLiquid(BaseModel):
    """One concentrate's demand / post-deduction level at one batch."""

    concentrate: str
    required_ml: float = Field(
        ..., description="按批次实际水量折算的本批用液需求 (mL)"
    )
    remaining_ml: float = Field(
        ...,
        description="本批扣减（并完成本批到货入库）后的库存 (mL)，恒 ≥ 0",
    )


class ProcurementGapLiquid(BaseModel):
    """One concentrate shortfall inside the first un-coverable batch."""

    concentrate: str
    required_ml: float
    available_ml: float = Field(
        ...,
        description=(
            "即使把各报价买到上限，本批扣减前可用的该浓缩液库存 (mL)"
        ),
    )
    shortfall_ml: float = Field(..., description="本批缺口 = required - available")


class ProcurementShortfall(BaseModel):
    """Earliest batch that cannot be covered even when buying at every cap."""

    batch_index: int = Field(..., description="最早出现缺口的批次序号（从 1 起）")
    start_utc: str
    local_start: str
    local_end: str
    attributed_date: str
    water_ml: float
    liquids: list[ProcurementGapLiquid] = Field(
        ...,
        description=(
            "该批次发生缺口的浓缩液及各自需求/可供给量/缺口（同批多个浓缩液"
            "同时不足时全部列出）"
        ),
    )
    message: str


class ProcurementBatch(BaseModel):
    """One actual (post-merge) batch with arrivals, demand and stock levels."""

    index: int
    start_utc: str
    local_start: str
    local_end: str
    attributed_date: str = Field(
        ...,
        description=(
            "归属本地日：取批次本地开始日；跨午夜/合并批次整体只在该日扣一次"
        ),
    )
    water_ml: float = Field(..., description="合并后批次实际水量，用液需求据此折算")
    merged: bool
    batch_count_before_merge: int
    crosses_midnight: bool
    inbound: list[ProcurementInbound] = Field(
        default_factory=list,
        description=(
            "本批开始前入库的整包（仅到货日本地首个批次携带，避免重复登记）；"
            "到货早于计划起始日的报价计入各液期初可用量"
        ),
    )
    liquids: list[ProcurementBatchLiquid]
    sufficient: bool = Field(
        ...,
        description=(
            "把各报价买到上限时本批扣减后库存是否非负；仅在整体无解的"
            "诊断轨迹中可能为 false"
        ),
    )


class ProcurementLiquidSummary(BaseModel):
    concentrate: str
    initial_stock_ml: float = Field(
        ..., description="请求中的当前库存 (mL)"
    )
    initial_available_ml: float = Field(
        ...,
        description=(
            "首个批次前的期初可用量 = 当前库存 + 到货日早于计划起始日的"
            "报价采购量（无解时按买到上限计）"
        ),
    )
    mix_ml_per_l: float
    total_required_ml: float
    purchased_ml: Optional[float] = Field(
        ...,
        description=(
            "该浓缩液各报价补货体积合计；无解时为 null（不伪造采购量）"
        ),
    )
    capped_supply_ml: Optional[float] = Field(
        None,
        description=(
            "无解诊断：所有**有限上限**报价买到上限时该浓缩液的可达供货量"
            "（不含不限量报价）；可求解时为 null"
        ),
    )
    final_remaining_ml: Optional[float] = Field(
        ...,
        description="最末批次扣减后的剩余库存；无解时为 null"
    )


class ProcurementPlanResult(BaseModel):
    recipe_id: int
    version: int
    recipe_feasible: bool = Field(
        ..., description="对应配方版本求解结果是否在容差内可行"
    )
    warning: Optional[str] = Field(
        None, description="配方版本不可行时的提示；可行时为 null"
    )
    timezone: str
    start_date: str
    end_date: str
    days: int
    suspended_dates: list[str] = Field(default_factory=list)
    batch_count_before_merge: int
    batch_count_after_merge: int
    planned_water_ml: float
    feasible: bool = Field(
        ...,
        description=(
            "是否存在使每批扣减后各浓缩液库存均非负的整包采购组合；"
            "false 时不返回任何采购包数，缺口见 first_shortfall"
        ),
    )
    total_cost: Optional[float] = Field(
        ..., description="最低总价；无解时为 null"
    )
    total_packages: Optional[int] = Field(
        ...,
        description="最优方案总包数（同价时取总包数最少）；无解时为 null",
    )
    quotes: list[ProcurementQuoteResult]
    liquids: list[ProcurementLiquidSummary]
    batches: list[ProcurementBatch]
    first_shortfall: Optional[ProcurementShortfall] = Field(
        None,
        description=(
            "报价时点或供应上限导致无解时，最早出现缺口的批次与浓缩液；"
            "可求解时为 null"
        ),
    )
    message: str = ""


# --------------------------------------------------------------------------- #
# Integer pump-stroke dispense planning
# --------------------------------------------------------------------------- #
class PumpDoseInput(BaseModel):
    """One pump's fixed per-stroke delivery for a recipe concentrate."""

    concentrate: str = Field(
        ..., min_length=1, description="配方版本中的浓缩液名称"
    )
    pump_step_ml: float = Field(
        ...,
        gt=0,
        strict=True,
        description=(
            "单次泵送量 (mL)，必须为有限正数（数字，不接受 \"125\" 这类"
            "数字字符串）；该液总投料量为此值的整数倍"
        ),
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("pump_step_ml")
    @classmethod
    def _finite_step(cls, v: float) -> float:
        # float("nan") / float("inf") pass Pydantic's gt check; reject them
        # explicitly as illegal stroke sizes.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("单次泵送量必须为有限正数")
        return float(v)


class DispensePlanRequest(BaseModel):
    """Per-concentrate single-stroke pump volumes for one recipe version."""

    pumps: list[PumpDoseInput] = Field(
        ..., min_length=1, description="各浓缩液的单次泵送量；名称须与配方版本一一对应"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("pumps")
    @classmethod
    def _unique_pumps(cls, v: list[PumpDoseInput]) -> list[PumpDoseInput]:
        names = [p.concentrate for p in v]
        if len(names) != len(set(names)):
            raise ValueError("泵配置中浓缩液名称不能重复")
        return v


class DispensePumpResult(BaseModel):
    """Planned integer stroke count and resulting dosage of one pump."""

    concentrate: str
    pump_step_ml: float = Field(..., description="单次泵送量 (mL)")
    pump_count: int = Field(
        ..., description="泵送次数（非负整数）；实际投料量 = pump_step_ml × pump_count"
    )
    volume_ml: float = Field(..., description="实际投料量 (mL)")
    cap_ml: float = Field(
        ..., description="本配方中该液使用上限 = min(可用体积, max_volume) (mL)"
    )
    available_volume_ml: float = Field(..., description="该液可用体积 (mL)")
    max_pumps: int = Field(
        ..., description="受单液可用量/使用上限约束时该液允许的最大泵次数"
    )
    note: Optional[str] = Field(
        None,
        description=(
            "异常提示：例如单次泵送量已超过该液上限（此时 pump_count 只能为 0）；"
            "正常为 null"
        ),
    )


class DispensePlanResult(BaseModel):
    recipe_id: int
    version: int
    feasible: bool = Field(
        ...,
        description=(
            "体积硬约束（体积等式、最低稀释水量、浓缩液占比、单液上限）"
            "是否存在可执行整数方案；false 时泵次数全为 0，原因见 violations"
        ),
    )
    within_tolerance: bool = Field(
        ...,
        description="最优整数泵送方案的各元素最大相对偏差是否达到版本容差 tolerance",
    )
    optimal: bool = Field(
        ..., description="整数 minimax 求解器是否返回最优解（否则为可行近似）"
    )
    max_relative_deviation: Optional[float] = Field(
        ..., description="最优整数泵送方案的各元素最大相对偏差（分数）"
    )
    total_volume_ml: float
    water_volume_ml: float = Field(
        ...,
        description="稀释水量 = 目标总体积 - 各液实际投料量之和；原水贡献按此实际水量计算",
    )
    concentrate_total_ml: float
    total_pump_count: int = Field(..., description="各浓缩液泵次数合计")
    tolerance: float = Field(..., description="采用的版本容差（分数）")
    min_water_ml: float
    max_concentrate_fraction: float
    pumps: list[DispensePumpResult]
    elements: list[ElementResult]
    violations: list[Violation] = []
    message: str = ""


# --------------------------------------------------------------------------- #
# In-tank retest correction (在缸复测纠偏)
# --------------------------------------------------------------------------- #
class MeasuredInterval(BaseModel):
    """Measured concentration interval (mg/L) of one target element."""

    element: str = Field(
        ..., min_length=1, description="目标元素符号（大小写不敏感，如 N/Ca/fe）"
    )
    lower_mg_l: float = Field(
        ..., ge=0, description="实测浓度下界 (mg/L)，有限非负"
    )
    upper_mg_l: float = Field(
        ..., ge=0, description="实测浓度上界 (mg/L)，有限非负"
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("element")
    @classmethod
    def _normalize(cls, v: str) -> str:
        elem = normalize_element(v)
        if elem is None:
            raise ValueError(
                f"未知元素 '{v}'，支持: {', '.join(ELEMENTS)}"
            )
        return elem

    @field_validator("lower_mg_l", "upper_mg_l")
    @classmethod
    def _finite(cls, v: float) -> float:
        # ge=0 alone cannot be trusted for NaN/Inf (comparisons with NaN are
        # always False); reject non-finite measurements explicitly.
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("实测浓度必须为有限非负数")
        return float(v)

    @model_validator(mode="after")
    def _ordered(self) -> "MeasuredInterval":
        if self.lower_mg_l > self.upper_mg_l:
            raise ValueError(
                f"元素 {self.element} 实测区间颠倒：下界 {self.lower_mg_l:g} "
                f"mg/L 大于上界 {self.upper_mg_l:g} mg/L"
            )
        return self


class RetestCorrectionRequest(BaseModel):
    """在缸复测纠偏请求：现有液量 + 实测区间 + 目标终体积 + 可追加库存。"""

    current_volume_ml: float = Field(
        ..., gt=0, description="现有液量 (mL)，有限正数"
    )
    final_volume_ml: float = Field(
        ...,
        gt=0,
        description="目标终体积 (mL)，有限正数，不得小于现有液量",
    )
    measurements: list[MeasuredInterval] = Field(
        ...,
        min_length=1,
        description=(
            "各目标元素实测浓度上下界 (mg/L)；元素须与配方版本目标一一对应"
        ),
    )
    stock: list[StockLevel] = Field(
        ...,
        min_length=1,
        description="各浓缩液可追加库存 (mL)；名称须与版本浓缩液一一对应",
    )

    model_config = ConfigDict(extra="forbid")

    @field_validator("current_volume_ml", "final_volume_ml")
    @classmethod
    def _finite_volume(cls, v: float) -> float:
        if v != v or v in (float("inf"), float("-inf")):
            raise ValueError("体积必须为有限正数")
        return float(v)

    @field_validator("measurements")
    @classmethod
    def _unique_measurements(
        cls, v: list[MeasuredInterval]
    ) -> list[MeasuredInterval]:
        elems = [m.element for m in v]
        if len(elems) != len(set(elems)):
            raise ValueError("实测区间中的元素不能重复")
        return v

    @field_validator("stock")
    @classmethod
    def _unique_stock(cls, v: list[StockLevel]) -> list[StockLevel]:
        names = [s.concentrate for s in v]
        if len(names) != len(set(names)):
            raise ValueError("库存浓缩液名称不能重复")
        return v

    @model_validator(mode="after")
    def _final_not_below_current(self) -> "RetestCorrectionRequest":
        if self.final_volume_ml < self.current_volume_ml:
            raise ValueError(
                f"目标终体积 {self.final_volume_ml:g} mL 不能小于现有液量 "
                f"{self.current_volume_ml:g} mL"
            )
        return self


class RetestAddition(BaseModel):
    """Optimal top-up volume of one version concentrate."""

    concentrate: str
    volume_ml: float = Field(
        ..., description="最优补液量 (mL)，0 ≤ volume_ml ≤ stock_ml"
    )
    stock_ml: float = Field(..., description="请求给出的该液可追加库存 (mL)")


class RetestElementResult(BaseModel):
    """Worst-endpoint deviation of one target element after correction."""

    element: str
    target_concentration: float = Field(..., description="版本目标浓度 (mg/L)")
    measured_lower_mg_l: float = Field(..., description="实测浓度下界 (mg/L)")
    measured_upper_mg_l: float = Field(..., description="实测浓度上界 (mg/L)")
    target_amount_mg: float = Field(..., description="终体积下的目标量 (mg)")
    final_concentration_at_lower: float = Field(
        ..., description="按实测下界端点折算、补液后终体积下的浓度 (mg/L)"
    )
    final_concentration_at_upper: float = Field(
        ..., description="按实测上界端点折算、补液后终体积下的浓度 (mg/L)"
    )
    worst_endpoint: str = Field(
        ...,
        description=(
            "偏差最大的实测端点：lower=下界，upper=上界；"
            "两端点偏差绝对值相等（完全居中）时取 lower"
        ),
    )
    worst_concentration: float = Field(
        ..., description="最坏端点在终体积下的浓度 (mg/L)"
    )
    relative_deviation: float = Field(
        ..., description="最坏端点的带符号相对偏差（分数）"
    )
    status: str  # ok | under | over


class RetestCorrectionResult(BaseModel):
    recipe_id: int
    version: int
    within_tolerance: bool = Field(
        ...,
        description="最优补液方案的最坏端点最大相对偏差是否 ≤ 版本容差；"
        "即使为 false 也返回最优补液量",
    )
    optimal: bool = Field(..., description="minimax LP 是否返回最优解")
    max_relative_deviation: float = Field(
        ..., description="各目标元素最坏端点相对偏差绝对值的最大值（分数）"
    )
    current_volume_ml: float
    final_volume_ml: float
    water_to_add_ml: float = Field(
        ...,
        description="清水补液量 (mL) = 目标终体积 - 现有液量 - 浓缩液补液合计",
    )
    concentrate_total_ml: float = Field(..., description="浓缩液补液合计 (mL)")
    tolerance: float = Field(..., description="采用的版本容差（分数）")
    additions: list[RetestAddition]
    elements: list[RetestElementResult]
    violations: list[Violation] = []
    message: str = ""
