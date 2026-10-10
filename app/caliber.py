"""指标口径计算。

所有推算值一律用 :class:`Estimated` 包装，响应里带上 ``estimated`` 与
``estimate_method``，前端必须渲染「估算」角标（RK-06）。
"""

from __future__ import annotations

import calendar
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

from . import config
from .money import dec, money_str, safe_div


@dataclass(frozen=True)
class Estimated:
    """一个可能不是精确值的指标。"""

    value: Decimal
    method: str
    is_estimated: bool = True

    def as_dict(self, places: int = 2) -> dict[str, Any]:
        return {
            "value": money_str(self.value, places),
            "estimated": self.is_estimated,
            "estimate_method": self.method,
        }


def exact(value: Any) -> Estimated:
    """精确值，但仍走同一包装以便前端统一渲染。"""
    return Estimated(dec(value), "由原始记录直接汇总", is_estimated=False)


# ------------------------------------------------------------------ 基础口径
def cache_hit_rate(cache_read_tokens: int, fresh_input_tokens: int) -> Decimal:
    """缓存命中率 = 缓存读 ÷（缓存读 + 全新输入）。

    注意：**缓存写不计入分母**。分母为 0 时返回 0，不做除零。
    """
    cr = int(cache_read_tokens or 0)
    fresh = int(fresh_input_tokens or 0)
    return safe_div(cr, cr + fresh)


def per_request_cost(total_cost: Decimal, request_count: int) -> Decimal:
    """单请求平均成本。请求数为 0 时返回 0。"""
    return safe_div(total_cost, int(request_count or 0))


def cache_saved_usd(
    rows: Iterable[tuple[str, int]],
    price_lookup: Callable[[str], Any],
) -> Estimated:
    """缓存节省 = Σ_模型 缓存读 token ×（输入单价 − 缓存读单价）。

    ``rows`` 为 ``(model, cache_read_tokens)`` 序列。必须按模型分别计算后求和——
    用一个全局常量代替会算错（原型里就是这个错）。
    未识别模型（无价格）跳过，不计入节省额。
    """
    total = Decimal(0)
    priced_models = 0
    for model, cache_read in rows:
        price = price_lookup(model)
        if price is None:
            continue
        cr = int(cache_read or 0)
        if cr <= 0:
            continue
        delta = price.input_per_mtok - price.cache_read_per_mtok
        if delta <= 0:
            continue
        total += Decimal(cr) * delta / Decimal("1000000")
        priced_models += 1
    method = "Σ_模型 缓存读token ×(输入单价 − 缓存读单价)"
    if priced_models == 0:
        method += "；当前无可定价模型，结果为 0"
    return Estimated(total, method)


# ------------------------------------------------------------------ 时间口径
def month_bounds(day: dt.date | None = None) -> tuple[dt.date, dt.date, int]:
    """返回 (月初, 当月天数, 已过天数含当天)。"""
    today = day or dt.date.today()
    days_in_month = calendar.monthrange(today.year, today.month)[1]
    first = today.replace(day=1)
    return first, days_in_month, today.day


def month_end_forecast(
    mtd_cost: Decimal,
    days_elapsed: int,
    days_in_month: int,
) -> Estimated:
    """月末预测 = 月内日均 × 当月自然日天数（线性外推）。

    样本不足 3 天时不外推，直接返回当前值并标注不可靠。
    """
    if days_elapsed < config.FORECAST_MIN_SAMPLE_DAYS:
        return Estimated(
            dec(mtd_cost),
            f"样本不足（仅 {days_elapsed} 天，少于 {config.FORECAST_MIN_SAMPLE_DAYS} 天），不做外推",
        )
    forecast = dec(mtd_cost) / Decimal(days_elapsed) * Decimal(days_in_month)
    return Estimated(
        forecast,
        f"月内日均 × 当月自然日天数（{days_in_month} 天），线性外推",
    )


def budget_usage_pct(cost: Decimal, budget: Decimal) -> Decimal:
    """预算已用比例。预算为 0 时返回 0。"""
    return safe_div(cost, budget)


def days_until_budget_exhausted(
    mtd_cost: Decimal, budget: Decimal, days_elapsed: int, days_in_month: int
) -> Estimated:
    """按当前日均速度，预算还能撑几天（用于「提前预警」）。

    返回剩余可支撑天数；已超支返回 0。
    """
    cost = dec(mtd_cost)
    if cost <= 0 or days_elapsed <= 0:
        return Estimated(
            Decimal(days_in_month),
            "尚无有效样本，无法估算",
        )
    daily_avg = cost / Decimal(days_elapsed)
    remaining = dec(budget) - cost
    if remaining <= 0:
        return Estimated(Decimal(0), "已超出预算，剩余天数按 0 计")
    days_left = int(remaining / daily_avg)
    return Estimated(
        Decimal(days_left),
        "（预算 − 已用）÷ 月内日均，线性推算",
    )


# ------------------------------------------------------------------ 汇总辅助
def token_layers(rows: Sequence[dict[str, Any]]) -> dict[str, int]:
    """把若干行的四层 token 求和。"""
    keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
    out = {k: 0 for k in keys}
    for row in rows:
        for k in keys:
            out[k] += int(row.get(k) or 0)
    out["total"] = sum(out.values())
    return out
