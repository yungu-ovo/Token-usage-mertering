"""采集流水线与数据新鲜度。

B-1：这条流水线早先只活在 scripts/ 里，服务层想采也采不了，
于是「重新采集」按钮其实什么都没采（A-2）。抽进 app/collect.py 之后，
脚本与接口必须跑**同一条路径**，这里就是那个约束的守门人。
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from app import collect as collect_mod, db
from app.ingest import ingest_items
from app.models import IngestItem
from app.timeutil import now_local

FIXTURES = Path(__file__).parent / "fixtures"


def _item(rid: str, ts: str) -> IngestItem:
    return IngestItem.model_validate(
        {
            "request_id": rid,
            "ts": ts,
            "provider": "workbuddy",
            "model": "gpt-5",
            "session_id": "s-" + rid,
            "usage": {"input_tokens": 1000, "output_tokens": 10},
        }
    )


# ================================================================== 解析健康度
def test_parser_reports_stats_on_fixture():
    """解析器必须顺带产出成功率——RK-07 的监控不能是空的。

    夹具刻意混进了坏行、全 0 token、缺 id 三种边缘样本，
    所以成功率**不该**是 100%——它得如实反映「有多少该解析的没解析出来」。
    """
    records, stats = collect_mod.parse_records(home=FIXTURES)
    assert records, "夹具里应当能解析出记录"
    assert stats.scanned == 13 and stats.bad_lines == 1
    assert stats.usage_lines == 10
    assert stats.parsed == len(records) == 8
    assert stats.rate() == pytest.approx(0.8)
    assert stats.parser_version and stats.parser_version != "0"
    # 跳过原因要能区分，否则事后无从判断是格式变了还是数据本来就这样
    assert stats.skipped["zero_tokens"] == 1
    assert stats.skipped["missing_id"] == 1


def test_parser_stats_survive_into_meta(conn):
    from app import alerts

    collect_mod._record_parser_stats(
        conn, collect_mod.parse_records(home=FIXTURES)[1]
    )
    assert db.get_meta(conn, alerts.META_PARSER_VERSION)
    assert db.get_meta(conn, alerts.META_PARSE_RATE) == "0.800000"
    assert int(db.get_meta(conn, alerts.META_PARSER_USAGE_LINES)) == 10


# ================================================================== A-4 新鲜度
def test_freshness_on_empty_db(conn):
    fresh = collect_mod.data_freshness(conn)
    assert fresh["has_data"] is False
    assert fresh["is_stale"] is False
    assert fresh["freshness_hours"] is None


def test_freshness_is_fresh_right_after_ingest(conn):
    ingest_items(conn, [_item("r1", now_local().isoformat())])
    fresh = collect_mod.data_freshness(conn)
    assert fresh["has_data"] is True
    assert fresh["is_stale"] is False
    assert fresh["freshness_hours"] is not None and fresh["freshness_hours"] < 1


def test_freshness_flags_stale_data(conn):
    old = (now_local() - dt.timedelta(days=3)).isoformat()
    ingest_items(conn, [_item("r2", old)])
    fresh = collect_mod.data_freshness(conn)
    assert fresh["is_stale"] is True
    assert fresh["freshness_hours"] >= 24
    assert "滞后" in fresh["note"]


# ================================================================== B-1 采集
def test_collect_dry_run_writes_nothing(conn):
    """演练模式连 meta 都不该写——「看一眼」不能改变状态。"""
    summary = collect_mod.collect(conn, home=FIXTURES, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["parsed"] > 0
    assert conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"] == 0
    assert db.get_meta(conn, "last_collect_at") == ""


def test_collect_end_to_end(conn):
    summary = collect_mod.collect(conn, home=FIXTURES, backfill_cache=False)
    assert summary["parsed"] > 0
    ing = summary["ingest"]
    assert ing["received"] == summary["parsed"]
    assert ing["inserted"] > 0
    assert conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"] > 0
    assert db.get_meta(conn, "last_collect_at"), "采集时间要写进 meta"
    assert summary["freshness"]["has_data"] is True


def test_collect_is_idempotent(conn):
    """跑两次不该翻倍：幂等去重是 RK-02。"""
    first = collect_mod.collect(conn, home=FIXTURES, backfill_cache=False)
    before = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    second = collect_mod.collect(conn, home=FIXTURES, backfill_cache=False)
    after = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    assert after == before, "重复采集不该让行数变化"
    assert second["ingest"]["inserted"] == 0
    assert (
        second["ingest"]["updated"] + second["ingest"]["ignored"]
        == first["ingest"]["received"]
    )


def test_collect_with_no_records_still_patrols(conn):
    """没有新记录也要跑巡检——解析成功率可能刚刚跌破阈值。"""
    summary = collect_mod.collect(conn, home=Path(FIXTURES) / "不存在的目录")
    assert summary["parsed"] == 0
    assert "alerts" in summary and "parser" in summary["alerts"]
