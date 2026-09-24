"""Integer pump-stroke dispense planning for one saved recipe version.

The continuous solver (``solver.solve``) allocates arbitrary mL of each
concentrate.  Real dosing hardware instead dispenses each concentrate in
fixed strokes: bottle *i* delivers exactly ``q_i`` mL per pump action, so
its total dosage must be an integer multiple ``k_i * q_i`` with
``k_i in {0, 1, 2, ...}``.

This module solves the *integer* counterpart of the mixing problem:

* variables: ``k_i`` - integer number of strokes of concentrate *i*,
  ``w`` - dilution water (mL, continuous), ``t`` - worst-case relative
  deviation (continuous, >= 0).
* delivered amount of element ``e`` (mg):
  ``sum_i c_{i,e} * (k_i * q_i) / 1000 + s_e * w / 1000``
  — the raw-water baseline is multiplied by the **actual** dilution water
  ``w = V - sum_i k_i*q_i`` (the remainder after the integer-stroke
  concentrate dosage), never a pre-deducted fixed amount.

Hard constraints (identical semantics to the continuous version):

    sum_i k_i*q_i + w = V                    (体积等式)
    0 <= k_i*q_i <= cap_i                    (单液可用量/使用上限)
    w >= min_water_ml                        (最低稀释水量)
    sum_i k_i*q_i <= max_concentrate_fraction * V   (浓缩液占比)

The model is a mixed-integer linear program solved with HiGHS
(``scipy.optimize.milp``).  Stage 1 minimises ``t`` (the maximum relative
deviation across all targeted elements); stage 2 keeps ``t`` at its optimum
and minimises the total number of pump strokes as a deterministic,
operationally friendly tie-break.

When the optimum ``t`` still exceeds the version tolerance, the best
executable integer plan is returned anyway (``within_tolerance=false``).
The volume hard constraints can only be unsatisfiable when the version's
own request parameters conflict (e.g. ``min_water_ml > V``): in that case
``feasible=false`` is returned together with the concrete reason.
"""
from __future__ import annotations

from typing import Any, Optional

import numpy as np
from scipy.optimize import Bounds, LinearConstraint, milp

from .models import SolveRequest
from .solver import (
    _EPS,
    _build_element_matrix,
    _elems,
    _element_report,
    _hard_volume_checks,
    _source_vector,
)

# mL rounding for reported volumes.
_ROUND = 4
# Slack (relative + absolute) for re-fixing the optimum deviation in the
# tie-break stage so the second MILP cannot lose the stage-1 optimum to
# floating-point noise.
_T_SLACK_REL = 1e-9
_T_SLACK_ABS = 1e-12


class DispenseError(ValueError):
    """Pump configuration does not match the recipe version's concentrates."""


def build_dispense_plan(
    *,
    recipe_id: int,
    version: int,
    request_payload: dict[str, Any],
    pumps: list[tuple[str, float]],
) -> dict[str, Any]:
    """Compute the integer-stroke dispense plan for a saved recipe version.

    ``request_payload`` is the version's persisted :class:`SolveRequest`
    dict; ``pumps`` is an ordered list of ``(concentrate_name,
    pump_step_ml)`` covering every concentrate exactly once (uniqueness is
    enforced by the API model).  Raises :class:`DispenseError` when the
    pump names do not match the version's concentrates.
    """
    req = SolveRequest.model_validate(request_payload)
    concentrates = req.concentrates
    n = len(concentrates)
    names = [c.name for c in concentrates]

    steps: dict[str, float] = {}
    for name, step in pumps:
        steps[name] = float(step)

    unknown = sorted(set(steps) - set(names))
    if unknown:
        raise DispenseError(
            f"泵配置中存在该配方版本未使用的浓缩液：{ '、'.join(unknown) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing = [name for name in names if name not in steps]
    if missing:
        raise DispenseError(
            f"泵配置缺少该配方版本使用的浓缩液：{ '、'.join(missing) }；"
            "请为每一种浓缩液提供单次泵送量。"
        )

    V = float(req.target_volume_ml)
    tol = float(req.tolerance)

    caps = np.array(
        [
            min(c.available_volume, c.max_volume or c.available_volume)
            for c in concentrates
        ],
        dtype=float,
    )
    # delivered mg per mL of concentrate: c [mg/L] / 1000
    A = np.zeros((n, len(_elems(req))), dtype=float)
    elem_index = _build_element_matrix(req, A)
    source = _source_vector(req, elem_index)
    targets = {
        e: float(v) * V / 1000.0
        for e, v in req.targets.concentrations.items()
    }

    q = np.array([steps[name] for name in names], dtype=float)
    # Largest legal stroke count per bottle: k*q <= cap.
    max_k = np.array(
        [int(np.floor(caps[i] / q[i] + 1e-9)) for i in range(n)], dtype=int
    )

    zero_k = np.zeros(n, dtype=int)
    hard_violations = _hard_volume_checks(req, caps)
    if hard_violations:
        return _infeasible_plan(
            recipe_id=recipe_id,
            version=version,
            req=req,
            caps=caps,
            q=q,
            max_k=max_k,
            k=zero_k,
            A=A,
            elem_index=elem_index,
            source=source,
            targets=targets,
            violations=hard_violations,
            message=(
                "体积硬约束无可行解：版本请求参数本身冲突，"
                "任何泵送方案（含整数步长）都无法同时满足体积等式、"
                "最低稀释水量与浓缩液占比约束，详见 violations。"
            ),
        )

    # -- stage 1: minimise the maximum relative deviation ------------------ #
    res1 = _milp(req, q, max_k, A, elem_index, source, targets,
                 t_upper=np.inf, minimize_pumps=False)
    if res1 is None or res1.x is None:
        # k = 0 / w = V is always feasible once the hard sanity checks pass,
        # so this branch is purely defensive: report the hard polytope state.
        violations = _hard_volume_checks(req, caps)
        if not violations:
            violations = [
                {
                    "code": "VOLUME_INFEASIBLE",
                    "message": (
                        "整数泵送步长下体积硬约束无可行解：无法在满足"
                        "体积等式、最低稀释水量、浓缩液占比与单液上限的"
                        "同时完成投料。"
                    ),
                }
            ]
        return _infeasible_plan(
            recipe_id=recipe_id,
            version=version,
            req=req,
            caps=caps,
            q=q,
            max_k=max_k,
            k=zero_k,
            A=A,
            elem_index=elem_index,
            source=source,
            targets=targets,
            violations=violations,
            message="体积硬约束无可行解，详见 violations。",
        )

    t_star = float(res1.fun)

    # -- stage 2: keep t at its optimum, minimise total stroke count ------- #
    t_cap = t_star * (1.0 + _T_SLACK_REL) + _T_SLACK_ABS
    res2 = _milp(req, q, max_k, A, elem_index, source, targets,
                 t_upper=t_cap, minimize_pumps=True)
    res = res2 if (res2 is not None and res2.x is not None) else res1
    optimal = bool(res.status == 0)

    k = np.rint(res.x[:n]).astype(int)
    k = np.clip(k, 0, max_k)
    x = q * k.astype(float)
    # Dilution water is exactly the remaining volume after the integer
    # dosing; the raw-water baseline is evaluated against THIS volume.
    w = V - float(x.sum())

    elements, max_dev = _element_report(
        req, A, elem_index, source, targets, x, w
    )

    within_tolerance = max_dev is not None and max_dev <= tol + _EPS
    if max_dev is None:
        # No positive target element: nothing can violate the tolerance.
        within_tolerance = True

    pumps_out = []
    for i, c in enumerate(concentrates):
        note: Optional[str] = None
        if q[i] > caps[i] + _EPS:
            if c.max_volume is not None and c.max_volume < c.available_volume:
                cap_note = (
                    f"单液使用上限 {float(c.max_volume):g} mL"
                    f"（可用体积 {c.available_volume:g} mL）"
                )
            else:
                cap_note = f"可用体积上限 {c.available_volume:g} mL"
            note = (
                f"单次泵送量 {q[i]:g} mL 已超过该液{cap_note}，"
                "本方案中该液泵次数只能为 0（无法投出哪怕一次）。"
            )
        pumps_out.append(
            {
                "concentrate": c.name,
                "pump_step_ml": float(q[i]),
                "pump_count": int(k[i]),
                "volume_ml": _r(float(x[i])),
                "cap_ml": float(caps[i]),
                "available_volume_ml": float(c.available_volume),
                "max_pumps": int(max_k[i]),
                "note": note,
            }
        )

    deviations: list[dict[str, Any]] = []
    if not within_tolerance:
        for row in elements:
            if row["status"] not in ("under", "over"):
                continue
            T = float(row["target_amount_mg"])
            actual = float(row["actual_amount_mg"])
            band = (
                T * (1.0 - tol) if row["status"] == "under"
                else T * (1.0 + tol)
            )
            direction = "偏低" if row["status"] == "under" else "偏高"
            deviations.append(
                {
                    "code": "ELEMENT_DEVIATION",
                    "message": (
                        f"元素 {row['element']} 在最优整数泵送方案中{direction}："
                        f"实际 {actual:.3f} mg，目标 {T:.3f} mg"
                        f"（容差 {tol:.0%}，容差边界 {band:.3f} mg），"
                        f"相对偏差 {row['relative_deviation']:.2%}；"
                        "受整数泵送步长限制无法进一步逼近。"
                    ),
                    "element": row["element"],
                    "limit": round(band, 6),
                    "actual": round(actual, 6),
                }
            )

    if within_tolerance:
        message = (
            "泵送方案求解成功：整数泵次下各目标元素偏差均在版本容差范围内。"
        )
    else:
        message = (
            f"整数泵送步长下最优最大相对偏差为 {t_star:.2%}，"
            f"超过版本容差 {tol:.2%}；已返回偏差最小的可执行方案，"
            "详见逐元素偏差与 violations。"
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "feasible": True,
        "within_tolerance": bool(within_tolerance),
        "optimal": optimal,
        "max_relative_deviation": (
            round(max_dev, 6) if max_dev is not None else None
        ),
        "total_volume_ml": _r(V),
        "water_volume_ml": _r(w),
        "concentrate_total_ml": _r(float(x.sum())),
        "total_pump_count": int(k.sum()),
        "tolerance": tol,
        "min_water_ml": float(req.min_water_ml),
        "max_concentrate_fraction": float(req.max_concentrate_fraction),
        "pumps": pumps_out,
        "elements": elements,
        "violations": deviations,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# MILP
# --------------------------------------------------------------------------- #
def _milp(
    req: SolveRequest,
    q: np.ndarray,
    max_k: np.ndarray,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
    *,
    t_upper: float,
    minimize_pumps: bool,
):
    """Solve one stage of the integer minimax program.

    Variables ``z = (k_0..k_{n-1}, w, t)``; ``k_i`` are integral.  Element
    bands are linear because each concentrate contributes
    ``A[i, e] * q_i`` mg per stroke.
    """
    n = len(q)
    dim = n + 2
    V = float(req.target_volume_ml)

    rows: list[np.ndarray] = []
    lowers: list[float] = []
    uppers: list[float] = []

    # Volume identity: sum q_i*k_i + w = V
    eq = np.zeros(dim)
    eq[:n] = q
    eq[n] = 1.0
    rows.append(eq)
    lowers.append(V)
    uppers.append(V)

    # Concentrate share: sum q_i*k_i <= max_fraction * V
    share = np.zeros(dim)
    share[:n] = q
    rows.append(share)
    lowers.append(-np.inf)
    uppers.append(float(req.max_concentrate_fraction) * V)

    # Per-element minimax bands (elements with a positive target).
    for e, T in targets.items():
        if T <= _EPS:
            continue
        # mg of element e delivered by one stroke of each bottle
        stroke = A[:, elem_index[e]] * q
        s = source[elem_index[e]]
        # delivered <= T*(1+t)  ->  stroke·k + s*w - T*t <= T
        up = np.zeros(dim)
        up[:n] = stroke
        up[n] = s
        up[n + 1] = -T
        rows.append(up)
        lowers.append(-np.inf)
        uppers.append(T)
        # delivered >= T*(1-t)  ->  -stroke·k - s*w - T*t <= -T
        lo = np.zeros(dim)
        lo[:n] = -stroke
        lo[n] = -s
        lo[n + 1] = -T
        rows.append(lo)
        lowers.append(-np.inf)
        uppers.append(-T)

    constraints = LinearConstraint(
        np.vstack(rows), np.array(lowers), np.array(uppers)
    )
    lower = [0.0] * n + [float(req.min_water_ml), 0.0]
    upper = [float(max_k[i]) for i in range(n)] + [V, float(t_upper)]
    bounds = Bounds(np.array(lower), np.array(upper))

    c_obj = np.zeros(dim)
    if minimize_pumps:
        # Deterministic operational tie-break: fewer pump strokes.
        c_obj[:n] = 1.0
    else:
        c_obj[n + 1] = 1.0

    integrality = np.zeros(dim)
    integrality[:n] = 1.0  # stroke counts integral; w and t continuous

    # milp always solves with the bundled HiGHS MIP/LP solver.
    return milp(
        c=c_obj,
        integrality=integrality,
        bounds=bounds,
        constraints=constraints,
    )


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _r(x: float) -> float:
    """Round for output, normalizing -0.0 from float arithmetic."""
    v = round(float(x), _ROUND)
    return 0.0 if v == 0 else v


def _infeasible_plan(
    *,
    recipe_id: int,
    version: int,
    req: SolveRequest,
    caps: np.ndarray,
    q: np.ndarray,
    max_k: np.ndarray,
    k: np.ndarray,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
    violations: list[dict],
    message: str,
) -> dict[str, Any]:
    """Build a feasible=false result (volume hard constraints unsatisfiable)."""
    n = len(q)
    x = q * k.astype(float)
    w = float(req.target_volume_ml) - float(x.sum())
    elements, max_dev = _element_report(
        req, A, elem_index, source, targets, x, w
    )
    pumps_out = [
        {
            "concentrate": req.concentrates[i].name,
            "pump_step_ml": float(q[i]),
            "pump_count": int(k[i]),
            "volume_ml": _r(float(x[i])),
            "cap_ml": float(caps[i]),
            "available_volume_ml": float(req.concentrates[i].available_volume),
            "max_pumps": int(max_k[i]),
            "note": None,
        }
        for i in range(n)
    ]
    return {
        "recipe_id": recipe_id,
        "version": version,
        "feasible": False,
        "within_tolerance": False,
        "optimal": False,
        "max_relative_deviation": (
            round(max_dev, 6) if max_dev is not None else None
        ),
        "total_volume_ml": _r(float(req.target_volume_ml)),
        "water_volume_ml": _r(w),
        "concentrate_total_ml": _r(float(x.sum())),
        "total_pump_count": int(k.sum()),
        "tolerance": float(req.tolerance),
        "min_water_ml": float(req.min_water_ml),
        "max_concentrate_fraction": float(req.max_concentrate_fraction),
        "pumps": pumps_out,
        "elements": elements,
        "violations": violations,
        "message": message,
    }
