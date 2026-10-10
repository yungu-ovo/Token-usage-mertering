"""金额工具：全局禁止浮点运算。

约定
----
* Python 内部一律使用 ``Decimal``。
* 落库一律使用 **微美元整数**（1e-6 USD）。比分更细，能表达 $0.0004 级单请求成本。
* 从 JSON 解析出来的 float 必须先转字符串再构造 Decimal，否则会把二进制浮点误差带进来。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_UP
from typing import Any, Sequence

MICRO = Decimal("1000000")
CENT = Decimal("0.01")


def dec(value: Any) -> Decimal:
    """安全地把任意值转成 Decimal，绝不经过二进制浮点。

    >>> dec(1e-7)
    Decimal('0.0000001')
    >>> dec(0.184)
    Decimal('0.184')
    >>> dec(None)
    Decimal('0')
    """
    if value is None or value == "":
        return Decimal(0)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        # 关键：先转字符串，避免 0.1 + 0.2 这类误差
        return Decimal(repr(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)


def to_micro(value: Any) -> int:
    """Decimal（或可转换值）-> 微美元整数。"""
    return int((dec(value) * MICRO).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_micro(micro: int | None) -> Decimal:
    """微美元整数 -> Decimal。"""
    return Decimal(int(micro or 0)) / MICRO


def q2(value: Any) -> Decimal:
    """量化到分，四舍五入。用于展示。"""
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: Any, places: int = 2) -> str:
    """输出给前端的金额字符串。Pydantic 也会把 Decimal 序列化成字符串。"""
    exp = Decimal(1).scaleb(-places)
    return str(dec(value).quantize(exp, rounding=ROUND_HALF_UP))


def safe_div(numerator: Any, denominator: Any) -> Decimal:
    """带除零兜底的除法。"""
    d = dec(denominator)
    if d == 0:
        return Decimal(0)
    return dec(numerator) / d


def apportion(total: Any, parts: Sequence[Any], places: int = 2) -> list[Decimal]:
    """把 ``parts`` 各自量化到 ``places`` 位小数，同时保证**各项之和恰好等于 total**。

    为什么需要这个：构成区的每一项都四舍五入到分之后，加总常常比总额差一两分钱。
    用户拿计算器一按就对不上，会怀疑整个看板的可信度。这里用最大余数法
    （先向下取整，再把差额按小数部分从大到小逐分补回去）消除这个误差。

    ``total`` 本身也会被量化到 ``places`` 位，返回值之和恒等于它。
    """
    step = Decimal(1).scaleb(-places)
    total_value = dec(total)
    target_units = int(
        (total_value / step).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    )

    floors: list[int] = []
    remainders: list[Decimal] = []
    for part in parts:
        units = dec(part) / step
        base = int(units.to_integral_value(rounding=ROUND_FLOOR))
        floors.append(base)
        remainders.append(units - base)

    deficit = target_units - sum(floors)
    if deficit > 0 and floors:
        # 小数部分大的先补，保证「四舍五入」的直觉不被破坏
        order = sorted(range(len(floors)), key=lambda i: (-remainders[i], i))
        for k in range(deficit):
            floors[order[k % len(order)]] += 1
    elif deficit < 0 and floors:
        # 理论上到不了这里（向下取整之和不会超过目标），保险起见也处理掉
        order = sorted(range(len(floors)), key=lambda i: (remainders[i], i))
        for k in range(-deficit):
            floors[order[k % len(order)]] -= 1

    return [Decimal(units) * step for units in floors]
