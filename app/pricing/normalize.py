"""模型别名归一（RK-03）。

策略（顺序敏感，先精确后清洗）：

1. ``aliases.json`` / 库内 ``model_alias`` 精确表命中（原样命中）
2. 原样就是价格表里的模型名 —— 直接用，**不进入清洗**
3. 内置正则规则（去供应商前缀、去日期后缀、统一分隔符、重排 Claude 版本号）
4. 仍未命中 -> ``__unknown__``，由调用方记账并告警（**不静默计入成本**）

第 2 步不可省：``deepseek-v3`` 这类名字本身带 ``-v3`` 后缀，若先清洗会被
最后一条规则剥成 ``deepseek``，导致价格表里有、却按未定价处理的假阴性。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

UNKNOWN = "__unknown__"

# 顺序敏感：先做重排，再做通用清理
_REWRITES: tuple[tuple[re.Pattern[str], str], ...] = (
    # claude-4.5-sonnet -> claude-sonnet-4.5
    (re.compile(r"^claude-(\d+(?:[.\-]\d+)?)-(opus|sonnet|haiku)$"), r"claude-\2-\1"),
    # claude-sonnet-4-5 -> claude-sonnet-4.5
    (re.compile(r"^(claude-(?:opus|sonnet|haiku))-(\d+)-(\d+)$"), r"\1-\2.\3"),
)

_CLEANERS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"^[a-z0-9][a-z0-9._-]*/"), ""),   # 供应商前缀 anthropic/claude-...
    (re.compile(r"[@:].*$"), ""),                   # @20250929 / :free
    (re.compile(r"[-_]\d{8}$"), ""),                # -20250929
    (re.compile(r"[-_]\d{4}-\d{2}-\d{2}$"), ""),    # -2025-09-29
    (re.compile(r"[-_](latest|preview|stable|beta|v\d+)$"), ""),
    (re.compile(r"[_ ]"), "-"),                      # 统一分隔符
    (re.compile(r"\.(?=\d)"), "."),                  # 保留 4.5 这类版本点
    (re.compile(r"-{2,}"), "-"),
    (re.compile(r"^-|-$"), ""),
)


@dataclass(frozen=True)
class Normalized:
    """归一结果。

    ``recognized=False`` 时 ``model`` 为 ``__unknown__``，调用方必须记账告警。
    """

    model: str
    raw: str
    recognized: bool
    rule: str  # exact | rewrite | cleaners | unknown


class Normalizer:
    def __init__(self, aliases: dict[str, str] | None = None,
                 known_models: Iterable[str] = ()) -> None:
        # 别名表键统一小写，便于大小写无关命中
        self._aliases = {k.strip().lower(): v for k, v in (aliases or {}).items()}
        self._known = {m.strip().lower() for m in known_models}

    @property
    def known_models(self) -> set[str]:
        return set(self._known)

    def canonicalize(self, raw: str | None) -> str:
        """只做字符串归一，不判断是否已知。"""
        s = (raw or "").strip().lower()
        if not s:
            return ""
        for pattern, repl in _REWRITES:
            s = pattern.sub(repl, s)
        for pattern, repl in _CLEANERS:
            s = pattern.sub(repl, s)
        # 版本号里的 ``-`` 归一到 ``.``：claude-sonnet-4-5 -> claude-sonnet-4.5
        s = re.sub(r"^([a-z]+-[a-z]+)-(\d)-(\d)$", r"\1-\2.\3", s)
        return s

    def apply(self, raw: str | None) -> Normalized:
        original = raw or ""
        key = original.strip().lower()

        if key and key in self._aliases:
            target = self._aliases[key]
            return Normalized(
                model=target,
                raw=original,
                recognized=self._is_known(target),
                rule="exact",
            )

        # 原样就是价格表里的模型名 —— 必须先判，不能先清洗。
        # 反例：``deepseek-v3`` 本身是价格表的正式名字，但 cleaners 里有
        # ``[-_](latest|preview|stable|beta|v\d+)$`` 这条规则会把 ``-v3``
        # 当作版本后缀剥掉，于是变成 ``deepseek`` 再判为未知模型，
        # 结果是「价格表里有、却按未定价处理」的假阴性（成本记 0）。
        if key and self._is_known(key):
            return Normalized(model=key, raw=original, recognized=True, rule="exact")

        canon = self.canonicalize(original)
        if canon and canon in self._aliases:
            target = self._aliases[canon]
            return Normalized(target, original, self._is_known(target), "exact")

        if canon and self._is_known(canon):
            return Normalized(canon, original, True, "rewrite" if canon != key else "cleaners")

        if not canon:
            return Normalized(UNKNOWN, original, False, "unknown")

        # 归一后仍不在价格表里：也算 unknown，但保留归一名便于人工排查
        return Normalized(UNKNOWN, original, False, "unknown")

    def _is_known(self, model: str) -> bool:
        return model.strip().lower() in self._known
