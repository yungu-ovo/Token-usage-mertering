"""缓存 token 回填（B-1）的测试。

这里守的是三件事，任何一件被破坏都会让看板上的缓存命中率变成假数字：

1. **守恒**：分摊出去的缓存总量必须精确等于 trace 观测到的量。
2. **精度**：库内回算出的会话缓存率必须与 trace 观测值一致。
   ——曾经出过把缓存摊到全部请求上的错，命中率从 94% 掉到 46%。
3. **幂等**：重复跑不会翻倍。
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest

from app import db
from app.pricing import seed_builtin
from app.sources.trace_cache import (
    LOW_COVERAGE,
    SessionCache,
    apportion_session,
    backfill,
    coverage_note,
)


class FakeRow(dict):
    """够用的 sqlite3.Row替身：apportion 只按 key 取值。"""

    def __getitem__(self, k):  # type: ignore[override]
        return dict.__getitem__(self, k)


@pytest.fixture()
def conn(tmp_path):
    """打开一个写连接的上下文管理器，用完自动提交关闭。

    注意 ``db.session()`` 是**上下文管理器**而不是连接本身——拿它当连接用
    会报'_GeneratorContextManager' object has no attribute 'execute'。
    """
    path = tmp_path / "cache-test.db"
    db.init_db(path)
    manager = db.session(path)
    connection = manager.__enter__()
    seed_builtin(connection)
    yield connection
    manager.__exit__(None, None, None)


def _rows(n: int, input_each: int = 1000):
    return [FakeRow(request_id=f"r{i}", input_tokens=input_each) for i in range(n)]


# ------------------------------------------------------------------ 分摊守恒
def test_apportion_conserves_total():
    """分摊之和必须精确等于目标，一个 token 都不能差。"""
    session = SessionCache("s1", input_tokens=1_000_000, cached_tokens=950_000,
                           call_count=3, file_count=1)
    shares = apportion_session(session, _rows(3, 1000), covered_requests=3)
    assert sum(shares) == 950_000, "分摊之和必须精确等于 trace 观测值"


def test_apportion_never_exceeds_available_rows():
    session = SessionCache("s1", input_tokens=1000, cached_tokens=900,
                           call_count=99, file_count=1)
    shares = apportion_session(session, _rows(3, 100), covered_requests=99)
    assert len(shares) == 3
    assert sum(shares) == 900


def test_uncovered_rows_get_zero():
    """trace 只观测到部分请求时，未观测的请求必须是 0，不能摊到它们头上。

    这是修过的真 bug：把缓存摊到全部请求会让命中率从 94% 掉到 46%。
    """
    session = SessionCache("s1", input_tokens=10_000, cached_tokens=9_000,
                           call_count=2, file_count=1)
    rows = _rows(10, 1000)
    shares = apportion_session(session, rows, covered_requests=2)
    assert sum(shares) == 9_000
    # 前2 条（按输入降序后）拿到量，后 8 条必须是 0
    assert shares[:2] != [0, 0]
    assert shares[2:] == [0] * 8, "未观测的请求不能分到缓存量"


def test_apportion_by_input_weight():
    """输入大的请求应分到更多缓存。"""
    session = SessionCache("s1", input_tokens=3000, cached_tokens=3000,
                           call_count=3, file_count=1)
    rows = [
        FakeRow(request_id="a", input_tokens=1000),
        FakeRow(request_id="b", input_tokens=1500),
        FakeRow(request_id="c", input_tokens=500),
    ]
    shares = apportion_session(session, rows, covered_requests=3)
    assert sum(shares) == 3000
    by_id = {"a": shares[0], "b": shares[1], "c": shares[2]}
    assert by_id["b"] > by_id["a"] > by_id["c"]


def test_zero_input_falls_back_to_even_split():
    """输入都是 0 时不能除零，退化为按请求数均摊且仍守恒。"""
    session = SessionCache("s1", input_tokens=100, cached_tokens=95,
                           call_count=4, file_count=1)
    rows = _rows(4, 0)
    shares = apportion_session(session, rows, covered_requests=4)
    assert sum(shares) == 95


def test_empty_rows_returns_empty():
    session = SessionCache("s1", 100, 50, 1, 1)
    assert apportion_session(session, [], covered_requests=1) == []


# ------------------------------------------------------------------ SessionCache
def test_rate_and_coverage():
    s = SessionCache("s1", input_tokens=2000, cached_tokens=1900, call_count=5, file_count=2)
    assert float(s.rate) == pytest.approx(0.95)
    assert float(s.coverage(10)) == pytest.approx(0.5)


def test_low_coverage_boundary():
    """覆盖率判定阈值两侧行为不同，边界值要稳定。"""
    just_above = SessionCache("s1", 1000, 900, int(LOW_COVERAGE * 100) + 5, 1)
    just_below = SessionCache("s2", 1000, 900, int(LOW_COVERAGE * 100) - 5, 1)
    assert float(just_above.coverage(100)) >= float(LOW_COVERAGE)
    assert float(just_below.coverage(100)) < float(LOW_COVERAGE)


# ------------------------------------------------------------------ 端到端
def _seed_usage(conn, session_id: str, n: int, input_each: int = 1000):
    for i in range(n):
        conn.execute(
            "INSERT INTO usage_fact (request_id, ts, ts_day, model, session_id, "
            "input_tokens, output_tokens, cost_usd_micro) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"{session_id}-{i}", "2026-10-01T10:00:00", "2026-10-01",
             "gpt-5", session_id, input_each, 100, 1000),
        )
    conn.commit()


def test_backfill_is_idempotent(conn):
    """重复跑不翻倍。"""
    _seed_usage(conn, "sess-a", 5)
    calls = {"n": 2}

    class FakeParser:
        def discover(self):
            return [1, 2]

        def read_trace(self, _):
            return {
                "session_id": "sess-a",
                "total_input_tokens": 5000,
                "total_output_tokens": 500,
                "total_cached_tokens": 4500,
                "call_count": 2,
            }

    first = backfill(conn, apply=True, home=FakeParser())
    total1 = conn.execute(
        "SELECT COALESCE(SUM(cache_read_tokens),0) s FROM usage_fact"
    ).fetchone()["s"]
    second = backfill(conn, apply=True, home=FakeParser())
    total2 = conn.execute(
        "SELECT COALESCE(SUM(cache_read_tokens),0) s FROM usage_fact"
    ).fetchone()["s"]

    assert total1 == 9000, (
        "同会话的多个 trace 必须求和（滚动切分），"
        "discover() 返回 2 个文件各报 4500 → 总量应为 9000；取 max 会丢数据"
    )
    assert total2 == total1, "重复回填不得改变总量"
    assert second["cache_read_tokens"] == 0, "第二次没有待补记录，应为 0"


def test_backfill_dry_run_does_not_write(conn):
    """演练绝不写库。"""
    _seed_usage(conn, "sess-b", 3)

    class FakeParser:
        def discover(self):
            return [1]

        def read_trace(self, _):
            return {
                "session_id": "sess-b",
                "total_input_tokens": 3000,
                "total_output_tokens": 0,
                "total_cached_tokens": 2700,
                "call_count": 3,
            }

    stats = backfill(conn, apply=False, home=FakeParser())
    total = conn.execute(
        "SELECT COALESCE(SUM(cache_read_tokens),0) s FROM usage_fact"
    ).fetchone()["s"]
    assert stats["cache_read_tokens"] == 2700, "演练应算出将要写入的量"
    assert total == 0, "演练不得写库"


def test_backfill_preserves_session_rate(conn):
    """最关键的一条：回填后的会话缓存率必须与 trace 观测值一致。

    这正是被修掉的 bug ——摊到全部请求时库内率会掉到 31.9%（观测值 93.8%）。
    """
    _seed_usage(conn, "sess-c", 4, input_each=1000)

    class FakeParser:
        def discover(self):
            return [1]

        def read_trace(self, _):
            return {
                "session_id": "sess-c",
                "total_input_tokens": 4000,
                "total_output_tokens": 0,
                "total_cached_tokens": 3800,   # 观测率 95%
                "call_count": 4,
            }

    backfill(conn, apply=True, home=FakeParser())
    row = conn.execute(
        "SELECT SUM(input_tokens) i, SUM(cache_read_tokens) c FROM usage_fact "
        "WHERE session_id = 'sess-c'"
    ).fetchone()
    actual = row["c"] / row["i"]
    assert actual == pytest.approx(0.95, abs=0.01), f"回填后率应≈95%，实际 {actual:.1%}"


def test_coverage_note_reports_ratio(conn):
    _seed_usage(conn, "sess-d", 4)
    with conn:
        conn.execute("UPDATE usage_fact SET cache_read_tokens = 1 WHERE request_id = 'sess-d-0'")
    prov = coverage_note(conn)
    assert prov["total_requests"] == 4
    assert prov["covered_requests"] == 1
    assert prov["coverage"] == Decimal("0.25")


def test_coverage_note_on_empty_db(conn):
    prov = coverage_note(conn)
    assert prov["total_requests"] == 0
    assert prov["coverage"] == Decimal(0)