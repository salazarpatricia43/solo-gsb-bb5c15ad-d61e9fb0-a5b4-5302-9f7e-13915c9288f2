"""在缸复测纠偏：按实测浓度区间求最优补液量（minimax LP）。

缸内工作液放置/使用后浓度会漂移，复测只能给出各目标元素浓度的**区间**
（上下界，mg/L），实际值不确定。纠偏只允许向缸中追加**版本内浓缩液**
（受可追加库存约束）与**清水**，把液量补齐到目标终体积。清水只占体积、
不携带元素；缸内既有本底完全由实测区间刻画，不再计入版本的原水基线。

物料守恒（体积 mL、元素量 mg）：

    V0 + Σ x_i + w = Vf                 （现有液量 + 补液 + 清水 = 终体积）
    0 <= x_i <= stock_i                 （各液可追加库存）
    w >= 0   ⇔   Σ x_i <= Vf - V0       （清水非负）

元素 e 在缸中的既有量 m_e ∈ [lo_e·V0/1000, hi_e·V0/1000]；补液后终体积下
的实际量为 m_e + Σ c_{i,e}·x_i/1000，目标量 T_e = target_e·Vf/1000。相对
偏差 |m_e + a_e·x − T_e| / T_e 关于 m_e 是线性的，最大值必在区间端点处
取得，因此每个元素只需两条约束即可覆盖全部不确定性：

    a_e·x − T_e·t <= T_e − hi_e·V0/1000     （上偏差最坏在实测上界端点）
    −a_e·x − T_e·t <= lo_e·V0/1000 − T_e    （下偏差最坏在实测下界端点）

目标：最小化 t —— 所有实测区间端点在终体积下的最大相对目标偏差。该 LP
恒可行且有界（x = 0 时 t 取当前偏差即可），因此接口总是返回最优补液量；
超出版本容差时照常返回（within_tolerance=false），逐元素给出最坏端点，
超限项列入 violations。接口只读取配方版本，不改写它。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy.optimize import linprog

from .models import SolveRequest
from .solver import _EPS

# mL rounding for reported volumes.
_ROUND = 4
# Absolute tolerance (on the deviation fraction) for declaring the two
# measured endpoints equally bad; ties are deterministically reported as the
# lower endpoint.
_TIE_EPS = 1e-9


class RetestError(ValueError):
    """在缸复测请求与配方版本内容不一致（库存/实测名称、零目标等）。"""


def build_retest_correction(
    *,
    recipe_id: int,
    version: int,
    request_payload: dict[str, Any],
    current_volume_ml: float,
    final_volume_ml: float,
    measurements: list[tuple[str, float, float]],
    stock: dict[str, float],
) -> dict[str, Any]:
    """Compute the optimal in-tank top-up for one saved recipe version.

    ``request_payload`` is the version's persisted :class:`SolveRequest`
    dict; ``measurements`` is a list of ``(element, lower_mg_l,
    upper_mg_l)`` covering every target element exactly once; ``stock``
    maps each version concentrate name to its addable stock (mL).  Raises
    :class:`RetestError` on any mismatch with the version (zero target
    concentrations, unknown/missing concentrate or element names).
    """
    req = SolveRequest.model_validate(request_payload)
    targets = {e: float(v) for e, v in req.targets.concentrations.items()}
    zero = sorted(e for e, v in targets.items() if v <= 0.0)
    if zero:
        raise RetestError(
            "目标浓度为零的版本暂不接受在缸复测纠偏：该版本目标中元素 "
            f"{ '、'.join(zero) } 的浓度为 0，相对目标偏差无定义；"
            "请改用目标浓度均为正的配方版本。"
        )

    concentrates = req.concentrates
    n = len(concentrates)
    names = [c.name for c in concentrates]

    unknown = sorted(set(stock) - set(names))
    if unknown:
        raise RetestError(
            f"可追加库存中存在该配方版本未使用的浓缩液：{ '、'.join(unknown) }；"
            f"该版本浓缩液为：{ '、'.join(names) }。"
        )
    missing = [name for name in names if name not in stock]
    if missing:
        raise RetestError(
            f"可追加库存缺少该配方版本使用的浓缩液：{ '、'.join(missing) }；"
            "请为每一种浓缩液提供可追加库存 (mL)。"
        )

    measured = {e: (float(lo), float(hi)) for e, lo, hi in measurements}
    extra_e = sorted(set(measured) - set(targets))
    if extra_e:
        raise RetestError(
            f"实测区间包含该版本目标之外的元素：{ '、'.join(extra_e) }；"
            f"该版本目标元素为：{ '、'.join(targets) }。"
        )
    missing_e = sorted(set(targets) - set(measured))
    if missing_e:
        raise RetestError(
            f"缺少目标元素 { '、'.join(missing_e) } 的实测浓度区间；"
            "请为每一个目标元素提供实测浓度上下界 (mg/L)。"
        )

    V0 = float(current_volume_ml)
    Vf = float(final_volume_ml)
    tol = float(req.tolerance)

    caps = np.array([float(stock[name]) for name in names], dtype=float)
    bad_caps = [names[i] for i in range(n) if not math.isfinite(caps[i])]
    if bad_caps:
        raise RetestError(
            f"浓缩液 { '、'.join(bad_caps) } 的可追加库存必须为有限非负数。"
        )

    elems = list(targets)
    eidx = {e: j for j, e in enumerate(elems)}
    # mg of element e delivered per mL of concentrate i: c [mg/L] / 1000
    A = np.zeros((n, len(elems)), dtype=float)
    for i, c in enumerate(concentrates):
        for e, conc in c.concentrations.items():
            j = eidx.get(e)
            if j is not None:
                A[i, j] = conc / 1000.0

    lo_amt = np.array([measured[e][0] * V0 / 1000.0 for e in elems])
    hi_amt = np.array([measured[e][1] * V0 / 1000.0 for e in elems])
    target_mg = np.array([targets[e] * Vf / 1000.0 for e in elems])

    res = _minimax_lp(A, caps, Vf - V0, lo_amt, hi_amt, target_mg)
    if res is not None and res.success and res.x is not None:
        x = np.clip(np.asarray(res.x[:n], dtype=float), 0.0, caps)
        optimal = True
    else:  # pragma: no cover - LP 按构造恒可行有界，此分支仅为防御
        x = np.zeros(n, dtype=float)
        optimal = False

    water = Vf - V0 - float(x.sum())
    if -1e-6 < water < 0.0:
        water = 0.0

    elements, violations, max_dev = _element_report(
        A, x, Vf, tol, elems, measured, lo_amt, hi_amt, target_mg
    )
    within_tolerance = max_dev <= tol + _EPS

    additions = [
        {
            "concentrate": names[i],
            "volume_ml": _r(float(x[i])),
            "stock_ml": float(caps[i]),
        }
        for i in range(n)
    ]

    if within_tolerance:
        message = (
            "复测纠偏求解成功：补液后各目标元素在最坏实测端点下的"
            "相对偏差均在版本容差范围内。"
        )
    else:
        message = (
            f"最优补液方案的最大相对偏差为 {max_dev:.2%}，"
            f"超过版本容差 {tol:.2%}；已返回偏差最小的补液量，"
            "各元素最坏端点与超限项详见 elements 与 violations。"
        )

    return {
        "recipe_id": recipe_id,
        "version": version,
        "within_tolerance": bool(within_tolerance),
        "optimal": optimal,
        "max_relative_deviation": round(max_dev, 6),
        "current_volume_ml": _r(V0),
        "final_volume_ml": _r(Vf),
        "water_to_add_ml": _r(water),
        "concentrate_total_ml": _r(float(x.sum())),
        "tolerance": tol,
        "additions": additions,
        "elements": elements,
        "violations": violations,
        "message": message,
    }


# --------------------------------------------------------------------------- #
# LP
# --------------------------------------------------------------------------- #
def _minimax_lp(
    A: np.ndarray,
    caps: np.ndarray,
    headroom: float,
    lo_amt: np.ndarray,
    hi_amt: np.ndarray,
    target_mg: np.ndarray,
):
    """Minimise t over top-up volumes x (mL) for both measured endpoints.

    ``headroom`` is ``Vf - V0`` (mL): the most concentrate that can be added
    while keeping the pure-water top-up non-negative.  Variables are
    ``(x_0..x_{n-1}, t)``; all coefficients are in mg / mL / mg·L⁻¹ units as
    documented on the module docstring.
    """
    n, m = A.shape
    dim = n + 1  # x..., t

    rows: list[np.ndarray] = []
    rhs: list[float] = []

    # Σ x_i <= Vf - V0   (清水补液量非负)
    row = np.zeros(dim)
    row[:n] = 1.0
    rows.append(row)
    rhs.append(float(headroom))

    for j in range(m):
        a = A[:, j]
        T = float(target_mg[j])
        # m_e + a·x <= T(1+t)，最坏在实测上界端点 m_e = hi_amt
        up = np.zeros(dim)
        up[:n] = a
        up[n] = -T
        rows.append(up)
        rhs.append(T - float(hi_amt[j]))
        # m_e + a·x >= T(1-t)，最坏在实测下界端点 m_e = lo_amt
        lo = np.zeros(dim)
        lo[:n] = -a
        lo[n] = -T
        rows.append(lo)
        rhs.append(float(lo_amt[j]) - T)

    c_obj = np.zeros(dim)
    c_obj[n] = 1.0
    bounds = [(0.0, float(caps[i])) for i in range(n)]
    bounds.append((0.0, None))  # t >= 0
    return linprog(
        c=c_obj,
        A_ub=np.vstack(rows),
        b_ub=np.array(rhs, dtype=float),
        bounds=bounds,
        method="highs",
    )


# --------------------------------------------------------------------------- #
# Result assembly
# --------------------------------------------------------------------------- #
def _element_report(
    A: np.ndarray,
    x: np.ndarray,
    Vf: float,
    tol: float,
    elems: list[str],
    measured: dict[str, tuple[float, float]],
    lo_amt: np.ndarray,
    hi_amt: np.ndarray,
    target_mg: np.ndarray,
) -> tuple[list[dict], list[dict], float]:
    """Per-element worst-endpoint deviations at the optimal top-up ``x``."""
    elements: list[dict] = []
    violations: list[dict] = []
    max_dev = 0.0

    for j, e in enumerate(elems):
        T = float(target_mg[j])
        added = float(A[:, j] @ x)
        final_lo = float(lo_amt[j]) + added
        final_hi = float(hi_amt[j]) + added
        conc_lo = final_lo * 1000.0 / Vf
        conc_hi = final_hi * 1000.0 / Vf
        dev_lo = (final_lo - T) / T
        dev_hi = (final_hi - T) / T
        # Worst endpoint; a (near-)tie is deterministically the lower one.
        if abs(dev_lo) >= abs(dev_hi) - _TIE_EPS * max(1.0, abs(dev_hi)):
            worst, dev, worst_conc, worst_amt = (
                "lower", dev_lo, conc_lo, final_lo
            )
        else:
            worst, dev, worst_conc, worst_amt = (
                "upper", dev_hi, conc_hi, final_hi
            )
        max_dev = max(max_dev, abs(dev))
        if abs(dev) <= tol + _EPS:
            status = "ok"
        else:
            status = "under" if dev < 0 else "over"

        lo_c, hi_c = measured[e]
        elements.append(
            {
                "element": e,
                "target_concentration": round(T * 1000.0 / Vf, 6),
                "measured_lower_mg_l": lo_c,
                "measured_upper_mg_l": hi_c,
                "target_amount_mg": round(T, 6),
                "final_concentration_at_lower": round(conc_lo, 6),
                "final_concentration_at_upper": round(conc_hi, 6),
                "worst_endpoint": worst,
                "worst_concentration": round(worst_conc, 6),
                "relative_deviation": round(dev, 6),
                "status": status,
            }
        )

        if status == "ok":
            continue
        band = T * (1.0 + tol) if dev > 0 else T * (1.0 - tol)
        endpoint_cn = "下界" if worst == "lower" else "上界"
        direction = "偏低" if dev < 0 else "偏高"
        violations.append(
            {
                "code": "ELEMENT_DEVIATION",
                "element": e,
                "limit": round(band, 6),
                "actual": round(worst_amt, 6),
                "message": (
                    f"元素 {e} 在最优补液方案下仍{direction}：实测{endpoint_cn}"
                    f"端点在终体积 {Vf:g} mL 下为 {worst_amt:.3f} mg"
                    f"（{worst_conc:.3f} mg/L），目标 {T:.3f} mg"
                    f"（容差 {tol:.0%}，容差边界 {band:.3f} mg），"
                    f"相对偏差 {dev:.2%}；受可追加库存与终体积限制，"
                    "无法进一步逼近。"
                ),
            }
        )
    return elements, violations, max_dev


def _r(x: float) -> float:
    """Round for output, normalizing -0.0 from float arithmetic."""
    v = round(float(x), _ROUND)
    return 0.0 if v == 0 else v
