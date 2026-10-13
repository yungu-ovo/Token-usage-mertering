"""文件级增量扫描（P0-B）：半行保护、截断检测、幂等、版本自愈。

这里守的是四件事，任何一件被破坏都会让「调用即记录」变成「调用即丢失」：

1. **半行保护**：文件正被写入时，末尾的残行不许被消费——否则那条用量
   会被当成坏行跳过，永久少算（RK-07 的变体）。
2. **截断/轮转检测**：文件变小说明被替换，必须从头读，不能停在旧 offset 上。
3. **幂等**：同一文件重复扫描，第二轮必须 0 条新记录（水位生效）。
4. **只读新增**：第二轮的 stats 只反映新增行，不把历史行算进本轮分母。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from app import db
from app.pricing import seed_builtin
from app.sources import incremental
from app.sources.workbuddy_jsonl import WorkBuddyJsonlParser, parse_file_from
from app.sources.base import ParseStats


@pytest.fixture()
def conn(tmp_path):
    path = tmp_path / "inc-test.db"
    db.init_db(path)
    manager = db.session(path)
    connection = manager.__enter__()
    seed_builtin(connection)
    yield connection
    manager.__exit__(None, None, None)


@pytest.fixture()
def wb_home(tmp_path, monkeypatch):
    """造一个假的 WORKBUDDY_HOME，返回 projects 目录。"""
    home = tmp_path / "wbhome"
    projects = home / "projects" / "c-Users-demo-Proj"
    projects.mkdir(parents=True)
    monkeypatch.setattr(incremental.config, "WORKBUDDY_HOME", home)
    monkeypatch.setattr(
        incremental.config, "WORKBUDDY_PROJECTS_DIR", projects
    )
    return projects


def _line(mid: str, inp: int = 1000, out: int = 10, ts: float | None = None) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "status": "success",
            "timestamp": ts if ts is not None else 1791818958709,
            "sessionId": "sess-inc",
            "cwd": "C:\\Users\\demo\\Proj",
            "providerData": {
                "messageId": mid,
                "requestModelName": "gpt-5",
                "rawUsage": {"prompt_tokens": inp, "completion_tokens": out},
            },
        }
    )


def _write_lf(path: Path, *lines: str) -> None:
    """按 LF 写文件——真实数据源就是 LF；Windows 的 write_text 会转成
    CRLF，那会让「按 \\n 数偏移」的测试算出错误的期望值。"""
    path.write_bytes(("\n".join(lines) + "\n").encode("utf-8"))


# ------------------------------------------------------------------ 半行保护
def test_parse_file_from_does_not_consume_partial_line(tmp_path):
    """半行不消费：残行留给下一轮，offset 停在上一个完整行末。"""
    path = tmp_path / "s.jsonl"
    line2 = _line("m2")
    path.write_bytes((_line("m1") + "\n" + line2[:-1]).encode("utf-8"))

    stats = ParseStats(parser_name="t", parser_version="1")
    records, new_offset = parse_file_from(path, 0, stats)

    assert len(records) == 1, "只应读出第一条完整行"
    assert records[0].raw_id == "m1"
    assert new_offset == len((_line("m1") + "\n").encode("utf-8"))
    # 半行未被计为坏行——它只是还没写完
    assert stats.bad_lines == 0

    # 补全后半行后再读：从原 offset 继续，拿到第二条
    with open(path, "ab") as fh:
        fh.write((line2[-1:] + "\n").encode("utf-8"))
    records2, offset2 = parse_file_from(path, new_offset, stats)
    assert len(records2) == 1 and records2[0].raw_id == "m2"
    assert offset2 == path.stat().st_size


def test_parse_file_from_accepts_crlf_files(tmp_path):
    """Windows 风格 CRLF 也要能读：\\r 留在行尾，被 strip 掉，不污染字段。"""
    path = tmp_path / "s.jsonl"
    path.write_bytes((_line("m1") + "\r\n" + _line("m2") + "\r\n").encode("utf-8"))
    stats = ParseStats(parser_name="t", parser_version="1")
    records, new_offset = parse_file_from(path, 0, stats)
    assert [r.raw_id for r in records] == ["m1", "m2"]
    assert new_offset == path.stat().st_size
    assert stats.bad_lines == 0


def test_parse_file_from_no_newline_returns_offset_unchanged(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(_line("m1"), encoding="utf-8")  # 连一个完整行都没有
    stats = ParseStats(parser_name="t", parser_version="1")
    records, new_offset = parse_file_from(path, 0, stats)
    assert records == [] and new_offset == 0


def test_parse_file_from_handles_bad_lines_without_losing_good_ones(tmp_path):
    path = tmp_path / "s.jsonl"
    _write_lf(path, _line("m1"), "{broken json", _line("m2"))
    stats = ParseStats(parser_name="t", parser_version="1")
    records, new_offset = parse_file_from(path, 0, stats)
    assert [r.raw_id for r in records] == ["m1", "m2"]
    assert stats.bad_lines == 1
    assert new_offset == path.stat().st_size


# ------------------------------------------------------------------ 增量扫描
def test_scan_new_reads_everything_first_time(conn, wb_home):
    _write_lf(wb_home / "s1.jsonl", _line("m1"), _line("m2"))
    records, stats, info = incremental.scan_new(conn)
    assert len(records) == 2
    assert stats.parsed == 2
    assert info["files_seen"] == 1 and info["files_read"] == 1
    assert info["new_files"] == 1


def test_scan_new_second_round_reads_nothing(conn, wb_home):
    """幂等：没有新内容时，第二轮 0 条记录、0 个文件被读。"""
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"))
    incremental.scan_new(conn)

    records, stats, info = incremental.scan_new(conn)
    assert records == []
    assert stats.scanned == 0
    assert info["files_read"] == 0 and info["unchanged_files"] == 1
    assert info["tracked_files"] == 1


def test_scan_new_picks_up_appended_lines_only(conn, wb_home):
    """追加两行后，第三轮只读这两行——stats 分母也只有这两行。"""
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"))
    incremental.scan_new(conn)

    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_line("m2") + "\n" + _line("m3") + "\n")

    records, stats, info = incremental.scan_new(conn)
    assert [r.raw_id for r in records] == ["m2", "m3"]
    assert stats.scanned == 2 and stats.parsed == 2
    assert info["files_read"] == 1 and info["bytes_read"] > 0


def test_scan_new_discovers_new_files(conn, wb_home):
    _write_lf(wb_home / "s1.jsonl", _line("m1"))
    incremental.scan_new(conn)
    # 新会话文件（含 subagents 子目录）自动进入视野
    sub = wb_home / "s1" / "subagents"
    sub.mkdir(parents=True)
    _write_lf(sub / "agent-x.jsonl", _line("m2"))

    records, _stats, info = incremental.scan_new(conn)
    assert [r.raw_id for r in records] == ["m2"]
    assert info["new_files"] == 1
    assert info["tracked_files"] == 2


def test_scan_new_truncated_file_is_reread_from_zero(conn, wb_home):
    """文件被替换/截断（size < offset）：从头读，不留在旧 offset 上装死。"""
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"), _line("m2"))
    incremental.scan_new(conn)

    # 模拟轮转：同名文件被换成更短的新内容
    _write_lf(path, _line("m9"))
    records, _stats, info = incremental.scan_new(conn)
    assert [r.raw_id for r in records] == ["m9"]
    assert info["truncated_files"] == 1


def test_scan_new_half_written_line_survives_next_round(conn, wb_home):
    """端到端半行保护：写着 half 行的轮次不消费，补全后的轮次读到它。"""
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"))
    incremental.scan_new(conn)

    # 模拟「另一个进程正在写第二行，写了一半」
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_line("m2")[:40])
    records, _stats, _info = incremental.scan_new(conn)
    assert records == [], "半行不该被消费"

    # 进程把行写完了
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(_line("m2")[40:] + "\n")
    records2, _s2, _i2 = incremental.scan_new(conn)
    assert [r.raw_id for r in records2] == ["m2"], "补全后必须读到，不许静默丢失"


def test_scan_new_stores_relative_paths_only(conn, wb_home):
    """隐私：水位只存相对路径，不出现绝对路径/用户名（RK-04）。"""
    _write_lf(wb_home / "s1.jsonl", _line("m1"))
    incremental.scan_new(conn)
    rows = conn.execute("SELECT path FROM source_file_state").fetchall()
    assert len(rows) == 1
    stored = rows[0]["path"]
    assert not stored.startswith("/") and ":" not in stored
    assert stored.startswith("projects/")
    assert str(Path.home().name) not in stored


def test_watermark_roundtrip_and_prune(conn, wb_home):
    _write_lf(wb_home / "s1.jsonl", _line("m1"))
    incremental.scan_new(conn)
    marks = incremental.load_watermarks(conn)
    assert len(marks) == 1

    # 文件消失后，prune_missing 才清理（扫描中不删，防 glob 竞态）
    (wb_home / "s1.jsonl").unlink()
    incremental.scan_new(conn)
    assert len(incremental.load_watermarks(conn)) == 1, "扫描不删水位"
    assert incremental.prune_missing(conn) == 1
    assert incremental.load_watermarks(conn) == {}


def test_clear_watermarks_forces_full_reread(conn, wb_home):
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"))
    incremental.scan_new(conn)
    incremental.clear_watermarks(conn)
    records, _stats, info = incremental.scan_new(conn)
    assert [r.raw_id for r in records] == ["m1"], "水位清空后应全量重读"
    assert info["new_files"] == 1


def test_mtime_only_change_does_not_reread(conn, wb_home):
    """只改 mtime（内容没变）不该触发重读——size/offset 相同就够了。"""
    path = wb_home / "s1.jsonl"
    _write_lf(path, _line("m1"))
    incremental.scan_new(conn)
    # 触碰文件改 mtime，不动内容
    st = path.stat()
    path.touch()
    os_utime = time.time()
    import os as _os

    _os.utime(path, (os_utime + 5, os_utime + 5))
    records, _stats, info = incremental.scan_new(conn)
    assert records == [] and info["files_read"] == 0
