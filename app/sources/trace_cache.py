"""从 traces 补齐缓存 token（会话级补充源）。

为什么需要
----------
``projects/**/*.jsonl`` 里**没有任何缓存 token 字段**，所以 ``usage_fact``
的 ``cache_read_tokens``恒为 0，缓存命中率与缓存节省金额在真实数据下永远是
「—」。而 ``traces/*/trace_*.json`` 的 ``modelInfo.totalCachedTokens`` 有这个数。

为什么不直接把 trace 灌进usage_fact
----------------------------------
两个源的**粒度不同**，硬灌会重复计数：

* jsonl 是**请求级**：一次 LLM 调用一行。
* trace 是**会话级聚合**：一个会话可能有多个 trace 文件（滚动切分），
  必须**求和**而不是取 max。

实测（本机 18 个会话）给出了清晰的两类模式：

===============  ==========================
trace 覆盖情况    判定依据
===============  ==========================
完整覆盖          trace 调用数 == jsonl 请求数
片段覆盖          trace 调用数 << jsonl 请求数（本机有低至 10% 的）
===============  ==========================

所以这里的策略是：

1. 同一会话的多个 trace **先求和**；
2. 用 **会话缓存率**（缓存读 / 输入）作为一个可核验的比例；
3. 按该会话 jsonl 的输入 token **分摊**到每条记录，缓存率越高分得越多；
4. **全程标``estimated`` + 写明方法**，覆盖率不足的会话额外标注可信度。

分摊而不是直接写入，是为了让「总量」守恒：分摊后的缓存读总量恰好等于
``会话输入 × 会话缓存率``，不会因为 trace 覆盖不全而虚增缓存总量。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any

from ..money import dec
from .workbuddy_trace import WorkBuddyTraceParser

# 覆盖率低于此值就认为 trace 只是片段，结论标注为「低可信」
LOW_COVERAGE = 0.60


@dataclass
class SessionCache:
    """一个会话的缓存事实（来自 traces 求和）。"""

    session_id: str
    input_tokens: int
    cached_tokens: int
    call_count: int
    file_count: int

    @property
    def rate(self) -> Decimal:
        """会话缓存率 = 缓存读 / 输入。无输入时为 0。"""
        if self.input_tokens <= 0:
            return Decimal(0)
        return dec(self.cached_tokens) / dec(self.input_tokens)

    def coverage(self, jsonl_requests: int) -> Decimal:
        """trace 调用数 / jsonl 请求数。>1 说明两边口径不同，不可信。"""
        if jsonl_requests <= 0:
            return Decimal(0)
        return dec(self.call_count) / dec(jsonl_requests)

    @property
    def trustworthy(self) -> bool:
        return self.call_count > 0 and self.input_tokens > 0


def collect_sessions(home: Any = None) -> dict[str, SessionCache]:
    """扫描 traces，按会话求和。

    一个会话可能有多个 trace 文件（滚动切分），**必须求和**——
    取 max 会丢掉绝大部分数据。这是实测踩过的坑。

    ``home`` 可以是 traces 目录路径，也可以直接是一个解析器实例
    （测试注入用），省得测试要去造真实的目录结构。
    """
    # 用类型判定而不是 hasattr：实例上的 ``discover`` 是**绑定方法**，
    # 再去当未绑定方法调用会丢掉 self。hasattr 对实例和类都为真，分不清。
    parser = home if isinstance(home, WorkBuddyTraceParser) or not isinstance(
        home, (str, Path, type(None))
    ) else WorkBuddyTraceParser(home)
    buckets: dict[str, dict[str, int]] = {}
    for path in parser.discover():
        data = parser.read_trace(path)
        if not data:
            continue
        sid = data["session_id"]
        b = buckets.setdefault(
            sid,
            {"input": 0, "cached": 0, "calls": 0, "files": 0},
        )
        b["input"] += data["total_input_tokens"]
        b["cached"] += data["total_cached_tokens"]
        b["calls"] += data["call_count"]
        b["files"] += 1

    return {
        sid: SessionCache(
            session_id=sid,
            input_tokens=b["input"],
            cached_tokens=b["cached"],
            call_count=b["calls"],
            file_count=b["files"],
        )
        for sid, b in buckets.items()
    }


def apportion_session(
    session: SessionCache,
    rows: list[sqlite3.Row],
    covered_requests: int,
    target: int | None = None,
) -> list[int]:
    """把会话的缓存读分摊到**前 covered_requests 条**记录上。

    为什么只摊一部分
    ----------------
    trace 只覆盖会话里的部分调用（实测有低至 10% 的）。如果把 trace 观测到的
    缓存总量平均摊到该会话的**全部**请求上，会把缓存量稀释到严重失真——
    实测某会话 trace 缓存率 93.8%，摊到全部 123 条后库内只剩31.9%。

    正确的做法是：缓存量只归属给**确实被 trace 观察到的那批请求**，
    没被观察到的请求保持0。这样「有缓存支撑的样本命中率」才等于 trace
    真实观测到的会话缓存率。

    ``target`` 用于**增量分摊**（幂等）：默认摊``session.cached_tokens``，
    重跑时传入「总量 − 库里已有量」，避免第二次把总量再翻一遍。
    """
    if not rows or session.input_tokens <= 0:
        return [0 for _ in rows]

    limit = max(1, min(covered_requests, len(rows)))
    target_rows = rows[:limit]
    rest = [0] * (len(rows) - limit)

    goal = session.cached_tokens if target is None else target
    if goal <= 0:
        return [0 for _ in rows]

    weights = [max(0, int(r["input_tokens"] or 0)) for r in target_rows]
    total_weight = sum(weights)
    if total_weight <= 0:
        # 没有输入 token 可按比例分：退化为按请求数均摊
        per = goal // limit
        shares = [per] * limit
        shares[-1] += goal - per * limit
        return shares + rest

    # 按覆盖部分的输入占比分摊
    raw = [Decimal(w) * dec(goal) / dec(total_weight) for w in weights]
    floors = [int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw]
    remainders = [x - int(x.to_integral_value(rounding=ROUND_FLOOR)) for x in raw]

    # 最大余数法补差，保证求和精确等于 goal（否则总量会漂）
    deficit = goal - sum(floors)
    if deficit > 0:
        order = sorted(range(len(raw)), key=lambda i: (-remainders[i], i))
        for k in range(deficit):
            floors[order[k % len(order)]] += 1
    return floors + rest


def backfill(
    conn: sqlite3.Connection, home: Any = None, *, apply: bool = False
) -> dict[str, Any]:
    """计算（或写入）缓存 token 补齐。

    ``apply=False``（默认）只算不写，用于 ``--dry-run``。
    """
    sessions = collect_sessions(home)
    stats = {
        "sessions": 0,
        "requests": 0,
        "cache_read_tokens": 0,
        "low_coverage_sessions": 0,
        "skipped_no_trace": 0,
        "samples": [],
    }

    for sid, session in sessions.items():
        if not session.trustworthy:
            continue
        rows = conn.execute(
            "SELECT request_id, ts_day, input_tokens, cache_read_tokens "
            "FROM usage_fact WHERE session_id = ? ORDER BY request_id",
            (sid,),
        ).fetchall()
        if not rows:
            continue

        # 幂等的关键：先算出「trace 观测到的总量」与「库里已有的量」，
        # 只把**差额**分摊下去。若简单地跳过已填的记录再按全量分摊一次，
        # 第二次跑就会翻倍（实测 9000 → 18000）。
        already = sum(int(r["cache_read_tokens"] or 0) for r in rows)
        remaining_target = session.cached_tokens - already
        if remaining_target <= 0:
            # 已经补齐（或超出）——什么都不做，这是幂等路径
            stats["sessions"] += 1
            continue

        pending = [r for r in rows if int(r["cache_read_tokens"] or 0) == 0]
        if not pending:
            continue

        # 只给 trace 实际观察到的那批请求分摊：trace 调用数 = 被观测的请求数。
        # 剩下的保持 0（未观测 ≠ 没有缓存，但我们没有证据，不能编）。
        covered = min(session.call_count, len(pending))
        if covered <= 0:
            stats["skipped_no_trace"] += 1
            continue

        cov = session.coverage(len(rows))
        # 按输入降序：被 trace 观测到的更可能是耗时较长的大请求，
        # 让它们承载缓存量比随机取前 N 条更贴近真实。
        pending.sort(key=lambda r: -int(r["input_tokens"] or 0))
        shares = apportion_session(session, pending, covered, target=remaining_target)
        if cov < LOW_COVERAGE:
            stats["low_coverage_sessions"] += 1

        stats["sessions"] += 1
        stats["requests"] += sum(1 for s in shares if s > 0)
        stats["cache_read_tokens"] += sum(shares)

        if len(stats["samples"]) < 8:
            stats["samples"].append(
                {
                    "session_id": sid[:36],
                    "trace_files": session.file_count,
                    "rate_pct": round(float(session.rate * 100), 1),
                    "coverage_pct": round(float(cov * 100), 1),
                    "requests": sum(1 for s in shares if s > 0),
                    "session_requests": len(pending),
                    "cache_read_tokens": sum(shares),
                    "trusted": cov >= LOW_COVERAGE,
                }
            )

        if apply:
            for row, share in zip(pending, shares):
                conn.execute(
                    "UPDATE usage_fact SET cache_read_tokens = ? WHERE request_id = ?",
                    (share, row["request_id"]),
                )

    return stats


def method_note(coverage: Decimal) -> str:
    """给前端的一句话说明：这个数字是怎么来的、可信度如何。"""
    pct = f"{(coverage * 100):.0f}%"
    if coverage < LOW_COVERAGE:
        return (
            f"缓存读由 traces 按会话缓存率分摊估算（trace 覆盖该会话 {pct} 的调用，"
            "属片段覆盖，结论为低可信）"
        )
    return f"缓存读由 traces 按会话缓存率分摊估算（trace 覆盖该会话 {pct} 的调用）"


def coverage_note(conn: sqlite3.Connection) -> dict[str, Any]:
    """全局覆盖率：有多少请求真的有 trace 支撑。

    这个数字必须跟着命中率一起展示。没有它，用户会把
    「43%（混合分母）」误读成「缓存命中率只有 43%」，
    而真实情况是「有 trace 的会话缓存率约 95%，只是 trace 只覆盖了 83% 的请求」。
    两者的结论方向完全不同。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN cache_read_tokens > 0 THEN 1 ELSE 0 END) AS covered "
        "FROM usage_fact"
    ).fetchone()
    total = int(row["total"] or 0)
    covered = int(row["covered"] or 0)
    if total == 0:
        return {"total_requests": 0, "covered_requests": 0, "coverage": Decimal(0)}
    return {
        "total_requests": total,
        "covered_requests": covered,
        "coverage": dec(covered) / dec(total),
    }