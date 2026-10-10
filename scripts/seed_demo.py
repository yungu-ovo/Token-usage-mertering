"""生成一份合成演示数据，用来看板「没有真实数据时」也能跑起来。

为什么需要它：

* 给非本机用户 / 首次拿到代码的人一个**不需要 WorkBuddy 数据**也能看全貌的入口；
* 演示数据把看板所有分支都走一遍：缓存命中、失败请求、未知模型、预算告警、多项目拆分，
  便于回归时肉眼核对；
* **只写独立库**（默认 ``data/demo.db``），绝不碰 ``data/usage.db``。要看演示数据，
  用 ``TUM_DB_PATH`` 指向演示库再起服务即可。

数据全部虚构：模型名用公共模型名（走内置价格表，成本可解释），项目名/会话名全部是
编造的占位符，不含任何真实业务信息。

用法::

    python scripts/seed_demo.py                 # 写入 data/demo.db（已存在则追加，幂等）
    python scripts/seed_demo.py --reset         # 先删掉演示库再重建
    python scripts/seed_demo.py --days 60       # 生成 60 天
    python scripts/seed_demo.py --db /tmp/x.db  # 换位置

    # 用演示数据启动看板
    TUM_DB_PATH=data/demo.db python -m uvicorn app.main:app --port 8787
"""

from __future__ import annotations

import argparse
import datetime as dt
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, db  # noqa: E402
from app.ingest import ingest_items  # noqa: E402
from app.models import IngestItem  # noqa: E402
from app.pricing import seed_builtin  # noqa: E402
from app.timeutil import LOCAL_TZ  # noqa: E402

# 固定种子：同样参数每次生成完全一样的数据，方便回归比对
SEED = 20261010

# 走内置价格表的公共模型名 -> 使用权重（贵的模型用得少，贴近真实形态）
MODELS: list[tuple[str, int]] = [
    ("gpt-5-mini", 34),
    ("gpt-5", 22),
    ("claude-haiku-4.5", 16),
    ("gemini-2.5-flash", 12),
    ("claude-sonnet-4.5", 9),
    ("gemini-2.5-pro", 4),
    ("deepseek-v3", 2),
    ("claude-opus-4.1", 1),
]

# 故意留两个「价格表里没有」的模型名，用来演示未定价洞察与台账（占比很低）
UNKNOWN_MODELS = ["某内部代号模型-alpha", "internal-trial-model-b"]

PROJECTS = ["demo-web-app", "demo-data-pipeline", "demo-docs-site"]
TOOLS = ["chat", "agent", "autocomplete"]

STATUS_WEIGHTS: list[tuple[str, int]] = [
    ("success", 95),
    ("error", 2),
    ("timeout", 2),
    ("throttled", 1),
]


def _pick(rng: random.Random, weighted: list[tuple[str, int]]) -> str:
    names = [n for n, _ in weighted]
    weights = [w for _, w in weighted]
    return rng.choices(names, weights=weights, k=1)[0]


def _tokens(rng: random.Random, model: str) -> dict[str, int]:
    """按模型档次给一组合理的 token 量级。"""
    if model in ("claude-opus-4.1", "gemini-2.5-pro", "gpt-5"):
        scale = 12
    elif model in ("claude-sonnet-4.5", "gpt-5-mini", "gemini-2.5-flash"):
        scale = 6
    else:
        scale = 3

    prompt = rng.randint(2_000, 9_000) * scale
    completion = rng.randint(300, 2_600) * scale

    # 真实场景里输入侧大量命中缓存（多轮对话 + 长系统提示复用）
    cache_read = 0
    if rng.random() < 0.62:
        cache_read = int(prompt * rng.uniform(0.25, 0.85))
    cache_write = 0
    if rng.random() < 0.18:
        cache_write = int(prompt * rng.uniform(0.05, 0.30))

    return {
        "input_tokens": prompt,
        "output_tokens": completion,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def build_items(days: int, rng: random.Random) -> list[IngestItem]:
    today = dt.datetime.now(tz=LOCAL_TZ).date()
    items: list[IngestItem] = []

    # 会话池：每个项目若干会话，让「按项目/按会话」两个维度都有内容
    sessions = {
        p: [f"sess-{p}-{i:02d}" for i in range(1, rng.randint(3, 6) + 1)]
        for p in PROJECTS
    }

    for back in range(days - 1, -1, -1):
        day = today - dt.timedelta(days=back)
        # 周末量少一点；整体有缓慢增长趋势
        factor = 0.45 if day.weekday() >= 5 else 1.0
        factor *= 0.75 + 0.5 * (days - back) / max(days, 1)
        count = max(3, int(rng.gauss(28 * factor, 6)))

        for _ in range(count):
            project = rng.choice(PROJECTS)
            model = _pick(rng, MODELS)
            # 2% 概率换成一个价格表里没有的模型名
            model = rng.choice(UNKNOWN_MODELS) if rng.random() < 0.02 else model

            hour = rng.choices(
                range(24),
                weights=[1, 1, 1, 1, 1, 2, 3, 5, 7, 8, 9, 8, 7, 8, 9, 9, 8, 6, 4, 3, 3, 2, 2, 1],
                k=1,
            )[0]
            minute = rng.randint(0, 59)
            second = rng.randint(0, 59)
            moment = dt.datetime(day.year, day.month, day.day, hour, minute, second, tzinfo=LOCAL_TZ)

            status = _pick(rng, STATUS_WEIGHTS)
            usage = _tokens(rng, model)
            if status != "success":
                # 失败/超时的请求也会产生输入侧消耗，但输出很少
                usage["output_tokens"] = rng.randint(0, 120)
                usage["cache_write_tokens"] = 0

            session = rng.choice(sessions[project])
            items.append(
                IngestItem.model_validate(
                    {
                        "request_id": f"demo-{day.isoformat()}-{len(items):06d}",
                        "ts": moment.isoformat(),
                        "provider": "demo",
                        "model": model,
                        "tool": rng.choice(TOOLS),
                        "project": project,
                        "session_id": session,
                        "user_id": "demo-user",
                        "machine_id": "demo-machine",
                        "billing_mode": "api",
                        "usage": usage,
                        "latency_ms": (
                            rng.randint(400, 9_000)
                            if status == "success"
                            else rng.randint(9_000, 60_000)
                        ),
                        "status": status,
                    }
                )
            )

    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="生成合成演示数据（独立库，不碰真实数据）")
    ap.add_argument("--db", default=str(ROOT / "data" / "demo.db"))
    ap.add_argument("--days", type=int, default=45, help="生成最近多少天的数据")
    ap.add_argument("--reset", action="store_true", help="先删除演示库再重建")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    target = Path(args.db)
    real = Path(config.DB_PATH)
    if target.resolve() == real.resolve():
        print(f"拒绝执行：--db 指向真实库 {real}。演示数据必须写到独立库。")
        return 1

    if args.reset:
        for suffix in ("", "-wal", "-shm"):
            p = target.with_name(target.name + suffix)
            if p.exists():
                p.unlink()
        print(f"已清空 {target}")

    target.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(target)
    with db.tx(target) as conn:
        seed_builtin(conn)

    rng = random.Random(args.seed)
    items = build_items(args.days, rng)

    with db.tx(target) as conn:
        summary = ingest_items(conn, items, data_source=config.DATA_SOURCE_DEMO)

    with db.session(target) as conn:
        rows = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
        span = conn.execute(
            "SELECT MIN(ts_day) AS lo, MAX(ts_day) AS hi, "
            "ROUND(SUM(equivalent_cost_usd_micro)/1000000.0, 2) AS usd FROM usage_fact"
        ).fetchone()

    print(
        f"\n演示数据已写入 {target}\n"
        f"  生成 {len(items):,} 条 / 落库 {rows:,} 行（去重后）\n"
        f"  区间 {span['lo']} ~ {span['hi']}\n"
        f"  API 等价成本合计 ${span['usd']}\n"
        f"  未定价模型 {len(summary['unknown_models'])} 个：{summary['unknown_models']}\n"
        "\n用演示数据启动看板：\n"
        f"  TUM_DB_PATH={target} python -m uvicorn app.main:app --port 8787\n"
        "\n（演示库与真实库 data/usage.db 完全隔离，看板顶部会显示「示例数据」角标）"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
