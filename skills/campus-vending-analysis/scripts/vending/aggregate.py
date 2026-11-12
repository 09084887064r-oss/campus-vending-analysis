"""单期聚合：把一期记录压成一份 PeriodStat，供六模块与环比共用。

只做算术，不做渲染；同一份记录必得同一结果。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .core import NA, Record, safe_div

UNASSIGNED_DORM = "未匹配人数表"
UNPRICED_BAND = "价格无法计算（数量≤0）"
UNCATEGORIZED = "未分类"


@dataclass
class Group:
    """一个分组（小时/档位/品类/机器/楼栋）的单期汇总。"""

    key: str
    qty: float = 0.0
    amount: float = 0.0
    skus: set[str] = field(default_factory=set)
    users: set[str] = field(default_factory=set)
    orders: set[str] = field(default_factory=set)
    extra: dict[str, Any] = field(default_factory=dict)

    def add(self, record: Record) -> None:
        if record.qty:
            self.qty += record.qty
        if record.amount:
            self.amount += record.amount
        self.skus.add(record.sku_id)
        if record.user:
            self.users.add(record.user)
        if record.order:
            self.orders.add(record.order)

    @property
    def user_count(self) -> int:
        return len(self.users)

    @property
    def order_count(self) -> int:
        return len(self.orders)


@dataclass
class PeriodStat:
    label: str
    days: int
    amount: float = 0.0
    qty: float = 0.0
    users: int = 0
    orders: int | None = None
    hours: dict[int, Group] = field(default_factory=dict)
    bands: dict[str, Group] = field(default_factory=dict)
    categories: dict[str, Group] = field(default_factory=dict)
    machines: dict[str, Group] = field(default_factory=dict)
    dorms: dict[str, Group] = field(default_factory=dict)
    records: int = 0

    def share(self, part: float | None) -> float | None:
        ratio = safe_div(part, self.amount, digits=6)
        return None if ratio is None else ratio


def band_of(price: float | None, bands: Sequence[Any]) -> str:
    if price is None:
        return UNPRICED_BAND
    for band in bands:
        low = getattr(band, "low", None)
        high = getattr(band, "high", None)
        label = getattr(band, "label", None) or getattr(band, "name", None) or "?"
        if low is not None and price < float(low):
            continue
        if high is not None and price >= float(high):
            continue
        return str(label)
    return "超出价格带区间"


def _group(store: dict[str, Group], key: str) -> Group:
    if key not in store:
        store[key] = Group(key=key)
    return store[key]


def build_period(records: Iterable[Record], label: str, days: int,
                 cfg: Any, roster: dict[str, dict[str, Any]] | None = None) -> PeriodStat:
    stat = PeriodStat(label=label, days=max(days, 1))
    roster = roster or {}
    user_pool: set[str] = set()
    order_pool: set[str] = set()
    order_known = True
    for record in records:
        stat.records += 1
        stat.amount += record.amount or 0.0
        stat.qty += record.qty or 0.0
        if record.user:
            user_pool.add(record.user)
        if record.order:
            order_pool.add(record.order)
        else:
            order_known = False

        if record.ts is not None:
            _group(stat.hours, f"{record.ts.hour:02d}").add(record)
        _group(stat.bands, band_of(record.unit_price, cfg.price_bands)).add(record)
        _group(stat.categories, record.category or UNCATEGORIZED).add(record)
        _group(stat.machines, record.machine or "未标注机器").add(record)
        dorm = record.dorm if record.dorm in roster else (record.dorm or UNASSIGNED_DORM)
        _group(stat.dorms, dorm).add(record)

    for dorm in roster:
        if dorm not in stat.dorms:
            stat.dorms[dorm] = Group(key=dorm)          # 零消费楼栋保留
    stat.amount = round(stat.amount, 2)
    stat.qty = round(stat.qty, 3)
    stat.users = len(user_pool)
    stat.orders = len(order_pool) if (order_pool and order_known) else None
    for store in (stat.bands, stat.categories, stat.machines, stat.dorms):
        for group in store.values():
            group.amount = round(group.amount, 2)
            group.qty = round(group.qty, 3)
    return stat


# ---------------------------------------------------------------- 指标字典

@dataclass(frozen=True)
class Metric:
    name: str
    unit: str
    fmt: str               # money | num | int | pct | ratio
    higher_is_better: bool = True


METRICS: tuple[Metric, ...] = (
    Metric("销售额", "元", "money"),
    Metric("在校人数", "人", "int"),
    Metric("消费人数", "人", "int"),
    Metric("用户消费率", "%", "pct"),
    Metric("订单数", "单", "int"),
    Metric("商品销售数量", "件", "int"),
    Metric("连带率", "件/单", "num"),
    Metric("件单价", "元/件", "money"),
    Metric("客单价", "元/单", "money"),
    Metric("人均消费", "元/人", "money"),
    Metric("用户周频次", "单/人", "num"),
)


def metric_values(stat: PeriodStat, roster_total: float | None) -> dict[str, float | None]:
    users = stat.users or None
    orders = stat.orders
    return {
        "销售额": round(stat.amount, 2),
        "在校人数": roster_total,
        "消费人数": stat.users,
        "用户消费率": safe_div(stat.users, roster_total, digits=6),
        "订单数": orders,
        "商品销售数量": round(stat.qty, 3),
        "连带率": safe_div(stat.qty, orders, digits=4),
        "件单价": safe_div(stat.amount, stat.qty, digits=4),
        "客单价": safe_div(stat.amount, orders, digits=4),
        "人均消费": safe_div(stat.amount, users, digits=4),
        "用户周频次": safe_div(orders, users, digits=4),
    }


def delta(cur: Any, prev: Any) -> tuple[Any, Any]:
    """返回 (变化额, 变化率)；任一侧缺失 → (N/A, N/A)，上期为 0 → (额, N/A)。"""
    if cur is None or prev is None:
        return NA, NA
    change = cur - prev
    if prev == 0:
        return round(change, 6), NA
    return round(change, 6), round(change / prev, 6)


def point(cur: Any, prev: Any) -> Any:
    """百分点差（用于比例类指标，避免把 0.03 说成 3%）。"""
    if cur is None or prev is None:
        return NA
    return round((cur - prev) * 100, 4)
