"""用 tests/fixture 的模拟原始数据端到端跑一遍技能，做两类比对：

A. 独立重算（正确性判据）：不引用技能聚合代码，直接从 CSV 重算每个边际，
   与技能输出的"本期"列逐格对比，容差 0.02 元 / 1 人 / 1 件。
B. 对成品表（保真度）：与 C:\\Users\\PC\\Desktop\\新建文件夹 的结果表逐格对比，
   差异 = 模拟数据构造残差 + 口径差异，用来判断哪些格子需要人工复核。

跑法：python tests/run_golden_check.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FIXTURE = ROOT / "fixture"
ARCHIVE = ROOT / "archive"
SKILL_SCRIPTS = ROOT.parent / "skills" / "campus-vending-analysis" / "scripts"
sys.path.insert(0, str(SKILL_SCRIPTS))
sys.path.insert(0, str(ROOT))

PERIODS = {"2026-W44": "p1", "2026-W45": "p2"}
BANDS = [("≤3元", 0.0, 3.0), ("3-5元", 3.0, 5.0), ("5-8元", 5.0, 8.0)]
DORM_GROUPS = ["一栋", "二栋", "三栋", "四栋", "六栋", "七栋", "八栋", "九栋", "十栋+十一栋"]


# --------------------------------------------------------------- 独立重算

def read_rows(period_key: str) -> list[dict[str, str]]:
    with (FIXTURE / period_key / "sales_detail.csv").open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def dorm_of(machine: str) -> str:
    token = machine.replace("软件", "").rstrip("男女")
    return "十栋+十一栋" if token in ("十栋", "十一栋") else token


def recompute(period_key: str) -> dict:
    hours: dict[int, dict] = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "users": set()})
    machines: dict[str, dict] = defaultdict(lambda: {"amount": 0.0, "users": set()})
    dorms: dict[str, dict] = defaultdict(lambda: {"amount": 0.0, "users": set()})
    amount = qty = 0.0
    users: set[str] = set()
    orders: set[str] = set()
    for row in read_rows(period_key):
        price = float(row["销售金额"])
        quantity = float(row["销售数量"])
        hour = int(str(row["交易时间"]).split(" ")[1].split(":")[0])
        machine, user = row["机器编号"], row["用户标识"]
        amount += price
        qty += quantity
        users.add(user)
        orders.add(row["订单标识"])
        hours[hour]["qty"] += quantity
        hours[hour]["amount"] += price
        hours[hour]["users"].add(user)
        machines[machine]["amount"] += price
        machines[machine]["users"].add(user)
        dorms[dorm_of(machine)]["amount"] += price
        dorms[dorm_of(machine)]["users"].add(user)
    return {"amount": amount, "qty": qty, "users": users, "orders": orders,
            "hours": hours, "machines": machines, "dorms": dorms}


def category_map() -> dict[str, str]:
    with (FIXTURE / "categories.csv").open(encoding="utf-8-sig", newline="") as handle:
        return {row["商品名称"]: row["品类"] for row in csv.DictReader(handle)}


def recompute_categories(period_key: str, mapping: dict[str, str]) -> dict[str, dict]:
    result: dict[str, dict] = defaultdict(lambda: {"qty": 0.0, "amount": 0.0, "skus": set()})
    for row in read_rows(period_key):
        name = mapping.get(row["商品名称"], "未分类")
        price = float(row["销售金额"])
        quantity = float(row["销售数量"])
        result[name]["qty"] += quantity
        result[name]["amount"] += price
        result[name]["skus"].add(row["商品名称"])
    return result


def recompute_bands(period_key: str) -> dict[str, dict]:
    result: dict[str, dict] = {label: {"qty": 0.0, "amount": 0.0, "skus": set()}
                               for label, _lo, _hi in BANDS}
    for row in read_rows(period_key):
        price = float(row["销售金额"])
        quantity = float(row["销售数量"]) or 1.0
        unit = price / quantity
        if unit in (3.0, 5.0, 8.0):
            raise AssertionError(f"单价 {unit} 正好在档位边界上，口径有歧义：{row['商品名称']}")
        label = next((name for name, lo, hi in BANDS if lo <= unit < hi), "")
        if not label:
            raise AssertionError(f"单价 {unit} 越出档位：{row['商品名称']}")
        result[label]["qty"] += quantity
        result[label]["amount"] += price
        result[label]["skus"].add(row["商品名称"])
    return result


# --------------------------------------------------------------- 技能侧

def run_skill() -> dict[str, object]:
    from vending.core import PriceBand
    from vending.pipeline import analyze, archive_run, export
    from vending.preflight import apply_answers, run_preflight

    runs: dict[str, object] = {}
    for period_key in PERIODS:
        preflight = run_preflight(
            sales=str(FIXTURE / period_key / "sales_detail.csv"),
            categories=str(FIXTURE / "categories.csv"),
            dorm_roster=str(FIXTURE / "roster.csv"),
            machine_map=str(FIXTURE / "machine_map.csv"),
            out_dir=str(ROOT / "preflight" / period_key))
        config = apply_answers(preflight, {}, accept_proposals=True)
        config.price_bands = [PriceBand(low=lo, high=hi, label=label) for label, lo, hi in BANDS]
        config.save(ROOT / "preflight" / period_key / "analysis_config.json")
        run = analyze(config, data_root=str(ARCHIVE), period_kind="weekly", period_key=period_key)
        run.outputs = export(run, ROOT / "reports" / period_key)
        archive_run(run, data_root=str(ARCHIVE), force=True)
        runs[period_key] = run
    return runs


def cur(columns: list[str], label: str, *keywords: str,
        exclude: tuple[str, ...] = ("变化", "环比", "上期")) -> str | None:
    for column in columns:
        if column.startswith(label) and all(word in column for word in keywords) \
                and not any(word in column for word in exclude):
            return column
    return None


def table_map(table) -> dict[str, dict]:
    rows = {}
    for row in table.rows:
        key = str(row[0])
        if key in ("合计", "总计"):
            continue
        rows[key] = {table.columns[i]: row[i] for i in range(min(len(table.columns), len(row)))}
    return rows


# --------------------------------------------------------------- 比对

class Report:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ok = 0
        self.failed: list[dict] = []

    def check(self, scope: str, item: str, expected, actual, tol: float, unit: str = "") -> None:
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            good = abs(float(expected) - float(actual)) <= tol
        else:
            good = expected == actual
        if good:
            self.ok += 1
        else:
            self.failed.append({"scope": scope, "item": item, "expected": expected,
                                "actual": actual, "unit": unit})

    def summary(self) -> dict:
        return {"total": self.ok + len(self.failed), "failed": len(self.failed),
                "ok": self.ok, "failures": self.failed}


def check_truth(run, truth: dict, mapping: dict[str, str], period_key: str) -> Report:
    report = Report(f"A-{period_key}")
    label = str(run.ctx.period_key)
    core_table = run.tables["core"]
    core_columns = list(core_table.columns)
    core = table_map(core_table)

    def core_value(metric: str):
        row = core.get(metric, {})
        column = cur(core_columns, label)
        return row.get(column) if column else None

    report.check("核心", "销售额", round(truth["amount"], 2), core_value("销售额"), 0.02, "元")
    report.check("核心", "商品销售数量", truth["qty"], core_value("商品销售数量"), 0.01, "件")
    report.check("核心", "消费人数", len(truth["users"]), core_value("消费人数"), 0, "人")
    report.check("核心", "订单数", len(truth["orders"]), core_value("订单数"), 0, "单")
    report.check("核心", "连带率", round(truth["qty"] / len(truth["orders"]), 4),
                 core_value("连带率"), 0.0002)
    report.check("核心", "客单价", round(truth["amount"] / len(truth["orders"]), 4),
                 core_value("客单价"), 0.0002)
    report.check("核心", "件单价", round(truth["amount"] / truth["qty"], 4),
                 core_value("件单价"), 0.0002)
    report.check("核心", "人均消费", round(truth["amount"] / len(truth["users"]), 4),
                 core_value("人均消费"), 0.0002)
    report.check("核心", "用户周频次", round(len(truth["orders"]) / len(truth["users"]), 4),
                 core_value("用户周频次"), 0.0002)

    hourly_table = run.tables["hourly"]
    hourly = table_map(hourly_table)
    h_columns = list(hourly_table.columns)
    col_qty = cur(h_columns, label, "销量")
    col_amount = cur(h_columns, label, "销售额")
    col_users = cur(h_columns, label, "用户数", exclude=("变化", "环比", "上期", "占比"))
    for key, row in hourly.items():
        hour = int(key.split(":")[0])
        report.check("时段", f"{key} 销量", truth["hours"][hour]["qty"], row.get(col_qty), 0.01, "件")
        report.check("时段", f"{key} 销售额", round(truth["hours"][hour]["amount"], 2),
                     row.get(col_amount), 0.02, "元")
        report.check("时段", f"{key} 用户数", len(truth["hours"][hour]["users"]),
                     row.get(col_users), 0, "人")

    price_table = run.tables["price"]
    price = table_map(price_table)
    p_columns = list(price_table.columns)
    band_truth = recompute_bands(period_key)
    for band, _lo, _hi in BANDS:
        row = price.get(band, {})
        report.check("价格带", f"{band} 销量", band_truth[band]["qty"],
                     row.get(cur(p_columns, label, "销量")), 0.01, "件")
        report.check("价格带", f"{band} 销售额", round(band_truth[band]["amount"], 2),
                     row.get(cur(p_columns, label, "销售额",
                                 exclude=("变化", "环比", "上期", "占比"))), 0.02, "元")
        report.check("价格带", f"{band} 单品数", len(band_truth[band]["skus"]),
                     row.get(cur(p_columns, label, "单品数")), 0, "个")

    category_table = run.tables["category"]
    category = table_map(category_table)
    c_columns = list(category_table.columns)
    cat_truth = recompute_categories(period_key, mapping)
    for name, values in cat_truth.items():
        row = category.get(name, {})
        report.check("品类", f"{name} 销量", values["qty"],
                     row.get(cur(c_columns, label, "销量")), 0.01, "件")
        report.check("品类", f"{name} 销售额", round(values["amount"], 2),
                     row.get(cur(c_columns, label, "销售额",
                                 exclude=("变化", "环比", "上期", "占比"))), 0.02, "元")
        report.check("品类", f"{name} 单品数", len(values["skus"]),
                     row.get(cur(c_columns, label, "单品数")), 0, "个")

    machine_table = run.tables["machine"]
    machine = table_map(machine_table)
    m_columns = list(machine_table.columns)
    for name, values in truth["machines"].items():
        row = machine.get(name, {})
        report.check("机组", f"{name} 销售额", round(values["amount"], 2),
                     row.get(cur(m_columns, label, "销售额")), 0.02, "元")
        report.check("机组", f"{name} 用户数", len(values["users"]),
                     row.get(cur(m_columns, label, "用户数",
                                 exclude=("变化", "环比", "上期"))), 0, "人")

    dorm_table = run.tables["dorm"]
    dorm = table_map(dorm_table)
    d_columns = list(dorm_table.columns)
    for name in DORM_GROUPS:
        row = dorm.get(name, {})
        report.check("楼栋", f"{name} 行存在", True, bool(row), 0)
        if not row:
            continue
        report.check("楼栋", f"{name} 用户数", len(truth["dorms"][name]["users"]),
                     row.get(cur(d_columns, label, "用户数",
                                 exclude=("变化", "环比", "上期", "占比"))), 0, "人")
        report.check("楼栋", f"{name} 销售额", round(truth["dorms"][name]["amount"], 2),
                     row.get(cur(d_columns, label, "销售额")), 0.02, "元")
    return report


def check_fidelity(run, golden: dict, tag: str) -> Report:
    report = Report(f"B-{tag}")
    index = 0 if tag == "p1" else 1
    label = str(run.ctx.period_key)
    core_table = run.tables["core"]
    core = table_map(core_table)
    column = cur(list(core_table.columns), label)
    for metric, values in golden["core"].items():
        actual = core.get(metric, {}).get(column)
        if isinstance(actual, (int, float)):
            tolerance = 0.02 if abs(values[index]) > 100 else 0.003
            report.check("核心", metric, values[index], actual, tolerance)

    hourly_table = run.tables["hourly"]
    hourly = table_map(hourly_table)
    h_columns = list(hourly_table.columns)
    col_amount = cur(h_columns, label, "销售额")
    col_qty = cur(h_columns, label, "销量")
    worst_amount = worst_qty = 0.0
    for key, row in hourly.items():
        hour = int(key.split(":")[0])
        worst_amount = max(worst_amount,
                           abs(float(row.get(col_amount) or 0) - golden["hours"][tag][hour][1]))
        worst_qty = max(worst_qty,
                        abs(float(row.get(col_qty) or 0) - golden["hours"][tag][hour][0]))
    report.check("时段", "销售额最大偏差(元)", 0.0, round(worst_amount, 2), 0.02)
    report.check("时段", "销量最大偏差(件)", 0.0, round(worst_qty, 2), 0.02)

    machine_table = run.tables["machine"]
    machine = table_map(machine_table)
    m_columns = list(machine_table.columns)
    col_amount = cur(m_columns, label, "销售额")
    worst = max(abs(float(machine.get(name, {}).get(col_amount) or 0) - golden["machines"][tag][name][0])
                for name in golden["machines"][tag])
    report.check("机组", "销售额最大偏差(元)", 0.0, round(worst, 2), 0.02)

    price_table = run.tables["price"]
    price = table_map(price_table)
    p_columns = list(price_table.columns)
    col_amount = cur(p_columns, label, "销售额", exclude=("变化", "环比", "上期", "占比"))
    col_qty = cur(p_columns, label, "销量")
    for band in golden["bands"][tag]:
        row = price.get(band, {})
        _skus, qty, amount = golden["bands"][tag][band]
        report.check("价格带", f"{band} 销量", qty, row.get(col_qty), 0.01, "件")
        report.check("价格带", f"{band} 销售额", amount, row.get(col_amount), 0.02, "元")

    category_table = run.tables["category"]
    category = table_map(category_table)
    c_columns = list(category_table.columns)
    col_amount = cur(c_columns, label, "销售额", exclude=("变化", "环比", "上期", "占比"))
    col_qty = cur(c_columns, label, "销量")
    for name, (_skus, qty, amount) in golden["cats"][tag].items():
        row = category.get(name, {})
        report.check("品类", f"{name} 销量", qty, row.get(col_qty), 0.01, "件")
        report.check("品类", f"{name} 销售额", amount, row.get(col_amount), 0.02, "元")

    dorm_table = run.tables["dorm"]
    dorm = table_map(dorm_table)
    d_columns = list(dorm_table.columns)
    col_users = cur(d_columns, label, "用户数", exclude=("变化", "环比", "上期", "占比"))
    for name in DORM_GROUPS:
        expected = golden["dorms"][tag].get(name)
        if not expected:
            continue
        report.check("楼栋", f"{name} 用户数", expected[3], dorm.get(name, {}).get(col_users), 0, "人")
    return report


def check_chain(runs: dict, truths: dict) -> Report:
    """环比链路与派生公式：上期列必须等于上期的独立重算，变化额/变化率按定义成立。"""
    report = Report("C-环比")
    earlier, later = runs["2026-W44"], runs["2026-W45"]
    truth_earlier = truths["2026-W44"]
    truth_later = truths["2026-W45"]

    core_table = later.tables["core"]
    core = table_map(core_table)
    columns = list(core_table.columns)
    prev_label = str(earlier.ctx.period_key)
    cur_label = str(later.ctx.period_key)
    prev_col = next((c for c in columns if c == prev_label), None)
    cur_col = next((c for c in columns if c == cur_label), None)
    change_col = "变化额" if "变化额" in columns else None
    rate_col = "变化率" if "变化率" in columns else None
    report.check("环比", "核心表有上期列", True, bool(prev_col), 0)
    if prev_col and cur_col:
        report.check("环比", "上期销售额 = 上周独立重算", round(truth_earlier["amount"], 2),
                     core["销售额"].get(prev_col), 0.02, "元")
        report.check("环比", "上期消费人数 = 上周独立重算", len(truth_earlier["users"]),
                     core["消费人数"].get(prev_col), 0, "人")
        report.check("环比", "上期订单数 = 上周独立重算", len(truth_earlier["orders"]),
                     core["订单数"].get(prev_col), 0, "单")
        report.check("环比", "上期商品销售数量 = 上周独立重算", truth_earlier["qty"],
                     core["商品销售数量"].get(prev_col), 0.01, "件")
    if change_col and cur_col and prev_col:
        report.check("环比", "销售额变化额 = 本期 - 上期",
                     round(core["销售额"][cur_col] - core["销售额"][prev_col], 2),
                     round(core["销售额"][change_col], 2), 0.02, "元")
    if rate_col and change_col and prev_col:
        report.check("环比", "销售额变化率 = 变化额 / 上期",
                     round(core["销售额"][change_col] / core["销售额"][prev_col], 6),
                     round(core["销售额"][rate_col], 6), 1e-6)

    amount = truth_later["amount"]
    qty = truth_later["qty"]
    users = len(truth_later["users"])
    orders = len(truth_later["orders"])
    report.check("公式", "件单价 = 销售额 / 销量", round(amount / qty, 4), core["件单价"].get(cur_col), 0.0002)
    report.check("公式", "客单价 = 销售额 / 订单数", round(amount / orders, 4),
                 core["客单价"].get(cur_col), 0.0002)
    report.check("公式", "人均消费 = 销售额 / 消费人数", round(amount / users, 4),
                 core["人均消费"].get(cur_col), 0.0002)
    report.check("公式", "连带率 = 销量 / 订单数", round(qty / orders, 4),
                 core["连带率"].get(cur_col), 0.0002)
    report.check("公式", "用户频次 = 订单数 / 消费人数", round(orders / users, 4),
                 core["用户周频次"].get(cur_col), 0.0002)

    dorm_table = later.tables["dorm"]
    dorm = table_map(dorm_table)
    d_columns = list(dorm_table.columns)
    rows = truth_later["dorms"]
    residents = {"一栋": 891, "二栋": 604, "三栋": 897, "四栋": 249, "六栋": 1105,
                 "七栋": 1123, "八栋": 1134, "九栋": 1137, "十栋+十一栋": 2167}
    for name, total in residents.items():
        row = dorm.get(name, {})
        if not row:
            report.check("公式", f"{name} 行存在", True, False, 0)
            continue
        user_col = next((c for c in d_columns if c == cur_label and "用户数" in c
                         and "占比" not in c and "变化" not in c), None)
        rate_column = next((c for c in d_columns if c == cur_label and "渗透率" in c), None)
        per_column = next((c for c in d_columns if c == cur_label and "人均消费" in c), None)
        if user_col:
            report.check("公式", f"{name} 用户数 = 独立重算", len(rows[name]["users"]),
                         row[user_col], 0, "人")
        if rate_column:
            report.check("公式", f"{name} 渗透率 = 用户数 / 住宿人数",
                         round(len(rows[name]["users"]) / total, 6),
                         round(row[rate_column], 6), 1e-6)
        if per_column:
            report.check("公式", f"{name} 人均消费 = 销售额 / 住宿人数",
                         round(rows[name]["amount"] / total, 4), round(row[per_column], 4), 0.0002)

    for module in later.modules:
        report.check("洞察", f"{module.title} 含【问题】", True, "【问题】" in module.insight, 0)
        report.check("洞察", f"{module.title} 含【假设】", True, "【假设】" in module.insight, 0)
        report.check("洞察", f"{module.title} 含【动作】", True, "【动作】" in module.insight, 0)

    outputs = dict(later.outputs)
    for kind in ("excel", "html", "docx"):
        path = Path(str(outputs.get(kind, "")))
        report.check("导出", f"{kind} 已生成且非空", True,
                     path.is_file() and path.stat().st_size > 2000, 0)
    points = later.points
    report.check("提升点", "至少 1 条销售提升点", True, len(points) >= 1, 0)
    for point in points:
        report.check("提升点", f"{point.id} 指出问题", True, bool(point.problem.strip()), 0)
        report.check("提升点", f"{point.id} 有原因假设", True, "假设" in point.hypothesis, 0)
        report.check("提升点", f"{point.id} 有可落地动作", True, bool(point.action.strip()), 0)
        report.check("提升点", f"{point.id} 责任对象=角色+对象", True,
                     bool(point.owner) and "（" in point.owner, 0)
        report.check("提升点", f"{point.id} 有验证方式", True, bool(point.verify.strip()), 0)
        report.check("提升点", f"{point.id} 门槛可量化", True,
                     any(token in (point.verify + point.expected)
                         for token in ("≥", "≤", ">", "<", "回升", "转正")), 0)
        report.check("提升点", f"{point.id} 有证据", True, len(point.evidence) >= 1, 0)

    # 楼栋类提升点必须和 09 表用同一套“楼栋组合并”口径：
    # 曾经出现过 diagnose 直接用人数表组名去未合并的 stat 里取，取不到就报“渗透率 0.0%”。
    dorm_table = later.tables["dorm"]
    dorm_rows = {str(row[0]): row for row in dorm_table.rows if row and row[0]}
    dorm_columns = list(dorm_table.columns)
    rate_idx = next((i for i, c in enumerate(dorm_columns)
                     if c.startswith(str(later.ctx.period_key)) and "渗透率" in c
                     and "变化" not in c), None)
    user_idx = next((i for i, c in enumerate(dorm_columns)
                     if c.startswith(str(later.ctx.period_key)) and c.endswith("用户数")), None)
    for point in points:
        if point.module != "dorm":
            continue
        for scope in point.scope:
            row = dorm_rows.get(str(scope))
            report.check("提升点", f"{point.id} 楼栋 {scope} 在 09 表有行", True, row is not None, 0)
            if row is None:
                continue
            if rate_idx is not None and isinstance(row[rate_idx], (int, float)):
                text = next((item.cur for item in point.evidence if item.metric == "渗透率"), "")
                shown = float(str(text).replace("%", "").strip() or 0) / 100
                report.check("提升点", f"{point.id} {scope} 渗透率与 09 表一致",
                             round(row[rate_idx], 4), round(shown, 4), 0.0015)
            if user_idx is not None and isinstance(row[user_idx], (int, float)):
                text = next((item.cur for item in point.evidence if item.metric == "消费用户数"), "")
                report.check("提升点", f"{point.id} {scope} 消费用户数与 09 表一致",
                             int(row[user_idx]), int(str(text).strip() or -1), 0)
    return report


def main() -> int:
    from make_fixture import load_golden

    golden = load_golden()
    mapping = category_map()
    runs = run_skill()
    truth_reports, fidelity_reports = {}, {}
    truths = {period_key: recompute(period_key) for period_key in PERIODS}
    for period_key, run in runs.items():
        tag = PERIODS[period_key]
        report_a = check_truth(run, truths[period_key], mapping, period_key)
        report_b = check_fidelity(run, golden, tag)
        truth_reports[period_key] = report_a.summary()
        fidelity_reports[period_key] = report_b.summary()
        print(f"== {period_key}")
        summary = report_a.summary()
        print(f"   A 独立重算：{summary['total']} 项 / 通过 {summary['ok']} / 失败 {summary['failed']}")
        for item in summary["failures"][:15]:
            print(f"      DIFF {item['scope']}/{item['item']}: 重算 {item['expected']} 技能 {item['actual']}")
        summary = report_b.summary()
        print(f"   B 对成品表：{summary['total']} 项 / 通过 {summary['ok']} / 超差 {summary['failed']}")
        for item in summary["failures"][:10]:
            print(f"      ~ {item['scope']}/{item['item']}: 成品 {item['expected']} 模拟 {item['actual']}")
    report_c = check_chain(runs, truths)
    summary_c = report_c.summary()
    truth_reports["C-环比与公式"] = summary_c
    print("== 环比 / 公式 / 导出 / 提升点")
    print(f"   C：{summary_c['total']} 项 / 通过 {summary_c['ok']} / 失败 {summary_c['failed']}")
    for item in summary_c["failures"][:15]:
        print(f"      DIFF {item['scope']}/{item['item']}: 期望 {item['expected']} 实得 {item['actual']}")

    (ROOT / "report_truth.json").write_text(
        json.dumps(truth_reports, ensure_ascii=False, indent=1), encoding="utf-8")
    (ROOT / "report_fidelity.json").write_text(
        json.dumps(fidelity_reports, ensure_ascii=False, indent=1), encoding="utf-8")
    failed = sum(item["failed"] for item in truth_reports.values())
    print(f"\n合计：独立重算失败 {failed} 项")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
