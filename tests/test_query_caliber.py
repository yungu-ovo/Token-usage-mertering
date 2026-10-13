"""聚合口径一致性：所有聚合金额必须是**美元**，且互相对得上。

这个文件的存在理由：微美元整数落库后，只要有一处忘了 ``/ 1e6``，
汇总数字就会比明细大 100 万倍。这类 bug 在单个模块里看不出来，
只有做「总额对账」才会暴露——所以这里专治它。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.ingest import ingest_items
from app.models import IngestItem
from app.queries import details, kpi, mix, series
from app.queries.common import daily_costs, fetch_cost

TODAY = dt.date.today().isoformat()
TS = f"{TODAY}T10:00:00+08:00"

# gpt-5：输入 $1.25/Mtok，输出 $10.00/Mtok
#   1,000,000 输入 -> $1.25
#   2,000,000 输入 -> $2.50
#   1,000,000 输出 -> $10.00
EXPECTED_TOTAL = Decimal("13.75")


@pytest.fixture()
def seeded(conn):
    def item(rid: str, inp: int = 0, out: int = 0, project: str = "proj-a"):
        return IngestItem.model_validate(
            {
                "request_id": rid,
                "ts": TS,
                "provider": "workbuddy",
                "model": "gpt-5",
                "session_id": "s-" + rid,
                "project": project,
                "usage": {"input_tokens": inp, "output_tokens": out},
            }
        )

    ingest_items(
        conn,
        [
            item("a1", inp=1_000_000),
            item("a2", inp=2_000_000),
            item("a3", out=1_000_000, project="proj-b"),
        ],
    )
    conn.commit()
    return conn


# ------------------------------------------------------------------ 单一事实
def test_fetch_cost_returns_usd_not_micro(seeded):
    total, _vendor = fetch_cost(seeded, TODAY, TODAY)
    assert total == EXPECTED_TOTAL, f"fetch_cost 返回 {total}，应为美元 {EXPECTED_TOTAL}"


def test_daily_costs_matches_fetch_cost(seeded):
    series_rows = daily_costs(seeded, TODAY, TODAY)
    assert len(series_rows) == 1
    assert series_rows[0]["cost"] == EXPECTED_TOTAL
    assert series_rows[0]["requests"] == 3


# ------------------------------------------------------------------ 五处聚合互相对账
def test_kpi_north_star_matches_total(seeded):
    payload = kpi.build(seeded)
    assert payload["north_star"]["mtd_cost"] == "13.75"
    # 预算 300 / 花了 13.75 -> 4.58%
    assert payload["north_star"]["pct_label"] == "4.6%"
    assert payload["north_star"]["budget"] == "300.00"


def test_mix_total_matches_and_shares_sum_to_one(seeded):
    payload = mix.by_dimension(seeded, "model")
    assert payload["total_cost"] == "13.75"
    assert payload["total_requests"] == 3

    shares = sum(r["share"] for r in payload["items"])
    assert abs(shares - 1.0) < 1e-9, f"share 之和为 {shares}，应为 1"

    item_costs = sum(Decimal(r["cost"]) for r in payload["items"])
    assert item_costs == EXPECTED_TOTAL, "构成区各项之和 != 总额"


def test_mix_by_project_also_reconciles(seeded):
    payload = mix.by_dimension(seeded, "project")
    assert payload["total_cost"] == "13.75"
    assert sum(Decimal(r["cost"]) for r in payload["items"]) == EXPECTED_TOTAL
    names = {r["name"] for r in payload["items"]}
    assert names == {"proj-a", "proj-b"}


def test_requests_sum_matches_total(seeded):
    payload = details.requests(seeded, limit=50)
    assert payload["sum_cost"] == "13.75"
    assert payload["total"] == 3
    # 逐行求和也必须等于总额
    assert sum(Decimal(r["cost"]) for r in payload["items"]) == EXPECTED_TOTAL


def test_trend_total_matches(seeded):
    payload = series.trend(seeded, days=7)
    assert payload["total_cost"] == "13.75"
    assert sum(Decimal(p["cost"]) for p in payload["points"]) == EXPECTED_TOTAL
    assert payload["data_days"] == 1
    # 日预算 10，当日 13.75 -> 超预算
    assert payload["points"][-1]["over_budget"] is True


def test_insights_total_matches(seeded):
    from app.queries import insights

    payload = insights.build(seeded)
    assert payload["total_cost"] == "13.75"


# ------------------------------------------------------------------ 全链路同一数字
def test_every_endpoint_agrees_on_the_same_number(seeded):
    """六个出口必须报同一个数，任何一个忘了换算都会在这里露馅。"""
    numbers = {
        "fetch_cost": str(fetch_cost(seeded, TODAY, TODAY)[0]),
        "kpi": kpi.build(seeded)["north_star"]["mtd_cost"],
        "mix": mix.by_dimension(seeded, "model")["total_cost"],
        "trend": series.trend(seeded, days=7)["total_cost"],
        "requests": details.requests(seeded, limit=50)["sum_cost"],
        "tokens_costless": str(
            sum(Decimal(s["cost"]) for s in daily_costs(seeded, TODAY, TODAY))
        ),
    }
    assert set(numbers.values()) == {"13.75"}, numbers


# ------------------------------------------------------------------ 空库不炸
def test_empty_db_reports_zero_everywhere(conn):
    assert fetch_cost(conn, TODAY, TODAY) == (Decimal(0), Decimal(0))
    assert kpi.build(conn)["north_star"]["mtd_cost"] == "0.00"
    assert mix.by_dimension(conn, "model")["total_cost"] == "0.00"
    assert mix.by_dimension(conn, "model")["items"] == []
    assert series.trend(conn, days=7)["total_cost"] == "0.00"
    assert details.requests(conn, limit=10)["total"] == 0
    # 空库必须给出解释，而不是让前端显示一个无意义的 0
    assert mix.by_dimension(conn, "model")["empty_note"]
    assert series.trend(conn, days=7)["empty_note"]


def test_mix_rejects_unknown_dimension(conn):
    with pytest.raises(ValueError):
        mix.by_dimension(conn, "password")


# ------------------------------------------------------------------ 尾差对账
def test_mix_parts_sum_exactly_when_rounding_would_drift(conn):
    """三条 $1.875 的记录：朴素四舍五入会得出 5.64，总额其实只有 5.63。"""
    ingest_items(
        conn,
        [
            IngestItem.model_validate(
                {
                    "request_id": f"d{i}",
                    "ts": TS,
                    "provider": "workbuddy",
                    "model": "gpt-5",
                    "session_id": f"sd{i}",
                    "project": "proj-drift",
                    "usage": {"input_tokens": 1_500_000},
                }
            )
            for i in range(3)
        ],
    )
    conn.commit()

    payload = mix.by_dimension(conn, "model", days=30)
    assert payload["total_cost"] == "5.63"
    assert sum(Decimal(r["cost"]) for r in payload["items"]) == Decimal("5.63")


def test_trend_points_sum_exactly_to_total(conn):
    """折线各点之和必须等于总额，否则按天加总会和月度数字打架。"""
    items = []
    for offset in range(3):
        day = (dt.date.today() - dt.timedelta(days=offset)).isoformat()
        items.append(
            IngestItem.model_validate(
                {
                    "request_id": f"t{offset}",
                    "ts": f"{day}T09:00:00+08:00",
                    "provider": "workbuddy",
                    "model": "gpt-5",
                    "session_id": f"st{offset}",
                    "usage": {"input_tokens": 1_500_000},
                }
            )
        )
    ingest_items(conn, items)
    conn.commit()

    payload = series.trend(conn, days=7)
    points_sum = sum(Decimal(p["cost"]) for p in payload["points"])
    assert points_sum == Decimal(payload["total_cost"])
    assert payload["total_cost"] == "5.63"


# ------------------------------------------------------------------ 缓存口径（P0-A）
def _cache_item(rid: str, inp: int, cache_read: int) -> IngestItem:
    return IngestItem.model_validate(
        {
            "request_id": rid,
            "ts": TS,
            "provider": "workbuddy",
            "model": "gpt-5",
            "session_id": "s-" + rid,
            "usage": {
                "input_tokens": inp,
                "output_tokens": 10,
                "cache_read_tokens": cache_read,
            },
        }
    )


def test_cache_provenance_direct_when_all_reported(conn):
    """全部直报时：来源标 direct、不算估算、命中率按全窗口分母。"""
    ingest_items(
        conn,
        [
            _cache_item("d1", inp=9_000, cache_read=8_000),
            _cache_item("d2", inp=1_000, cache_read=0),
        ],
    )
    conn.commit()
    block = kpi.build(conn, days=7)["cache"]
    assert block["cache_source"] == "direct"
    assert block["cache_source_label"] == "请求级直报"
    assert block["cache_estimated"] is False
    # 8000 / 10000——含未命中那条做分母，因为直报是事实不是抽样
    assert block["hit_rate"] == pytest.approx(0.8)
    assert block["cache_coverage"] == pytest.approx(0.5)  # 1/2 条有缓存读


def test_cache_provenance_estimate_keeps_sample_denominator(conn):
    """纯 traces 估算时：命中率只按有缓存支撑的样本算（历史口径不破）。"""
    ingest_items(conn, [_cache_item("e1", inp=10_000, cache_read=0)])
    conn.commit()
    # 手工写一条估算值，模拟 backfill 的产物
    conn.execute(
        "UPDATE usage_fact SET cache_read_tokens = 9000, cache_source = 'trace_estimate' "
        "WHERE request_id = 'e1'"
    )
    conn.commit()
    block = kpi.build(conn, days=7)["cache"]
    assert block["cache_source"] == "trace_estimate"
    assert block["cache_estimated"] is True
    assert block["hit_rate"] == pytest.approx(0.9)


def test_cache_provenance_none_when_no_cache_data(conn):
    ingest_items(conn, [_cache_item("n1", inp=10_000, cache_read=0)])
    conn.commit()
    block = kpi.build(conn, days=7)["cache"]
    assert block["cache_source"] == "none"
    assert block["hit_rate"] == 0


def test_cache_provenance_mixed_reports_both_counts(conn):
    """混合来源：两类请求数都报出来，整体按估算对待（RK-06）。"""
    ingest_items(
        conn,
        [
            _cache_item("m1", inp=9_000, cache_read=8_000),
            _cache_item("m2", inp=10_000, cache_read=0),
        ],
    )
    conn.commit()
    conn.execute(
        "UPDATE usage_fact SET cache_read_tokens = 9000, cache_source = 'trace_estimate' "
        "WHERE request_id = 'm2'"
    )
    conn.commit()
    block = kpi.build(conn, days=7)["cache"]
    assert block["cache_source"] == "mixed"
    assert block["direct_requests"] == 1
    assert block["estimated_requests"] == 1
    assert block["cache_estimated"] is True
    # 17000 / 19000，两条都进样本
    assert block["hit_rate"] == pytest.approx(17_000 / 19_000)
