"""指标口径：命中率分母、除零兜底、预测样本不足、按模型算缓存节省（RK-06）。"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.caliber import (
    Estimated,
    budget_usage_pct,
    cache_hit_rate,
    cache_saved_usd,
    days_until_budget_exhausted,
    exact,
    month_bounds,
    month_end_forecast,
    per_request_cost,
    token_layers,
)
from app.pricing import Price


def _price(model: str, inp: str, cr: str) -> Price:
    return Price(
        model=model,
        input_per_mtok=Decimal(inp),
        output_per_mtok=Decimal("0"),
        cache_read_per_mtok=Decimal(cr),
        cache_write_per_mtok=Decimal("0"),
        pricing_version="test",
        source="builtin",
    )


# ------------------------------------------------------------------ 缓存命中率
def test_cache_hit_rate_excludes_cache_write_from_denominator():
    """分母只有「缓存读 + 全新输入」，缓存写不算分母。"""
    # 缓存读 900，全新输入 100，缓存写 5000（应被忽略）
    rate = cache_hit_rate(cache_read_tokens=900, fresh_input_tokens=100)
    assert rate == Decimal("0.9")


def test_cache_hit_rate_never_divides_by_zero():
    assert cache_hit_rate(0, 0) == Decimal(0)
    assert cache_hit_rate(None, None) == Decimal(0)


def test_cache_hit_rate_all_fresh_input():
    assert cache_hit_rate(0, 1000) == Decimal(0)


# ------------------------------------------------------------------ 除零兜底
def test_per_request_cost_guards_zero_requests():
    assert per_request_cost(Decimal("10"), 0) == Decimal(0)
    assert per_request_cost(Decimal("10"), 4) == Decimal("2.5")


def test_budget_usage_pct_guards_zero_budget():
    assert budget_usage_pct(Decimal("50"), Decimal("0")) == Decimal(0)
    assert budget_usage_pct(Decimal("50"), Decimal("100")) == Decimal("0.5")


# ------------------------------------------------------------------ 缓存节省
def test_cache_saved_is_computed_per_model():
    """必须按模型分别算再求和——用一个全局常量代替会算错。"""
    lookup = {
        "gpt-5": _price("gpt-5", "1.25", "0.125"),
        "gpt-5-mini": _price("gpt-5-mini", "0.25", "0.025"),
    }
    est = cache_saved_usd(
        [("gpt-5", 1_000_000), ("gpt-5-mini", 1_000_000)],
        lambda m: lookup.get(m),
    )
    # gpt-5: 1e6 × (1.25 − 0.125) / 1e6 = 1.125
    # mini : 1e6 × (0.25 − 0.025) / 1e6 = 0.225
    assert est.value == Decimal("1.35")
    assert est.is_estimated is True


def test_cache_saved_skips_unpriced_models():
    est = cache_saved_usd([("no-such-model", 1_000_000)], lambda m: None)
    assert est.value == Decimal(0)
    # 未识别模型必须明示，而不是假装算出 0
    assert "无可定价模型" in est.method


def test_cache_saved_skips_non_positive_delta():
    lookup = {"weird": _price("weird", "0.10", "0.50")}  # 缓存读比输入还贵
    est = cache_saved_usd([("weird", 1_000_000)], lambda m: lookup.get(m))
    assert est.value == Decimal(0)


# ------------------------------------------------------------------ 时间与预测
def test_month_bounds():
    first, days_in_month, elapsed = month_bounds(dt.date(2026, 10, 5))
    assert first == dt.date(2026, 10, 1)
    assert days_in_month == 31
    assert elapsed == 5
    # 二月闰年
    assert month_bounds(dt.date(2028, 2, 29))[1] == 29


def test_month_end_forecast_linear():
    est = month_end_forecast(Decimal("50"), days_elapsed=5, days_in_month=31)
    assert est.value == Decimal("310")
    assert est.is_estimated is True
    assert "线性外推" in est.method


def test_month_end_forecast_marks_insufficient_sample():
    """样本不足 3 天时不外推，但仍要带 estimated=true。"""
    est = month_end_forecast(Decimal("10"), days_elapsed=2, days_in_month=31)
    assert est.value == Decimal("10")
    assert est.is_estimated is True
    assert "样本不足" in est.method
    assert "不做外推" in est.method


def test_days_until_budget_exhausted():
    est = days_until_budget_exhausted(
        Decimal("50"), Decimal("100"), days_elapsed=5, days_in_month=31
    )
    # 日均 10，剩余 50 -> 还能撑 5 天
    assert est.value == Decimal("5")


def test_days_until_budget_exhausted_when_already_over():
    est = days_until_budget_exhausted(
        Decimal("150"), Decimal("100"), days_elapsed=5, days_in_month=31
    )
    assert est.value == Decimal(0)
    assert "已超出预算" in est.method


def test_days_until_budget_exhausted_without_sample():
    est = days_until_budget_exhausted(
        Decimal("0"), Decimal("100"), days_elapsed=0, days_in_month=31
    )
    assert "无法估算" in est.method


# ------------------------------------------------------------------ 包装与求和
def test_estimated_serialization_shape():
    est = Estimated(Decimal("12.3456"), "某推算方法")
    payload = est.as_dict()
    assert payload == {
        "value": "12.35",
        "estimated": True,
        "estimate_method": "某推算方法",
    }


def test_exact_is_not_marked_estimated():
    est = exact("7.5")
    assert est.value == Decimal("7.5")
    assert est.is_estimated is False


def test_token_layers_sums_four_layers_only():
    totals = token_layers(
        [
            {"input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 100, "cache_write_tokens": 2},
            {"input_tokens": 1, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
        ]
    )
    assert totals == {
        "input_tokens": 11,
        "output_tokens": 5,
        "cache_read_tokens": 100,
        "cache_write_tokens": 2,
        "total": 118,
    }


def test_token_layers_handles_missing_and_none():
    totals = token_layers([{}, {"input_tokens": None, "output_tokens": 3}])
    assert totals["input_tokens"] == 0
    assert totals["output_tokens"] == 3
    assert totals["total"] == 3


# ------------------------------------------------------------------ 配平
def test_apportion_removes_rounding_drift():
    """各项四舍五入后加总必须 == 总额，不能差一分钱。"""
    from decimal import ROUND_HALF_UP

    from app.money import apportion

    parts = [Decimal("1.005"), Decimal("2.005"), Decimal("3.005")]
    total = sum(parts)  # 6.015
    got = apportion(total, parts)
    assert sum(got) == Decimal("6.02")

    # 朴素做法（逐项 half-up）会得到 1.01 + 2.01 + 3.01 = 6.03，多出一分
    naive = [p.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for p in parts]
    assert sum(naive) == Decimal("6.03")
    assert sum(got) != sum(naive)


def test_apportion_is_exact_across_many_mixes():
    from decimal import ROUND_HALF_UP

    from app.money import apportion

    cases = [
        [Decimal("1.875")] * 3,
        [Decimal("1.005")] * 7,
        [Decimal("0.001")] * 9,
        [Decimal("10.00"), Decimal("0.005"), Decimal("0.005")],
        [Decimal("0")],
        [],
    ]
    for parts in cases:
        total = sum(parts, Decimal(0))
        got = apportion(total, parts)
        # 期望总额与应用内一致，统一 half-up（与 money_str 同口径）
        target = total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert sum(got) == target, f"{parts} -> {got} 之和 {sum(got)} != {target}"
        assert all(d.as_tuple().exponent == -2 for d in got)


def test_apportion_split_is_reasonable():
    """配平不应把差额全堆到一项上。"""
    from app.money import apportion

    got = apportion(Decimal("5.625"), [Decimal("1.875")] * 3)
    assert got == [Decimal("1.88"), Decimal("1.88"), Decimal("1.87")] or got == [
        Decimal("1.88"),
        Decimal("1.87"),
        Decimal("1.88"),
    ]
