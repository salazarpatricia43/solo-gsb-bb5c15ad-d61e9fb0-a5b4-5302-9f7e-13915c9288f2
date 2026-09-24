"""In-tank re-measurement correction for one saved recipe version.

A working tank already contains ``v0`` mL of mixed solution whose true
element concentrations are only known within measured intervals
``[l_e, u_e]`` (mg/L).  The tank must be topped up to a prescribed final
volume ``V`` by adding **only** that recipe version's concentrates
(``x_i`` mL, bounded by per-liquid on-hand stock ``s_i``) and clean,
mineral-free water (``w`` mL).  Element masses obey conservation:

    final mass of element e  =  v0 * c_e / 1000 + sum_i c_{i,e} * x_i / 1000

with ``c_e`` either measured endpoint ``l_e`` / ``u_e``.  Clean water
carries no elements (the version's raw-water baseline belongs to the
original recipe solve, not to this correction; the water added here is
plain water), so its only role is the volume identity

    sum_i x_i + w = V - v0.

For every target element with positive target concentration ``tau_e``
(mg/L), *both* measured interval endpoints must fit a minimax relative
band around the target mass ``tau_e * V / 1000``:

    delivered_e(endpoint) <= target_mass_e * (1 + t)
    delivered_e(endpoint) >= target_mass_e * (1 - t)

Objective: minimise ``t`` — the maximum relative deviation across all
elements **and both interval endpoints** (continuous LP, HiGHS).  Stage 2
holds ``t`` at its optimum and minimises the total concentrate volume
added, a deterministic, stock-conserving tie-break (free dilution water
fills the remaining volume).

When the optimum ``t`` still exceeds the version tolerance, the best
correction is returned anyway (``within_tolerance=false``) with each
element's worst endpoint and the violating elements listed.  The saved
recipe version is never modified.
"""
from __future__ import annotations

from typing import Any

import numpy as np
from scipy.optimize import linprog

from .models import SolveRequest
from .solver import _EPS

# mL rounding for reported volumes.
_ROUND = 4
# Slack for re-fixing the optimum deviation in the tie-break stage so the
# second LP cannot lose the stage-1 optimum to floating-point noise.
_T_SLACK_REL = 1e-9
_T_SLACK_ABS = 1e-12


class TankCorrectionError(ValueError):
    """Version/inputs cannot support an in-tank correction."""


def build_tank_correction(
    *,
    recipe_id: int,
    version: int,
    request_payload: dict[str, Any],
    current_volume_ml: float,
    target_final_volume_ml: float,
    measurements: list[tuple[str, float, float]],
    stock: list[tuple[str, float]],
) -> dict[str, Any]:
    """Solve the in-tank correction LP for one saved version.

    ``request_payload`` is the version's persisted :class:`SolveRequest`
    dict.  ``measurements`` is an ordered list of
    ``(element, measured_lower, measured_upper)``; ``stock`` maps each
    version concentrate name to its addable on-hand volume.  Raises
    :class:`TankCorrectionError` for a zero-target version, measurement
    elements or stock names that do not match the version.
    """
    req = SolveRequest.model_validate(request_payload)
    concentrates = req.concentrates
    names = [c.name for c in concentrates]
    n = len(concentrates)

    v0 = float(current_volume_ml)
    V = float(target_final_volume_ml)
    add_total = V - v0
    tol = float(req.tolerance)

    targets: dict[str, float] = {
        e: float(c) for e, c in req.targets.concentrations.items()
    }
    # Relative deviation is undefined at zero target; such versions are
    # not accepted for correction.
    zero_targets = sorted(e for e, c in targets.items() if c <= _EPS)
    if not targets or zero_targets:
        raise TankCorrectionError(
            "该配方版本存在目标浓度为零的元素"
            f"（{ '、'.join(zero_targets) if zero_targets else '目标元素为空' }），"
            "无法按相对偏差定义在缸复测纠偏，暂不接受。"
        )

    measured: dict[str, tuple[float, float]] = {}
    for elem, lo, hi in measurements:
        measured[elem] = (float(lo), float(hi))
    unknown = sorted(set(measured) - set(targets))
    if unknown:
        raise TankCorrectionError(
            f"实测数据中存在该版本未设目标的元素：{ '、'.join(unknown) }；"
            f"该版本目标元素为：{ '、'.join(targets) }。"
        )
    missing = [e for e in targets if e not in measured]
    if missing:
        raise TankCorrectionError(
            f"实测数据缺少该版本的目标元素：{ '、'.join(missing) }；"
            "请为每一个目标元素提供实测浓度上下界。"
        )

    stocks: dict[str, float] = {}
    for name, amount in stock:
        stocks[name] = float(amount)
    unknown_stock = sorted(set(stocks) - set(names))
    if unknown_stock:
        raise TankCorrectionError(
            f"库存中存在该版本未使用的浓缩液：{ '、'.join(unknown_stock) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing_stock = [name for name in names if name not in stocks]
    if missing_stock:
        raise TankCorrectionError(
            f"库存缺少该版本使用的浓缩液：{ '、'.join(missing_stock) }；"
            "请为每一种浓缩液提供可追加库存（可为 0）。"
        )

    # Element order follows the version's target declaration.
    elems = list(targets.keys())
    # mg of each element delivered per mL of concentrate i: c [mg/L] / 1000.
    A = np.zeros((n, len(elems)), dtype=float)
    for i, c in enumerate(concentrates):
        for j, e in enumerate(elems):
            A[i, j] = float(c.concentrations.get(e, 0.0)) / 1000.0

    # Mass (mg) already in the tank at each measured interval endpoint.
    m_lo = np.array(
        [v0 * measured[e][0] / 1000.0 for e in elems], dtype=float
    )
    m_hi = np.array(
        [v0 * measured[e][1] / 1000.0 for e in elems], dtype=float
    )
    # Target mass (mg) at the final volume.
    T = np.array(
        [targets[e] * V / 1000.0 for e in elems], dtype=float
    )
    caps = np.array([stocks[name] for name in names], dtype=float)

    # x = 0 / w = add_total always satisfies the volume identity and the
    # non-negativity / stock bounds, so stage 1 is always feasible.
    res1 = _lp(A, T, m_lo, m_hi, caps, add_total,
               t_upper=np.inf, save_concentrate=False)
    if res1 is None:  # purely defensive
        raise TankCorrectionError("在缸纠偏求解失败，请检查输入参数。")
    t_star = float(res1.fun)
    if t_star < 0.0:
        t_star = 0.0

    # Stage 2: keep t at its optimum, minimise total concentrate added
    # (water fills whatever volume remains).
    t_cap = t_star * (1.0 + _T_SLACK_REL) + _T_SLACK_ABS
    res2 = _lp(A, T, m_lo, m_hi, caps, add_total,
               t_upper=t_cap, save_concentrate=True)
    res = res2 if res2 is not None else res1
    optimal = bool(res.success)

    x = np.maximum(res.x[:n], 0.0)
    w = add_total - float(x.sum())
    t_star = max(t_star, 0.0)

    additions = [
        {
            "concentrate": names[i],
            "add_volume_ml": _r(float(x[i])),
            "stock_ml": float(caps[i]),
        }
        for i in range(n)
    ]

    # -- per-element endpoint report -------------------------------------- #
    element_rows: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []
    for j, e in enumerate(elems):
        added_mg = float(A[:, j] @ x)
        final_lo_conc = (m_lo[j] + added_mg) * 1000.0 / V
        final_hi_conc = (m_hi[j] + added_mg) * 1000.0 / V
        target_conc = float(targets[e])
        dev_lo = (final_lo_conc - target_conc) / target_conc
        dev_hi = (final_hi_conc - target_conc) / target_conc
        if abs(dev_lo) >= abs(dev_hi):
            worst_endpoint, worst_dev = "lower", dev_lo
        else:
            worst_endpoint, worst_dev = "upper", dev_hi
        if abs(worst_dev) <= tol + _EPS:
            status = "ok"
        else:
            status = "under" if worst_dev < 0 else "over"
        element_rows.append(
            {
                "element": e,
                "target_concentration": round(target_conc, 6),
                "measured_lower_mg_per_l": round(float(measured[e][0]), 6),
                "measured_upper_mg_per_l": round(float(measured[e][1]), 6),
                "final_lower_concentration": round(float(final_lo_conc), 6),
                "final_upper_concentration": round(float(final_hi_conc), 6),
                "worst_endpoint": worst_endpoint,
                "worst_relative_deviation": round(float(worst_dev), 6),
                "status": status,
            }
        )
        if status != "ok":
            band = (
                target_conc * (1.0 - tol) if status == "under"
                else target_conc * (1.0 + tol)
            )
            worst_conc = (
                final_lo_conc if worst_endpoint == "lower"
                else final_hi_conc
            )
            endpoint_name = "下界" if worst_endpoint == "lower" else "上界"
            direction = "偏低" if status == "under" else "偏高"
            violations.append(
                {
                    "code": "ELEMENT_DEVIATION",
                    "message": (
                        f"元素 {e} 实测{endpoint_name}端点在终体积下"
                        f"{direction}：{worst_conc:.3f} mg/L，目标 "
                        f"{target_conc:.3f} mg/L（容差 {tol:.0%}，"
                        f"容差边界 {band:.3f} mg/L），相对偏差 "
                        f"{worst_dev:.2%}；受可追加库存/成分配比耦合限制，"
                        "该端点在最优补液方案中仍超出容差。"
                    ),
                    "element": e,
                    "limit": round(float(band), 6),
                    "actual": round(float(worst_conc), 6),
                }
            )

    max_dev = max(abs(r["worst_relative_deviation"]) for r in element_rows)
    within_tolerance = max_dev <= tol + _EPS
    if within_tolerance:
        message = (
            "在缸复测纠偏求解成功：按建议追加后，所有目标元素实测区间端点"
            "在终体积下的偏差均在版本容差范围内。"
        )
    else:
        message = (
            f"在缸复测纠偏最优最大相对偏差为 {max_dev:.2%}，"
            f"超过版本容差 {tol:.2%}；仍返回偏差最小的补液方案，"
            "各元素最坏端点与超限项见 elements 与 violations。"
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "within_tolerance": bool(within_tolerance),
        "optimal": optimal,
        "max_relative_deviation": round(float(max_dev), 6),
        "tolerance": tol,
        "current_volume_ml": _r(v0),
        "target_final_volume_ml": _r(V),
        "final_volume_ml": _r(v0 + float(x.sum()) + w),
        "added_total_ml": _r(add_total),
        "water_add_ml": _r(w),
        "concentrate_total_ml": _r(float(x.sum())),
        "additions": additions,
        "elements": element_rows,
        "violations": violations,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# LP
# --------------------------------------------------------------------------- #
def _lp(
    A: np.ndarray,
    T: np.ndarray,
    m_lo: np.ndarray,
    m_hi: np.ndarray,
    caps: np.ndarray,
    add_total: float,
    *,
    t_upper: float,
    save_concentrate: bool,
):
    """Solve one stage of the in-tank correction minimax program.

    Variables ``z = (x_0..x_{n-1}, w, t)``.  For each element both measured
    endpoints must fit the band:

        m_endpoint + A[:, j]·x <= T_j*(1+t)   (upper endpoints for the
            high reading, lower endpoints for the low reading)
        m_endpoint + A[:, j]·x >= T_j*(1-t)
    """
    n = len(caps)
    dim = n + 2

    rows: list[np.ndarray] = []
    rhs: list[float] = []
    for j in range(len(T)):
        a = A[:, j]
        # High endpoint upper band: m_hi + a·x <= T(1+t)
        #   ->  a·x - T*t <= T - m_hi
        row_up = np.zeros(dim)
        row_up[:n] = a
        row_up[n + 1] = -T[j]
        rows.append(row_up)
        rhs.append(T[j] - m_hi[j])
        # Low endpoint lower band: m_lo + a·x >= T(1-t)
        #   ->  -a·x - T*t <= m_lo - T
        row_lo = np.zeros(dim)
        row_lo[:n] = -a
        row_lo[n + 1] = -T[j]
        rows.append(row_lo)
        rhs.append(m_lo[j] - T[j])

    # Volume identity: sum x + w = V - v0.
    eq = np.zeros(dim)
    eq[: n + 1] = 1.0

    bounds = [(0.0, float(caps[i])) for i in range(n)]
    bounds.append((0.0, add_total))  # clean water
    bounds.append((0.0, float(t_upper)))

    c_obj = np.zeros(dim)
    if save_concentrate:
        c_obj[:n] = 1.0  # stage-2 tie-break: add as little concentrate as possible
    else:
        c_obj[n + 1] = 1.0

    res = linprog(
        c=c_obj,
        A_ub=np.vstack(rows),
        b_ub=np.array(rhs),
        A_eq=eq.reshape(1, -1),
        b_eq=np.array([add_total]),
        bounds=bounds,
        method="highs",
    )
    return res if res.success else None


def _r(x: float) -> float:
    """Round for output, normalizing -0.0 from float arithmetic."""
    v = round(float(x), _ROUND)
    return 0.0 if v == 0 else v
