"""FastAPI application: hydroponic nutrient mixing solver."""
from __future__ import annotations

import json
import math
from contextlib import asynccontextmanager
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import database
from .dispense import DispenseError, build_dispense_plan
from .forecast import ForecastError, build_forecast
from .irrigation import build_plan
from .models import (
    ConsumptionForecastRequest,
    ConsumptionForecastResult,
    DispensePlanRequest,
    DispensePlanResult,
    IrrigationPlanRequest,
    IrrigationPlanResult,
    ProcurementPlanRequest,
    ProcurementPlanResult,
    RecipeSummary,
    RecipeVersion,
    RetestCorrectionRequest,
    RetestCorrectionResult,
    SaveRecipeRequest,
    SolveRequest,
    SolveResult,
    VersionSummary,
)
from .procurement import ProcurementError, build_procurement_plan
from .retest import RetestError, build_retest_correction
from .solver import solve


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize / migrate SQLite on startup.
    database.init_db()
    yield


app = FastAPI(
    title="水培营养液配比求解 API",
    description=(
        "根据多种浓缩液的元素浓度、可用体积与稀释水约束，"
        "最小化各元素相对目标配方的最大偏差（minimax LP）。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _sanitize_json(value):
    """Replace non-JSON floats (NaN/Inf) with their textual names.

    Pydantic rejects those inputs with 422, but it also echoes the offending
    value in the error detail; Starlette's strict JSON encoder would then
    turn the 422 into a 500 while rendering.  Stringify such values so the
    validation error is serializable.
    """
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, dict):
        return {k: _sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize_json(v) for v in value]
    return value


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request, exc: RequestValidationError):
    detail = _sanitize_json(exc.errors())
    return JSONResponse(
        status_code=422,
        content={"detail": jsonable_encoder(detail)},
    )


@app.get("/health", tags=["system"])
def health() -> dict:
    return {"status": "ok"}


@app.post("/api/solve", response_model=SolveResult, tags=["solver"])
def trial_solve(req: SolveRequest) -> SolveResult:
    """试算：不入库，返回配比、各元素偏差及不可行原因。"""
    return solve(req)


@app.post(
    "/api/irrigation/plan",
    response_model=IrrigationPlanResult,
    tags=["irrigation"],
    summary="生成逐日灌溉执行批次（时区/DST 感知）",
)
def irrigation_plan(req: IrrigationPlanRequest) -> IrrigationPlanResult:
    """按 IANA 时区把每日重复的灌溉时段展开为 UTC 执行批次。

    - 夏令时**缺失**的本地时间（春季跳变）自动顺延并标注 ``gap``；
      **重复**的本地时间（秋季回拨）取第一次出现并标注 ``duplicate``。
    - 跨午夜批次为真实流逝时间区间；按 UTC 开始时间排序，相邻且间隔
      ≤ 1 分钟（含重叠）的批次合并，合并可跨越午夜。
    - 每日容量按**合并前**当日计划总水量校验；超出容量的日期在
      ``capacity_violations`` 中给出明确缺口（``shortfall_ml``），
      批次照常返回、绝不截断水量。
    - ``suspended_dates`` 中的本地日期停灌：不生成当日排定的批次，
      响应仍保留这些日期并标明停灌；不取消前一日开始的跨午夜批次。
    - 非法时区/日期/时间、空时段、重复开始时间、负数等均返回 422。
    """
    tz = ZoneInfo(req.timezone)
    sessions = [
        (s.start_time, s.start_time.strftime("%H:%M:%S"),
         s.duration_minutes, s.water_ml)
        for s in req.sessions
    ]
    plan = build_plan(
        tz=tz,
        start_date=req.start_date,
        end_date=req.end_date,
        sessions=sessions,
        daily_capacity_ml=req.daily_capacity_ml,
        suspended_dates=set(req.suspended_dates),
    )
    return IrrigationPlanResult(**plan)


@app.post(
    "/api/recipes",
    response_model=RecipeVersion,
    status_code=201,
    tags=["recipes"],
)
def save_recipe(
    payload: SaveRecipeRequest,
    force: bool = Query(
        False, description="为 true 时允许保存超出容差（不可行）的结果"
    ),
) -> RecipeVersion:
    """保存配方版本：同名配方自动追加新版本号，否则新建配方。"""
    result = solve(payload.request)
    if not result.feasible and not force:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "求解结果超出容差，未保存；可先调参，"
                "或带 ?force=true 强制保存。",
                "result": json.loads(result.model_dump_json()),
            },
        )

    recipe = database.ensure_recipe(payload.name, payload.description)
    row = database.create_version(
        recipe_id=recipe["id"],
        note=payload.note,
        request_payload=json.loads(payload.request.model_dump_json()),
        result_payload=json.loads(result.model_dump_json()),
    )
    return RecipeVersion(**database.serialize_version(row))


@app.get("/api/recipes", response_model=list[RecipeSummary], tags=["recipes"])
def list_recipes() -> list[RecipeSummary]:
    return [
        RecipeSummary(**database.serialize_recipe(r))
        for r in database.list_recipes()
    ]


@app.get("/api/recipes/{recipe_id}", response_model=RecipeSummary, tags=["recipes"])
def get_recipe(recipe_id: int) -> RecipeSummary:
    row = database.get_recipe(recipe_id)
    if row is None:
        raise HTTPException(404, "配方不存在")
    return RecipeSummary(**database.serialize_recipe(row))


@app.get(
    "/api/recipes/{recipe_id}/versions",
    response_model=list[VersionSummary],
    tags=["recipes"],
)
def list_versions(recipe_id: int) -> list[VersionSummary]:
    if database.get_recipe(recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    rows = database.list_versions(recipe_id)
    return [
        VersionSummary(
            id=r["id"],
            recipe_id=r["recipe_id"],
            version=r["version"],
            note=r["note"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


@app.get(
    "/api/recipes/{recipe_id}/versions/{version}",
    response_model=RecipeVersion,
    tags=["recipes"],
)
def get_version(recipe_id: int, version: int) -> RecipeVersion:
    if database.get_recipe(recipe_id) is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")
    return RecipeVersion(**database.serialize_version(row))


@app.post(
    "/api/recipes/{recipe_id}/versions/{version}/consumption-forecast",
    response_model=ConsumptionForecastResult,
    tags=["recipes"],
    summary="按灌溉计划预测浓缩液消耗与库存耗尽日期",
)
def consumption_forecast(
    recipe_id: int, version: int, req: ConsumptionForecastRequest
) -> ConsumptionForecastResult:
    """读取指定配方版本的求解结果，按实际灌溉批次等比例扣减浓缩液。

    - 配方中每升灌溉水需要的各浓缩液体积（``allocations /
      total_volume_ml``）乘以每个**合并后实际批次**的水量得到本批需求；
      跨午夜/合并批次整体只扣一次（归属批次本地开始日）。
    - ``suspended_dates`` 中的本地日期停灌：当日不排定批次、不扣减库存，
      但前一日本地日期开始、跨入该日的批次仍照常扣减一次。
    - 任一浓缩液库存不足时：批次水量与需求保持完整，实际扣减到 0 为止，
      缺口记入该批次与当日，**绝不出现负库存**，后续批次预测照常进行。
    - 输出每日消耗/剩余、各液全程汇总以及首个出现缺口的本地日期。
    - 配方/版本不存在返回 404；库存名称未知、缺少某液、库存为负或计划
      参数非法返回 422/4xx。
    """
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    result_payload = json.loads(row["result_json"])
    stock = {s.concentrate: s.stock_ml for s in req.stock}
    try:
        forecast = build_forecast(
            recipe_id=recipe_id,
            version=version,
            solve_result=result_payload,
            tz=ZoneInfo(req.timezone),
            start_date=req.start_date,
            end_date=req.end_date,
            sessions=[
                (s.start_time, s.start_time.strftime("%H:%M:%S"),
                 s.duration_minutes, s.water_ml)
                for s in req.sessions
            ],
            stock=stock,
            suspended_dates=set(req.suspended_dates),
        )
    except ForecastError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})
    return ConsumptionForecastResult(**forecast)


@app.post(
    "/api/recipes/{recipe_id}/versions/{version}/procurement-plan",
    response_model=ProcurementPlanResult,
    tags=["recipes"],
    summary="整包补货采购：按灌溉批次求最低总价的整包采购组合",
)
def procurement_plan(
    recipe_id: int, version: int, req: ProcurementPlanRequest
) -> ProcurementPlanResult:
    """读取指定配方版本的求解结果，按实际灌溉批次求整包补货采购组合。

    - 复用灌溉批次展开（DST/跨午夜/合并/停灌）与版本求解结果：每批用液
      需求 = 版本分配体积 ÷ 目标总体积 × **该批次实际（合并后）水量**；
      跨午夜批次在其本地开始时刻只扣一次。
    - 每个报价按**整包**出售（``package_ml`` mL/包、单价 ``price``、
      到货本地日期 ``arrival_date``、最多可购 ``max_packages`` 包）：
      到货在该日本地**首个批次前**入库，当日（含本地 00:00）批次均可使用。
    - 目标：在每批扣减后各浓缩液库存均非负的前提下，求**最低总价**的
      非负整数包数组合；总价相同时取**总包数最少**（整数线性规划，
      HiGHS，两阶段求解）。
    - 报价时点过晚或供应上限使目标无解时返回 200 且 ``feasible=false``，
      **不伪造采购单**（``quotes[].packages`` 全为 null），并在
      ``first_shortfall`` 给出最早出现缺口的批次及该批缺口的浓缩液
      （需求/可达供给/缺口）。
    - 配方/版本不存在返回 404；库存或报价名称与版本不匹配、重复/缺失
      库存、非正或非有限的整包容量/价格、到货日期或灌溉计划参数非法、
      多余字段均返回 422。
    """
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    result_payload = json.loads(row["result_json"])
    stock = {s.concentrate: s.stock_ml for s in req.stock}
    quotes = [
        {
            "concentrate": q.concentrate,
            "package_ml": q.package_ml,
            "price": q.price,
            "arrival_date": q.arrival_date,
            "max_packages": q.max_packages,
        }
        for q in req.quotes
    ]
    try:
        plan = build_procurement_plan(
            recipe_id=recipe_id,
            version=version,
            solve_result=result_payload,
            tz=ZoneInfo(req.timezone),
            start_date=req.start_date,
            end_date=req.end_date,
            sessions=[
                (s.start_time, s.start_time.strftime("%H:%M:%S"),
                 s.duration_minutes, s.water_ml)
                for s in req.sessions
            ],
            stock=stock,
            quotes=quotes,
            suspended_dates=set(req.suspended_dates),
        )
    except ProcurementError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})
    return ProcurementPlanResult(**plan)


@app.post(
    "/api/recipes/{recipe_id}/versions/{version}/dispense-plan",
    response_model=DispensePlanResult,
    tags=["recipes"],
    summary="整数泵送投料方案：按单次泵送量求解各浓缩液泵送次数",
)
def dispense_plan(
    recipe_id: int, version: int, req: DispensePlanRequest
) -> DispensePlanResult:
    """按版本目标浓度、总体积与原水基线，在固定单次泵送量下求整数泵次。

    - 每种浓缩液的投料量必须是其 ``pump_step_ml`` 的非负整数倍；
      稀释水精确补足 ``目标总体积 - 浓缩液投料量之和``，原水矿物质基线
      按**实际稀释水量**计入，而非按目标总体积预扣。
    - 以混合整数线性规划（HiGHS）最小化各目标元素的**最大相对偏差**；
      最优偏差相同时取泵次总数最少的方案。
    - 同时满足版本中的单液可用量 / ``max_volume`` 使用上限、
      浓缩液最大占比与最低稀释水量；某液单次泵送量超过其上限时该液
      泵次只能为 0，并在 ``note`` 中说明。
    - 整数步长无法达到版本容差时仍返回最优可执行方案
      （``feasible=true, within_tolerance=false``，逐元素偏差与
      violations 照常给出）；体积硬约束无解时返回
      ``feasible=false`` 并在 violations 中说明具体原因。
    - 配方 / 版本不存在返回 404；泵配置缺失、多余、名称重复或步长
      非正 / 非法返回 422。
    """
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    request_payload = json.loads(row["request_json"])
    try:
        plan = build_dispense_plan(
            recipe_id=recipe_id,
            version=version,
            request_payload=request_payload,
            pumps=[(p.concentrate, p.pump_step_ml) for p in req.pumps],
        )
    except DispenseError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})
    return DispensePlanResult(**plan)


@app.post(
    "/api/recipes/{recipe_id}/versions/{version}/retest-correction",
    response_model=RetestCorrectionResult,
    tags=["recipes"],
    summary="在缸复测纠偏：按实测浓度区间求最优补液量（只读，不改写版本）",
)
def retest_correction(
    recipe_id: int, version: int, req: RetestCorrectionRequest
) -> RetestCorrectionResult:
    """读取版本目标浓度/容差/浓缩液成分，按实测区间求 minimax 补液量。

    - 缸内现有 ``current_volume_ml`` mL 工作液，各目标元素实测浓度落在
      ``measurements`` 给出的 [下界, 上界] (mg/L) 区间内；只允许追加
      **版本内浓缩液**（受 ``stock`` 可追加库存约束）与**清水**，把液量
      补齐到 ``final_volume_ml``（物料守恒：现有液量 + Σ补液量 + 清水
      = 目标终体积；清水只占体积、不携带元素）。
    - 元素在缸中的实际量只知道落在实测区间内，而相对偏差关于该量是
      线性的、最坏情况必在端点取得；minimax LP（HiGHS）最小化所有
      实测区间**端点**在终体积下的最大相对目标偏差。LP 恒可行，故
      总是返回最优补液量。
    - 超出版本容差时仍返回最优补液量（``within_tolerance=false``），
      逐元素给出最坏端点（``worst_endpoint: lower/upper``）、终体积下
      两端点浓度与带符号偏差，超限项列入 ``violations``。
    - 接口**只读**：读取但不改写配方版本。
    - 配方/版本不存在返回 404；目标终体积小于现有液量、实测区间颠倒、
      实测元素与版本目标不符（缺少/多余/重复）、库存名称不符（未知/
      缺少/重复/为负/非有限）、版本含零目标浓度元素、体积或浓度非正/
      非有限、多余字段均返回 422。
    """
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    request_payload = json.loads(row["request_json"])
    try:
        result = build_retest_correction(
            recipe_id=recipe_id,
            version=version,
            request_payload=request_payload,
            current_volume_ml=req.current_volume_ml,
            final_volume_ml=req.final_volume_ml,
            measurements=[
                (m.element, m.lower_mg_l, m.upper_mg_l)
                for m in req.measurements
            ],
            stock={s.concentrate: s.stock_ml for s in req.stock},
        )
    except RetestError as exc:
        raise HTTPException(status_code=422, detail={"message": str(exc)})
    return RetestCorrectionResult(**result)


@app.get(
    "/api/recipes/{recipe_id}/versions/{version}/export",
    tags=["recipes"],
    summary="导出单个配方版本为 JSON 文件",
)
def export_version(recipe_id: int, version: int) -> JSONResponse:
    """以 attachment 方式下载该版本（含请求参数与求解结果）的 JSON。"""
    recipe = database.get_recipe(recipe_id)
    if recipe is None:
        raise HTTPException(404, "配方不存在")
    row = database.get_version(recipe_id, version)
    if row is None:
        raise HTTPException(404, "该版本不存在")

    payload = {
        "exported_at": database._now(),  # noqa: SLF001
        "recipe": database.serialize_recipe(recipe),
        "version": database.serialize_version(row),
    }
    # HTTP headers are latin-1: provide an ASCII fallback name plus an
    # RFC 5987 UTF-8 filename for Chinese recipe names.
    ascii_name = f"recipe-{recipe['id']}-v{version}.json"
    utf8_name = quote(f"recipe-{recipe['name']}-v{version}.json")
    disposition = (
        f"attachment; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{utf8_name}"
    )
    return JSONResponse(
        content=payload,
        headers={
            "Content-Disposition": disposition,
            "Content-Type": "application/json; charset=utf-8",
        },
    )
