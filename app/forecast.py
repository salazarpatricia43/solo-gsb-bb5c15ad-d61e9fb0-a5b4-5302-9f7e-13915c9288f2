"""Concentrate consumption forecast for one saved recipe version.

The saved recipe version's solve result gives, for its target working
solution volume ``V`` (mL), the volume ``a_i`` (mL) of each concentrate
allocated per batch.  Every actual irrigation batch of water ``W`` (mL)
is assumed to mix nutrients proportionally, so it draws

    required_i = a_i / V * W

mL from bottle *i*.

Forecast rules (all implemented here):

* Deduction happens once per **actual (post-merge) irrigation batch**,
  in execution order (UTC start).  Cross-midnight and merged batches are
  therefore counted exactly once; the whole batch is attributed to its
  **local start date** for per-day reporting.
* **Suspended local days** schedule no batches and consume no stock;
  they are still echoed (flagged ``suspended``) in the per-day rows and
  top-level ``suspended_dates``.  A cross-midnight batch begun on the
  previous local day is not cancelled and is still deducted once.
* When a bottle cannot cover its requirement, the batch still keeps its
  full planned water and full per-liquid requirement: consumption is
  clamped to the stock on hand and the missing amount is reported as
  ``shortfall_ml``.  Remaining stock never goes negative, and the
  forecast continues unchanged for all later batches.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .irrigation import build_plan

# Results are reported to 4 decimal places (mL); gaps below this are
# treated as numerical noise rather than a real shortage.
_ROUND = 4
_EPS = 1e-7


class ForecastError(ValueError):
    """Stock list does not match the recipe version's concentrates."""


def _r(x: float) -> float:
    """Round for output, normalizing -0.0 produced by float arithmetic."""
    v = round(float(x), _ROUND)
    return 0.0 if v == 0 else v


def build_forecast(
    *,
    recipe_id: int,
    version: int,
    solve_result: dict[str, Any],
    tz: ZoneInfo,
    start_date,
    end_date,
    sessions: list[tuple],
    stock: dict[str, float],
    suspended_dates: set | None = None,
) -> dict:
    """Compute the consumption forecast.

    ``solve_result`` is the persisted ``result`` payload of the recipe
    version; ``stock`` maps every concentrate name used by the result to
    its current on-hand mL.  Dates in ``suspended_dates`` schedule no
    batches and consume no stock (a batch begun on the previous local
    day still runs).  Raises :class:`ForecastError` when the stock keys
    do not match the result's concentrate names exactly.
    """
    allocations = solve_result.get("allocations") or []
    names = [str(a.get("concentrate")) for a in allocations]
    total_volume_ml = float(solve_result["total_volume_ml"])
    if total_volume_ml <= 0:
        raise ForecastError("配方版本的目标总体积必须为正，无法按比例折算消耗。")

    unknown = sorted(set(stock) - set(names))
    if unknown:
        raise ForecastError(
            f"库存中存在配方版本未使用的未知浓缩液：{ '、'.join(unknown) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing = [n for n in names if n not in stock]
    if missing:
        raise ForecastError(
            f"库存缺少该配方版本使用的浓缩液：{ '、'.join(missing) }；"
            "请为每一种浓缩液提供当前库存。"
        )

    # mL of concentrate per mL of irrigation water, per bottle.
    ratio = {
        n: float(a.get("volume_ml", 0.0)) / total_volume_ml
        for n, a in zip(names, allocations)
    }
    remaining = {n: float(stock[n]) for n in names}

    # No capacity input for forecasts: skip the device-capacity checks.
    plan = build_plan(
        tz=tz,
        start_date=start_date,
        end_date=end_date,
        sessions=sessions,
        daily_capacity_ml=None,
        suspended_dates=suspended_dates,
    )

    total_days = (end_date - start_date).days + 1
    all_dates = [
        (start_date + timedelta(days=i)).isoformat()
        for i in range(total_days)
    ]
    suspended = set(plan["suspended_dates"])
    # Cumulative per-liquid totals up to and including each day.
    day_required: dict[str, dict[str, float]] = {
        d: {n: 0.0 for n in names} for d in all_dates
    }
    day_consumed: dict[str, dict[str, float]] = {
        d: {n: 0.0 for n in names} for d in all_dates
    }
    day_short: dict[str, dict[str, float]] = {
        d: {n: 0.0 for n in names} for d in all_dates
    }
    day_water: dict[str, float] = {d: 0.0 for d in all_dates}
    day_batches: dict[str, int] = {d: 0 for d in all_dates}
    day_sufficient: dict[str, bool] = {d: True for d in all_dates}

    batches_out: list[dict] = []
    first_shortfall_date: str | None = None
    totals = {
        n: {"required": 0.0, "consumed": 0.0, "shortfall": 0.0}
        for n in names
    }

    for idx, b in enumerate(plan["batches"], start=1):
        water = float(b["water_ml"])
        # Whole batch attributed to its local start calendar day, so a
        # cross-midnight / merged batch is deducted exactly once.
        attributed_date = b["local_start"][:10]
        if attributed_date not in day_required:  # pragma: no cover - defensive
            attributed_date = (
                start_date + timedelta(days=total_days - 1)
            ).isoformat()

        liquids: list[dict] = []
        batch_short = False
        for n in names:
            required = ratio[n] * water
            on_hand = remaining[n]
            # Tolerate sub-1e-7 mL float accumulation (e.g. exact stock
            # exhaustion over several batches must not show a phantom gap).
            if required <= on_hand + _EPS:
                consumed = required
                shortfall = 0.0
            else:
                consumed = max(on_hand, 0.0)
                shortfall = required - consumed
                batch_short = True
            residual = max(on_hand - consumed, 0.0)
            if shortfall <= _EPS:
                shortfall = 0.0
            # clamp sub-epsilon residue / phantom negative to exact zero
            remaining[n] = 0.0 if residual <= _EPS else residual

            liquids.append(
                {
                    "concentrate": n,
                    "required_ml": _r(required),
                    "consumed_ml": _r(consumed),
                    "shortfall_ml": _r(shortfall),
                }
            )
            totals[n]["required"] += required
            totals[n]["consumed"] += consumed
            totals[n]["shortfall"] += shortfall
            day_required[attributed_date][n] += required
            day_consumed[attributed_date][n] += consumed
            day_short[attributed_date][n] += shortfall

        day_water[attributed_date] += water
        day_batches[attributed_date] += 1
        if batch_short:
            day_sufficient[attributed_date] = False
            if first_shortfall_date is None:
                first_shortfall_date = attributed_date

        batches_out.append(
            {
                "index": idx,
                "start_utc": b["start_utc"],
                "local_start": b["local_start"],
                "local_end": b["local_end"],
                "attributed_date": attributed_date,
                "water_ml": _r(water),
                "merged": b["merged"],
                "batch_count_before_merge": b["batch_count_before_merge"],
                "crosses_midnight": b["crosses_midnight"],
                "sufficient": not batch_short,
                "liquids": liquids,
            }
        )

    # Per-day rows (every day in range, even with no attributed batch),
    # remaining computed from cumulative consumption so end-of-day stock
    # follows directly from the initial level.
    days_out: list[dict] = []
    cum_consumed: dict[str, float] = {n: 0.0 for n in names}
    for d in all_dates:
        liquids_out = []
        for n in names:
            cum_consumed[n] += day_consumed[d][n]
            liquids_out.append(
                {
                    "concentrate": n,
                    "required_ml": _r(day_required[d][n]),
                    "consumed_ml": _r(day_consumed[d][n]),
                    "shortfall_ml": _r(day_short[d][n]),
                    "remaining_ml": _r(
                        stock[n] - cum_consumed[n]
                    ),
                }
            )
        days_out.append(
            {
                "date": d,
                "suspended": d in suspended,
                "batches": day_batches[d],
                "water_ml": _r(day_water[d]),
                "sufficient": day_sufficient[d],
                "liquids": liquids_out,
            }
        )

    liquids_summary = []
    overall_sufficient = first_shortfall_date is None
    for n in names:
        first_date: str | None = None
        for d in all_dates:
            if day_short[d][n] > _EPS:
                first_date = d
                break
        liquids_summary.append(
            {
                "concentrate": n,
                "initial_stock_ml": _r(stock[n]),
                "mix_ml_per_l": _r(ratio[n] * 1000.0),
                "total_required_ml": _r(totals[n]["required"]),
                "total_consumed_ml": _r(totals[n]["consumed"]),
                "total_shortfall_ml": _r(totals[n]["shortfall"]),
                "remaining_ml": _r(
                    stock[n] - totals[n]["consumed"]
                ),
                "first_shortfall_date": first_date,
            }
        )

    feasible = bool(solve_result.get("feasible"))
    warning = None
    if not feasible:
        warning = (
            "该配方版本的求解结果在容差范围内不可行（feasible=false），"
            "消耗按其折中方案的 allocations 折算，结果仅供参考。"
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "recipe_feasible": feasible,
        "warning": warning,
        "timezone": plan["timezone"],
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "days": plan["days"],
        "suspended_dates": plan["suspended_dates"],
        "batch_count_before_merge": plan["batch_count_before_merge"],
        "batch_count_after_merge": plan["batch_count_after_merge"],
        "planned_water_ml": _r(plan["planned_water_ml"]),
        "sufficient": overall_sufficient,
        "first_shortfall_date": first_shortfall_date,
        "liquids": liquids_summary,
        "day_results": days_out,
        "batches": batches_out,
    }
