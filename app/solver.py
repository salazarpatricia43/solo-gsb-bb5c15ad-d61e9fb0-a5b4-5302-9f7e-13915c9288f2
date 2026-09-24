"""Nutrient mixing solver.

The problem is modelled as a linear program (mL for volumes, mg for amounts):

* variables: ``x_i`` - used volume (mL) of each concentrate,
  ``w`` - dilution water (mL), ``t`` - worst-case relative deviation.
* delivered amount of element ``e`` (mg):
  ``sum_i c_{i,e} * x_i / 1000 + s_e * w / 1000``
  where ``s_e`` is the raw-water baseline concentration (mg/L); the source
  water contributes in proportion to the *solved* water volume ``w`` rather
  than a fixed amount pre-deducted from the target volume.
* target amount: ``target_concentration_e * V / 1000`` (mg)

Hard constraints
    sum x_i + w = V
    0 <= x_i <= min(available_volume_i, max_volume_i)   (单液上限)
    w >= min_water_ml                                    (稀释水约束)
    sum x_i <= max_concentrate_fraction * V             (总量约束)

Soft constraints (one pair per element with a non-zero target)
    delivered_e <= target_e * (1 + t)
    delivered_e >= target_e * (1 - t)

Objective: minimize ``t`` — the maximum relative deviation across all
targeted elements (minimax).  When the optimum ``t`` exceeds the requested
tolerance the mix is reported as infeasible-within-tolerance, and auxiliary
LPs pinpoint *which* element violates and *why* (bottle cap shortage vs.
unavoidable surplus forced by companion elements).
"""
from __future__ import annotations

from typing import Optional

import numpy as np
from scipy.optimize import linprog

from .models import Concentrate, SolveRequest, SolveResult

_EPS = 1e-7
# cap (mL) below which a variable is considered bound in a diagnostic LP
_BIND_EPS = 1e-6


def _violation(code: str, message: str, **kw) -> dict:
    v = {"code": code, "message": message}
    v.update({k: v_ for k, v_ in kw.items() if v_ is not None})
    return v


def solve(req: SolveRequest) -> SolveResult:
    """Run the full solve + diagnosis pipeline for one trial request."""
    concentrates = req.concentrates
    n = len(concentrates)
    V = req.target_volume_ml

    caps = np.array(
        [
            min(c.available_volume, c.max_volume or c.available_volume)
            for c in concentrates
        ],
        dtype=float,
    )
    # delivered mg per element per mL of concentrate: c [mg/L] / 1000
    A = np.zeros((n, len(_elems(req))), dtype=float)
    elem_index = _build_element_matrix(req, A)

    # raw-water baseline mg per mL of dilution water: s [mg/L] / 1000
    source = _source_vector(req, elem_index)

    targets = {
        e: float(v) * V / 1000.0
        for e, v in req.targets.concentrations.items()
    }

    # -- 1. hard constraint sanity checks ---------------------------------- #
    hard = _hard_volume_checks(req, caps)
    if hard:
        return _infeasible_result(
            req, caps, A, elem_index, source, targets, hard,
            "硬约束冲突，不存在满足体积/稀释水约束的配比。",
        )

    # -- 2. minimax LP ----------------------------------------------------- #
    res = _minimax_lp(req, caps, A, elem_index, source, targets)
    if res is None:
        return _infeasible_result(
            req, caps, A, elem_index, source, targets,
            [_violation("SOLVER_ERROR", "求解器未能完成计算。")],
            "求解失败，请检查输入参数。",
        )

    x = res.x[:n]
    w = res.x[n]
    t_star = float(res.x[n + 1])
    tol = req.tolerance

    elements, max_dev = _element_report(
        req, A, elem_index, source, targets, x, w
    )
    allocations = [
        {
            "concentrate": concentrates[i].name,
            "volume_ml": float(round(x[i], 4)),
            "cap_ml": float(caps[i]),
        }
        for i in range(n)
    ]

    if t_star <= tol + _EPS:
        return SolveResult(
            feasible=True,
            optimal=bool(res.success),
            max_relative_deviation=round(max_dev, 6) if max_dev is not None else 0.0,
            total_volume_ml=float(round(V, 4)),
            water_volume_ml=float(round(w, 4)),
            concentrate_total_ml=float(round(float(x.sum()), 4)),
            allocations=allocations,
            elements=elements,
            violations=[],
            message="求解成功：所有目标元素偏差均在容差范围内。",
        )

    # -- 3. diagnose why the tolerance band cannot be met ------------------ #
    violations = _diagnose(
        req, caps, A, elem_index, source, targets, x, w, t_star
    )
    return _infeasible_result(
        req, caps, A, elem_index, source, targets, violations,
        (
            f"无可行解：最优最大相对偏差为 {t_star:.2%}，"
            f"超过容差 {tol:.2%}，详见 violations。"
        ),
        x=x,
        w=w,
        elements=elements,
        max_dev=max_dev,
        allocations=allocations,
    )


# --------------------------------------------------------------------------- #
# LP builders
# --------------------------------------------------------------------------- #
def _elems(req: SolveRequest) -> list[str]:
    seen: list[str] = []
    for c in req.concentrates:
        for e in c.concentrations:
            if e not in seen:
                seen.append(e)
    for e in req.targets.concentrations:
        if e not in seen:
            seen.append(e)
    if req.source_water is not None:
        for e in req.source_water.concentrations:
            if e not in seen:
                seen.append(e)
    return seen


def _build_element_matrix(req: SolveRequest, A: np.ndarray) -> dict[str, int]:
    """Fill ``A`` (concentrate × element, mg per mL) and return column map."""
    all_elems = _elems(req)
    index = {e: j for j, e in enumerate(all_elems)}
    A.fill(0.0)
    for i, c in enumerate(req.concentrates):
        for e, conc in c.concentrations.items():
            A[i, index[e]] = conc / 1000.0
    return index


def _source_vector(
    req: SolveRequest, elem_index: dict[str, int]
) -> np.ndarray:
    """Source-water baseline per element (mg per mL of dilution water)."""
    s = np.zeros(len(elem_index), dtype=float)
    if req.source_water is not None:
        for e, conc in req.source_water.concentrations.items():
            s[elem_index[e]] = conc / 1000.0
    return s


def _hard_polytope(
    req: SolveRequest, caps: np.ndarray, with_t: bool
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[tuple]]:
    """Return A_ub, b_ub, A_eq, b_eq, bounds for x (,w [,t])."""
    n = len(caps)
    V = req.target_volume_ml
    dim = n + 2 if with_t else n + 1  # x_i..., w[, t]

    # sum x_i <= fraction * V
    row = np.zeros(dim)
    row[:n] = 1.0
    A_ub = [row]
    b_ub = [req.max_concentrate_fraction * V]

    eq = np.zeros(dim)
    eq[: n + 1] = 1.0  # sum x + w = V
    A_eq = [eq]
    b_eq = [V]

    bounds = [(0.0, float(caps[i])) for i in range(n)]
    bounds.append((float(req.min_water_ml), float(V)))  # water
    if with_t:
        bounds.append((0.0, None))
    return np.array(A_ub), np.array(b_ub), np.array(A_eq), np.array(b_eq), bounds


def _minimax_lp(
    req: SolveRequest,
    caps: np.ndarray,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
):
    n = len(caps)
    dim = n + 2
    A_ub, b_ub, A_eq, b_eq, bounds = _hard_polytope(req, caps, with_t=True)

    rows: list[np.ndarray] = [A_ub]
    rhs: list[float] = list(b_ub)
    for e, T in targets.items():
        if T <= _EPS:
            continue
        a = A[:, elem_index[e]]
        s = source[elem_index[e]]
        # delivered = a·x + s*w <= T(1+t)  ->  a·x + s·w - T t <= T
        row_up = np.zeros(dim)
        row_up[:n] = a
        row_up[n] = s
        row_up[n + 1] = -T
        rows.append(row_up)
        rhs.append(T)
        # delivered >= T(1-t)  ->  -a·x - s·w - T t <= -T
        row_lo = np.zeros(dim)
        row_lo[:n] = -a
        row_lo[n] = -s
        row_lo[n + 1] = -T
        rows.append(row_lo)
        rhs.append(-T)

    c_obj = np.zeros(dim)
    c_obj[n + 1] = 1.0
    result = linprog(
        c=c_obj,
        A_ub=np.vstack(rows),
        b_ub=np.array(rhs),
        A_eq=A_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    return result if result.success else None


def _extreme_element(
    req: SolveRequest,
    caps: np.ndarray,
    elem_col: np.ndarray,
    source_coef: float,
    maximize: bool,
    extra_rows: Optional[list[tuple[np.ndarray, float]]] = None,
):
    """Min/max deliverable amount of one element inside the hard polytope.

    The delivered amount is ``elem_col·x + source_coef * w`` (concentrate
    contribution plus raw-water baseline over the solved water volume).

    ``extra_rows`` adds (coefficient row over z=(x..., w), rhs) inequalities
    ``row·z <= b``.  Returns (value, z) or None if infeasible.
    """
    n = len(caps)
    dim = n + 1
    A_ub, b_ub, A_eq, b_eq, bounds = _hard_polytope(req, caps, with_t=False)
    rows = [A_ub]
    rhs = list(b_ub)
    for row, b in extra_rows or []:
        rows.append(row)
        rhs.append(b)

    # objective on the delivered amount: [elem_col, source_coef] · z
    c_obj = np.zeros(dim)
    c_obj[:n] = -elem_col if maximize else elem_col
    c_obj[n] = -source_coef if maximize else source_coef
    res = linprog(
        c=c_obj,
        A_ub=np.vstack(rows),
        b_ub=np.array(rhs),
        A_eq=A_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if not res.success:
        return None
    value = float(elem_col @ res.x[:n] + source_coef * res.x[n])
    return value, res.x


# --------------------------------------------------------------------------- #
# Diagnosis
# --------------------------------------------------------------------------- #
def _hard_volume_checks(req: SolveRequest, caps: np.ndarray) -> list[dict]:
    V = req.target_volume_ml
    violations: list[dict] = []

    if req.min_water_ml > V + _EPS:
        violations.append(
            _violation(
                "WATER_MIN_EXCEEDS_VOLUME",
                f"稀释水下限 {req.min_water_ml:g} mL 超过目标总体积 {V:g} mL。",
                element=None,
                limit=V,
                actual=req.min_water_ml,
            )
        )

    # Both ``w >= min_water_ml`` and ``w >= (1 - fraction) * V`` are LOWER
    # bounds, so they cannot conflict with each other: dilution water just
    # has to clear the larger floor.  The fraction cap can only conflict
    # with the water floor when the floor exceeds the total volume itself
    # (then no mixture can hold that much water while staying within the
    # concentrate share) — reported together with WATER_MIN_EXCEEDS_VOLUME
    # so callers see every violated user constraint.
    if req.min_water_ml > V + _EPS:
        violations.append(
            _violation(
                "CONCENTRATE_FRACTION_CONFLICT",
                (
                    f"稀释水下限 {req.min_water_ml:g} mL 已超过目标总体积 "
                    f"{V:g} mL，无法在满足浓缩液最大占比 "
                    f"{req.max_concentrate_fraction:.0%} 的同时补足稀释水。"
                ),
                limit=V,
                actual=req.min_water_ml,
            )
        )

    # Per-bottle cap larger than total volume is simply clamped by the
    # volume equality; nothing to flag.
    return violations


def _diagnose(
    req: SolveRequest,
    caps: np.ndarray,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
    x_star: np.ndarray,
    w_star: float,
    t_star: float,
) -> list[dict]:
    tol = req.tolerance
    concentrates = req.concentrates
    n = len(concentrates)
    violations: list[dict] = []
    explained: set[str] = set()

    def _delivered(a: np.ndarray, s: float, z: np.ndarray) -> float:
        return float(a @ z[:n] + s * z[n])

    for e, T in targets.items():
        if T <= _EPS:
            continue
        a = A[:, elem_index[e]]
        s = source[elem_index[e]]
        z_star = np.concatenate([x_star, [w_star]])
        delivered = _delivered(a, s, z_star)
        lower_band = T * (1.0 - tol)
        upper_band = T * (1.0 + tol)

        if lower_band - _EPS <= delivered <= upper_band + _EPS:
            continue  # this element is fine in the best compromise

        if delivered < lower_band - _EPS:
            # --- shortage: even using every bottle to its cap ----------- #
            extreme = _extreme_element(req, caps, a, s, maximize=True)
            if extreme is None:
                continue
            max_amount, z_max = extreme
            if max_amount < lower_band - _EPS:
                binding = [
                    concentrates[i].name
                    for i in range(n)
                    if z_max[i] >= caps[i] - _BIND_EPS and a[i] > _EPS
                ]
                reason = "受浓缩液可用体积/单液使用量上限约束"
                if binding:
                    reason += f"，已到上限的浓缩液：{ '、'.join(binding) }"
                violations.append(
                    _violation(
                        "ELEMENT_SHORTAGE",
                        (
                            f"元素 {e} 最多只能配出 {max_amount:.3f} mg，"
                            f"低于目标下限 {lower_band:.3f} mg"
                            f"（目标 {T:.3f} mg，容差 {tol:.0%}）；{reason}。"
                        ),
                        element=e,
                        limit=round(lower_band, 6),
                        actual=round(max_amount, 6),
                    )
                )
                explained.add(e)

        elif delivered > upper_band + _EPS:
            # --- surplus: force every OTHER target element to at least
            # its lower band, then minimize this element ----------------- #
            extra: list[tuple[np.ndarray, float]] = []
            dim = n + 1
            for other, To in targets.items():
                if other == e or To <= _EPS:
                    continue
                ao = A[:, elem_index[other]]
                so = source[elem_index[other]]
                # delivered_other >= To(1-tol)
                #   ->  -ao·x - so·w <= -lower
                row = np.zeros(dim)
                row[:n] = -ao
                row[n] = -so
                extra.append((row, -To * (1.0 - tol)))
            extreme = _extreme_element(
                req, caps, a, s, maximize=False, extra_rows=extra
            )
            if extreme is not None:
                min_amount, _ = extreme
                if min_amount > upper_band + _EPS:
                    violations.append(
                        _violation(
                            "ELEMENT_SURPLUS",
                            (
                                f"满足其余元素目标下限时，元素 {e} 最少也会达到 "
                                f"{min_amount:.3f} mg，高于目标上限 "
                                f"{upper_band:.3f} mg（目标 {T:.3f} mg，"
                                f"容差 {tol:.0%}）；浓缩液成分配比耦合，"
                                f"无法在满足其他元素的同时压低 {e}。"
                            ),
                            element=e,
                            limit=round(upper_band, 6),
                            actual=round(min_amount, 6),
                        )
                    )
                    explained.add(e)

    # Remaining out-of-band elements are trade-off victims (e.g. two elements
    # pull incompatible mixes), not individually hard-blocked.
    for e, T in targets.items():
        if T <= _EPS or e in explained:
            continue
        a = A[:, elem_index[e]]
        s = source[elem_index[e]]
        delivered = float(a @ x_star + s * w_star)
        lower_band = T * (1.0 - tol)
        upper_band = T * (1.0 + tol)
        if delivered < lower_band - _EPS or delivered > upper_band + _EPS:
            direction = "偏低" if delivered < lower_band else "偏高"
            violations.append(
                _violation(
                    "ELEMENT_DEVIATION",
                    (
                        f"元素 {e} 在最优折中方案中{direction}："
                        f"{delivered:.3f} mg，目标区间 "
                        f"[{lower_band:.3f}, {upper_band:.3f}] mg；"
                        f"与其他超限元素相互制约，放宽容差或调整浓缩液后可解。"
                    ),
                    element=e,
                    actual=round(delivered, 6),
                )
            )

    if not violations:
        violations.append(
            _violation(
                "TOLERANCE_UNREACHABLE",
                f"最优最大相对偏差 {t_star:.2%} 仍超过容差 {tol:.2%}。",
                actual=round(t_star, 6),
                limit=tol,
            )
        )
    return violations


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _element_report(
    req: SolveRequest,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
    x: np.ndarray,
    w: float,
) -> tuple[list[dict], Optional[float]]:
    V = req.target_volume_ml
    tol = req.tolerance
    rows: list[dict] = []
    max_dev: Optional[float] = None

    for e, T_mg in targets.items():
        j = elem_index.get(e)
        concentrate_mg = (
            float(A[:, j] @ x) if j is not None else 0.0
        )
        source_mg = float(source[j] * w) if j is not None else 0.0
        actual_mg = concentrate_mg + source_mg
        target_conc = T_mg * 1000.0 / V
        actual_conc = actual_mg * 1000.0 / V
        if T_mg > _EPS:
            dev = (actual_mg - T_mg) / T_mg
            max_dev = abs(dev) if max_dev is None else max(max_dev, abs(dev))
            if abs(dev) <= tol + _EPS:
                status = "ok"
            else:
                status = "under" if dev < 0 else "over"
            rows.append(
                {
                    "element": e,
                    "target_concentration": round(target_conc, 6),
                    "actual_concentration": round(actual_conc, 6),
                    "target_amount_mg": round(T_mg, 6),
                    "actual_amount_mg": round(actual_mg, 6),
                    "concentrate_amount_mg": round(concentrate_mg, 6),
                    "source_water_amount_mg": round(source_mg, 6),
                    "relative_deviation": round(dev, 6),
                    "status": status,
                }
            )
        else:
            rows.append(
                {
                    "element": e,
                    "target_concentration": 0.0,
                    "actual_concentration": round(actual_conc, 6),
                    "target_amount_mg": 0.0,
                    "actual_amount_mg": round(actual_mg, 6),
                    "concentrate_amount_mg": round(concentrate_mg, 6),
                    "source_water_amount_mg": round(source_mg, 6),
                    "relative_deviation": None,
                    "status": "unconstrained",
                }
            )
    return rows, max_dev


def _infeasible_result(
    req: SolveRequest,
    caps: np.ndarray,
    A: np.ndarray,
    elem_index: dict[str, int],
    source: np.ndarray,
    targets: dict[str, float],
    violations: list[dict],
    message: str,
    x: Optional[np.ndarray] = None,
    w: Optional[float] = None,
    elements: Optional[list[dict]] = None,
    max_dev: Optional[float] = None,
    allocations: Optional[list[dict]] = None,
) -> SolveResult:
    if x is None:
        x = np.zeros(len(caps))
        w = req.target_volume_ml
    if elements is None:
        elements, max_dev = _element_report(
            req, A, elem_index, source, targets, x, float(w)
        )
    if allocations is None:
        allocations = [
            {
                "concentrate": req.concentrates[i].name,
                "volume_ml": 0.0,
                "cap_ml": float(caps[i]),
            }
            for i in range(len(caps))
        ]
    return SolveResult(
        feasible=False,
        optimal=False,
        max_relative_deviation=round(max_dev, 6) if max_dev is not None else None,
        total_volume_ml=float(round(req.target_volume_ml, 4)),
        water_volume_ml=float(round(float(w), 4)) if w is not None else 0.0,
        concentrate_total_ml=float(round(float(x.sum()), 4)),
        allocations=allocations,
        elements=elements,
        violations=violations,
        message=message,
    )
