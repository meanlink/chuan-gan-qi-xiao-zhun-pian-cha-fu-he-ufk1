"""统计原语：Student t 分布分位数与均值置信区间（纯标准库）。

t 分布 CDF 通过正则化不完全贝塔函数计算（连分式展开，Numerical Recipes），
PPF 用二分反解，精度约 1e-12。无第三方依赖，便于在受限环境运行与审计。
"""
from __future__ import annotations

import math
from statistics import fmean, stdev

__all__ = ["regularized_beta", "t_cdf", "t_ppf", "mean_confidence_interval"]

_BETACF_MAX_ITER = 200
_BETACF_EPS = 3e-14
_FPMIN = 1e-300


def _betacf(a: float, b: float, x: float) -> float:
    """不完全贝塔函数的连分式求值。"""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < _FPMIN:
        d = _FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, _BETACF_MAX_ITER + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < _FPMIN:
            d = _FPMIN
        c = 1.0 + aa / c
        if abs(c) < _FPMIN:
            c = _FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < _BETACF_EPS:
            break
    return h


def regularized_beta(x: float, a: float, b: float) -> float:
    """正则化不完全贝塔函数 I_x(a, b)。"""
    if not (0.0 <= x <= 1.0):
        raise ValueError("x 必须在 [0, 1] 内")
    if x == 0.0:
        return 0.0
    if x == 1.0:
        return 1.0
    bt = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return bt * _betacf(a, b, x) / a
    return 1.0 - bt * _betacf(b, a, 1.0 - x) / b


def t_cdf(t: float, df: int) -> float:
    """Student t 分布累积分布函数，df 为自由度。"""
    if df < 1:
        raise ValueError("自由度必须 >= 1")
    x = df / (df + t * t)
    ib = regularized_beta(x, df / 2.0, 0.5)
    if t >= 0:
        return 1.0 - 0.5 * ib
    return 0.5 * ib


def t_ppf(p: float, df: int) -> float:
    """Student t 分布分位数（CDF 反函数，二分反解）。"""
    if df < 1:
        raise ValueError("自由度必须 >= 1")
    if not (0.0 < p < 1.0):
        raise ValueError("p 必须在 (0, 1) 内")
    lo, hi = -1.0, 1.0
    for _ in range(300):
        if t_cdf(lo, df) <= p:
            break
        lo *= 2.0
    for _ in range(300):
        if t_cdf(hi, df) >= p:
            break
        hi *= 2.0
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if t_cdf(mid, df) < p:
            lo = mid
        else:
            hi = mid
        if hi - lo <= 1e-14 * max(1.0, abs(lo), abs(hi)):
            break
    return 0.5 * (lo + hi)


def mean_confidence_interval(values, alpha: float) -> dict:
    """均值的双侧 (1 - alpha) t 置信区间。

    返回 {"n", "mean", "sd", "lo", "hi"}；要求 n >= 2、0 < alpha < 1。
    """
    vals = [float(v) for v in values]
    n = len(vals)
    if n < 2:
        raise ValueError("至少需要 2 个样本")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha 必须在 (0, 1) 内")
    mean = fmean(vals)
    sd = stdev(vals)
    crit = t_ppf(1.0 - alpha / 2.0, n - 1)
    half = crit * sd / math.sqrt(n)
    return {"n": n, "mean": mean, "sd": sd, "lo": mean - half, "hi": mean + half}
