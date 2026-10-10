"""RK-02：幂等去重。

核心断言：同一 request_id 重复上报，行数不变，且保留 output_tokens 最大的那条。
"""

from __future__ import annotations

from app.ingest import ingest_items
from app.models import IngestItem


def _item(rid: str, *, out: int, inp: int = 100, cache_read: int = 0, **kw):
    payload = {
        "request_id": rid,
        "ts": "2026-10-01T10:00:00+08:00",
        "provider": "test",
        "model": "gpt-5",
        "session_id": "s1",
        "usage": {
            "input_tokens": inp,
            "output_tokens": out,
            "cache_read_tokens": cache_read,
            "cache_write_tokens": 0,
            "cost": "0.10",
        },
    }
    payload.update(kw)
    return IngestItem.model_validate(payload)


def _count(conn, rid: str | None = None) -> int:
    if rid:
        return conn.execute(
            "SELECT COUNT(*) AS c FROM usage_fact WHERE request_id = ?", (rid,)
        ).fetchone()["c"]
    return conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]


def test_same_id_keeps_max_output(conn):
    """同一 id 依次上报 output=100 -> 300 -> 200，最终应为 1 行且 output=300。"""
    for out in (100, 300, 200):
        ingest_items(conn, [_item("req-1", out=out)])
    conn.commit()

    assert _count(conn) == 1
    row = conn.execute("SELECT output_tokens FROM usage_fact WHERE request_id='req-1'").fetchone()
    assert row["output_tokens"] == 300


def test_action_reporting(conn):
    """inserted / updated / ignored 三种动作都要如实报告。"""
    s1 = ingest_items(conn, [_item("req-2", out=100)])
    assert s1["inserted"] == 1 and s1["ignored"] == 0

    s2 = ingest_items(conn, [_item("req-2", out=300)])
    assert s2["updated"] == 1 and s2["inserted"] == 0

    s3 = ingest_items(conn, [_item("req-2", out=200)])
    assert s3["ignored"] == 1, "更小的 output 应被去重忽略"
    conn.commit()

    assert _count(conn) == 1
    assert conn.execute(
        "SELECT output_tokens FROM usage_fact WHERE request_id='req-2'"
    ).fetchone()["output_tokens"] == 300


def test_identical_resend_is_noop(conn):
    """完全相同的重发不应产生第二行。"""
    for _ in range(3):
        ingest_items(conn, [_item("req-3", out=250)])
    conn.commit()
    assert _count(conn, "req-3") == 1


def test_different_ids_are_separate_rows(conn):
    ingest_items(conn, [_item("req-4a", out=100), _item("req-4b", out=100)])
    conn.commit()
    assert _count(conn) == 2


def test_late_arriving_cache_fields_are_kept(conn):
    """流式写入时 cache 字段后到：后到的非零值必须保留，不能被整行替换丢掉。"""
    ingest_items(conn, [_item("req-5", out=300, inp=1000, cache_read=0)])
    conn.commit()
    # 同一条记录，稍后才带上 cache 数
    ingest_items(conn, [_item("req-5", out=300, inp=1000, cache_read=8888)])
    conn.commit()

    row = conn.execute(
        "SELECT output_tokens, cache_read_tokens FROM usage_fact WHERE request_id='req-5'"
    ).fetchone()
    assert row["output_tokens"] == 300
    assert row["cache_read_tokens"] == 8888


def test_input_follows_the_row_with_larger_output(conn):
    """input_tokens 应跟随 output 更大的那条记录，而不是逐字段取 MAX。

    实测真实数据里出现过同一 messageId 的两条记录 input/output 互不包含，
    逐字段 MAX 会拼出一条现实中不存在的行。
    """
    ingest_items(conn, [_item("req-6", out=239, inp=22221)])
    conn.commit()
    ingest_items(conn, [_item("req-6", out=155, inp=25591)])
    conn.commit()

    row = conn.execute(
        "SELECT input_tokens, output_tokens FROM usage_fact WHERE request_id='req-6'"
    ).fetchone()
    assert row["output_tokens"] == 239
    assert row["input_tokens"] == 22221, "input 必须来自 output 更大的那条，而不是 MAX"


def test_request_id_derived_when_absent(conn):
    """没给 request_id 时按 provider|tool|session|raw_id 派生，且重放幂等。"""
    a = IngestItem.model_validate(
        {"ts": "2026-10-01T10:00:00+08:00", "provider": "p", "tool": "t",
         "session_id": "s", "raw_id": "raw-9", "model": "gpt-5",
         "usage": {"output_tokens": 10}}
    )
    ingest_items(conn, [a])
    ingest_items(conn, [a])
    conn.commit()

    assert _count(conn) == 1
    rid = conn.execute("SELECT request_id FROM usage_fact").fetchone()["request_id"]
    assert rid.startswith("p_")
