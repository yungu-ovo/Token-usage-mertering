"""FastAPI 应用：路由 + 静态前端。

API 路由全部注册在静态挂载之前——Starlette 按注册顺序匹配，
所以 ``/api/*`` 不会被挂在 ``/`` 的 StaticFiles 抢先。
"""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import __version__, alerts, collect as collect_mod, config, currency, db, scheduler
from .aggregator import rebuild_all
from .ingest import ingest_items
from .timeutil import now_local_str
from .models import (
    BudgetUpdate,
    CreditRateUpdate,
    IngestItem,
    PricingOverride,
)
from .queries import details, insights, kpi, mix, series

log = logging.getLogger("tum")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    with db.session() as conn:
        from .pricing import seed_builtin

        seed_builtin(conn)
    scheduler.start()
    log.info("Token 消耗看板已就绪 → http://%s:%s", config.HOST, config.PORT)
    try:
        yield
    finally:
        scheduler.shutdown()


app = FastAPI(
    title="Token 消耗看板",
    version=__version__,
    description="本地优先的 LLM 用量与成本采集服务。所有金额以微美元整数存储，禁用浮点。",
    lifespan=lifespan,
)


def _conn():
    """每次请求开一个连接。SQLite + WAL 在单机低并发下足够。"""
    return db.session()


# ================================================================ meta
@app.get("/api/meta", tags=["meta"])
def api_meta() -> dict[str, Any]:
    with _conn() as conn:
        from .db import get_meta
        from .pricing import current_version

        rate, calibrated = db.get_credit_rate(conn)
        parser = {
            "version": db.get_meta(conn, alerts.META_PARSER_VERSION),
            "success_rate": db.get_meta(conn, alerts.META_PARSE_RATE),
            "scanned": int(db.get_meta(conn, alerts.META_PARSER_SCANNED) or 0),
            "usage_lines": int(db.get_meta(conn, alerts.META_PARSER_USAGE_LINES) or 0),
            "parsed": int(db.get_meta(conn, alerts.META_PARSER_PARSED) or 0),
            "min_success_rate": str(config.PARSE_SUCCESS_MIN),
            "last_collect_at": db.get_meta(conn, "last_collect_at"),
        }
        parser["healthy"] = (
            parser["success_rate"] == ""
            or Decimal(parser["success_rate"]) >= config.PARSE_SUCCESS_MIN
        )
        return {
            "app": config.APP_NAME,
            "version": __version__,
            "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
            "pricing_version": current_version(conn),
            "data_source": get_meta(conn, "data_source", config.DATA_SOURCE_DEMO),
            "credit_rate": {"rate": rate, "calibrated": calibrated},
            "currencies": currency.supported(conn),
            "fx": currency.fx_block(conn),
            "scheduler_enabled": scheduler.enabled(),
            # A-4：没有这个，旧数据和新数据在界面上长得一模一样
            "data_freshness": collect_mod.data_freshness(conn),
            # B-2：解析器版本与成功率，数据源改格式时这里会先掉下来
            "parser": parser,
            "auto_collect": config.AUTO_COLLECT,
            # P0-B：自动检测与记录的运行态（顶栏「实时监控中」芯片的数据源）
            "watcher": _watcher_block(conn),
        }


def _watcher_block(conn) -> dict[str, Any]:
    """采集监控的运行态。

    ``stalled`` 由前端按「开启但 last_scan_at 老于 3 倍间隔」即时判定——
    等服务端每小时巡检才发现就太晚了；服务端的 ``collector:stalled``
    告警作为持久记录兜底。
    """
    from .db import get_meta
    from .timeutil import now_local

    last_scan_at = get_meta(conn, collect_mod.META_WATCH_LAST_SCAN_AT)
    stalled = False
    if config.AUTO_COLLECT and last_scan_at:
        try:
            moment = dt.datetime.fromisoformat(last_scan_at)
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=now_local().tzinfo)
            age_s = (now_local() - moment).total_seconds()
            stalled = age_s > max(300, config.WATCH_INTERVAL_SECONDS * 3)
        except (TypeError, ValueError):
            stalled = False
    freshness = collect_mod.data_freshness(conn)
    return {
        "enabled": config.AUTO_COLLECT,
        "interval_seconds": config.WATCH_INTERVAL_SECONDS,
        "calibration_minutes": config.CALIBRATION_MINUTES,
        "last_scan_at": last_scan_at,
        "last_scan_ms": int(get_meta(conn, collect_mod.META_WATCH_LAST_SCAN_MS) or 0),
        "last_new_records": int(
            get_meta(conn, collect_mod.META_WATCH_LAST_NEW_RECORDS) or 0
        ),
        "tracked_files": int(get_meta(conn, collect_mod.META_WATCH_TRACKED_FILES) or 0),
        "detect_latency_seconds": freshness.get("detect_latency_seconds"),
        "stalled": stalled,
    }


# ================================================================ 采集
@app.post("/api/ingest", tags=["ingest"])
def api_ingest(payload: dict = Body(...)) -> dict[str, Any]:
    """接受单条记录，或 ``{"items": [...]}`` 批量形式。"""
    raw_items = payload.get("items")
    if raw_items is None:
        if "usage" in payload or "ts" in payload:
            raw_items = [payload]
        else:
            raise HTTPException(status_code=422, detail="请求体既不是 items 数组，也不是单条记录")

    if not isinstance(raw_items, list):
        raise HTTPException(status_code=422, detail="items 必须是数组")

    items: list[IngestItem] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_items):
        try:
            items.append(IngestItem.model_validate(raw))
        except ValidationError as exc:
            errors.append({"index": index, "error": exc.errors(include_url=False)})

    if not items:
        return JSONResponse(status_code=422, content={"detail": "没有任何合法记录", "errors": errors})

    data_source = payload.get("data_source")
    with db.tx() as conn:
        summary = ingest_items(conn, items, data_source=data_source)
        summary["budget_alerts"] = alerts.evaluate_budget(conn)["fired"]
        alerts.evaluate_unpriced(conn)

    summary["errors"] = errors
    return summary


# ================================================================ 看板数据
@app.get("/api/kpi", tags=["dashboard"])
def api_kpi(days: int = Query(default=7, ge=1, le=90)) -> dict[str, Any]:
    with _conn() as conn:
        return kpi.build(conn, days=days)


@app.get("/api/trend", tags=["dashboard"])
def api_trend(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    with _conn() as conn:
        return series.trend(conn, days=days)


@app.get("/api/tokens", tags=["dashboard"])
def api_tokens(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    with _conn() as conn:
        return series.tokens_daily(conn, days=days)


@app.get("/api/mix", tags=["dashboard"])
def api_mix(
    dim: str = Query(default="model", pattern="^(model|project|tool|session)$"),
    days: int = Query(default=30, ge=1, le=365),
    top: int = Query(default=8, ge=1, le=50),
) -> dict[str, Any]:
    with _conn() as conn:
        return mix.by_dimension(conn, dim=dim, days=days, top=top)


@app.get("/api/insights", tags=["dashboard"])
def api_insights(days: int = Query(default=30, ge=1, le=365)) -> dict[str, Any]:
    with _conn() as conn:
        return insights.build(conn, days=days)


@app.get("/api/requests", tags=["dashboard"])
def api_requests(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    model: str | None = None,
    project: str | None = None,
    tool: str | None = None,
    session_id: str | None = None,
    status: str | None = None,
    days: int | None = Query(default=None, ge=1, le=365),
    cache: str | None = Query(default=None, pattern="^(read|write)$"),
) -> dict[str, Any]:
    with _conn() as conn:
        return details.requests(
            conn,
            limit=limit,
            offset=offset,
            model=model,
            project=project,
            tool=tool,
            session_id=session_id,
            status=status,
            days=days,
            cache=cache,
        )


@app.get("/api/drill/{model}/sessions", tags=["dashboard"])
def api_drill_model(
    model: str, days: int = Query(default=30, ge=1, le=365)
) -> dict[str, Any]:
    with _conn() as conn:
        return details.sessions_for_model(conn, model, days=days)


# ================================================================ 配置
@app.get("/api/budget", tags=["config"])
def api_get_budget() -> dict[str, Any]:
    with _conn() as conn:
        from .money import dec, money_str

        daily, monthly, cur = db.get_budget(conn)
        return {
            "daily_usd": money_str(dec(daily) / 1000000),
            "monthly_usd": money_str(dec(monthly) / 1000000),
            "currency": cur,
        }


@app.put("/api/budget", tags=["config"])
def api_put_budget(body: BudgetUpdate) -> dict[str, Any]:
    with _conn() as conn:
        daily, monthly, cur = db.get_budget(conn)
        from .money import dec, money_str

        new_daily = body.daily_usd if body.daily_usd is not None else dec(daily) / 1000000
        new_monthly = body.monthly_usd if body.monthly_usd is not None else dec(monthly) / 1000000
        db.set_budget(conn, new_daily, new_monthly, body.currency or cur)
        # 预算变了，重新评估一遍阈值（可能立刻触发）
        fired = alerts.evaluate_budget(conn)
        return {
            "daily_usd": money_str(new_daily),
            "monthly_usd": money_str(new_monthly),
            "currency": body.currency or cur,
            "alerts_fired": fired["fired"],
        }


@app.get("/api/pricing", tags=["config"])
def api_pricing() -> dict[str, Any]:
    with _conn() as conn:
        from .pricing import current_version, list_unknown

        rows = conn.execute(
            "SELECT pricing_version, model, effective_from, effective_to, "
            "input_per_mtok, output_per_mtok, cache_read_per_mtok, cache_write_per_mtok, source "
            "FROM price_table ORDER BY model, effective_from DESC"
        ).fetchall()
        return {
            "current_version": current_version(conn),
            "models": [dict(r) for r in rows],
            "unknown_models": list_unknown(conn),
        }


@app.put("/api/pricing/override", tags=["config"])
def api_pricing_override(body: PricingOverride) -> dict[str, Any]:
    with _conn() as conn:
        from .pricing import override

        version = override(
            conn,
            model=body.model,
            input_per_mtok=body.input_per_mtok,
            output_per_mtok=body.output_per_mtok,
            cache_read_per_mtok=body.cache_read_per_mtok,
            cache_write_per_mtok=body.cache_write_per_mtok,
            effective_from=body.effective_from,
        )
        return {"model": body.model, "pricing_version": version, "note": "老版本未被修改，历史成本不变"}


@app.put("/api/pricing/credit-rate", tags=["config"])
def api_credit_rate(body: CreditRateUpdate) -> dict[str, Any]:
    with _conn() as conn:
        now = now_local_str()
        conn.execute(
            "INSERT INTO credit_rate (id, rate, calibrated, updated_at) VALUES (1, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET rate = excluded.rate, "
            "calibrated = excluded.calibrated, updated_at = excluded.updated_at",
            (str(body.rate), 1 if body.calibrated else 0, now),
        )
        return {"rate": str(body.rate), "calibrated": body.calibrated}


@app.get("/api/currency", tags=["config"])
def api_currency() -> dict[str, Any]:
    with _conn() as conn:
        return {"supported": currency.supported(conn), "rates": currency.list_rates(conn)}


# ================================================================ 告警
@app.get("/api/alerts", tags=["alerts"])
def api_alerts(
    limit: int = Query(default=50, ge=1, le=500), unacked: bool = False
) -> dict[str, Any]:
    with _conn() as conn:
        return {"items": alerts.recent(conn, limit=limit, unacked_only=unacked)}


@app.post("/api/alerts/{alert_id}/ack", tags=["alerts"])
def api_alert_ack(alert_id: int) -> dict[str, Any]:
    with _conn() as conn:
        ok = alerts.acknowledge(conn, alert_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"告警 {alert_id} 不存在")
    return {"id": alert_id, "acknowledged": True}


# ================================================================ 运维
@app.post("/api/refresh", tags=["ops"])
def api_refresh() -> dict[str, Any]:
    """重算聚合 + 跑一次告警巡检。

    注意：这里**不采集**。要拉新数据请用 ``POST /api/collect``——
    早先两者混在一个按钮上，于是「重新采集」其实什么都没采（A-2）。
    """
    with db.tx() as conn:
        rows = rebuild_all(conn)
        result = alerts.evaluate_scheduled(conn)
    return {"agg_rows": rows, "alerts": result}


@app.post("/api/collect", tags=["ops"])
def api_collect(
    since: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    dry_run: bool = False,
    backfill_cache: bool = True,
    full: bool = False,
) -> dict[str, Any]:
    """真·采集：解析本机会话转录 → 幂等落库 → 重算 → 告警巡检。

    默认走**增量**（文件水位，只读新增行）；``full=1`` 强制全量重扫
    （修了 parser、怀疑水位漂移时用）。``dry_run=1`` 只解析不写库，
    且固定按全量口径演练（「看看还有多少没采」），用于接口自检。
    """
    if dry_run:
        with db.session() as conn:
            return collect_mod.collect(conn, since=since, dry_run=True)
    # 手动采集优先于定时轮：排队等锁而不是报 "database is locked"
    if not collect_mod.COLLECT_LOCK.acquire(timeout=120):
        raise HTTPException(status_code=503, detail="另一轮采集正在进行，请稍后再试")
    try:
        with db.tx() as conn:
            return collect_mod.collect(
                conn, since=since, backfill_cache=backfill_cache, incremental=not full
            )
    finally:
        collect_mod.COLLECT_LOCK.release()


@app.get("/api/health", tags=["ops"])
def api_health() -> dict[str, Any]:
    with _conn() as conn:
        fact = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
        agg = conn.execute("SELECT COUNT(*) AS c FROM daily_agg").fetchone()["c"]
    return {"ok": True, "usage_fact_rows": int(fact), "daily_agg_rows": int(agg)}


# ================================================================ 静态前端
# 必须放在所有 API 路由之后
@app.get("/", include_in_schema=False)
def index() -> RedirectResponse:
    """根路径直接进看板，免得用户要记住文件名。"""
    return RedirectResponse(url="/dashboard.html", status_code=307)


if config.WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=str(config.WEB_DIR), html=True), name="web")
else:  # pragma: no cover
    log.warning("前端目录不存在: %s", config.WEB_DIR)


def main() -> None:  # pragma: no cover
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=False)


if __name__ == "__main__":  # pragma: no cover
    main()
