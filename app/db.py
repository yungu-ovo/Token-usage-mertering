"""SQLite 连接与初始化。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config
from .money import dec, to_micro

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

META_SCHEMA_VERSION = "schema_version"
META_PRICING_VERSION = "pricing_version_current"
META_DATA_SOURCE = "data_source"


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
        "VALUES (1, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(id) DO UPDATE SET "
        "  daily_usd_micro = excluded.daily_usd_micro, "
        "  monthly_usd_micro = excluded.monthly_usd_micro, "
        "  currency = excluded.currency, "
        "  updated_at = datetime('now')",
        (to_micro(daily_usd), to_micro(monthly_usd), currency),
    )


def get_credit_rate(conn: sqlite3.Connection) -> tuple[str, bool]:
    row = conn.execute("SELECT rate, calibrated FROM credit_rate WHERE id = 1").fetchone()
    if row is None:
        return str(config.DEFAULT_CREDIT_TO_USD_RATE), False
    return row["rate"], bool(row["calibrated"])
