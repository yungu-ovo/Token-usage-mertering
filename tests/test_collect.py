"""采集流水线与数据新鲜度。

B-1：这条流水线早先只活在 scripts/ 里，服务层想采也采不了，
于是「重新采集」按钮其实什么都没采（A-2）。抽进 app/collect.py 之后，
脚本与接口必须跑**同一条路径**，这里就是那个约束的守门人。
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from app import collect as collect_mod, db
from app.ingest import ingest_items
from app.models import IngestItem
from app.timeutil import now_local

FIXTURES = Path(__file__).parent / "fixtures"


def _item(rid: str, ts: str) -> IngestItem:
    return IngestItem.model_validate(
        {
            "request_id": rid,
            "ts": ts,
            "provider": "workbuddy",
            "model": "gpt-5",
            "session_id": "s-" + rid,
            "usage": {"input_tokens": 1000, "output_tokens": 10},
        }
    )


# ================================================================== 解析健康度
def test_parser_reports_stats_on_fixture():
    """解析器必须顺带产出成功率——RK-07 的监控不能是空的。

    夹具刻意混进了坏行、全 0 token、缺 id 三种边缘样本，
    所以成功率**不该**是 100%——它得如实反映「有多少该解析的没解析出来」。
    """
    records, stats = collect_mod.parse_records(home=FIXTURES)
    assert records, "夹具里应当能解析出记录"
    assert stats.scanned == 14 and stats.bad_lines == 1
    assert stats.usage_lines == 11
    assert stats.parsed == len(records) == 9
    assert stats.rate() == pytest.approx(9 / 11)
    assert stats.parser_version and stats.parser_version != "0"
    # 跳过原因要能区分，否则事后无从判断是格式变了还是数据本来就这样
    assert stats.skipped["zero_tokens"] == 1
    assert stats.skipped["missing_id"] == 1


def test_parser_stats_survive_into_meta(conn):
    from app import alerts

    collect_mod._record_parser_stats(
        conn, collect_mod.parse_records(home=FIXTURES)[1]
    )
    assert db.get_meta(conn, alerts.META_PARSER_VERSION)
    assert db.get_meta(conn, alerts.META_PARSE_RATE) == f"{9 / 11:.6f}"
    assert int(db.get_meta(conn, alerts.META_PARSER_USAGE_LINES)) == 11


# ================================================================== A-4 新鲜度
def test_freshness_on_empty_db(conn):
    fresh = collect_mod.data_freshness(conn)
    assert fresh["has_data"] is False
    assert fresh["is_stale"] is False
    assert fresh["freshness_hours"] is None


def test_freshness_is_fresh_right_after_ingest(conn):
    ingest_items(conn, [_item("r1", now_local().isoformat())])
    fresh = collect_mod.data_freshness(conn)
    assert fresh["has_data"] is True
    assert fresh["is_stale"] is False
    assert fresh["freshness_hours"] is not None and fresh["freshness_hours"] < 1


def test_freshness_flags_stale_data(conn):
    old = (now_local() - dt.timedelta(days=3)).isoformat()
    ingest_items(conn, [_item("r2", old)])
    fresh = collect_mod.data_freshness(conn)
    assert fresh["is_stale"] is True
    assert fresh["freshness_hours"] >= 24
    assert "滞后" in fresh["note"]


# ================================================================== B-1 采集
def test_collect_dry_run_writes_nothing(conn):
    """演练模式连 meta 都不该写——「看一眼」不能改变状态。"""
    summary = collect_mod.collect(conn, home=FIXTURES, dry_run=True)
    assert summary["dry_run"] is True
    assert summary["parsed"] > 0
    assert conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"] == 0
    assert db.get_meta(conn, "last_collect_at") == ""


def test_collect_end_to_end(conn):
    summary = collect_mod.collect(
        conn, home=FIXTURES, backfill_cache=False, incremental=False
    )
    assert summary["parsed"] > 0
    ing = summary["ingest"]
    assert ing["received"] == summary["parsed"]
    assert ing["inserted"] > 0
    assert conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"] > 0
    assert db.get_meta(conn, "last_collect_at"), "采集时间要写进 meta"
    assert summary["freshness"]["has_data"] is True


def test_collect_is_idempotent(conn):
    """跑两次不该翻倍：幂等去重是 RK-02。"""
    first = collect_mod.collect(
        conn, home=FIXTURES, backfill_cache=False, incremental=False
    )
    before = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    second = collect_mod.collect(
        conn, home=FIXTURES, backfill_cache=False, incremental=False
    )
    after = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    assert after == before, "重复采集不该让行数变化"
    assert second["ingest"]["inserted"] == 0
    assert (
        second["ingest"]["updated"] + second["ingest"]["ignored"]
        == first["ingest"]["received"]
    )


def test_collect_with_no_records_still_patrols(conn):
    """没有新记录也要跑巡检——解析成功率可能刚刚跌破阈值。"""
    summary = collect_mod.collect(conn, home=Path(FIXTURES) / "不存在的目录")
    assert summary["parsed"] == 0
    assert "alerts" in summary and "parser" in summary["alerts"]


# ================================================================== P0-B 双模采集
def test_collect_incremental_reads_new_lines_only(conn, tmp_path, monkeypatch):
    """增量模式：第二轮没有新内容时不该重读历史（水位生效）。"""
    from app.sources import incremental

    home = tmp_path / "wbhome"
    projects = home / "projects" / "c-demo-Proj"
    projects.mkdir(parents=True)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_HOME", home)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_PROJECTS_DIR", projects)

    def write(mid: str) -> None:
        with open(projects / "s.jsonl", "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({
                "type": "assistant", "status": "success", "timestamp": 1791818958709,
                "sessionId": "sess-b", "cwd": "C:\\demo\\Proj",
                "providerData": {"messageId": mid, "requestModelName": "gpt-5",
                                 "rawUsage": {"prompt_tokens": 1000, "completion_tokens": 5}},
            }) + "\n")

    first = collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert first["mode"] == "incremental"
    assert first["parsed"] == 0  # 还没有任何文件

    write("b1")
    second = collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert second["parsed"] == 1 and second["ingest"]["inserted"] == 1

    write("b2")
    third = collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert third["parsed"] == 1, "第三轮只该读到新增的一行"
    assert third["ingest"]["inserted"] == 1
    assert third["ingest"]["updated"] == 0

    # 幂等：再跑一轮，0 条新记录，行数不变
    rows = conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"]
    fourth = collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert fourth["parsed"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM usage_fact").fetchone()["c"] == rows


def test_collect_incremental_writes_watcher_meta(conn, tmp_path, monkeypatch):
    from app.sources import incremental

    home = tmp_path / "wbhome"
    projects = home / "projects" / "c-demo-Proj"
    projects.mkdir(parents=True)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_HOME", home)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_PROJECTS_DIR", projects)
    with open(projects / "s.jsonl", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({
            "type": "assistant", "status": "success", "timestamp": 1791818958709,
            "sessionId": "sess-w", "cwd": "C:\\demo\\Proj",
            "providerData": {"messageId": "w1", "requestModelName": "gpt-5",
                             "rawUsage": {"prompt_tokens": 100, "completion_tokens": 5}},
        }) + "\n")

    collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert db.get_meta(conn, collect_mod.META_WATCH_LAST_SCAN_AT)
    assert db.get_meta(conn, collect_mod.META_WATCH_LAST_NEW_RECORDS) == "1"
    assert db.get_meta(conn, collect_mod.META_WATCH_TRACKED_FILES) == "1"


def test_collect_incremental_skips_rebuild_all(conn, monkeypatch):
    """增量轮不做全量重建：ingest_items 已按 touched days 重建过。"""
    calls = {"n": 0}

    def spy(c):
        calls["n"] += 1
        return 0

    monkeypatch.setattr(collect_mod, "rebuild_all", spy)
    collect_mod.collect(
        conn, home=FIXTURES, incremental=True, backfill_cache=False
    )
    assert calls["n"] == 0, "增量轮不该触发 rebuild_all"


def test_collect_parser_version_bump_forces_full_rescan(conn, monkeypatch):
    """版本自愈：解析器升版本后，增量轮自动升级为全量重扫一次。"""
    from app.sources import incremental

    monkeypatch.setattr(
        db, "get_meta",
        lambda c, k, d="": "1" if k == incremental.META_PARSER_VERSION_KEY else d,
    )
    summary = collect_mod.collect(
        conn, home=FIXTURES, incremental=True, backfill_cache=False
    )
    assert summary["mode"] == "full", "解析器版本变化必须触发全量重扫"
    assert summary["parsed"] > 0


def test_collect_incremental_without_usage_lines_keeps_parse_health(
    conn, tmp_path, monkeypatch
):
    """增量轮只读到非用量行时，不许把解析成功率写成 0.000000 假亮红灯。

    实测踩过：增量轮扫到 1 行用户消息（usage_lines=0），rate() 的
    「usage_lines or scanned」回退把成功率算成 0/1，/api/meta 的解析器
    健康假装装报警。没有样本 ≠ 0% 成功率。
    """
    from app.sources import incremental

    home = tmp_path / "wbhome"
    projects = home / "projects" / "c-demo-Proj"
    projects.mkdir(parents=True)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_HOME", home)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_PROJECTS_DIR", projects)

    # 先跑一轮全量，建立权威解析健康度
    first = collect_mod.collect(
        conn, home=FIXTURES, incremental=False, backfill_cache=False
    )
    assert first["mode"] == "full"
    good_rate = db.get_meta(conn, "parse_success_rate")
    # 夹具故意混了坏行/全0/缺id，成功率 9/11 ≈ 0.818——这里只要「是个正常的样本率」
    assert good_rate and float(good_rate) > 0.8

    # 再造一个只含非用量行的文件，让增量轮扫到它
    (projects / "noise.jsonl").write_bytes(
        b'{"type":"user","timestamp":1791818958709,"sessionId":"sess-n",'
        b'"cwd":"C:\\\\demo\\\\Proj","message":{"role":"user","content":"hi"}}\n'
    )
    second = collect_mod.collect(conn, incremental=True, backfill_cache=False)
    assert second["mode"] == "incremental"
    assert second["parsed"] == 0

    # 关键断言：解析成功率没有被这轮没有样本的扫描污染
    assert db.get_meta(conn, "parse_success_rate") == good_rate
    assert db.get_meta(conn, "last_collect_at"), "last_collect_at 仍要刷新（停滞告警靠它）"
