"""文件级增量扫描（P0-B）：字节水位 + 半行保护。

为什么需要
----------
v0.3.0 及之前每次采集都是**全量重扫**：所有 jsonl 逐行读完再幂等落库。
数据量小时无所谓（本机 0.27s），但它让「调用即记录」的两个目标打折：

* **时效**：轮询间隔受限于全量成本，只能做到分钟级；
* **增长**：会话文件只增不减（单文件已见 7MB），全量成本随历史线性上升。

增量扫描把每次轮询的工作量降到「新增的那几行」：服务每 15s 扫一遍，
单轮耗时毫秒级，调用发生后秒级可查。

三条铁律（每条都对应一种「静默少算」）
------------------------------------
1. **半行保护**：只消费以 ``\\n`` 结尾的行，offset 只推进到最后一个完整行末。
   正在写入的残行留给下一轮——否则它会被当成坏行跳过，那条用量永久丢失。
2. **截断检测**：文件 size < 已存 offset，说明被轮转/替换，offset 归零全读。
3. **版本自愈**：解析器版本号（``parser.version``）变化时作废全部水位，
   全量重扫一次。解析逻辑修 bug（如 P0-A 的缓存字段）不该依赖人工记得重采。

隐私：水位只存**相对路径 + 数值**，不存绝对路径/用户名，不读任何正文字段（RK-04）。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from .. import config
from ..timeutil import now_local_str
from .base import ParseStats
from .workbuddy_jsonl import WorkBuddyJsonlParser, parse_file_from

# meta 键：上次扫描用的解析器版本。与 parser.version 不一致 → 全量重扫。
META_PARSER_VERSION_KEY = "scanner_parser_version"


def _rel_path(path: Path) -> str:
    """相对 WORKBUDDY_HOME 的 POSIX 路径。跨不出 HOME 的（理论不存在）退为绝对路径。"""
    try:
        return path.resolve().relative_to(config.WORKBUDDY_HOME.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# ---------------------------------------------------------------- 水位读写
def load_watermarks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT path, offset, size, mtime_ns FROM source_file_state"
    ).fetchall()
    return {r["path"]: dict(r) for r in rows}


def save_watermark(
    conn: sqlite3.Connection, path: str, offset: int, size: int, mtime_ns: int
) -> None:
    conn.execute(
        "INSERT INTO source_file_state (path, kind, offset, size, mtime_ns, updated_at) "
        "VALUES (?, 'jsonl', ?, ?, ?, ?) "
        "ON CONFLICT(path) DO UPDATE SET "
        "  offset = excluded.offset, size = excluded.size, "
        "  mtime_ns = excluded.mtime_ns, updated_at = excluded.updated_at",
        (path, offset, size, mtime_ns, now_local_str()),
    )


def clear_watermarks(conn: sqlite3.Connection) -> int:
    cur = conn.execute("DELETE FROM source_file_state")
    return cur.rowcount or 0


def prune_missing(conn: sqlite3.Connection) -> int:
    """删掉「文件已不存在」的水位行（每日清理调用，防止表无限增长）。"""
    rows = conn.execute("SELECT path FROM source_file_state").fetchall()
    removed = 0
    for row in rows:
        target = config.WORKBUDDY_HOME / row["path"]
        if not target.exists():
            conn.execute("DELETE FROM source_file_state WHERE path = ?", (row["path"],))
            removed += 1
    return removed


# ---------------------------------------------------------------- 增量扫描
def scan_new(
    conn: sqlite3.Connection,
    parser: WorkBuddyJsonlParser | None = None,
    home: Path | str | None = None,
) -> tuple[list[Any], ParseStats, dict[str, Any]]:
    """只读各文件自水位以来的新增完整行。

    返回 ``(records, stats, scan_info)``。``stats`` 是**本轮**的解析健康度
    （增量轮的分母只有新增行，权威口径由每小时校准轮的全量扫描负责，见 collect.py）。

    水位随调用方的事务提交落库——扫描与写库在同一个事务里，崩溃也不会
    出现「读了没写」或「写了没记水位」的中间态。
    """
    parser = parser or WorkBuddyJsonlParser(home=Path(home) if home else None)
    # 本轮独立的 stats：增量轮的分母只有新增行，与全量轮的口径分开统计
    stats = ParseStats(parser_name=parser.name, parser_version=parser.version)

    watermarks = load_watermarks(conn)
    records: list[Any] = []
    scan_info: dict[str, Any] = {
        "files_seen": 0,
        "files_read": 0,
        "new_files": 0,
        "unchanged_files": 0,
        "truncated_files": 0,
        "bytes_read": 0,
    }

    for path in parser.discover():
        try:
            st = os.stat(path)
        except OSError:
            continue  # 扫描瞬间被删/被移，跳过即可
        scan_info["files_seen"] += 1
        rel = _rel_path(path)
        wm = watermarks.pop(rel, None)

        offset = 0
        if wm is not None:
            if int(wm["size"]) > st.st_size or int(wm["offset"]) > st.st_size:
                # 文件被轮转/替换（变小了）：从头读，幂等 UPSERT 保证不重复计数
                offset = 0
                scan_info["truncated_files"] += 1
            else:
                offset = int(wm["offset"])

        if offset >= st.st_size:
            # 无新内容——绝大多数文件在绝大多数轮次都是这条路
            scan_info["unchanged_files"] += 1
            save_watermark(conn, rel, offset, st.st_size, st.st_mtime_ns)
            continue

        if wm is None:
            scan_info["new_files"] += 1
        scan_info["files_read"] += 1
        scan_info["bytes_read"] += max(0, st.st_size - offset)

        file_records, new_offset = parse_file_from(path, offset, stats)
        records.extend(file_records)
        save_watermark(conn, rel, new_offset, st.st_size, st.st_mtime_ns)

    # 剩下的 watermark 是「这次没 discover 到」的文件：不删（可能是 glob 竞态），
    # 由每日清理的 prune_missing 兜底。
    scan_info["tracked_files"] = conn.execute(
        "SELECT COUNT(*) AS c FROM source_file_state"
    ).fetchone()["c"]
    return records, stats, scan_info
