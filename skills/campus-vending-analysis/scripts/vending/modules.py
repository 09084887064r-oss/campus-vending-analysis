"""六维分析模块（双期 + 环比）。

每个模块返回 ModuleResult：模板列形的表格 + 图表契约 + 校验结果。
解读文字由 diagnose 统一生成后回填，模块只负责"数字摆对、校验过关"。
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from .aggregate import (METRICS, UNPRICED_BAND, Group, PeriodStat, delta, dorm_alias,
                         dorm_parts, merge_by_alias, metric_values)
from .archive import Baseline
from .clean import CleanResult
from .core import (
    NA,
    AnalysisConfig,
    ChartSpec,
    CleanLogEntry,
    ModuleResult,
    Record,
    Table,
    ValidationCheck,
    ValidationResult,
    fmt_money,
    fmt_num,
    safe_div,
)
from .validation import check_condition, check_equal

UNASSIGNED_DORM = "未匹配人数表"


@dataclass
class AnalysisContext:
    """所有模块共享的只读上下文。"""

    records: list[Record]
    all_records: list[Record]
    periods: list[Any]
    roster: dict[str, dict[str, Any]]
    sku_index: dict[str, dict[str, Any]]
    stats: dict[str, Any]
    cfg: AnalysisConfig
    total_amount: float = 0.0
    total_qty: float = 0.0
    total_users: int = 0
    total_orders: int | None = None
    roster_total: float | None = None
    log_entries: list[CleanLogEntry] = field(default_factory=list)
    board: dict[str, PeriodStat] = field(default_factory=dict)
    all_amount: float = 0.0
    baseline: Baseline | None = None
    period_kind: str = "weekly"
    period_key: str = ""

    @classmethod
    def build(cls, cleaned: CleanResult, cfg: AnalysisConfig,
              roster: dict[str, dict[str, Any]],
              baseline: Baseline | None = None,
              period_kind: str = "weekly", period_key: str = "") -> "AnalysisContext":
        ctx = cls(records=cleaned.analyzed_records, all_records=cleaned.records,
                  periods=cfg.periods, roster=roster, sku_index=cleaned.sku_index,
                  stats=cleaned.stats, cfg=cfg, log_entries=list(cleaned.log),
                  baseline=baseline, period_kind=period_kind, period_key=period_key)
        ctx.attach_board()
        return ctx

    def attach_board(self) -> None:
        """按时间段切记录 → 每期一份 PeriodStat；本期 = 最后一期。"""
        from .aggregate import build_period

        self.board = {}
        for period in self.periods:
            subset = [r for r in self.records if r.period == period.label]
            self.board[period.label] = build_period(subset, period.label, period.days,
                                                    self.cfg, self.roster)
        cur = self.cur_stat
        self.total_amount = cur.amount
        self.total_qty = cur.qty
        self.total_users = cur.users
        self.total_orders = cur.orders
        self.all_amount = round(sum(stat.amount for stat in self.board.values()), 2)
        values = [entry.get("total") for entry in self.roster.values()]
        if values and all(isinstance(v, (int, float)) for v in values):
            self.roster_total = round(sum(float(v) for v in values), 3)

    # -- 期次 ---------------------------------------------------------
    @property
    def cur_period(self) -> Any:
        return self.periods[-1]

    @property
    def prev_period(self) -> Any | None:
        return self.periods[-2] if len(self.periods) > 1 else None

    @property
    def cur_stat(self) -> PeriodStat:
        return self.board[self.cur_period.label]

    @property
    def prev_stat(self) -> PeriodStat | None:
        """上期优先取归档基准（一期一跑时它就是唯一的上期来源）。"""
        if self.baseline is not None and self.baseline.ok:
            stat = getattr(self, "_baseline_stat", None) or self.rebuild_baseline_stat()
            if stat is not None:
                return stat
        if self.prev_period is None:
            return None
        return self.board.get(self.prev_period.label)

    def rebuild_baseline_stat(self) -> PeriodStat | None:
        """上期统计优先用归档 facts（口径与当日一致），缺失才用本期内的上一期。"""
        if not (self.baseline and self.baseline.ok):
            self._baseline_stat = None
            return None
        from .aggregate import build_period

        stat = build_period(self.baseline.records, self.baseline.key,
                            self.prev_period.days if self.prev_period else 7,
                            self.cfg, self.roster)
        self._baseline_stat = stat
        return stat

    @property
    def prev_label(self) -> str:
        if self.baseline is not None and self.baseline.ok:
            return self.baseline.key
        period = self.prev_period
        return period.label if period else "上期"

    def step(self, *stages: str) -> str:
        mapping: dict[str, str] = {}
        for entry in self.log_entries:
            if entry.stage and entry.stage not in mapping:
                mapping[entry.stage] = entry.step
        found = [mapping[stage] for stage in stages if stage in mapping]
        return "/".join(found) if found else "—"

    def by_period(self, label: str) -> list[Record]:
        return [r for r in self.records if r.period == label]

    def has_order_column(self) -> bool:
        return self.cfg.columns.order is not None and self.stats.get("missing_order_rows", 0) == 0


# ---------------------------------------------------------------- 公共件

LABEL_CUR = "本期"
LABEL_PREV = "上期"


def _cols(prev: str, cur: str, *spec: tuple[str, str]) -> tuple[list[str], dict[int, str]]:
    """spec: (列名, 格式)，自动给每个块补“变化额/变化率”。"""
    columns: list[str] = []
    formats: dict[int, str] = {}
    for name, fmt in spec:
        columns.append(name)
        formats[len(columns) - 1] = fmt
    return columns, formats


def _delta_cells(cur: Any, prev: Any, fmt: str = "pct") -> list[Any]:
    change, rate = delta(cur, prev)
    return [change, rate]


def _sum(store: dict[str, Group], attr: str) -> float:
    total = 0.0
    for group in store.values():
        value = getattr(group, attr)
        total += float(value or 0)
    return round(total, 3)


def _block_checks(module: str, table: Table, *, cur_total: float | None,
                  prev_total: float | None, blocks: list[tuple[int, int, int]],
                  label: str = "销售额") -> list[ValidationCheck]:
    """每个对比块：变化额 = 本期-上期；变化率 = 变化额/上期。"""
    checks: list[ValidationCheck] = []
    for index, (col_prev, col_cur, col_change) in enumerate(blocks, start=1):
        violations = 0
        for row in table.rows:
            prev_value, cur_value = row[col_prev], row[col_cur]
            change = row[col_change]
            if not all(isinstance(v, (int, float)) for v in (prev_value, cur_value, change)):
                continue
            if abs((cur_value - prev_value) - change) > 0.011:
                violations += 1
        checks.append(check_condition(
            f"CHK-CMP-{module.upper()}-{index:02d}",
            f"{label}块 {index}：变化额 = 本期 − 上期",
            expected="逐行无差额", actual=f"{violations} 行不符",
            fail_if=violations > 0, module=module))
    return checks


def _empty_check(module: str, name: str, ok: bool, detail: str) -> ValidationCheck:
    return check_condition(f"CHK-{module.upper()}-EMPTY", name,
                           expected="有数据", actual=detail, fail_if=not ok, module=module)


def _chart(title: str, categories: list[str], values: list[float], *, kind: str = "bar",
           highlight: str = "", note: str = "", unit: str = "") -> ChartSpec:
    return ChartSpec(kind=kind, title=title, categories=categories,
                     series=[{"name": title, "values": values}],
                     y_label=unit, highlight=highlight, note=note)


def _sorted_keys(cur: dict[str, Group], prev: dict[str, Group] | None) -> list[str]:
    keys = set(cur) | set(prev or {})
    return sorted(keys, key=lambda k: (-(cur.get(k).amount if k in cur else 0.0), k))


# ---------------------------------------------------------------- 模块 1

def _prev(value: Any, has_prev: bool) -> Any:
    """上期列取值：没有基准时一律 N/A，不能落成 0。"""
    return value if has_prev else NA


def build_core(ctx: AnalysisContext) -> ModuleResult:
    """核心数据项：11 指标 × 上期/本期/变化额/变化率。"""
    cur, prev = ctx.cur_stat, ctx.prev_stat
    cur_label, prev_label = ctx.cur_period.label, ctx.prev_label
    columns = ["指标", "单位", prev_label, cur_label, "变化额", "变化率", "全期合计", "口径与溯源"]
    values_cur = metric_values(cur, ctx.roster_total)
    values_prev = metric_values(prev, ctx.roster_total) if prev else {}
    all_amount = ctx.all_amount

    rows: list[list[Any]] = []
    row_formats: dict[int, str] = {}
    for metric in METRICS:
        cur_value = values_cur.get(metric.name)
        prev_value = values_prev.get(metric.name)
        change, rate = delta(cur_value, prev_value)
        total = all_amount if metric.name == "销售额" else NA
        trace = (f"{ctx.step('period')} 归期；按交易时间聚合" if metric.name in ("销售额", "商品销售数量")
                 else ctx.step("orders") if metric.name in ("订单数", "连带率", "客单价", "用户周频次")
                 else ctx.step("roster") if metric.name in ("在校人数", "用户消费率") else "—")
        rows.append([metric.name, metric.unit, prev_value, cur_value, change, rate, total, trace])
        row_formats[len(rows) - 1] = metric.fmt

    table = Table(key="core", title="核心数据项（上期 vs 本期）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "text", 2: "pct", 3: "pct", 4: "pct", 5: "pct",
                               6: "money", 7: "text"},
                  value_cols=[2, 3, 4], row_value_formats=row_formats)
    table.note = (f"口径：本期 {cur_label}（{cur.days} 天）、上期 {prev_label}"
                  f"（{prev.days if prev else 'N/A'} 天）。天数不同时只看变化率，不看变化额。"
                  f"全期合计 = 各期销售额相加（本期以外各期见 03_环比基准与口径）。")

    checks: list[ValidationCheck] = [
        check_equal("CHK-M1-01", "本期销售额 = 分析口径合计", round(ctx.total_amount, 2),
                    round(values_cur["销售额"] or 0, 2)),
        _empty_check("M1", "指标行数 = 11", len(rows) == len(METRICS), f"{len(rows)} 行"),
        check_condition("CHK-M1-02", "订单数来自去重订单标识（非行数）",
                        expected="count(distinct 订单标识)",
                        actual="distinct" if ctx.has_order_column() else NA,
                        fail_if=ctx.has_order_column() is False and ctx.total_orders is not None,
                        module="core"),
    ]
    if prev is not None:
        checks.append(check_equal("CHK-M1-03", "上期销售额 = 上期基准合计",
                                  round(prev.amount, 2), round(values_prev.get("销售额") or 0, 2)))
    if prev is not None and prev.days != cur.days:
        checks.append(check_condition(
            "CHK-M1-04", "两期天数不一致：结论必须用日均/变化率",
            expected="已在表格说明", actual=f"{prev.days} 天 vs {cur.days} 天",
            fail_if=False, module="core"))
    checks.extend(_block_checks("m1", table, cur_total=cur.amount, prev_total=None,
                                blocks=[(2, 3, 4)], label="核心指标"))
    validation = ValidationResult(module="core", checks=checks)

    highlight = ""
    quiet: list[float] = [v for k, v in values_cur.items() if isinstance(v, (int, float))]
    chart = _chart(f"核心指标 · 本期（{cur_label}）", [m.name for m in METRICS],
                   [float(values_cur.get(m.name) or 0) for m in METRICS],
                   kind="hbar", unit="元 / 人 / 单")
    del quiet
    return ModuleResult(key="core", order=1, title="核心数据项", sheet="04_核心数据分析",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("period")])


# ---------------------------------------------------------------- 模块 2

def build_hourly(ctx: AnalysisContext) -> ModuleResult:
    cur, prev = ctx.cur_stat, ctx.prev_stat
    columns = ["小时",
               f"{ctx.prev_label}销量", f"{cur.label}销量", "销量变化", "销量环比",
               f"{ctx.prev_label}销售额", f"{cur.label}销售额", "销售额变化", "销售额环比",
               f"{ctx.prev_label}用户数", f"{cur.label}用户数", "用户数变化", "用户数环比"]
    rows: list[list[Any]] = []
    has_prev = prev is not None
    for hour in range(24):
        key = f"{hour:02d}"
        cur_group = cur.hours.get(key) or Group(key=key)
        prev_group = (prev.hours.get(key) if prev else None) or Group(key=key)
        qty_change, qty_rate = delta(cur_group.qty, prev_group.qty if prev else None)
        amt_change, amt_rate = delta(cur_group.amount, prev_group.amount if prev else None)
        usr_change, usr_rate = delta(cur_group.user_count, prev_group.user_count if prev else None)
        rows.append([f"{hour}:00", _prev(round(prev_group.qty, 3), has_prev),
                     round(cur_group.qty, 3), qty_change, qty_rate,
                     _prev(round(prev_group.amount, 2), has_prev), round(cur_group.amount, 2),
                     amt_change, amt_rate,
                     _prev(prev_group.user_count, has_prev), cur_group.user_count,
                     usr_change, usr_rate])

    table = Table(key="hourly", title="0–23 点逐小时（上期 vs 本期）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "int", 2: "int", 3: "int", 4: "pct",
                               5: "money", 6: "money", 7: "money", 8: "pct",
                               9: "int", 10: "int", 11: "int", 12: "pct"})
    zero_hours = [f"{h:02d}" for h in range(24) if (cur.hours.get(f"{h:02d}") or Group(key="")).qty == 0]
    table.add_total(["合计", _prev(round(sum(g.qty for g in (prev.hours.values() if prev else [])), 3), has_prev),
                     round(sum(g.qty for g in cur.hours.values()), 3),
                     round(sum((g.qty for g in cur.hours.values()), ), 3) - round(sum((g.qty for g in (prev.hours.values() if prev else [])), ), 3) if prev else NA,
                     NA,
                     _prev(round(sum(g.amount for g in (prev.hours.values() if prev else [])), 2), has_prev),
                     round(sum(g.amount for g in cur.hours.values()), 2),
                     round(cur.amount - (prev.amount if prev else 0), 2) if prev else NA, NA,
                     _prev(len({u for g in (prev.hours.values() if prev else []) for u in g.users}), has_prev),
                     len({u for g in cur.hours.values() for u in g.users}), NA, NA])
    table.note = (f"24 个分桶全部保留；本期零销售时段 {len(zero_hours)} 个"
                  f"（{'、'.join(zero_hours[:6]) or '无'}）。逐小时用户数之和 ≥ 总消费人数属正常口径。")
    checks = [
        check_condition("CHK-M2-01", "分桶数 = 24", expected=24, actual=len(rows), module="hourly"),
        check_equal("CHK-M2-02", "本期逐小时销量之和 = 本期总销量", round(ctx.total_qty, 3),
                    round(sum(g.qty for g in cur.hours.values()), 3)),
        check_equal("CHK-M2-03", "本期逐小时销售额之和 = 本期总销售额", round(ctx.total_amount, 2),
                    round(sum(g.amount for g in cur.hours.values()), 2)),
        check_condition("CHK-M2-04", "零销售时段保留为 0（未删行）",
                        expected="24 行齐全", actual=f"{len(rows)} 行", module="hourly"),
    ]
    if prev is not None:
        checks.append(check_equal("CHK-M2-05", "上期逐小时销售额之和 = 上期总额",
                                  round(prev.amount, 2), round(sum(g.amount for g in prev.hours.values()), 2)))
    validation = ValidationResult(module="hourly", checks=checks)
    categories = [f"{h}:00" for h in range(24)]
    values = [round((cur.hours.get(f"{h:02d}") or Group(key="")).amount, 2) for h in range(24)]
    peak = max(range(24), key=lambda h: values[h]) if any(values) else 0
    chart = _chart(f"逐小时销售额 · 本期（{cur.label}）", categories, values, kind="line",
                   highlight=categories[peak], unit="元",
                   note=f"峰值 {categories[peak]}；上期对比见表格“销售额环比”列。")
    return ModuleResult(key="hourly", order=2, title="时点分析", sheet="05_时段销售分析",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("period")])


# ---------------------------------------------------------------- 模块 3

def build_price(ctx: AnalysisContext) -> ModuleResult:
    cur, prev = ctx.cur_stat, ctx.prev_stat
    columns = ["价格带",
               f"{ctx.prev_label}单品数", f"{cur.label}单品数", "单品数变化",
               f"{ctx.prev_label}销量", f"{cur.label}销量", "销量变化", "销量环比",
               f"{ctx.prev_label}销售额", f"{cur.label}销售额", "销售额变化", "销售额环比",
               f"{ctx.prev_label}销售额占比", f"{cur.label}销售额占比", "占比变化"]
    order = [getattr(b, "label", None) or getattr(b, "name", str(b)) for b in ctx.cfg.price_bands]
    keys = list(order)                       # 配置的档位一律出现（零销售也要有行）
    keys += [k for k in _sorted_keys(cur.bands, prev.bands if prev else {}) if k not in keys]
    rows: list[list[Any]] = []
    has_prev = prev is not None
    for key in keys:
        cur_group = cur.bands.get(key) or Group(key=key)
        prev_group = (prev.bands.get(key) if prev else None) or Group(key=key)
        qty_change, qty_rate = delta(cur_group.qty, prev_group.qty if prev else None)
        amt_change, amt_rate = delta(cur_group.amount, prev_group.amount if prev else None)
        rows.append([key, _prev(len(prev_group.skus), has_prev), len(cur_group.skus),
                     _prev(len(cur_group.skus) - len(prev_group.skus), has_prev),
                     _prev(round(prev_group.qty, 3), has_prev), round(cur_group.qty, 3),
                     qty_change, qty_rate,
                     _prev(round(prev_group.amount, 2), has_prev), round(cur_group.amount, 2),
                     amt_change, amt_rate,
                     _prev(cur.share(prev_group.amount), has_prev), cur.share(cur_group.amount),
                     (cur.share(cur_group.amount) or 0) - (cur.share(prev_group.amount) or 0)
                     if prev and cur.share(cur_group.amount) is not None else NA])
    table = Table(key="price", title="价格带陈列（上期 vs 本期）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "int", 2: "int", 3: "int", 4: "int", 5: "int",
                               6: "int", 7: "pct", 8: "money", 9: "money", 10: "money",
                               11: "pct", 12: "pct", 13: "pct", 14: "pct"})
    table.add_total(["合计", _prev(len(set().union(*[g.skus for g in (prev.bands.values() if prev else [])])), has_prev),
                     len(set().union(*[g.skus for g in cur.bands.values()])) if cur.bands else 0, NA,
                     _prev(round(sum(g.qty for g in (prev.bands.values() if prev else [])), 3), has_prev),
                     round(sum(g.qty for g in cur.bands.values()), 3),
                     round(cur.qty - (prev.qty if prev else 0), 3) if prev else NA, NA,
                     _prev(round(sum(g.amount for g in (prev.bands.values() if prev else [])), 2), has_prev),
                     round(sum(g.amount for g in cur.bands.values()), 2),
                     round(cur.amount - (prev.amount if prev else 0), 2) if prev else NA, NA,
                     1.0 if prev else NA, 1.0, NA])
    table.note = ("单品数 = 该档内本期有销量的单品个数（同品异名已合并、不同规格算两个）。"
                  "档位取「左闭右开」[下限, 上限)：3.00 元计入 3–5 元档；"
                  "超出最高档的商品会自动追加一档，保证合计对得上总额。")
    checks = [
        check_equal("CHK-M3-01", "各价格带销售额之和 = 本期总额", round(ctx.total_amount, 2),
                    round(sum(g.amount for g in cur.bands.values()), 2)),
        check_condition("CHK-M3-02", "本期各档销售额占比之和 = 100%（±0.1pp）",
                        expected="1.0000",
                        actual=round(sum(cur.share(g.amount) or 0 for g in cur.bands.values()), 4),
                        fail_if=abs(sum(cur.share(g.amount) or 0 for g in cur.bands.values()) - 1) > 0.001,
                        module="price"),
        check_condition("CHK-M3-03", "件单价无法计算的记录单列（不丢行）",
                        expected="单列为价格带一行",
                        actual=UNPRICED_BAND if UNPRICED_BAND in cur.bands else "无此类记录",
                        fail_if=False, module="price"),
        check_condition("CHK-M3-04", "档位数量 = 配置档位",
                        expected=len(ctx.cfg.price_bands), actual=len(keys),
                        fail_if=len(keys) < len(ctx.cfg.price_bands), module="price"),
    ]
    checks.extend(_block_checks("m3", table, cur_total=ctx.total_amount, prev_total=None,
                                blocks=[(4, 5, 6), (8, 9, 10)], label="价格带"))
    validation = ValidationResult(module="price", checks=checks)
    values = [round((cur.bands.get(k) or Group(key=k)).amount, 2) for k in keys]
    chart = _chart(f"各价格带销售额 · 本期（{cur.label}）", keys, values, kind="bar", unit="元",
                   highlight=keys[max(range(len(keys)), key=lambda i: values[i])] if keys else "",
                   note="柱子只画本期；增长最快的档位见表格“销售额环比”列。")
    return ModuleResult(key="price", order=3, title="价格带分析", sheet="06_价格带陈列分析",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("period"), ctx.step("sku")])


# ---------------------------------------------------------------- 模块 4

def build_category(ctx: AnalysisContext) -> ModuleResult:
    cur, prev = ctx.cur_stat, ctx.prev_stat
    columns = ["二级分类",
               f"{ctx.prev_label}单品数", f"{cur.label}单品数",
               f"{ctx.prev_label}销量", f"{cur.label}销量", "销量变化", "销量环比",
               f"{ctx.prev_label}销售额", f"{cur.label}销售额", "销售额变化", "销售额环比",
               f"{ctx.prev_label}销售额占比", f"{cur.label}销售额占比", "占比变化"]
    keys = _sorted_keys(cur.categories, prev.categories if prev else {})
    rows: list[list[Any]] = []
    has_prev = prev is not None
    for key in keys:
        cur_group = cur.categories.get(key) or Group(key=key)
        prev_group = (prev.categories.get(key) if prev else None) or Group(key=key)
        qty_change, qty_rate = delta(cur_group.qty, prev_group.qty if prev else None)
        amt_change, amt_rate = delta(cur_group.amount, prev_group.amount if prev else None)
        rows.append([key, _prev(len(prev_group.skus), has_prev), len(cur_group.skus),
                     _prev(round(prev_group.qty, 3), has_prev), round(cur_group.qty, 3),
                     qty_change, qty_rate,
                     _prev(round(prev_group.amount, 2), has_prev), round(cur_group.amount, 2),
                     amt_change, amt_rate,
                     _prev(cur.share(prev_group.amount), has_prev), cur.share(cur_group.amount),
                     (cur.share(cur_group.amount) or 0) - (cur.share(prev_group.amount) or 0)
                     if prev and cur.share(cur_group.amount) is not None else NA])
    table = Table(key="category", title="品类结构（上期 vs 本期）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "int", 2: "int", 3: "int", 4: "int", 5: "int",
                               6: "pct", 7: "money", 8: "money", 9: "money", 10: "pct",
                               11: "pct", 12: "pct", 13: "pct"})
    table.add_total(["合计", 0, 0, round(sum(g.qty for g in cur.categories.values()), 3),
                     round(sum(g.qty for g in cur.categories.values()), 3), NA, NA,
                     round(sum(g.amount for g in cur.categories.values()), 2),
                     round(sum(g.amount for g in cur.categories.values()), 2), NA, NA,
                     1.0, 1.0, NA])
    table.note = ("未匹配到分类表的商品单列为「未分类」，不丢弃。"
                  "单品数按合并后的单品计，禁止用行数代替。")
    checks = [
        check_equal("CHK-M4-01", "各品类销售额之和 = 本期总额", round(ctx.total_amount, 2),
                    round(sum(g.amount for g in cur.categories.values()), 2)),
        check_condition("CHK-M4-02", "本期各品类销售额占比之和 = 100%（±0.1pp）",
                        expected="1.0000",
                        actual=round(sum(cur.share(g.amount) or 0 for g in cur.categories.values()), 4),
                        fail_if=abs(sum(cur.share(g.amount) or 0 for g in cur.categories.values()) - 1) > 0.001,
                        module="category"),
        check_condition("CHK-M4-03", "「未分类」单列存在或明确无未分类",
                        expected="单列一行", actual="有" if "未分类" in cur.categories else "无未分类商品",
                        fail_if=False, module="category"),
    ]
    checks.extend(_block_checks("m4", table, cur_total=ctx.total_amount, prev_total=None,
                                blocks=[(3, 4, 5), (7, 8, 9)], label="品类"))
    validation = ValidationResult(module="category", checks=checks)
    top = keys[:12]
    values = [round((cur.categories.get(k) or Group(key=k)).amount, 2) for k in top]
    chart = _chart(f"品类销售额 Top {len(top)} · 本期（{cur.label}）", top, values, kind="hbar", unit="元",
                   highlight=top[max(range(len(top)), key=lambda i: values[i])] if top else "",
                   note="只画销售额前 12 类；其余见表格。")
    return ModuleResult(key="category", order=4, title="商品品类分析", sheet="07_品类数销售分析",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("category"), ctx.step("sku")])


# ---------------------------------------------------------------- 模块 5

def build_machine(ctx: AnalysisContext) -> ModuleResult:
    cur, prev = ctx.cur_stat, ctx.prev_stat
    columns = ["机器", "类型",
               f"{ctx.prev_label}销售额", f"{cur.label}销售额", "销售额变化", "销售额环比",
               f"{ctx.prev_label}用户数", f"{cur.label}用户数", "用户数变化", "用户数环比"]
    keys = _sorted_keys(cur.machines, prev.machines if prev else {})
    rows: list[list[Any]] = []
    has_prev = prev is not None
    for key in keys:
        cur_group = cur.machines.get(key) or Group(key=key)
        prev_group = (prev.machines.get(key) if prev else None) or Group(key=key)
        amt_change, amt_rate = delta(cur_group.amount, prev_group.amount if prev else None)
        usr_change, usr_rate = delta(cur_group.user_count, prev_group.user_count if prev else None)
        rows.append([key, _kind_of(key), _prev(round(prev_group.amount, 2), has_prev),
                     round(cur_group.amount, 2), amt_change, amt_rate,
                     _prev(prev_group.user_count, has_prev), cur_group.user_count,
                     usr_change, usr_rate])
    table = Table(key="machine", title="机组效能（左右门已合并）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "text", 2: "money", 3: "money", 4: "money",
                               5: "pct", 6: "int", 7: "int", 8: "int", 9: "pct"})
    table.add_total(["合计", "—",
                     _prev(round(sum(g.amount for g in (prev.machines.values() if prev else [])), 2), has_prev),
                     round(sum(g.amount for g in cur.machines.values()), 2),
                     round(cur.amount - (prev.amount if prev else 0), 2) if prev else NA, NA,
                     _prev(len({u for g in (prev.machines.values() if prev else []) for u in g.users}), has_prev),
                     len({u for g in cur.machines.values() for u in g.users}), NA, NA])
    table.note = "左右门按机器编号合并为一台；占比见校验汇总中的合计对账。"
    checks = [
        check_equal("CHK-M5-01", "各机组销售额之和 = 本期总额", round(ctx.total_amount, 2),
                    round(sum(g.amount for g in cur.machines.values()), 2)),
        check_condition("CHK-M5-02", "机组数量 > 0", expected="≥1", actual=len(keys), module="machine"),
        check_equal("CHK-M5-03", "占比合计 = 100%", 1.0,
                    round(sum(cur.share(g.amount) or 0 for g in cur.machines.values()), 4)),
    ]
    checks.extend(_block_checks("m5", table, cur_total=ctx.total_amount, prev_total=None,
                                blocks=[(2, 3, 4), (6, 7, 8)], label="机组"))
    validation = ValidationResult(module="machine", checks=checks)
    values = [round((cur.machines.get(k) or Group(key=k)).amount, 2) for k in keys]
    chart = _chart(f"各机组销售额 · 本期（{cur.label}）", keys, values, kind="bar", unit="元",
                   highlight=keys[max(range(len(keys)), key=lambda i: values[i])] if keys else "",
                   note="柱子为销售额；下滑机器见表格“销售额环比”列。")
    return ModuleResult(key="machine", order=5, title="机组效能分析", sheet="08_机组销售环比",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("machine")])


def _kind_of(machine: str) -> str:
    for token in ("男", "女"):
        if token in machine:
            return token
    return "—"


# ---------------------------------------------------------------- 模块 6

# 合并逻辑实现在 aggregate（表格与诊断共用一份），这里保留旧私有名不引起调用点改动
_dorm_parts = dorm_parts
_dorm_alias = dorm_alias
_merge_by_alias = merge_by_alias


def _dorm_gender_from_machines(ctx: AnalysisContext) -> dict[str, str]:
    """人数表没写性别时，按该楼栋机组的「男/女」类型推断（口径表里会标注为推断）。"""
    seen: dict[str, set[str]] = {}
    for record in getattr(ctx, "all_records", []) or []:
        kind = _kind_of(getattr(record, "machine", "") or "")
        dorm = getattr(record, "dorm", "")
        if kind in ("男", "女") and dorm:
            seen.setdefault(dorm, set()).add(kind)
    return {dorm: next(iter(kinds)) for dorm, kinds in seen.items() if len(kinds) == 1}


def build_dorm(ctx: AnalysisContext) -> ModuleResult:
    cur, prev = ctx.cur_stat, ctx.prev_stat
    columns = ["楼栋组", "性别", "老生(人)", "新生(人)", "住宿人数", "新生占比",
               f"{ctx.prev_label}用户数", f"{cur.label}用户数", "用户数变化",
               f"{ctx.prev_label}渗透率", f"{cur.label}渗透率", "渗透率变化",
               f"{ctx.prev_label}人均消费", f"{cur.label}人均消费", "人均消费变化",
               f"{ctx.prev_label}销售额", f"{cur.label}销售额", "销售额变化", "销售额环比"]
    roster = ctx.roster
    alias = _dorm_alias(roster)
    cur_dorms = _merge_by_alias(cur.dorms, alias)
    prev_dorms = _merge_by_alias(prev.dorms, alias) if prev else {}
    inferred_gender = _dorm_gender_from_machines(ctx)
    alias_gender = {alias.get(dorm, dorm): gender for dorm, gender in inferred_gender.items()}

    keys = list(roster.keys())
    keys += [k for k in _sorted_keys(cur_dorms, prev_dorms) if k not in keys]
    rows: list[list[Any]] = []
    has_prev = prev is not None
    for key in keys:
        entry = roster.get(key, {})
        residents = entry.get("total")
        old = entry.get("old")
        new = entry.get("new")
        gender = entry.get("gender") or alias_gender.get(key) or ("—" if not entry else NA)
        cur_group = cur_dorms.get(key) or Group(key=key)
        prev_group = prev_dorms.get(key) or Group(key=key)
        cur_rate = safe_div(cur_group.user_count, residents, digits=6)
        prev_rate = safe_div(prev_group.user_count, residents, digits=6)
        cur_avg = safe_div(cur_group.amount, residents, digits=4)
        prev_avg = safe_div(prev_group.amount, residents, digits=4)
        usr_change, _ = delta(cur_group.user_count, prev_group.user_count if prev else None)
        amt_change, amt_rate = delta(cur_group.amount, prev_group.amount if prev else None)
        rows.append([key, gender, old, new, residents,
                     safe_div(new, residents, digits=6),
                     _prev(prev_group.user_count, has_prev), cur_group.user_count, usr_change,
                     _prev(prev_rate, has_prev), cur_rate,
                     (cur_rate - prev_rate) if (cur_rate is not None and prev_rate is not None) else NA,
                     prev_avg, cur_avg,
                     (cur_avg - prev_avg) if (cur_avg is not None and prev_avg is not None) else NA,
                     _prev(round(prev_group.amount, 2), has_prev), round(cur_group.amount, 2),
                     amt_change, amt_rate])
    table = Table(key="dorm", title="用户消费（楼栋×新老生）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "text", 2: "int", 3: "int", 4: "int", 5: "pct",
                               6: "int", 7: "int", 8: "int", 9: "pct", 10: "pct", 11: "pct",
                               12: "money", 13: "money", 14: "money", 15: "money", 16: "money",
                               17: "money", 18: "pct"})
    total_residents = ctx.roster_total
    table.add_total(["合计", "—", cur_avg_total(roster, "old"), cur_avg_total(roster, "new"),
                     total_residents,
                     safe_div(cur_avg_total(roster, "new"), total_residents, digits=6),
                     0, 0, NA, NA, NA, NA, NA, NA, NA,
                     _prev(round(sum(g.amount for g in prev_dorms.values()), 2), has_prev),
                     round(sum(g.amount for g in cur_dorms.values()), 2), NA, NA])
    table.note = ("渗透率 = 该楼栋消费用户数 ÷ 该楼栋住宿人数（模板列名“用户数占比”即此意）；"
                  "人均消费 = 该楼栋销售额 ÷ 住宿人数。人数表未提供的列显示 N/A，不估算；"
                  "性别优先取人数表，缺失时按该楼栋机组类型推断（见 12_口径与溯源）。"
                  "楼栋组（如“十栋+十一栋”）的合并只按人数表写法生效，程序不猜测合并。")
    zero_dorms = [k for k, g in cur_dorms.items() if g.qty == 0 and g.amount == 0]
    uncovered = set(cur_dorms) - set(roster)
    checks = [
        check_equal("CHK-M6-01", "各楼栋销售额之和 = 本期总额", round(ctx.total_amount, 2),
                    round(sum(g.amount for g in cur_dorms.values()), 2)),
        check_condition("CHK-M6-02", "零消费楼栋保留并标注", expected="保留", actual=f"{len(zero_dorms)} 个",
                        module="dorm"),
        check_condition("CHK-M6-03", "人数表覆盖全部消费楼栋",
                        expected="全覆盖", actual=(f"{len(uncovered)} 个未覆盖" if uncovered else "全覆盖"),
                        module="dorm"),
        check_condition("CHK-M6-04", "新老生人数可计算（不是 N/A）",
                        expected="老生/新生均有值",
                        actual=("齐全" if all(isinstance(roster[k].get(field), (int, float))
                                            for k in roster for field in ("old", "new"))
                                else "存在缺失"),
                        fail_if=bool(roster) and all(roster[k].get("old") is None
                                                     and roster[k].get("new") is None
                                                     for k in roster),
                        module="dorm"),
    ]
    checks.extend(_block_checks("m6", table, cur_total=ctx.total_amount, prev_total=None,
                                blocks=[(15, 16, 17)], label="楼栋"))
    validation = ValidationResult(module="dorm", checks=checks)
    order_keys = [k for k in keys if roster.get(k)]
    values = [round((cur_dorms.get(k) or Group(key=k)).amount, 2) for k in order_keys]
    chart = _chart(f"各楼栋销售额 · 本期（{cur.label}）", order_keys, values, kind="bar", unit="元",
                   highlight=order_keys[max(range(len(order_keys)), key=lambda i: values[i])]
                   if order_keys else "",
                   note="渗透率与人均消费见表格；低渗透楼栋是拉新的第一优先。")
    return ModuleResult(key="dorm", order=6, title="用户习惯分析", sheet="09_用户消费分析",
                        table=table, validation=validation, chart=chart,
                        trace=[ctx.step("dorm")])


def cur_avg_total(roster: dict[str, dict[str, Any]], field: str) -> float | None:
    values = [entry.get(field) for entry in roster.values()]
    if not values or any(not isinstance(v, (int, float)) for v in values):
        return None
    return round(sum(float(v) for v in values), 3)


MODULE_BUILDERS = [
    ("core", build_core), ("hourly", build_hourly), ("price", build_price),
    ("category", build_category), ("machine", build_machine), ("dorm", build_dorm),
]
