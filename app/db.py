"""SQLite 连接与初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .money import dec, to_micro
from .timeutil import now_local_str

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

META_SCHEMA_VERSION = "schema_version"
META_PRICING_VERSION = "pricing_version_current"
META_DATA_SOURCE = "data_source"

META_MIGRATION_PREFIX = "migration:"

# ---------------------------------------------------------------- 迁移
# schema.sql 全是 CREATE TABLE IF NOT EXISTS，已经存在的库**不会**自动拿到新列。
# 结构迁移按「缺列就补」幂等执行，不记 meta——补完就是最终态，重复执行无副作用。
STRUCTURAL_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # A-1：告警事件需要「系统已自动解除」这一态，否则过期告警永远挂在界面上
    ("alert_event", "resolved_at", "TEXT"),
    # P0-A：缓存读来源（direct 直报 / trace_estimate traces 分摊）。
    # ALTER TABLE 带 DEFAULT 会把已有行整体填成默认值——历史行的缓存读本来就
    # 全部来自 traces 分摊，填 trace_estimate 恰好是事实，无需数据迁移。
    ("usage_fact", "cache_source", "TEXT NOT NULL DEFAULT 'trace_estimate'"),
)

# 数据迁移跑一次就记进 meta，绝不重复执行。
# A-3：告警时间戳曾被 SQLite datetime('now') 写成 UTC，界面上早了整整一个时区。
DATA_MIGRATIONS: tuple[tuple[str, str], ...] = (
    (
        "alert_ts_local_v1",
        "UPDATE alert_event "
        f"SET fired_at = datetime(fired_at, '{config.LOCAL_TZ_OFFSET_HOURS:+d} hours') "
        "WHERE fired_at LIKE '____-__-__ __:__:__'; "
        "UPDATE alert_state "
        f"SET triggered_at = datetime(triggered_at, '{config.LOCAL_TZ_OFFSET_HOURS:+d} hours') "
        "WHERE triggered_at LIKE '____-__-__ __:__:__'; "
        "UPDATE alert_state "
        f"SET cleared_at = datetime(cleared_at, '{config.LOCAL_TZ_OFFSET_HOURS:+d} hours') "
        "WHERE cleared_at LIKE '____-__-__ __:__:__'",
    ),
)


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in STRUCTURAL_MIGRATIONS:
        _ensure_column(conn, table, column, decl)

    applied = {
        r["key"]
        for r in conn.execute(
            "SELECT key FROM meta WHERE key LIKE ?", (META_MIGRATION_PREFIX + "%",)
        ).fetchall()
    }
    for name, sql in DATA_MIGRATIONS:
        key = META_MIGRATION_PREFIX + name
        if key in applied:
            continue
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (key, now_local_str()),
        )
        applied.add(key)


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """打开一个新连接。调用方负责关闭（推荐用 `session()`）。"""
    target = Path(path or config.DB_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target), timeout=10.0, isolation_level=None)
    _configure(conn)
    return conn


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """连接上下文：正常退出提交，异常回滚。"""
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def tx(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """显式事务：整块原子执行。"""
    conn = connect(path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def init_db(path: Path | None = None) -> None:
    """建表 + 首次运行时写入默认预算/汇率/元信息。幂等。"""
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    with session(path) as conn:
        conn.executescript(sql)
        _migrate(conn)

        # 默认预算
        row = conn.execute("SELECT COUNT(*) AS c FROM budget").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO budget (id, daily_usd_micro, monthly_usd_micro, currency) "
                "VALUES (1, ?, ?, 'USD')",
                (
                    to_micro(config.DEFAULT_DAILY_BUDGET_USD),
                    to_micro(config.DEFAULT_MONTHLY_BUDGET_USD),
                ),
            )

        # 积分换算率占位（未校准）
        row = conn.execute("SELECT COUNT(*) AS c FROM credit_rate").fetchone()
        if row["c"] == 0:
            conn.execute(
                "INSERT INTO credit_rate (id, rate, calibrated) VALUES (1, ?, ?)",
                (
                    str(config.DEFAULT_CREDIT_TO_USD_RATE),
                    1 if config.CREDIT_RATE_CALIBRATED_DEFAULT else 0,
                ),
            )

        # 默认汇率
        row = conn.execute("SELECT COUNT(*) AS c FROM fx_rate").fetchone()
        if row["c"] == 0:
            for quote, (rate, date) in config.DEFAULT_FX_RATES.items():
                conn.execute(
                    "INSERT OR REPLACE INTO fx_rate (rate_date, base, quote, rate) "
                    "VALUES (?, 'USD', ?, ?)",
                    (date, quote, rate),
                )

        # 元信息
        defaults = {
            META_SCHEMA_VERSION: "1",
            META_DATA_SOURCE: config.DATA_SOURCE_DEMO,
        }
        for key, value in defaults.items():
            conn.execute(
                "INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", (key, value)
            )


# ------------------------------------------------------------------ meta 读写
def get_meta(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def get_budget(conn: sqlite3.Connection) -> tuple[int, int, str]:
    row = conn.execute(
        "SELECT daily_usd_micro, monthly_usd_micro, currency FROM budget WHERE id = 1"
    ).fetchone()
    if row is None:
        return (
            to_micro(config.DEFAULT_DAILY_BUDGET_USD),
            to_micro(config.DEFAULT_MONTHLY_BUDGET_USD),
            "USD",
        )
    return row["daily_usd_micro"], row["monthly_usd_micro"], row["currency"]


def set_budget(
    conn: sqlite3.Connection, daily_usd: object, monthly_usd: object, currency: str
) -> None:
    conn.execute(
        "INSERT INTO budget (id, daily_usd_micro, monthly_usd_micro, currency, updated_at) "
        "VALUES (1, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  daily_usd_micro = excluded.daily_usd_micro, "
        "  monthly_usd_micro = excluded.monthly_usd_micro, "
        "  currency = excluded.currency, "
        "  updated_at = excluded.updated_at",
            (to_micro(daily_usd), to_micro(monthly_usd), currency, now_local_str()),
        )


def get_credit_rate(conn: sqlite3.Connection) -> tuple[str, bool]:
    row = conn.execute("SELECT rate, calibrated FROM credit_rate WHERE id = 1").fetchone()
    if row is None:
        return str(config.DEFAULT_CREDIT_TO_USD_RATE), False
    return row["rate"], bool(row["calibrated"])
