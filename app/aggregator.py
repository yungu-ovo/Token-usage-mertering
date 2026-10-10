"""聚合：明细 -> daily_agg，以及明细过期清理。

明细保留 90 天，`daily_agg` 永久。查询层对超过保留期的区间自动改走聚合表。
"""

from __future__ import annotations

import datetime as dt
import sqlite3

from . import config

_AGG_COLUMNS = (
    "ts_day, model, project, tool, provider, billing_mode, "
    "input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, "
    "cost_usd_micro, equivalent_cost_usd_micro, vendor_credit_micro, req_count, fail_count"
)

_AGG_SELECT_BODY = """
SELECT ts_day, model, project, tool, provider, billing_mode,
       SUM(input_tokens)                          AS input_tokens,
       SUM(output_tokens)                         AS output_tokens,
       SUM(cache_read_tokens)                     AS cache_read_tokens,
       SUM(cache_write_tokens)                    AS cache_write_tokens,
       SUM(cost_usd_micro)                        AS cost_usd_micro,
       SUM(COALESCE(equivalent_cost_usd_micro,0))  AS equivalent_cost_usd_micro,
       SUM(COALESCE(vendor_credit_micro,0))        AS vendor_credit_micro,
       COUNT(*)                                   AS req_count,
       SUM(CASE WHEN status <> 'success' THEN 1 ELSE 0 END) AS fail_count
FROM usage_fact
"""

_AGG_GROUP_BY = "GROUP BY ts_day, model, project, tool, provider, billing_mode"

_AGG_SELECT = (
    _AGG_SELECT_BODY + "WHERE ts_day = ?\n" + _AGG_GROUP_BY
)


def rebuild_day(conn: sqlite3.Connection, ts_day: str) -> int:
    """重建某一天的聚合。幂等——重跑不会翻倍。"""
    conn.execute("DELETE FROM daily_agg WHERE ts_day = ?", (ts_day,))
    conn.execute(f"INSERT INTO daily_agg ({_AGG_COLUMNS}) {_AGG_SELECT}", (ts_day,))
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM daily_agg WHERE ts_day = ?", (ts_day,)
    ).fetchone()
    return int(row["c"] if row else 0)


def rebuild_all(conn: sqlite3.Connection) -> int:
    """全量重建聚合表。"""
    conn.execute("DELETE FROM daily_agg")
    conn.execute(
        f"INSERT INTO daily_agg ({_AGG_COLUMNS}) {_AGG_SELECT_BODY} {_AGG_GROUP_BY}"
    )
    row = conn.execute("SELECT COUNT(*) AS c FROM daily_agg").fetchone()
    return int(row["c"] if row else 0)


def prune_details(conn: sqlite3.Connection, retention_days: int | None = None) -> int:
    """把超期明细汇总进 daily_agg 后删除。返回删除行数。"""
    days = int(retention_days or config.DETAIL_RETENTION_DAYS)
    cutoff = (dt.date.today() - dt.timedelta(days=days)).isoformat()

    stale = [
        r["ts_day"]
        for r in conn.execute(
            "SELECT DISTINCT ts_day FROM usage_fact WHERE ts_day < ?", (cutoff,)
        ).fetchall()
    ]
    for day in stale:
        rebuild_day(conn, day)

    if not stale:
        return 0
    placeholders = ",".join("?" for _ in stale)
    cur = conn.execute(f"DELETE FROM usage_fact WHERE ts_day IN ({placeholders})", stale)
    return cur.rowcount or 0


def agg_days_available(conn: sqlite3.Connection) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT MIN(ts_day) AS lo, MAX(ts_day) AS hi FROM daily_agg"
    ).fetchone()
    if row and row["lo"]:
        return row["lo"], row["hi"]
    return None
