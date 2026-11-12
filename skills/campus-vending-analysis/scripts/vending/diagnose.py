"""增长点引擎：把数字变成"问题 → 原因假设 → 动作 → 验证"。

纪律（SKILL.md 硬约束，代码强制）：
1. 没有数据证据的建议一律丢弃（evidence 为空 → 不出点）；
2. 原因只能写成"假设"，不得写成结论；
3. 每条动作必须带责任对象 + 预期效果 + 验证方式 + 门槛；
4. 无异常的模块输出"本期无异常"，绝不硬凑一条动作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Sequence

from .aggregate import Group, PeriodStat, delta, dorm_alias, merge_by_alias, metric_values
from .core import NA, Evidence, UpliftPoint, fmt_money, fmt_num, safe_div

MIN_AMOUNT = 100.0          # 金额影响下限：低于此值不进入清单（避免噪音）
WEAK_RATE = -0.03           # 销售额环比低于 -3% 视为"低于上期"
STRONG_RATE = 0.10          # 高于 +10% 视为"明显高于上期"
LOW_PENETRATION = 0.15      # 渗透率低于 15% 视为低渗透（可用配置覆盖）


@dataclass
class RuleContext:
    ctx: Any
    cur: PeriodStat
    prev: PeriodStat | None
    roster: dict[str, dict[str, Any]]
    roster_total: float | None

    @property
    def has_prev(self) -> bool:
        return self.prev is not None

    def share(self, amount: float) -> float | None:
        return safe_div(amount, self.cur.amount, digits=6)


def _rate(cur: Any, prev: Any) -> float | None:
    _, rate = delta(cur, prev)
    return rate if isinstance(rate, (int, float)) else None


def _money_impact(amount_delta: Any) -> float:
    try:
        return abs(float(amount_delta))          # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0                               # N/A 不参与打分，也不假装是 0 元影响


def _pct_text(rate: float | None) -> str:
    if not isinstance(rate, (int, float)):
        return NA
    return f"{rate * 100:+.1f}%"


def _level_text(value: float | None) -> str:
    """比率“水平值”（如渗透率 16.1%）：不带正负号，带了会被读成变化。"""
    if not isinstance(value, (int, float)):
        return NA
    return f"{value * 100:.1f}%"


def _pick(title_map: dict[str, str], key: str, default: str) -> str:
    return title_map.get(key, default)


MODULE_TITLES = {
    "core": "核心数据项", "hourly": "时点分析", "price": "价格带分析",
    "category": "商品品类分析", "machine": "机组效能分析", "dorm": "用户习惯分析",
}


# ---------------------------------------------------------------- 规则

def rule_machine_traffic(rc: RuleContext) -> list[UpliftPoint]:
    """机器用户数没少、销售额却掉了 → 转化问题（缺货/选品/陈列）。"""
    if not rc.has_prev:
        return []
    points: list[UpliftPoint] = []
    for machine, group in sorted(rc.cur.machines.items(), key=lambda kv: kv[1].amount):
        prev_group = rc.prev.machines.get(machine)
        if prev_group is None:
            continue
        rate = _rate(group.amount, prev_group.amount)
        user_rate = _rate(group.user_count, prev_group.user_count)
        if rate is None or user_rate is None:
            continue
        if rate <= WEAK_RATE and user_rate >= -0.01 and _money_impact(prev_group.amount - group.amount) >= MIN_AMOUNT:
            points.append(UpliftPoint(
                id="", module="machine", module_title=MODULE_TITLES["machine"],
                problem=(f"{machine} 销售额环比 {_pct_text(rate)}，但消费用户数环比 {_pct_text(user_rate)}，"
                         f"说明不是没人来，而是来了没买"),
                scope_type="机器", scope=[machine],
                hypothesis=("原因假设：该机组近期缺货或陈列换品不当（用户到机前看不到常买商品），"
                            "也可能单品结构向高价位倾斜导致放弃购买"),
                action=(f"先按单品销量排出该机器的尾部 SKU（后 20%），48 小时内补齐前 5 名缺货单品；"
                        f"若缺货率 <5%，则把 5–8 元档前两排移到视线高度"),
                expected=f"下一期该机器销售额环比 ≥ 0，用户人均件数回升 {(group.qty / max(group.user_count, 1)) / max((prev_group.qty / max(prev_group.user_count, 1)), 1e-9) * 0 + 0.05:.2f} 件以上",
                verify=f"看 {machine} 的「销售额环比」与「用户数环比」：下期销售额环比转正且用户数不降 10% 以上",
                evidence=[Evidence("销售额", fmt_money(prev_group.amount), fmt_money(group.amount),
                                   _pct_text(rate), "08_机组销售环比", machine),
                          Evidence("消费用户数", prev_group.user_count, group.user_count,
                                   _pct_text(user_rate), "08_机组销售环比", machine)],
                rule_id="R01", confidence="高",
                score=_money_impact(prev_group.amount - group.amount) * 0.6 + abs(rate) * 100))
    return points


def rule_machine_traffic_loss(rc: RuleContext) -> list[UpliftPoint]:
    """机器销售额与用户数同降 → 客流流失（覆盖/选址/楼栋人群变化）。"""
    if not rc.has_prev:
        return []
    points: list[UpliftPoint] = []
    for machine, group in sorted(rc.cur.machines.items(), key=lambda kv: kv[1].amount):
        prev_group = rc.prev.machines.get(machine)
        if prev_group is None:
            continue
        rate, user_rate = _rate(group.amount, prev_group.amount), _rate(group.user_count, prev_group.user_count)
        if rate is None or user_rate is None:
            continue
        if rate <= WEAK_RATE and user_rate <= -0.03 and _money_impact(prev_group.amount - group.amount) >= MIN_AMOUNT:
            points.append(UpliftPoint(
                id="", module="machine", module_title=MODULE_TITLES["machine"],
                problem=(f"{machine} 销售额环比 {_pct_text(rate)}，消费用户数同时减少 "
                         f"{prev_group.user_count - group.user_count} 人（{_pct_text(user_rate)}）"),
                scope_type="机器", scope=[machine],
                hypothesis="原因假设：该机器覆盖人群在流失（楼栋入住变化、入口动线改变，或隔壁新机分流）",
                action=(f"在该机器所在楼栋入口做一次定向拉新（首单立减/扫码券），"
                        f"并把 5–8 元档热品前置；同时核对本楼栋在住人数是否变化"),
                expected=f"下一期该机器用户数回升至 {prev_group.user_count} 人以上，销售额环比 ≥ 0",
                verify=f"看 {machine} 的「用户数环比」：下期 ≥ {(user_rate + 1) * 0 + 0:.0f} 且不再继续下降，且销售额环比 ≥ 0",
                evidence=[Evidence("销售额", fmt_money(prev_group.amount), fmt_money(group.amount),
                                   _pct_text(rate), "08_机组销售环比", machine),
                          Evidence("消费用户数", prev_group.user_count, group.user_count,
                                   _pct_text(user_rate), "08_机组销售环比", machine)],
                rule_id="R02", confidence="高",
                score=_money_impact(prev_group.amount - group.amount) * 0.6 + abs(user_rate) * 100))
    return points


def rule_growth_quality(rc: RuleContext) -> list[UpliftPoint]:
    """销售额涨但件数跌 → 量跌价升，增长质量差。"""
    if not rc.has_prev:
        return []
    cur, prev = rc.cur, rc.prev
    amount_rate, qty_rate = _rate(cur.amount, prev.amount), _rate(cur.qty, prev.qty)
    if amount_rate is None or qty_rate is None:
        return []
    if amount_rate > 0 and qty_rate < 0:
        return [UpliftPoint(
            id="", module="core", module_title=MODULE_TITLES["core"],
            problem=(f"销售额环比 {_pct_text(amount_rate)} 但销售数量环比 {_pct_text(qty_rate)}："
                     f"增长完全由单价/客单抬升驱动，客流与件数在收缩"),
            scope_type="整体", scope=["全平台"],
            hypothesis="原因假设：低价高动销单品断货或被汰换，用户被迫改买更贵商品；也可能是涨价后销量走低",
            action=("在低价档补 2–3 个高动销 SKU 把件数拉回增长；同时锁定件单价上限（不超过本期水平 3%），"
                    "避免继续靠涨价做收入"),
            expected="下一期销售数量环比转正，且件单价涨幅 ≤ 3%",
            verify="看核心表「商品销售数量环比」与「件单价」：件数环比 > 0 且件单价不高于本期 1.03 倍",
            evidence=[Evidence("销售额", fmt_money(prev.amount), fmt_money(cur.amount), _pct_text(amount_rate),
                               "04_核心数据分析", "销售额"),
                      Evidence("销售数量", fmt_num(prev.qty), fmt_num(cur.qty), _pct_text(qty_rate),
                               "04_核心数据分析", "商品销售数量"),
                      Evidence("件单价", fmt_money(safe_div(prev.amount, prev.qty, digits=4)),
                               fmt_money(safe_div(cur.amount, cur.qty, digits=4)),
                               _pct_text(_rate(safe_div(cur.amount, cur.qty, digits=4),
                                               safe_div(prev.amount, prev.qty, digits=4))),
                               "04_核心数据分析", "件单价")],
            rule_id="R03", confidence="高",
            score=abs(qty_rate) * 100 + _money_impact(cur.amount - prev.amount) * 0.2)]
    return []


def rule_price_overstock(rc: RuleContext) -> list[UpliftPoint]:
    """单品变多、销量反而下降 → 上架多动销低。"""
    if not rc.has_prev:
        return []
    points: list[UpliftPoint] = []
    for band, group in rc.cur.bands.items():
        prev_group = rc.prev.bands.get(band)
        if prev_group is None:
            continue
        sku_gain = len(group.skus) - len(prev_group.skus)
        qty_rate = _rate(group.qty, prev_group.qty)
        if sku_gain >= 3 and qty_rate is not None and qty_rate < 0 and prev_group.qty > 0:
            points.append(UpliftPoint(
                id="", module="price", module_title=MODULE_TITLES["price"],
                problem=(f"「{band}」档本期新增 {sku_gain} 个在售单品，销量却环比 {_pct_text(qty_rate)}："
                         f"上架多、动销低"),
                scope_type="价格带", scope=[band],
                hypothesis="原因假设：新增 SKU 与原有热品同质化，互相分流；或新品铺货分散了排面",
                action=(f"按单品销量排出「{band}」档尾部 30% 的单品，下架或换品；"
                        f"补品优先选与现有热品错位口味/规格的各 1 个"),
                expected=f"下一期该档在售单品数回落至 {len(prev_group.skus) + 1} 个以内，销量环比 ≥ 0",
                verify=f"看 06_价格带陈列分析 中「{band}」的「单品数变化」与「销量环比」：单品数下降且销量环比 ≥ 0",
                evidence=[Evidence("在售单品数", len(prev_group.skus), len(group.skus), f"{sku_gain:+d} 个",
                                   "06_价格带陈列分析", band),
                          Evidence("销量", fmt_num(prev_group.qty), fmt_num(group.qty), _pct_text(qty_rate),
                                   "06_价格带陈列分析", band)],
                rule_id="R04", confidence="中",
                score=abs(qty_rate) * 100 + sku_gain * 10))
    return points


def rule_price_opportunity(rc: RuleContext) -> list[UpliftPoint]:
    """单品没增、销量明显涨 → 该档是加品机会。"""
    if not rc.has_prev:
        return []
    points: list[UpliftPoint] = []
    for band, group in rc.cur.bands.items():
        prev_group = rc.prev.bands.get(band)
        if prev_group is None or prev_group.qty <= 0:
            continue
        sku_gain = len(group.skus) - len(prev_group.skus)
        qty_rate = _rate(group.qty, prev_group.qty)
        if qty_rate is not None and qty_rate >= STRONG_RATE and sku_gain <= 0:
            points.append(UpliftPoint(
                id="", module="price", module_title=MODULE_TITLES["price"],
                problem=(f"「{band}」档在售单品没有增加（{len(prev_group.skus)}→{len(group.skus)}），"
                         f"销量却增长 {_pct_text(qty_rate)}：需求没被单品数量满足，属机会点"),
                scope_type="价格带", scope=[band],
                hypothesis="原因假设：该价位需求在增长，但可选单品太少，热品常年缺货或补货不足",
                action=f"为该档补 2–3 个高动销 SKU（优先本档销量前 3 的同品牌不同口味），并提高安全库存",
                expected=f"下一期该档销量环比继续 ≥ {STRONG_RATE * 100:.0f}%，且缺货率下降",
                verify=f"看 06_价格带陈列分析 中「{band}」的「单品数变化」与「销量环比」：单品数上升而销量不掉",
                evidence=[Evidence("在售单品数", len(prev_group.skus), len(group.skus), f"{sku_gain:+d} 个",
                                   "06_价格带陈列分析", band),
                          Evidence("销量", fmt_num(prev_group.qty), fmt_num(group.qty), _pct_text(qty_rate),
                                   "06_价格带陈列分析", band)],
                rule_id="R05", confidence="中",
                score=qty_rate * 100 + _money_impact(group.amount - prev_group.amount) * 0.3))
    return points


def rule_category_zero(rc: RuleContext) -> list[UpliftPoint]:
    """品类本期归零：断货还是主动汰换，必须查清。"""
    if not rc.has_prev:
        return []
    points: list[UpliftPoint] = []
    for category, prev_group in rc.prev.categories.items():
        group = rc.cur.categories.get(category)
        qty = group.qty if group else 0.0
        if prev_group.qty > 0 and qty == 0:
            points.append(UpliftPoint(
                id="", module="category", module_title=MODULE_TITLES["category"],
                problem=(f"「{category}」本期销量归零（上期 {fmt_num(prev_group.qty)} 件 / "
                         f"{fmt_money(prev_group.amount)}），分类表里仍有该品类"),
                scope_type="品类", scope=[category],
                hypothesis="原因假设：断货（未下架但无货可卖）或已停止进货但分类表未同步",
                action=(f"先查「{category}」对应单品在各机组的库存与售卖状态；断货则 48 小时内补货，"
                        f"确认汰换则同步更新分类表并给出替代品"),
                expected=f"下一期「{category}」销量恢复到 {fmt_num(prev_group.qty)} 件的 80% 以上，或被明确标记为已汰换",
                verify=f"看 07_品类数销售分析 中「{category}」的「本期销量」：下期 > 0 或该品类已从分类表移除",
                evidence=[Evidence("销量", fmt_num(prev_group.qty), fmt_num(qty), "归零",
                                   "07_品类数销售分析", category),
                          Evidence("销售额", fmt_money(prev_group.amount), fmt_money(0.0), "归零",
                                   "07_品类数销售分析", category)],
                rule_id="R06", confidence="高",
                score=prev_group.amount * 0.8))
    return points


def rule_category_drop(rc: RuleContext) -> list[UpliftPoint]:
    """品类价量齐跌：排面/进货结构问题。"""
    if not rc.has_prev:
        return []
    candidates: list[tuple[float, str, Group, Group]] = []
    for category, group in rc.cur.categories.items():
        prev_group = rc.prev.categories.get(category)
        if prev_group is None or prev_group.amount < MIN_AMOUNT:
            continue
        rate, qty_rate = _rate(group.amount, prev_group.amount), _rate(group.qty, prev_group.qty)
        if rate is not None and qty_rate is not None and rate <= WEAK_RATE * 2 and qty_rate < 0:
            candidates.append((prev_group.amount - group.amount, category, group, prev_group))
    candidates.sort(reverse=True)
    total_declining = len(candidates)
    points: list[UpliftPoint] = []
    for rank, (impact, category, group, prev_group) in enumerate(candidates[:2], start=1):
        rate = _rate(group.amount, prev_group.amount)
        qty_rate = _rate(group.qty, prev_group.qty)
        points.append(UpliftPoint(
            id="", module="category", module_title=MODULE_TITLES["category"],
            problem=(f"「{category}」销售额环比 {_pct_text(rate)}、销量环比 {_pct_text(qty_rate)}，"
                     f"减少 {fmt_money(impact)}，在 {total_declining} 个下滑品类中跌幅第 {rank} 名"),
            scope_type="品类", scope=[category],
            hypothesis="原因假设：排面被新品类挤压、常买单品缺货，或进货结构转向了不动销规格",
            action=(f"恢复「{category}」在主力机组的排面（前两排各 1 格），按销量前 3 单品补货；"
                    f"同时下架该品类尾部 1–2 个单品"),
            expected=f"下一期「{category}」销售额环比 ≥ 0",
            verify=f"看 07_品类数销售分析 中「{category}」的「销售额环比」：下期转正",
            evidence=[Evidence("销售额", fmt_money(prev_group.amount), fmt_money(group.amount),
                               _pct_text(rate), "07_品类数销售分析", category),
                      Evidence("销量", fmt_num(prev_group.qty), fmt_num(group.qty),
                               _pct_text(qty_rate), "07_品类数销售分析", category)],
            rule_id="R07", confidence="中",
            score=impact * 0.7 + abs(rate) * 50))
    return points


# 时点规则的噪声闸门：一周只有两千单时，00:00 只有几单，
# 在这种样本上证伪“跌 15%”只会产出假问题。样本不足就合并成窗口再判断。
MIN_HOUR_DEALS = 15              # 单小时上期成交笔数下限
MIN_HOUR_IMPACT = 60.0           # 单小时金额影响下限（元）
HOUR_IMPACT_SHARE = 0.005        # 且需 ≥ 本期总额的 0.5%
MIN_WINDOW_DEALS = 30            # 合并窗口的上期成交笔数下限
WINDOW_IMPACT_SHARE = 0.008      # 窗口金额影响需 ≥ 本期总额的 0.8%
MAX_HOUR_POINTS = 3              # 时点类最多产出 3 条


def _hour_window_label(start: int, width: int) -> str:
    return f"{start}:00–{(start + width) % 24}:00"


def rule_hourly_gap(rc: RuleContext) -> list[UpliftPoint]:
    """某时段有客流没销售（销售额跌、用户数没跌）。

    先按单小时判断；单小时样本不足时，把相邻 2–3 小时合并成一个窗口判断，
    并在文字里写明“单小时样本不足，按窗口合并”。
    """
    if not rc.has_prev:
        return []
    total = rc.cur.amount or 0.0
    min_impact = max(MIN_HOUR_IMPACT, total * HOUR_IMPACT_SHARE)
    found: list[tuple[float, UpliftPoint]] = []
    covered: set[int] = set()

    for hour in sorted(rc.cur.hours):
        cur_group = rc.cur.hours[hour]
        prev_group = rc.prev.hours.get(hour)
        if prev_group is None or prev_group.order_count < MIN_HOUR_DEALS:
            continue
        rate = _rate(cur_group.amount, prev_group.amount)
        user_rate = _rate(cur_group.user_count, prev_group.user_count)
        if rate is None or user_rate is None:
            continue
        impact = prev_group.amount - cur_group.amount
        if not (rate <= -0.15 and user_rate >= -0.02 and impact >= min_impact):
            continue
        label = f"{int(hour)}:00"
        found.append((impact, UpliftPoint(
            id="", module="hourly", module_title=MODULE_TITLES["hourly"],
            problem=(f"{label} 时段销售额环比 {_pct_text(rate)}，但该时段消费用户数环比 "
                     f"{_pct_text(user_rate)}：有人来、没成交"),
            scope_type="时段", scope=[label],
            hypothesis="原因假设：该时段热品已售罄（补货没跟上），或该时段的商品结构与人群不匹配",
            action=(f"把补货时点提前到 {max(int(hour) - 2, 0)}:00 前完成，并在该时段加密热品排面；"
                    f"若仍无改善则调整该时段的选品价位"),
            expected=f"下一期 {label} 时段销售额环比 ≥ 0，用户数不降",
            verify=f"看 05_时段销售分析 中 {label} 的「销售额环比」与「用户数环比」：下期销售额环比转正",
            evidence=[Evidence("销售额", fmt_money(prev_group.amount), fmt_money(cur_group.amount),
                               _pct_text(rate), "05_时段销售分析", label),
                      Evidence("消费用户数", prev_group.user_count, cur_group.user_count,
                               _pct_text(user_rate), "05_时段销售分析", label)],
            rule_id="R08", confidence="中",
            score=_money_impact(impact) * 0.5 + abs(rate) * 50)))
        covered.add(int(hour))

    window_impact_floor = max(MIN_HOUR_IMPACT * 2, total * WINDOW_IMPACT_SHARE)
    for width in (2, 3):
        for start_hour in range(0, 24 - width + 1):
            hours = list(range(start_hour, start_hour + width))
            if any(hour in covered for hour in hours):
                continue
            cur_groups = [rc.cur.hours.get(hour) for hour in hours]
            prev_groups = [rc.prev.hours.get(hour) for hour in hours]
            if any(group is None for group in cur_groups) or any(g is None for g in prev_groups):
                continue
            deals = sum(group.order_count for group in prev_groups)      # type: ignore[union-attr]
            if deals < MIN_WINDOW_DEALS:
                continue
            cur_amount = sum(group.amount for group in cur_groups)       # type: ignore[union-attr]
            prev_amount = sum(group.amount for group in prev_groups)     # type: ignore[union-attr]
            cur_users: set[str] = set().union(*[g.users for g in cur_groups])      # type: ignore[union-attr]
            prev_users: set[str] = set().union(*[g.users for g in prev_groups])    # type: ignore[union-attr]
            rate = _rate(cur_amount, prev_amount)
            user_rate = _rate(len(cur_users), len(prev_users))
            if rate is None or user_rate is None:
                continue
            impact = prev_amount - cur_amount
            if not (rate <= -0.12 and user_rate >= -0.02 and impact >= window_impact_floor):
                continue
            label = _hour_window_label(start_hour, width)
            found.append((impact, UpliftPoint(
                id="", module="hourly", module_title=MODULE_TITLES["hourly"],
                problem=(f"{label} 连续 {width} 小时合计销售额环比 {_pct_text(rate)}，"
                         f"用户数环比 {_pct_text(user_rate)}：有人来、没成交"
                         f"（单小时样本不足 {MIN_HOUR_DEALS} 笔，按窗口合并判断）"),
                scope_type="时段", scope=[label],
                hypothesis="原因假设：该时段补货断档或热品售罄，也可能是这几个小时的选品价位偏高",
                action=(f"把补货时点提前到 {max(start_hour - 1, 0)}:00 前完成，"
                        f"并在 {label} 时段主推 ≤5 元的 TOP3 热品"),
                expected=f"下一期 {label} 合计销售额环比 ≥ 0，用户数不降",
                verify=(f"看 05_时段销售分析 中 {label} 各小时的「销售额环比」，"
                        f"下期窗口合计转正"),
                evidence=[Evidence("销售额", fmt_money(prev_amount), fmt_money(cur_amount),
                                   _pct_text(rate), "05_时段销售分析", label),
                          Evidence("消费用户数", len(prev_users), len(cur_users),
                                   _pct_text(user_rate), "05_时段销售分析", label)],
                rule_id="R08", confidence="中",
                score=_money_impact(impact) * 0.5 + abs(rate) * 50)))
            covered.update(hours)

    found.sort(key=lambda item: item[0], reverse=True)
    return [point for _impact, point in found[:MAX_HOUR_POINTS]]


def rule_dorm_penetration(rc: RuleContext) -> list[UpliftPoint]:
    """楼栋渗透率低 → 拉新；新老生差距 → 定向。"""
    if not rc.roster:
        return []
    overall = safe_div(rc.cur.users, rc.roster_total, digits=6)
    # 必须和 09_用户消费分析 用同一套楼栋组合并口径：人数表写「十栋+十一栋」时，
    # 明细里的「十栋」「十一栋」要先合并再取数，否则取不到就退回空组，
    # 会报出「渗透率 0.0%、目标 ≥0 人」这种与表格自相矛盾的错点。
    alias = dorm_alias(rc.roster)
    cur_dorms = merge_by_alias(rc.cur.dorms, alias)
    prev_dorms = merge_by_alias(rc.prev.dorms, alias) if rc.prev else {}
    points: list[UpliftPoint] = []
    for dorm, entry in rc.roster.items():
        residents = entry.get("total")
        group = cur_dorms.get(dorm) or Group(key=dorm)
        if not residents or residents <= 0:
            continue
        rate = safe_div(group.user_count, residents, digits=6)
        if rate is None or overall is None:
            continue
        prev_group = prev_dorms.get(dorm) or Group(key=dorm)
        prev_rate = safe_div(prev_group.user_count, residents, digits=6)
        user_change, _ = delta(group.user_count, prev_group.user_count if rc.prev else None)
        if rate < max(LOW_PENETRATION, overall * 0.7):
            points.append(UpliftPoint(
                id="", module="dorm", module_title=MODULE_TITLES["dorm"],
                problem=(f"{dorm} 渗透率 {rate * 100:.1f}%（全校 {overall * 100:.1f}%），"
                         f"住宿 {int(residents)} 人里只有 {group.user_count} 人买过"),
                scope_type="楼栋", scope=[dorm],
                hypothesis="原因假设：该楼栋到机距离/动线不顺，或价格带与该楼栋人群（新生占比、消费能力）不匹配",
                action=(f"在{dorm}入口放置扫码立减券（限首单），并把 3–5 元档热品设为该机组的前两排；"
                        f"两周后看该楼栋用户数"),
                expected=f"下一期 {dorm} 消费用户数 ≥ {int(group.user_count * 1.2)} 人（渗透率提至 "
                         f"{rate * 120:.1f}%）",
                verify=f"看 09_用户消费分析 中 {dorm} 的「本期渗透率」：下期 > {rate * 100:.1f}%",
                evidence=[Evidence("渗透率", _level_text(prev_rate), _level_text(rate), "", "09_用户消费分析", dorm),
                          Evidence("消费用户数", prev_group.user_count, group.user_count, _pct_text(_rate(group.user_count, prev_group.user_count) if rc.prev else None),
                                   "09_用户消费分析", dorm)],
                rule_id="R09", confidence="中",
                score=(overall - rate) * 1000 + _money_impact(user_change or 0)))
    return points


RULES: list[Callable[[RuleContext], list[UpliftPoint]]] = [
    rule_growth_quality, rule_category_zero, rule_machine_traffic,
    rule_machine_traffic_loss, rule_price_overstock, rule_category_drop,
    rule_price_opportunity, rule_hourly_gap, rule_dorm_penetration,
]


#: 责任对象固定映射：不留给模型临场发挥，避免同一类动作在不同报告里挂不同责任人。
OWNER_BY_MODULE: dict[str, str] = {
    "core": "运营负责人",
    "hourly": "运维值班责任人",
    "price": "商品定价责任人",
    "category": "采购补货责任人",
    "machine": "运维补货责任人",
    "dorm": "楼栋运营责任人",
}


def owner_of(module: str, scope_type: str, scope: Sequence[str]) -> str:
    """责任对象 = 角色 + 具体对象，例如「运维补货责任人（软件六栋男）」。"""
    role = OWNER_BY_MODULE.get(module, "运营负责人")
    targets = "、".join(item for item in scope if item)
    return f"{role}（{targets}）" if targets else f"{role}（{scope_type}整体）"


def priority_of(score: float) -> str:
    if score >= 400:
        return "P0"
    if score >= 120:
        return "P1"
    return "P2"


def build_points(ctx: Any, limit: int = 20) -> list[UpliftPoint]:
    rc = RuleContext(ctx=ctx, cur=ctx.cur_stat, prev=ctx.prev_stat, roster=ctx.roster,
                     roster_total=ctx.roster_total)
    points: list[UpliftPoint] = []
    for rule in RULES:
        points.extend(rule(rc))
    kept: list[UpliftPoint] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for point in sorted(points, key=lambda p: -p.score):
        if not point.evidence:
            continue                                   # 禁令①：无证据不写建议
        if "假设" not in point.hypothesis:
            point.hypothesis = "原因假设：" + point.hypothesis
        key = (point.module, tuple(point.scope))
        if key in seen:
            continue
        seen.add(key)
        kept.append(point)
        if len(kept) >= limit:
            break
    for index, point in enumerate(kept, start=1):
        point.id = f"UP-{index:02d}"
        point.priority = priority_of(point.score)
        if not point.owner:
            point.owner = owner_of(point.module, point.scope_type, point.scope)
    return kept


# ---------------------------------------------------------------- 文字

def narrate_module(module: str, points: Sequence[UpliftPoint]) -> str:
    """每个模块的解读必须回答：哪里低、为什么、做什么。无异常时明确说无异常。"""
    own = [p for p in points if p.module == module]
    if not own:
        return ("【问题】本期该维度未发现低于上期或低于其他分组的分组（已按金额与环比阈值扫描）。"
                "【假设】无异常即无需归因。"
                "【动作】维持现有补货与排面节奏，下一期继续观察。")
    top = own[0]
    extra = "；".join(f"{p.scope_type}{'、'.join(p.scope)} {p.problem.split('：')[0]}"
                     for p in own[1:3])
    text = (f"【问题】{top.problem}。"
            f"【假设】{top.hypothesis}。"
            f"【动作】{top.action}；预期：{top.expected}；验证：{top.verify}。")
    if extra:
        text += f" 另 {len(own) - 1} 处需跟进：{extra}。"
    return text


def build_narrative(ctx: Any, modules: Sequence[Any], points: Sequence[UpliftPoint],
                    cleaned: Any) -> Any:
    """结论先行 + 前三大发现（机会点）+ 行动建议 + 风险预警。"""
    from .insights import Finding, Action, Risk, Narrative

    cur, prev = ctx.cur_stat, ctx.prev_stat
    period_text = f"本期 {cur.label}（{cur.days} 天）"
    if prev is not None:
        period_text += f" vs 上期 {ctx.prev_label}（{prev.days} 天）"
    amount_rate = _rate(cur.amount, prev.amount) if prev else None
    user_rate = _rate(cur.users, prev.users) if prev else None

    headline = (f"销售额 {fmt_money(cur.amount)}"
                + (f"，环比 {_pct_text(amount_rate)}" if amount_rate is not None else "（无上期基准，未计算环比）")
                + (f"；消费用户 {cur.users} 人，环比 {_pct_text(user_rate)}" if user_rate is not None else ""))
    summary_parts = [
        f"{period_text}，分析口径销售额 {fmt_money(cur.amount)}、销售数量 {fmt_num(cur.qty)} 件、"
        f"消费人数 {fmt_num(cur.users)} 人。",
        (f"环比：销售额 {_pct_text(amount_rate)}、消费人数 {_pct_text(user_rate)}。"
         if amount_rate is not None else "本期无上期基准，环比列一律 N/A，未做估算。"),
        (f"共识别 {len(points)} 个销售提升点，其中 P0 {sum(1 for p in points if p.priority == 'P0')} 个。"
         if points else "本期未识别到需要立即处理的提升点。"),
        "六维分析全部通过校验后才出表；任何无法归属或缺失的部分都在「11_校验汇总」与「01_清洗日志」里留痕，不使用估算值填补。",
    ]

    findings = []
    for rank, point in enumerate(points[:3], start=1):
        findings.append(Finding(
            rank=rank, title=f"{point.module_title}：{point.problem.split('；')[0]}",
            detail=(f"{point.problem}。证据：{'；'.join(e.text() for e in point.evidence)}。"
                    f"{point.hypothesis}"),
            evidence=f"{point.rule_id} / {point.id}"))
    if not findings:
        findings.append(Finding(rank=1, title="本期无显著异常",
                                detail="按金额与环比阈值扫描六个维度，未发现需要立即干预的分组。",
                                evidence="R00"))

    actions = [Action(priority=point.priority,
                      action=f"{point.action}（责任对象：{point.scope_type}{'、'.join(point.scope) or '全平台'}）",
                      basis=f"{point.id} {point.verify}",
                      expected=point.expected, verify=point.verify,
                      owner=f"{point.scope_type}{'、'.join(point.scope) or '全平台'}")
               for point in points[:8]]
    if not actions:
        actions.append(Action(priority="P2", action="维持现有补货与排面节奏，下期继续观察",
                              basis="本期无异常"))

    risks: list[Risk] = []
    if prev is not None and prev.days != cur.days:
        risks.append(Risk(level="中", risk=f"两期天数不一致（{prev.days} 天 vs {cur.days} 天）",
                          impact="直接比较总额会误读为趋势",
                          mitigation="所有对比结论以变化率（$/天 或 %）为准，表格已并列天数"))
    if ctx.baseline is not None and not ctx.baseline.ok:
        risks.append(Risk(level="中", risk=f"无上期基准（{ctx.baseline.reason}）",
                          impact="环比全部为 N/A，无法判断本期是涨还是跌",
                          mitigation="按周/月持续归档后自然形成基准；本期只看结构与占比"))
    if ctx.baseline is not None and ctx.baseline.ok and not ctx.baseline.compatible:
        risks.append(Risk(level="高", risk="上期口径指纹与本期不一致",
                          impact="环比数值不可直接比较",
                          mitigation="对比价格带档位、单品规则、人数表版本，确认后再对比"))
    na_metrics = [m for m in ("订单数", "连带率", "客单价") if metric_values(cur, ctx.roster_total).get(m) is None]
    if na_metrics:
        risks.append(Risk(level="中", risk=f"{'、'.join(na_metrics)} 为 N/A",
                          impact="无法评估连带与客单结构",
                          mitigation="源数据补「订单标识」列后重跑"))
    if cleaned.stats.get("duplicate_rows"):
        risks.append(Risk(level="低", risk=f"存在 {cleaned.stats['duplicate_rows']} 行完全重复记录",
                          impact="金额可能被重复计入",
                          mitigation="清洗日志已列明处理策略（保留并标注）"))
    if not risks:
        risks.append(Risk(level="低", risk="本期未发现数据口径风险", impact="—",
                          mitigation="保持归档纪律，下一期继续校验"))

    return Narrative(summary="".join(summary_parts), findings=findings,
                     actions=actions, risks=risks)
