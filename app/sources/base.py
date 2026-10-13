"""采集适配器基类与隐私白名单（RK-04）。

设计要点：解析结果用 :class:`ParsedRecord` 表示，而这个 dataclass **没有**
任何承载对话正文的字段。就算原始 JSON 里有 ``content`` / ``arguments`` /
``output``，也不可能被带出来——这是结构性的保证，不依赖评审时记得逐条检查。
"""

from __future__ import annotations

import abc
from collections import Counter
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator

# 只允许从原始记录里取这些路径。任何新增字段都必须先加进这里。
ALLOWED_PATHS: frozenset[str] = frozenset(
    {
        "timestamp",
        "type",
        "status",
        "sessionId",
        "cwd",
        "id",
        "providerData.messageId",
        "providerData.conversationRequestId",
        "providerData.model",
        "providerData.requestModelId",
        "providerData.requestModelName",
        "providerData.agent",
        "providerData.usage.inputTokens",
        "providerData.usage.outputTokens",
        "providerData.usage.totalTokens",
        "providerData.rawUsage.prompt_tokens",
        "providerData.rawUsage.completion_tokens",
        "providerData.rawUsage.total_tokens",
        "providerData.rawUsage.cached_tokens",
        "providerData.rawUsage.cache_read_input_tokens",
        "providerData.rawUsage.cache_creation_input_tokens",
        "providerData.rawUsage.prompt_cache_hit_tokens",
        "providerData.rawUsage.prompt_cache_write_tokens",
        "providerData.rawUsage.completion_thinking_tokens",
        "providerData.rawUsage.credit",
        "message.usage.input_tokens",
        "message.usage.output_tokens",
        "message.usage.total_tokens",
        "message.usage.cache_read_input_tokens",
        "message.usage.cache_creation_input_tokens",
    }
)


def dig(record: dict[str, Any], path: str) -> Any:
    """按点分路径取值，任一层不是 dict 就返回 None。"""
    node: Any = record
    for part in path.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(part)
        if node is None:
            return None
    return node


def first_of(record: dict[str, Any], *paths: str) -> Any:
    """按顺序取第一个非空值。"""
    for path in paths:
        value = dig(record, path)
        if value is not None and value != "":
            return value
    return None


def max_of(record: dict[str, Any], *paths: str) -> int:
    """同一 token 概念的多种字段拼写，**组内取最大**。

    为什么不能对 token 组用 ``first_of``：实测 WorkBuddy 的 ``rawUsage`` 里
    ``cache_read_input_tokens`` 恒为 0、但字段**始终存在**，而真实缓存读在
    ``prompt_cache_hit_tokens`` 里（1,689/1,694 条非零，占输入 97.4%）。
    ``first_of`` 的「第一个非空」会被这个恒 0 字段阻断，直报数据整体丢失
    ——这正是 RK-07 禁止的「静默少算」。

    四组 token（输入/输出/缓存读/缓存写）实测 1,701 条多拼写法共存记录
    **零分歧**：一致时 ``max`` 与「第一个非空」等价；不一致时取有证据的一侧。
    """
    best = 0
    for path in paths:
        value = as_int(dig(record, path))
        if value > best:
            best = value
    return best


def as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0


def as_decimal(value: Any) -> Decimal | None:
    """注意：先转字符串再构造 Decimal，避免浮点误差（缺口 14）。"""
    if value is None or value == "":
        return None
    from ..money import dec

    return dec(value)


def map_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if text in ("", "success", "ok", "completed", "done", "finished"):
        return "success"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "throttl" in text or "rate" in text and "limit" in text:
        return "throttled"
    return "error"


@dataclass(frozen=True)
class ParsedRecord:
    """规范化后的用量记录。

    **刻意不含任何正文相关字段** —— 这是 RK-04 的结构性保证。
    """

    raw_id: str
    ts: str
    model: str
    session_id: str
    project: str
    tool: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    credit: Decimal | None = None
    status: str = "success"
    provider: str = "workbuddy"
    billing_mode: str = "subscription"

    def to_ingest_item(self) -> dict[str, Any]:
        d = asdict(self)
        usage = {
            "input_tokens": d.pop("input_tokens"),
            "output_tokens": d.pop("output_tokens"),
            "cache_read_tokens": d.pop("cache_read_tokens"),
            "cache_write_tokens": d.pop("cache_write_tokens"),
            "credit": None if d.pop("credit") is None else str(self.credit),
        }
        d["usage"] = usage
        return d


@dataclass
class ParseStats:
    """一次采集的解析健康度（RK-07）。

    数据源改格式时，解析器不会报错，只会**少解析**——看板上表现为"今天成本很低"，
    用户以为是自己没怎么用。所以必须把成功率显式算出来并纳入告警。

    分母取**含 token 计数的行数**：会话转录里绝大多数行是正文消息，
    拿总行数做分母会得到毫无意义的个位数成功率。
    """

    parser_name: str = ""
    parser_version: str = ""
    scanned: int = 0        # 读到的非空原始行
    bad_lines: int = 0      # 不是合法 JSON 对象的行
    usage_lines: int = 0    # 其中含 token 计数、本该能解析的行
    parsed: int = 0         # 实际解析成功的
    skipped: Counter[str] = field(default_factory=Counter)

    def bump_skipped(self, reason: str) -> None:
        self.skipped[reason] += 1

    def rate(self) -> float | None:
        """解析成功率；没有可判定的样本时返回 None（此时不该告警）。"""
        denom = self.usage_lines or self.scanned
        if denom <= 0:
            return None
        return self.parsed / denom

    def as_dict(self) -> dict[str, Any]:
        rate = self.rate()
        return {
            "parser": self.parser_name,
            "version": self.parser_version,
            "scanned": self.scanned,
            "bad_lines": self.bad_lines,
            "usage_lines": self.usage_lines,
            "parsed": self.parsed,
            "skipped": dict(self.skipped),
            "success_rate": None if rate is None else f"{rate:.6f}",
        }


class SourceParser(abc.ABC):
    """采集适配器接口。实现必须**只读**原始数据。"""

    name: str = "base"
    # 解析器版本：格式变了就 +1，随解析成功率一起写进 meta，便于事后定位
    version: str = "0"

    def __init__(self) -> None:
        self.stats = ParseStats(parser_name=self.name, parser_version=self.version)

    @abc.abstractmethod
    def discover(self) -> list[Path]:
        """列出可解析的原始文件。"""

    @abc.abstractmethod
    def iter_records(self, path: Path) -> Iterator[ParsedRecord]:
        """逐条产出规范化记录。无用量记录应被跳过。

        实现必须顺带维护 ``self.stats``，否则 RK-07 的监控就是空的。
        """

    def parse_all(self, files: Iterable[Path] | None = None) -> Iterator[ParsedRecord]:
        for path in files if files is not None else self.discover():
            yield from self.iter_records(path)

    def reset_stats(self) -> None:
        self.stats = ParseStats(parser_name=self.name, parser_version=self.version)
