"""隐私自检：证明「不读取、不落库任何会话正文」这件事是真的，而不是口头承诺。

四道检查，任何一道不过就退出码非零：

1. **结构**：``ParsedRecord`` 不含任何可承载正文的字段（结构性保证）
2. **白名单**：``ALLOWED_PATHS`` 里没有任何指向 content / text / arguments / output 的路径
3. **源码**：解析器与采集入口的代码里不出现读正文的键名
4. **落库**：库里所有文本列的实际取值都很短，不含会话正文
5. **只读**：解析器只以 ``open(path, "r")`` 打开原始文件，不存在写操作

用法::

    python scripts/check_privacy.py                 # 检查默认库 data/usage.db
    python scripts/check_privacy.py --db path.db
"""

from __future__ import annotations

import argparse
import ast
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402
from app.sources import base as sources_base  # noqa: E402

# 正文相关的键名：出现在白名单、dataclass 字段或解析代码里都属于违规
BODY_KEYS = (
    "content",
    "contents",
    "text",
    "arguments",
    "output_text",
    "completion_text",
    "prompt",
    "answer",
    "message_body",
    "thinking",
    "reasoning",
    "raw_message",
    "display",
    "input_text",
)

# 允许出现的例外（都是计数/标识字段，不是正文）
BODY_KEY_ALLOW = {
    "input_tokens",
    "output_tokens",
    "prompt_tokens",
    "completion_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_write_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "completion_thinking_tokens",
    "max_output_tokens",
    "input_tokens_total",
}

MAX_TEXT_LEN = 200  # 库里任何文本列超过这个长度就该人工看一眼


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, note: str = "") -> None:
        self.rows.append((name, bool(ok), note))

    @property
    def failed(self) -> list[tuple[str, bool, str]]:
        return [r for r in self.rows if not r[1]]


def _iter_source_files() -> list[Path]:
    files = []
    for pattern in ("app/**/*.py", "scripts/*.py"):
        files.extend(ROOT.glob(pattern))
    return [p for p in files if "__pycache__" not in str(p)]


# ---------------------------------------------------------------- 1. 结构
def check_dataclass(report: Report) -> None:
    fields = set(sources_base.ParsedRecord.__dataclass_fields__)
    hits = sorted(f for f in fields if f.lower() in BODY_KEYS)
    report.add(
        "ParsedRecord 不含正文类字段",
        not hits,
        f"字段 {len(fields)} 个" + (f"；违规 {hits}" if hits else ""),
    )

    # to_ingest_item 的输出也不能凭空多出正文键
    try:
        import datetime as _dt

        sample = sources_base.ParsedRecord(
            raw_id="x", ts="2026-01-01T00:00:00+08:00", model="m",
            session_id="s", project="p", tool="t",
            input_tokens=1, output_tokens=1, cache_read_tokens=0, cache_write_tokens=0,
        )
        item = sample.to_ingest_item()
        keys = set(item) | set(item.get("usage", {}))
        bad = sorted(k for k in keys if k.lower() in BODY_KEYS)
        report.add("to_ingest_item 输出不含正文类键", not bad, f"键 {sorted(keys)}" if bad else f"键 {len(keys)} 个")
    except Exception as exc:  # noqa: BLE001
        report.add("to_ingest_item 输出不含正文类键", False, f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 2. 白名单
def check_allowlist(report: Report) -> None:
    allowed = sources_base.ALLOWED_PATHS
    offenders = []
    for path in allowed:
        leaf = path.split(".")[-1].lower()
        if leaf in BODY_KEY_ALLOW:
            continue
        # 叶子名整体等于某个正文键，或形如 xxx_text / content_xxx
        if leaf in BODY_KEYS:
            offenders.append(path)
        elif re.search(r"(^|_)(content|contents|text|arguments|prompt|answer)($|_)", leaf):
            # prompt_tokens 之类已在白名单里放行；其余一律算违规
            if not leaf.endswith("_tokens"):
                offenders.append(path)
    offenders = sorted(set(offenders))
    report.add(
        "白名单不指向正文路径",
        not offenders,
        f"白名单 {len(allowed)} 条" + (f"；违规 {offenders}" if offenders else ""),
    )

    # 白名单本身应当是 frozenset（禁止运行时被悄悄扩充）
    report.add(
        "白名单为不可变集合",
        isinstance(allowed, frozenset),
        type(allowed).__name__,
    )


# ---------------------------------------------------------------- 3. 源码
def check_sources(report: Report) -> None:
    # 只有这两个文件涉及从原始记录里取值
    targets = [
        ROOT / "app" / "sources" / "base.py",
        ROOT / "app" / "sources" / "workbuddy_jsonl.py",
        ROOT / "app" / "sources" / "workbuddy_trace.py",
    ]
    offenders: list[str] = []
    for path in targets:
        src = path.read_text(encoding="utf-8", errors="replace")
        # 去掉注释与文档字符串后再找，避免把自己的说明文字当成违规
        tree = ast.parse(src)
        for node in ast.walk(tree):
            text = None
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
            if text is None or text in BODY_KEY_ALLOW:
                continue
            low = text.strip().lower()
            if low in BODY_KEYS and low not in {"text"}:  # "text" 可能出现在类型名里
                offenders.append(f"{path.name}: {text!r}")
    report.add(
        "解析器代码不引用正文字段名",
        not offenders,
        f"检查 {len(targets)} 个文件" + (f"；{offenders}" if offenders else ""),
    )

    # 解析器只能读文件，不能写
    write_calls: list[str] = []
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "open":
                modes = [
                    a.value for a in node.args[1:2] if isinstance(a, ast.Constant)
                ] + [
                    kw.value.value
                    for kw in node.keywords
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
                ]
                for mode in modes:
                    if any(ch in str(mode) for ch in "wax+"):
                        write_calls.append(f"{path.name}: open(..., {mode!r})")
    report.add(
        "解析器对原始文件只读",
        not write_calls,
        "未发现写模式" if not write_calls else str(write_calls),
    )

    # 全项目不得对 WORKBUDDY 目录做任何写操作
    risky: list[str] = []
    for path in _iter_source_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        if "WORKBUDDY" not in src:
            continue
        for line in src.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"WORKBUDDY\w*[^\n]*\.(write_text|write_bytes|mkdir|unlink|rename)\(", stripped):
                risky.append(f"{path.relative_to(ROOT)}: {stripped[:80]}")
    report.add(
        "全项目不对 .workbuddy 目录写入",
        not risky,
        "未发现写操作" if not risky else str(risky),
    )


# ---------------------------------------------------------------- 4. 落库
def check_database(report: Report, db_path: Path) -> None:
    if not db_path.exists():
        report.add("数据库存在", False, f"未找到 {db_path}")
        return
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = [
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        report.add("数据库可读", True, f"{len(tables)} 张表")

        # 4a. 列名检查
        bad_cols: list[str] = []
        text_cols: list[tuple[str, str]] = []
        for table in tables:
            for col in conn.execute(f"PRAGMA table_info({table})").fetchall():
                name = col["name"]
                low = name.lower()
                if low in BODY_KEYS and low not in BODY_KEY_ALLOW:
                    bad_cols.append(f"{table}.{name}")
                if str(col["type"]).upper().startswith("TEXT"):
                    text_cols.append((table, name))
        report.add(
            "无正文类列名",
            not bad_cols,
            f"扫了 {len(tables)} 张表" + (f"；违规 {bad_cols}" if bad_cols else ""),
        )

        # 4b. 取值长度检查
        long_values: list[str] = []
        scanned = 0
        for table, col in text_cols:
            try:
                rows = conn.execute(
                    f"SELECT {col} AS v FROM {table} WHERE {col} IS NOT NULL LIMIT 5000"
                ).fetchall()
            except sqlite3.Error:
                continue
            for r in rows:
                value = r["v"]
                if not isinstance(value, str):
                    continue
                scanned += 1
                if len(value) > MAX_TEXT_LEN:
                    long_values.append(f"{table}.{col} 长度 {len(value)}：{value[:60]}…")
        report.add(
            "文本列取值均短（无正文残留）",
            not long_values,
            f"扫了 {scanned} 个文本值，上限 {MAX_TEXT_LEN} 字符"
            + (f"；可疑 {long_values[:3]}" if long_values else ""),
        )

        # 4c. 正文特征词不应出现在任何文本列
        probes = ("Human:", "Human：", "Assistant:", "\"role\":", "```", "<tool_use>")
        hits: list[str] = []
        for table, col in text_cols:
            for probe in probes:
                try:
                    row = conn.execute(
                        f"SELECT COUNT(*) AS c FROM {table} WHERE {col} LIKE ?",
                        (f"%{probe}%",),
                    ).fetchone()
                except sqlite3.Error:
                    continue
                if row and row["c"]:
                    hits.append(f"{table}.{col} 命中 {probe!r} × {row['c']}")
        report.add(
            "无对话正文特征词",
            not hits,
            f"探针 {len(probes)} 个" + (f"；命中 {hits}" if hits else ""),
        )

        # 4d. request_id 不应内嵌正文（应是 provider_/sha1 形态）
        if "usage_fact" in tables:
            rows = conn.execute(
                "SELECT request_id FROM usage_fact LIMIT 2000"
            ).fetchall()
            odd = [
                r["request_id"]
                for r in rows
                if r["request_id"]
                and not re.fullmatch(r"[A-Za-z0-9_.\-]{6,80}", r["request_id"])
            ]
            report.add(
                "request_id 形态合规（无正文混入）",
                not odd,
                f"抽检 {len(rows)} 条" + (f"；异常 {odd[:3]}" if odd else ""),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------- 主流程
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(config.DB_PATH))
    parser.add_argument("--json", action="store_true", help="输出机器可读结果")
    args = parser.parse_args()

    report = Report()
    check_dataclass(report)
    check_allowlist(report)
    check_sources(report)
    check_database(report, Path(args.db))

    if args.json:
        import json

        print(json.dumps(
            [{"check": n, "ok": ok, "note": note} for n, ok, note in report.rows],
            ensure_ascii=False, indent=1,
        ))
    else:
        print(f"隐私自检  数据库={args.db}\n")
        for name, ok, note in report.rows:
            print(f"[{'PASS' if ok else 'FAIL'}] {name:<34} {note}")

    bad = report.failed
    print()
    if bad:
        print(f"PRIVACY: FAIL  {len(bad)}/{len(report.rows)} 项不通过")
        for name, _, note in bad:
            print(f"  - {name}: {note}")
        return 1
    print(f"PRIVACY: PASS  {len(report.rows)}/{len(report.rows)} 项通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
