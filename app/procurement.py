"""Whole-package replenishment procurement planning for one recipe version.

Given the saved recipe version's solved mix and an irrigation schedule
(identical expansion/merge/DST/suspension semantics to the consumption
forecast), each actual batch of water ``W`` draws

    required_i = a_i / V * W

mL of concentrate *i* — where ``a_i`` is the version's allocated volume
per full target batch and ``V`` its target volume.

Concentrates can be replenished through **quotes**: each quote sells one
concentrate in fixed whole packages (``package_ml`` mL each) at a fixed
per-package ``price``, with at most ``max_packages`` packages available,
arriving on one local date.  Packages arriving on local date *d* are
stocked **before the first batch of that local day**, so even a batch
starting at local 00:00 can use them; a cross-midnight batch draws once,
at its *start* instant: it is attributed to (and can receive deliveries
of) its local start date only, never again on the calendar day it ends
in.

The optimization chooses non-negative integer package counts to minimize
the total price of a plan whose per-batch post-deduction stock of every
concentrate is never negative; ties on total price are broken by the
minimum total package count.  With ``x_q`` the package count of quote
``q`` and ``C_i(d)`` the cumulative requirement of concentrate *i*
through the last batch attributed to local day *d*, feasibility is

    initial_i + sum_{q of i, arrival(q) <= d} package_q * x_q >= C_i(d)

for every batch day *d* (arrivals before the plan start are simply
available from day one).  This is a pure integer linear program (HiGHS).

If no purchase combination can cover the schedule (a quote arrives too
late or caps bound the total supply), the result is ``feasible=false``
with **no purchase counts at all** (never a fabricated unexecutable
order) and the *earliest* batch — together with every concentrate short
in it — where even buying the maximum at every quote still leaves a
gap.
"""
from __future__ import annotations

from typing import Any, Optional
from zoneinfo import ZoneInfo

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .irrigation import build_plan

# mL rounding for output volumes.
_ROUND = 4
# Currency rounding for prices / totals.
_COST_ROUND = 2
# Tolerance for float noise around zero stock (mirrors the forecast).
_EPS = 1e-7
# "Unlimited" supply is represented internally by a finite huge cap; real
# requirements are at most ~370 days * daily_water * ratio, far below this.
_BIG = 10 ** 9
# Slack for re-fixing the stage-1 minimum cost in the package-count
# tie-break stage so the second MILP cannot drift above the optimum.
_COST_SLACK_REL = 1e-9
_COST_SLACK_ABS = 1e-7


class ProcurementError(ValueError):
    """Stock or quotes do not match the recipe version's concentrates."""


def _r(x: float) -> float:
    """Round for output, normalizing -0.0 from float arithmetic."""
    v = round(float(x), _ROUND)
    return 0.0 if v == 0 else v


def build_procurement_plan(
    *,
    recipe_id: int,
    version: int,
    solve_result: dict[str, Any],
    tz: ZoneInfo,
    start_date,
    end_date,
    sessions: list[tuple],
    stock: dict[str, float],
    quotes: list[dict[str, Any]],
    suspended_dates: set | None = None,
) -> dict[str, Any]:
    """Compute the minimum-cost whole-package procurement plan.

    ``quotes`` is a list of dicts with keys ``concentrate``,
    ``package_ml``, ``price``, ``arrival_date`` (``datetime.date``) and
    optional ``max_packages`` (``None`` = unlimited).  Raises
    :class:`ProcurementError` when a stock entry or a quote names a
    concentrate not used by the version, or a version concentrate has no
    stock entry.
    """
    allocations = solve_result.get("allocations") or []
    names = [str(a.get("concentrate")) for a in allocations]
    total_volume_ml = float(solve_result["total_volume_ml"])
    if total_volume_ml <= 0:
        raise ProcurementError(
            "配方版本的目标总体积必须为正，无法按比例折算用液需求。"
        )

    unknown_stock = sorted(set(stock) - set(names))
    if unknown_stock:
        raise ProcurementError(
            f"库存中存在配方版本未使用的未知浓缩液：{ '、'.join(unknown_stock) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing = [n for n in names if n not in stock]
    if missing:
        raise ProcurementError(
            f"库存缺少该配方版本使用的浓缩液：{ '、'.join(missing) }；"
            "请为每一种浓缩液提供当前库存。"
        )
    unknown_quote = sorted({q["concentrate"] for q in quotes} - set(names))
    if unknown_quote:
        raise ProcurementError(
            f"报价中存在配方版本未使用的未知浓缩液：{ '、'.join(unknown_quote) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )

    # mL of concentrate per mL of irrigation water, per bottle.
    ratio = {
        n: float(a.get("volume_ml", 0.0)) / total_volume_ml
        for n, a in zip(names, allocations)
    }
    initial = {n: float(stock[n]) for n in names}

    plan = build_plan(
        tz=tz,
        start_date=start_date,
        end_date=end_date,
        sessions=sessions,
        daily_capacity_ml=None,
        suspended_dates=suspended_dates,
    )
    batches = plan["batches"]
    m = len(batches)

    # ---- per-batch requirements and attributed local days --------------- #
    req: list[dict[str, float]] = []
    days: list[str] = []
    for b in batches:
        water = float(b["water_ml"])
        days.append(b["local_start"][:10])
        req.append({n: ratio[n] * water for n in names})

    # Earliest batch index attributed to each local day: arrivals on that
    # day are stocked before exactly this batch (first-local-batch rule);
    # a cross-midnight batch attributed to the previous day never receives
    # the next day's deliveries.
    first_batch_of_day: dict[str, int] = {}
    for t, d in enumerate(days):
        first_batch_of_day.setdefault(d, t)

    # ---- quote arrays (request order preserved) ------------------------- #
    qty = np.array([float(q["package_ml"]) for q in quotes], dtype=float)
    price = np.array([float(q["price"]) for q in quotes], dtype=float)
    qday = [q["arrival_date"].isoformat() for q in quotes]
    qconc = [q["concentrate"] for q in quotes]
    qmax: list[Optional[int]] = [q.get("max_packages") for q in quotes]
    nq = len(quotes)
    x_cap = np.array(
        [float(_BIG if qmax[j] is None else qmax[j]) for j in range(nq)],
        dtype=float,
    )

    # ---- 1) maximal-supply feasibility check ---------------------------- #
    sim_cap = _simulate(
        start_iso=plan["start_date"],
        names=names,
        req=req,
        days=days,
        first_batch_of_day=first_batch_of_day,
        batches=batches,
        initial=initial,
        qty=qty,
        qday=qday,
        qconc=qconc,
        x=x_cap,
        show_inbound=False,
    )
    if sim_cap["shortfall"] is not None:
        return _infeasible_result(
            recipe_id=recipe_id,
            version=version,
            solve_result=solve_result,
            plan=plan,
            names=names,
            ratio=ratio,
            initial=initial,
            quotes=quotes,
            qty=qty,
            price=price,
            qday=qday,
            qconc=qconc,
            qmax=qmax,
            sim=sim_cap,
        )

    # ---- 2) integer program: min price, then min package count ---------- #
    # One constraint per (concentrate, attributed batch day):
    #   sum_{q of i, arrival(q) <= d} package_q * x_q >= C_i(d) - initial_i
    distinct_days = list(dict.fromkeys(days))
    cum = {n: 0.0 for n in names}
    cum_at: dict[str, dict[str, float]] = {}
    for t, d in enumerate(days):
        for n in names:
            cum[n] += req[t][n]
        cum_at[d] = dict(cum)

    rows: list[np.ndarray] = []
    rhs_list: list[float] = []
    # Slack scales with batch count so cumulative float noise (up to one
    # year of daily batches) never masquerades as a real shortage.
    rhs_slack = _EPS * max(1, m)
    for d in distinct_days:
        eligible = [j for j in range(nq) if qday[j] <= d]
        for n in names:
            rhs = cum_at[d][n] - initial[n]
            if rhs <= rhs_slack or not eligible:
                continue
            row = np.zeros(nq)
            for j in eligible:
                if qconc[j] == n:
                    row[j] = qty[j]
            if row.max() <= 0.0:
                # No quote of this bottle can arrive by the cut: the
                # maximal-supply check would already have failed; skip a
                # redundant infeasible row.
                continue
            rows.append(row)
            rhs_list.append(max(rhs - rhs_slack, 0.0))

    constraints = (
        LinearConstraint(
            np.vstack(rows), np.array(rhs_list), np.full(len(rhs_list), np.inf)
        )
        if rows
        else None
    )
    bounds = Bounds(np.zeros(nq), x_cap)
    integrality = np.ones(nq)

    x = np.zeros(nq, dtype=float)
    cost_star = 0.0
    if nq:
        res1 = milp(c=price, integrality=integrality, bounds=bounds,
                    constraints=constraints)
        # The maximal-supply simulation proved feasibility; guard anyway.
        if res1 is None or res1.x is None:
            return _infeasible_result(
                recipe_id=recipe_id,
                version=version,
                solve_result=solve_result,
                plan=plan,
                names=names,
                ratio=ratio,
                initial=initial,
                quotes=quotes,
                qty=qty,
                price=price,
                qday=qday,
                qconc=qconc,
                qmax=qmax,
                sim=sim_cap,
            )
        cost_star = float(res1.fun)

        # Stage 2: price <= stage-1 optimum (+ slack); minimize packages.
        cost_cap = cost_star * (1.0 + _COST_SLACK_REL) + _COST_SLACK_ABS
        rows2 = rows + [price]
        lb2 = np.array(rhs_list + [-np.inf])
        ub2 = np.array([np.inf] * len(rhs_list) + [cost_cap])
        res2 = milp(
            c=np.ones(nq),
            integrality=integrality,
            bounds=bounds,
            constraints=LinearConstraint(np.vstack(rows2), lb2, ub2),
        )
        res = res2 if (res2 is not None and res2.x is not None) else res1
        x = np.rint(res.x).astype(float)
        x = np.minimum(np.maximum(x, 0.0), x_cap)

    # ---- 3) simulate the chosen plan and assemble the response ---------- #
    sim = _simulate(
        start_iso=plan["start_date"],
        names=names,
        req=req,
        days=days,
        first_batch_of_day=first_batch_of_day,
        batches=batches,
        initial=initial,
        qty=qty,
        qday=qday,
        qconc=qconc,
        x=x,
        show_inbound=True,
    )
    xi = np.rint(x).astype(int)
    total_cost = round(float((price * xi).sum()), _COST_ROUND)
    total_packages = int(xi.sum())

    early_volume = {n: 0.0 for n in names}
    purchased = {n: 0.0 for n in names}
    for j in range(nq):
        purchased[qconc[j]] += float(qty[j]) * int(xi[j])
        if qday[j] < plan["start_date"]:
            early_volume[qconc[j]] += float(qty[j]) * int(xi[j])

    liquids_summary = [
        {
            "concentrate": n,
            "initial_stock_ml": _r(initial[n]),
            "initial_available_ml": _r(initial[n] + early_volume[n]),
            "mix_ml_per_l": _r(ratio[n] * 1000.0),
            "total_required_ml": _r(sim["total_required"][n]),
            "purchased_ml": _r(purchased[n]),
            "final_remaining_ml": _r(sim["final_level"][n]),
        }
        for n in names
    ]

    message = (
        "采购组合求解成功：在保证每批扣减后各浓缩液库存均非负的前提下，"
        f"最低总价 {total_cost:g}，共采购 {total_packages} 包"
        "（同价已取总包数最少）。"
    )
    return {
        "recipe_id": recipe_id,
        "version": version,
        "recipe_feasible": bool(solve_result.get("feasible")),
        "warning": _warning_for(solve_result),
        "timezone": plan["timezone"],
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "days": plan["days"],
        "suspended_dates": plan["suspended_dates"],
        "batch_count_before_merge": plan["batch_count_before_merge"],
        "batch_count_after_merge": plan["batch_count_after_merge"],
        "planned_water_ml": _r(plan["planned_water_ml"]),
        "feasible": True,
        "total_cost": total_cost,
        "total_packages": total_packages,
        "quotes": _quote_rows(quotes, qty, price, qday, qmax, xi),
        "liquids": liquids_summary,
        "batches": sim["batches"],
        "first_shortfall": None,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# Batch simulation
# --------------------------------------------------------------------------- #
def _simulate(
    *,
    start_iso: str,
    names: list[str],
    req: list[dict[str, float]],
    days: list[str],
    first_batch_of_day: dict[str, int],
    batches: list[dict],
    initial: dict[str, float],
    qty: np.ndarray,
    qday: list[str],
    qconc: list[str],
    x: np.ndarray,
    show_inbound: bool,
) -> dict[str, Any]:
    """Deduct batch requirements against stock under package counts ``x``.

    Quote packages are stocked before the first batch attributed to their
    arrival local day (quotes arriving before the horizon fold into the
    starting level).  Returns per-batch rows (each liquid's requirement
    and post-deduction remaining, always clamped at >= 0), per-liquid
    cumulative requirements and final levels, and the first un-coverable
    batch (``shortfall``) or ``None``.
    """
    nq = len(qday)
    arrivals: dict[str, dict[str, float]] = {}
    inbound_refs: dict[int, list[dict]] = {}
    early = {n: 0.0 for n in names}
    xi = np.rint(x).astype(int) if nq else []
    for j in range(nq):
        packs = int(xi[j])
        if packs == 0:
            continue
        vol = float(qty[j]) * packs
        if qday[j] < start_iso:
            # Arrives before the plan horizon: part of the starting level.
            early[qconc[j]] += vol
            continue
        t0 = first_batch_of_day.get(qday[j])
        if t0 is None:
            # Arrives on a day that schedules no attributed batch (e.g. a
            # suspended day): stocked at the start of that local day, hence
            # available to the next day's first batch.  Attach it there.
            later = [t for t, d in enumerate(days) if d > qday[j]]
            if not later:
                continue  # after the last batch: never usable
            t0 = later[0]
        slot = arrivals.setdefault(days[t0], {})
        slot[qconc[j]] = slot.get(qconc[j], 0.0) + vol
        if show_inbound:
            inbound_refs.setdefault(t0, []).append(
                {
                    "quote_index": j + 1,
                    "concentrate": qconc[j],
                    "packages": packs,
                    "volume_ml": _r(vol),
                }
            )

    level = {n: initial[n] + early[n] for n in names}
    total_required = {n: 0.0 for n in names}
    batches_out: list[dict] = []
    shortfall: Optional[dict] = None
    for t, b in enumerate(batches):
        d = days[t]
        if first_batch_of_day.get(d) == t:
            for n, vol in arrivals.get(d, {}).items():
                level[n] += vol
        inbound: list[dict] = []
        if show_inbound and first_batch_of_day.get(d) == t:
            inbound = sorted(inbound_refs.get(t, []),
                             key=lambda z: z["quote_index"])

        liquids = []
        gap_liquids = []
        batch_short = False
        for n in names:
            need = req[t][n]
            total_required[n] += need
            on_hand = level[n]
            if need <= on_hand + _EPS:
                residual = on_hand - need
                level[n] = 0.0 if residual <= _EPS else residual
            else:
                batch_short = True
                avail = max(on_hand, 0.0)
                gap_liquids.append(
                    {
                        "concentrate": n,
                        "required_ml": _r(need),
                        "available_ml": _r(avail),
                        "shortfall_ml": _r(need - avail),
                    }
                )
                level[n] = 0.0
            liquids.append(
                {
                    "concentrate": n,
                    "required_ml": _r(need),
                    "remaining_ml": _r(level[n]),
                }
            )

        if shortfall is None and batch_short:
            names_short = "、".join(g["concentrate"] for g in gap_liquids)
            shortfall = {
                "batch_index": t + 1,
                "start_utc": b["start_utc"],
                "local_start": b["local_start"],
                "local_end": b["local_end"],
                "attributed_date": d,
                "water_ml": _r(float(b["water_ml"])),
                "liquids": gap_liquids,
                "message": (
                    f"即使把所有报价都按上限采购，第 {t + 1} 个批次"
                    f"（{b['local_start']} 开始，归属本地日 {d}）的 "
                    f"{names_short} 仍出现缺口；未生成任何采购单，"
                    "请增加报价、放宽供应上限或安排更早的到货。"
                ),
            }

        batches_out.append(
            {
                "index": t + 1,
                "start_utc": b["start_utc"],
                "local_start": b["local_start"],
                "local_end": b["local_end"],
                "attributed_date": d,
                "water_ml": _r(float(b["water_ml"])),
                "merged": b["merged"],
                "batch_count_before_merge": b["batch_count_before_merge"],
                "crosses_midnight": b["crosses_midnight"],
                "inbound": inbound,
                "liquids": liquids,
                "sufficient": not batch_short,
            }
        )

    return {
        "batches": batches_out,
        "shortfall": shortfall,
        "total_required": total_required,
        "final_level": dict(level),
    }


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _warning_for(solve_result: dict[str, Any]) -> Optional[str]:
    if bool(solve_result.get("feasible")):
        return None
    return (
        "该配方版本的求解结果在容差范围内不可行（feasible=false），"
        "用液需求按其折中方案的 allocations 折算，采购结果仅供参考。"
    )


def _quote_rows(
    quotes: list[dict[str, Any]],
    qty: np.ndarray,
    price: np.ndarray,
    qday: list[str],
    qmax: list[Optional[int]],
    x,
) -> list[dict[str, Any]]:
    out = []
    for j, q in enumerate(quotes):
        packs = int(x[j])
        out.append(
            {
                "index": j + 1,
                "concentrate": q["concentrate"],
                "package_ml": float(qty[j]),
                "price": float(price[j]),
                "arrival_date": qday[j],
                "max_packages": qmax[j],
                "packages": packs,
                "volume_ml": _r(float(qty[j]) * packs),
                "cost": round(float(price[j]) * packs, _COST_ROUND),
            }
        )
    return out


def _infeasible_result(
    *,
    recipe_id,
    version,
    solve_result,
    plan,
    names,
    ratio,
    initial,
    quotes,
    qty,
    price,
    qday,
    qconc,
    qmax,
    sim,
) -> dict[str, Any]:
    """Build the feasible=false response (no fabricated purchase order)."""
    # Finite-cap maximal supply per bottle (unlimited quotes contribute
    # nothing to this finite diagnostic figure).
    capped_supply = {n: 0.0 for n in names}
    for j in range(len(quotes)):
        if qmax[j] is not None:
            capped_supply[qconc[j]] += float(qty[j]) * int(qmax[j])

    liquids_summary = [
        {
            "concentrate": n,
            "initial_stock_ml": _r(initial[n]),
            "initial_available_ml": _r(initial[n]),
            "mix_ml_per_l": _r(ratio[n] * 1000.0),
            "total_required_ml": _r(sim["total_required"][n]),
            "purchased_ml": None,
            "capped_supply_ml": _r(capped_supply[n]),
            "final_remaining_ml": None,
        }
        for n in names
    ]

    # No order exists: package counts / volumes / costs stay null instead
    # of pretending the maximal-cap "purchase" is executable.
    quote_rows = []
    for j, q in enumerate(quotes):
        quote_rows.append(
            {
                "index": j + 1,
                "concentrate": q["concentrate"],
                "package_ml": float(qty[j]),
                "price": float(price[j]),
                "arrival_date": qday[j],
                "max_packages": qmax[j],
                "packages": None,
                "volume_ml": None,
                "cost": None,
            }
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "recipe_feasible": bool(solve_result.get("feasible")),
        "warning": _warning_for(solve_result),
        "timezone": plan["timezone"],
        "start_date": plan["start_date"],
        "end_date": plan["end_date"],
        "days": plan["days"],
        "suspended_dates": plan["suspended_dates"],
        "batch_count_before_merge": plan["batch_count_before_merge"],
        "batch_count_after_merge": plan["batch_count_after_merge"],
        "planned_water_ml": _r(plan["planned_water_ml"]),
        "feasible": False,
        "total_cost": None,
        "total_packages": None,
        "quotes": quote_rows,
        "liquids": liquids_summary,
        "batches": sim["batches"],
        "first_shortfall": sim["shortfall"],
        "message": (
            "无满足全部批次扣减后库存非负的整包采购组合：报价到货过晚或"
            "供应上限不足，未生成采购单；最早缺口见 first_shortfall。"
        ),
    }
