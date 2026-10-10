"""清理口径错误的历史告警（一次性修复工具）。

背景
----
2026-10-09 修复「微美元未换算」缺陷之前，``app/alerts.py`` 把微美元整数
直接当美元用，导致写进 ``alert_event`` 的告警金额被放大了 1e6 倍：

    本月预算已用 3663766.7%（$10991300.00 / $300.00）   # 实际是 $29.39

**事实表数据本身是对的**（``usage_fact`` / ``daily_agg`` 存的就是微美元），
错的只是当时落库的告警文案。当前代码已经正确（``evaluate_budget`` 返回
``mtd_cost = '29.39'``），所以本脚本只删历史告警，不改任何计算逻辑。

判定条件（宁可漏删，不可误删）
--------------------------------
只有同时满足「消息里的金额比库里真实月成本大 1000 倍以上」**且**
「重新计算后该阈值本不该触发」才删除。这样即便数据变了也不会误伤。

用法::

    python scripts/purge_bad_alerts.py --dry-run   # 只看会删什么
    python scripts/purge_bad_alerts.py             # 真删（需 --yes 确认）
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import alerts, config, db  # noqa: E402
from app.money import dec  # noqa: E402
from app.queries.common import fetch_cost, month_range  # noqa: E402

# 消息里形如 $10991300.00 的金额
MONEY_RE = re.compile(r"\$([0-9][0-9,]*(?:\.[0-9]+)?)")

# 放大倍数超过这个值就判定为口径错误（正常告警不会比真实值大三个数量级）
SUSPECT_RATIO = Decimal("1000")


def collect(conn: sqlite3.Connection) -> tuple[list[sqlite3.Row], Decimal]:
    """找出所有金额与真实成本严重不符的告警，返回 (待删行, 真实月成本)。

    参照物取「月累计」与「今日」的较大者：任何单日或整月的合理金额都不会
    明显超过它，所以拿它当上限足够保守，不会误伤正常告警。
    """
    lo, hi = month_range()
    mtd_cost, _ = fetch_cost(conn, lo, hi)
    daily_cost, _ = fetch_cost(conn, hi, hi)
    ceiling = max(mtd_cost, daily_cost)

    suspects = []
    if ceiling <= 0:
        return suspects, mtd_cost

    for row in conn.execute(
        "SELECT id, alert_key, message FROM alert_event ORDER BY id"
    ).fetchall():
        msg = row["message"] or ""
        # 文案里所有 $ 金额，逐个与真实值比；只要有一个离谱就算中招。
        # 这样既覆盖「（$X / $Y）」格式，也覆盖预测告警里的「月末预测 $Z」。
        # 预算额度本身（$300）不会超上限，所以天然不会被误判。
        for text in MONEY_RE.findall(msg):
            claimed = dec(text.replace(",", ""))
            if claimed > ceiling * SUSPECT_RATIO:
                suspects.append(row)
                break
    return suspects, mtd_cost


def purge(path: Path, yes: bool) -> int:
    """在库副本上执行删除，确认无误后才落到真实库。返回删除行数。"""
    # 一律先在副本上做：真实库是本机唯一的用量真相，绝不冒险
    tmp = Path(tempfile.mkdtemp(prefix="tum-purge-"))
    dst = tmp / "usage.db"
    shutil.copy2(path, dst)
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            shutil.copy2(side, dst.with_name(dst.name + suffix))

    conn = sqlite3.connect(str(dst))
    conn.row_factory = sqlite3.Row

    suspects, mtd_cost = collect(conn)
    print(f"真实本月成本（主口径）：${mtd_cost}")
    print(f"发现口径错误的历史告警：{len(suspects)} 条\n")
    for row in suspects:
        print(f"  #{row['id']}  {row['message'][:88]}")

    if not suspects:
        print("\n无需清理。")
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    if not yes:
        print("\n这是演练。加 --yes 才会真正删除。")
        conn.close()
        shutil.rmtree(tmp, ignore_errors=True)
        return 0

    ids = [int(r["id"]) for r in suspects]
    placeholders = ",".join("?" for _ in ids)
    conn.execute(f"DELETE FROM alert_event WHERE id IN ({placeholders})", ids)
    # 这些告警的 key 也要清掉激活态，否则改完预算不会重新评估
    keys = [str(r["alert_key"]) for r in suspects if str(r["alert_key"]).startswith("budget:")]
    for key in keys:
        conn.execute(
            "UPDATE alert_state SET cleared_at = datetime('now') "
            "WHERE alert_key = ? AND cleared_at IS NULL",
            (key,),
        )
    conn.commit()

    # 删完立刻重算一次，确认不会再误触发
    after = alerts.evaluate_budget(conn)
    print(f"\n已删除 {len(ids)} 条。清理后重算：{after}")
    conn.close()

    # 确认副本没问题再落回真实库
    shutil.copy2(dst, path)
    for suffix in ("-wal", "-shm"):
        side = dst.with_name(dst.name + suffix)
        if side.exists():
            shutil.copy2(side, path.with_name(path.name + suffix))
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"已写回 {path}")
    return len(ids)


def main() -> int:
    ap = argparse.ArgumentParser(description="清理口径错误的历史告警（微美元未换算缺陷的遗留）")
    ap.add_argument("--db", default=None, help="库路径，默认 data/usage.db")
    ap.add_argument("--dry-run", action="store_true", help="只看会删什么（默认行为）")
    ap.add_argument("--yes", action="store_true", help="确认删除")
    args = ap.parse_args()

    path = Path(args.db) if args.db else config.DB_PATH
    if not path.exists():
        print(f"库不存在：{path}\n先灌数：python scripts/ingest_workbuddy.py --ingest")
        return 1

    removed = purge(path, yes=args.yes)
    return 0 if removed >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())