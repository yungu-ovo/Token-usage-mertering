"""告警引擎单测。

这个模块是 P0 里最该被信任的部分——它说的每一句话都会直接顶在用户眼前。
上一轮在这里出过两次事：金额被放大 1e6 倍、以及过期告警永不消失（A-1）。
所以这里的用例不只测「能不能触发」，更测**「还挂着的告警是不是仍然成立」**。
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from pathlib import Path

import pytest

from app import alerts, db

ALERTS_SRC = Path(alerts.__file__)


def _event(conn, key: str, level: str = "warn", category: str = "budget") -> dict:
    """绕过 fire() 直接写一条事件，用来构造「脏状态」。"""
    with conn:  # 注意：conn 由夹具管理，这里只保证立即可见
        cur = conn.execute(
            "INSERT INTO alert_event "
            "(alert_key, level, category, message, fired_at) VALUES (?, ?, ?, ?, ?)",
            (key, level, category, "测试用事件", "2026-10-09 10:00:00"),
        )
        eid = cur.lastrowid
    row = conn.execute("SELECT * FROM alert_event WHERE id = ?", (eid,)).fetchone()
    return alerts._event_to_item(row)


# ================================================================== A-1 自动解除
def test_clear_marks_open_event_resolved(conn):
    """指标回落时，未确认的事件必须跟着解除，不能继续挂在界面上。"""
    alerts.fire(conn, alerts.UNPRICED_KEY, "warn", "pricing", "10 条未定价")
    assert alerts.stale_open_events(conn) == []

    alerts.clear(conn, alerts.UNPRICED_KEY)

    items = alerts.recent(conn)
    assert items and items[0]["state"] == "resolved"
    assert items[0]["resolved_at"]


def test_resolved_events_disappear_from_pending_list(conn):
    """「待处理」= 未确认 **且** 未自动解除。已解除的不该再回来烦用户。"""
    alerts.fire(conn, alerts.UNPRICED_KEY, "warn", "pricing", "10 条未定价")
    assert len(alerts.recent(conn, unacked_only=True)) == 1

    alerts.clear(conn, alerts.UNPRICED_KEY)
    assert alerts.recent(conn, unacked_only=True) == []


def test_manually_injected_stale_event_is_detected(conn):
    """有一条事件在说一件事，但状态层根本没有它 → 这就是「界面在说谎」。"""
    _event(conn, "budget:daily:2026-01-01:75")
    stale = alerts.stale_open_events(conn)
    assert len(stale) == 1
    assert stale[0]["alert_key"] == "budget:daily:2026-01-01:75"


def test_sweep_resolves_stale_events(conn):
    _event(conn, "budget:daily:2026-01-01:75")
    assert alerts.sweep_stale(conn) == 1
    assert alerts.stale_open_events(conn) == []
    assert alerts.sweep_stale(conn) == 0, "再跑一次不该重复处理"


def test_acknowledged_events_are_never_touched_by_sweep(conn):
    """用户已经点过「确认」的事件，不能被系统改写成「自动解除」。"""
    item = _event(conn, "budget:daily:2026-01-01:75")
    conn.execute(
        "UPDATE alert_event SET acknowledged = 1 WHERE id = ?", (item["id"],)
    )
    assert alerts.sweep_stale(conn) == 0
    row = conn.execute(
        "SELECT acknowledged, resolved_at FROM alert_event WHERE id = ?", (item["id"],)
    ).fetchone()
    assert row["acknowledged"] == 1 and row["resolved_at"] is None


def test_no_stale_open_alerts_after_full_evaluation(conn):
    """不变量：跑完一次完整巡检，界面上不该留任何「已不成立」的告警。

    这是 A-1 的守门用例。上一轮那条「845 条未定价（实际 0 条）」就是漏在这里。
    """
    # 先人为制造几条跨周期的告警，再跑巡检，看它们是否都被收口
    _event(conn, "budget:daily:2026-01-01:75")
    _event(conn, "budget:monthly:2026-01:100")
    _event(conn, "forecast:exhaust:2026-01")

    alerts.evaluate_scheduled(conn)
    assert alerts.stale_open_events(conn) == []


def test_budget_threshold_drops_below_and_clears(conn):
    """三档阈值：越档触发，回落到阈值以下必须解除（否则再也不会重新触发）。"""
    key = f"budget:daily:{dt.date.today().isoformat()}:75"
    alerts.fire(conn, key, "warn", "budget", "已用 80%")
    assert len(alerts.recent(conn, unacked_only=True)) == 1

    alerts.clear(conn, key)
    assert alerts.recent(conn, unacked_only=True) == []
    # 解除后允许重新触发——否则告警只会出现一次
    assert alerts.fire(conn, key, "warn", "budget", "已用 85%") is True


# ================================================================== 烧钱速率
def test_burn_rate_is_stateful_not_hourly(conn, monkeypatch):
    """速率回落必须解除激活态。

    早先按小时拼 key，每个小时都是新 key，alert_state 无限膨胀且永不解除。
    """
    import app.queries.insights as insights

    calls = {"triggered": True}
    monkeypatch.setattr(
        insights,
        "_burn_rate",
        lambda _conn: {
            "triggered": calls["triggered"],
            "current": "12.00",
            "baseline": "3.00",
            "ratio": 4.0,
        },
    )
    assert alerts.evaluate_burn_rate(conn) is True
    assert alerts._is_active(conn, alerts.BURN_RATE_KEY)

    calls["triggered"] = False
    assert alerts.evaluate_burn_rate(conn) is False
    assert not alerts._is_active(conn, alerts.BURN_RATE_KEY), "回落后必须解除"

    # 再超一次要能再触发
    calls["triggered"] = True
    assert alerts.evaluate_burn_rate(conn) is True


# ================================================================== A-3 本地时间
def test_alert_timestamp_is_local_not_utc(conn):
    """fired_at 必须是本地时间。UTC+8 下差 8 小时会让告警时间早半天。"""
    alerts.fire(conn, alerts.UNPRICED_KEY, "warn", "pricing", "时间测试")
    item = alerts.recent(conn)[0]

    fired = dt.datetime.strptime(item["fired_at"], "%Y-%m-%d %H:%M:%S")
    delta = abs((dt.datetime.now() - fired).total_seconds())
    assert delta < 120, f"告警时间偏离本地时间 {delta:.0f} 秒，多半又写成 UTC 了"


def test_alerts_module_never_lets_sqlite_generate_time():
    """静态检查：SQLite 的 datetime('now') 是 UTC，告警模块不许再出现。

    项目自己的纪律就是「时间一律本地自然日」，这条得由机器守住，
    不能指望每次改代码都记得。
    """
    source = ALERTS_SRC.read_text(encoding="utf-8")
    assert "datetime('now')" not in source


# ================================================================== B-2 采集健康度
def test_parser_alert_fires_when_success_rate_drops(conn):
    """RK-07：数据源改格式 → 少解析 → 成本被低报。必须告警。"""
    db.set_meta(conn, alerts.META_PARSER_VERSION, "1")
    db.set_meta(conn, alerts.META_PARSER_USAGE_LINES, "1000")
    db.set_meta(conn, alerts.META_PARSER_PARSED, "800")
    db.set_meta(conn, alerts.META_PARSE_RATE, "0.800000")

    assert alerts.evaluate_parser(conn) is True
    items = [i for i in alerts.recent(conn) if i["category"] == "parser"]
    assert items and "少算" in items[0]["message"]


def test_parser_alert_clears_when_rate_recovers(conn):
    db.set_meta(conn, alerts.META_PARSER_USAGE_LINES, "1000")
    db.set_meta(conn, alerts.META_PARSER_PARSED, "1000")
    db.set_meta(conn, alerts.META_PARSE_RATE, "1.000000")

    assert alerts.evaluate_parser(conn) is False
    assert not alerts._is_active(conn, alerts.PARSER_KEY)


@pytest.mark.parametrize("rate", ["", "0.500000"])
def test_parser_alert_stays_silent_without_sample(conn, rate):
    """没有可判定的样本时不该告警——否则空库会一直红着。"""
    db.set_meta(conn, alerts.META_PARSER_USAGE_LINES, "0")
    db.set_meta(conn, alerts.META_PARSER_PARSED, "0")
    db.set_meta(conn, alerts.META_PARSE_RATE, rate)
    assert alerts.evaluate_parser(conn) is False


def test_parse_stats_rate_uses_usage_lines_as_denominator():
    """分母必须是「含用量的行数」，否则会被正文消息稀释成毫无意义的数字。"""
    from app.sources.base import ParseStats

    s = ParseStats(scanned=10000, usage_lines=1000, parsed=990)
    assert s.rate() == pytest.approx(0.99)
    assert ParseStats().rate() is None
