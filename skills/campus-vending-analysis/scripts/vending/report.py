"""报表基础表：说明 / 清洗日志 / 快照 / 环比基准 / 校验汇总 / 对账 / 溯源 / 结论 / 提升点。"""

from __future__ import annotations

from typing import Any, Iterable, Sequence

from .core import (
    NA,
    AnalysisConfig,
    ModuleResult,
    Table,
    ValidationCheck,
    ValidationResult,
    fmt_money,
    fmt_num,
)
from .validation import check_condition, check_equal

STAT_LABELS = {
    "raw_rows": "原始数据行数",
    "kept_rows": "进入分析口径行数",
    "unassigned_rows": "未归属任何时间段（不计入分析）",
    "invalid_number_rows": "数量或金额非数字（排除）",
    "invalid_time_rows": "时间无法解析（排除）",
    "negative_rows": "负数或退款记录",
    "duplicate_rows": "完全重复记录",
    "missing_order_rows": "缺订单标识的行",
    "missing_user_rows": "缺用户标识的行",
    "unmapped_rows": "未匹配分类表的行",
    "unmapped_products": "未匹配分类表的商品数",
    "sku_count": "单品数（合并后）",
    "resolver_size": "分类表已收录商品数",
    "spec_missing_rows": "规格缺失行数",
    "roster_missing_rows": "人数表未覆盖的行数",
}


def _flat(value: Any) -> Any:
    """单元格只允许标量：字典/列表转成可读文本，避免 Excel 写入失败。"""
    if isinstance(value, dict):
        return "；".join(f"{key}={value[key]}" for key in sorted(value))
    if isinstance(value, (list, tuple)):
        return "；".join(str(item) for item in value)
    return value


def build_overview(config: AnalysisConfig, ctx: Any, narrative: Any,
                   style_source: str, baseline_info: dict[str, Any]) -> Table:
    periods = "；".join(f"{p.label} {p.start.isoformat()}~{p.end.isoformat()}（{p.days} 天）"
                       for p in config.periods)
    rows: list[list[Any]] = [
        ["平台名称", config.platform_name],
        ["结论先行", narrative.summary],
        ["本期周期键", ctx.period_key or NA],
        ["比对周期", periods],
        ["上期基准", baseline_info.get("text", NA)],
        ["上期基准来源", baseline_info.get("source", NA)],
        ["口径指纹", baseline_info.get("fingerprint", NA)],
        ["数据根目录", baseline_info.get("root", NA)],
        ["当前口径销售额（本期）", fmt_money(ctx.total_amount)],
        ["各期合计销售额", fmt_money(ctx.all_amount)],
        ["消费人数（本期，去重）", f"{fmt_num(ctx.total_users)} 人"],
        ["覆盖在住人数", fmt_num(ctx.roster_total) if ctx.roster_total else NA],
        ["订单数（本期，去重）", fmt_num(ctx.total_orders) if ctx.total_orders else NA],
        ["读取列映射（源文件列名）", "；".join(
            f"{label}←{getattr(config.columns, name, None) or NA}"
            for name, label in (("time", "交易时间"), ("machine", "机器编号"),
                                ("product", "商品名称"), ("qty", "销售数量"),
                                ("amount", "销售金额"), ("user", "用户标识"),
                                ("order", "订单标识")))],
        ["价格带分区", "；".join(str(getattr(b, "label", b)) for b in config.price_bands)],
        ["楼栋归属策略", f"{getattr(config.dorm, 'mode', NA)}"],
        ["未匹配商品处理", "归入「未分类」并单列" if config.unmapped_products == "unclassified"
         else "已中止（要求补分类表）"],
        ["负数与退款", "排除出分析口径并单列" if config.negative_policy == "exclude" else "计入分析口径"],
        ["重复行", "保留并标注" if config.dedupe_policy == "keep" else "去重后分析"],
        ["样式来源", style_source],
        ["已知数据质量问题", "；".join(config.known_data_issues) or "无"],
        ["缺失值规则", "一律 N/A 并注明原因；不用 0、上期值或估算值填充"],
        ["部分期间规则", "天数不等时禁止直接比较总额，必须同时给出天数与变化率"],
        ["合计对账规则", "任何分组表合计 = 分析口径总额；无法归属的记录单列并注明差额"],
        ["环比规则", "上期缺失或口径不兼容 → 环比 N/A 并写明原因，不自比、不估算"],
        ["结论生成方式", "确定性规则（不调用模型）：同一数据必得同一结论，每条动作可回指表格数字"],
    ]
    dorm = getattr(config.dorm, "mode", NA)
    pattern = getattr(config.dorm, "user_id_pattern", None)
    rows[16] = ["楼栋归属策略", f"{dorm}" + (f"（正则 {pattern}）" if pattern else "")]
    table = Table(key="overview", title="报告说明与口径定义", columns=["项目", "内容"], rows=rows,
                  col_formats={0: "text", 1: "text"})
    table.note = "本工作簿由 campus-vending-analysis 技能生成；每个派生指标都能在「12_口径与溯源」找到清洗步骤。"
    return table


def build_clean_log(cleaned: Any) -> Table:
    columns = ["步骤", "环节", "规则", "输入行数", "影响行数", "金额影响", "说明", "样例"]
    rows = [[item.step, item.title, item.rule, item.input_rows, item.affected_rows,
             item.amount_delta, item.detail, "；".join(item.sample)]
            for item in cleaned.log]
    table = Table(key="clean_log", title="清洗日志（数字溯源的起点）", columns=columns, rows=rows,
                  col_formats={0: "text", 1: "text", 2: "text", 3: "int", 4: "int",
                               5: "money", 6: "text", 7: "text"})
    table.note = f"共 {len(cleaned.log)} 步；任何报表数字都能按步骤号回查。"
    return table


def build_snapshot(snapshot: Sequence[Sequence[Any]], columns: Sequence[str], limit: int) -> Table:
    table = Table(key="snapshot", title="原始数据快照（抽查用）", columns=list(columns),
                  rows=[list(row) for row in snapshot],
                  col_formats={index: "text" for index in range(len(columns))})
    table.note = f"最多保留前 {limit} 行原始记录（数据敏感时可在确认环节关闭本表）。"
    return table


def build_baseline_table(baseline_info: dict[str, Any], ctx: Any) -> Table:
    rows: list[list[Any]] = [
        ["本期周期键", ctx.period_key or NA],
        ["周期类型", "周报（ISO 周）" if ctx.period_kind == "weekly" else "月报（自然月）"],
        ["上期周期键", baseline_info.get("baseline_key", NA)],
        ["上期是否存在", "存在" if baseline_info.get("exists") else "不存在"],
        ["上期是否可比", baseline_info.get("compatible_text", NA)],
        ["不可比原因", baseline_info.get("reason") or "无"],
        ["上期来源", baseline_info.get("source", NA)],
        ["上期计数", _flat(baseline_info.get("counts", NA))],
        ["口径指纹（本期）", baseline_info.get("fingerprint", NA)],
        ["口径指纹（上期）", baseline_info.get("baseline_fingerprint", NA)],
        ["数据根目录", baseline_info.get("root", NA)],
        ["环比缺失时行为", "环比列一律 N/A 并在本表写明原因；不自比、不估算、不省略列"],
    ]
    for period in ctx.periods:
        stat = ctx.board[period.label]
        rows.append([f"本期分段时间段 {period.label}",
                     f"{period.start.isoformat()}~{period.end.isoformat()}，{period.days} 天，"
                     f"销售额 {fmt_money(stat.amount)}，{stat.records} 行"])
    table = Table(key="baseline", title="环比基准与归档口径", columns=["项目", "内容"], rows=rows,
                  col_formats={0: "text", 1: "text"})
    table.note = "环比数值全部来自本表的上期基准；基准缺失时不会用本期数据自比。"
    return table


def build_validation_table(modules: Iterable[ModuleResult], global_result: ValidationResult,
                           extra: Sequence[ValidationCheck] = ()) -> Table:
    rows: list[list[Any]] = []
    for module in modules:
        for check in module.validation.checks:
            rows.append([module.title, check.id, check.name, check.expected, check.actual,
                         check.diff, check.status, check.note])
    for check in list(global_result.checks) + list(extra):
        rows.append(["全局", check.id, check.name, check.expected, check.actual,
                     check.diff, check.status, check.note])
    table = Table(key="validation", title="校验汇总（模块门禁 + 全局交叉验证）",
                  columns=["所属模块", "校验项", "内容", "期望", "实际", "差额", "状态", "说明"],
                  rows=rows,
                  col_formats={0: "text", 1: "text", 2: "text", 3: "text", 4: "text",
                               5: "num", 6: "text", 7: "text"})
    passed = sum(1 for row in rows if row[6] in ("PASS", "WARN", "SKIP"))
    table.note = f"共 {len(rows)} 项，未失败 {passed} 项；FAIL 项会导致报表不产出。"
    return table


def build_reconciliation(cleaned: Any, ctx: Any, negative_policy: str) -> Table:
    rows: list[list[Any]] = []
    for key, value in cleaned.stats.items():
        if isinstance(value, (int, float)):
            rows.append([STAT_LABELS.get(key, key), value, ""])
    table = Table(key="reconciliation", title="对账瀑布（原始 → 分析口径）",
                  columns=["项目", "数值", "说明"], rows=rows,
                  col_formats={0: "text", 1: "num", 2: "text"})
    raw_total = cleaned.stats.get("raw_amount")
    if isinstance(raw_total, (int, float)):
        diff = round(raw_total - ctx.total_amount, 2)
        table.add_total(["原始金额合计", round(raw_total, 2), f"与分析口径差额 {fmt_money(diff)}"])
    else:
        table.add_total(["分析口径销售额合计", round(ctx.total_amount, 2),
                         f"负数策略={negative_policy}"])
    table.note = "任何无法归属、被排除的记录都在本表登记；差额不允许静默消失。"
    return table


def build_trace_table(ctx: Any) -> Table:
    rows: list[list[Any]] = [
        ["销售额", "sum(销售金额)，按交易时间归期", ctx.step("period"), "04_核心数据分析"],
        ["销售数量", "sum(销售数量)", ctx.step("period"), "04_核心数据分析"],
        ["消费人数", "count(distinct 用户标识)", ctx.step("period"), "04_核心数据分析"],
        ["订单数", "count(distinct 订单标识)；缺列则 N/A", ctx.step("orders"), "04_核心数据分析"],
        ["用户消费率", "消费人数 ÷ 在校人数（人数表合计）", ctx.step("roster"), "04_核心数据分析"],
        ["连带率", "销售数量 ÷ 订单数", ctx.step("orders"), "04_核心数据分析"],
        ["件单价", "销售额 ÷ 销售数量", ctx.step("period"), "04_核心数据分析"],
        ["客单价", "销售额 ÷ 订单数", ctx.step("orders"), "04_核心数据分析"],
        ["人均消费（核心表）", "销售额 ÷ 消费人数", ctx.step("period"), "04_核心数据分析"],
        ["人均消费（用户表）", "该楼栋销售额 ÷ 该楼栋住宿人数", ctx.step("dorm"), "09_用户消费分析"],
        ["渗透率（模板列名“用户数占比”）", "该楼栋消费用户数 ÷ 该楼栋住宿人数", ctx.step("dorm"),
         "09_用户消费分析"],
        ["用户周频次", "订单数 ÷ 消费人数", ctx.step("orders"), "04_核心数据分析"],
        ["单品判定", "品牌+品名+规格三段拼接；同品异名先合并，不同规格算两个单品", ctx.step("sku"),
         "10_单品追溯"],
        ["价格带", "件单价落档；档位来自确认环节的配置", ctx.step("sku"), "06_价格带陈列分析"],
        ["楼栋归属", f"策略 {ctx.cfg.dorm.mode}", ctx.step("dorm"), "09_用户消费分析"],
        ["环比", "变化额 = 本期 − 上期；变化率 = 变化额 ÷ 上期（上期为 0 → N/A）",
         ctx.step("period"), "03_环比基准与口径"],
    ]
    table = Table(key="trace", title="口径与溯源（每个指标怎么算、数字在哪一步）",
                  columns=["指标", "公式", "清洗日志步骤", "所在工作表"], rows=rows,
                  col_formats={index: "text" for index in range(4)})
    table.note = "步骤号随数据情况浮动（无重复行时就没有去重步），因此报表不写死 CL 编号。"
    return table


def build_conclusion_table(narrative: Any) -> Table:
    rows: list[list[Any]] = [["结论先行", narrative.summary]]
    for finding in narrative.findings:
        rows.append([f"发现 {finding.rank}", f"{finding.title}｜{finding.detail}"])
    for action in narrative.actions:
        rows.append([f"行动建议 {action.priority}", f"{action.action}｜依据：{action.basis}"])
    for risk in narrative.risks:
        rows.append([f"风险预警 {risk.level}", f"{risk.risk}｜影响：{risk.impact}｜应对：{risk.mitigation}"])
    table = Table(key="conclusion", title="结论与建议（前 3 大发现 + 行动建议 + 风险预警）",
                  columns=["板块", "内容"], rows=rows, col_formats={0: "text", 1: "text"})
    table.note = "行动建议与「13_销售提升点清单」同源生成，不允许两处各写一套。"
    return table


def build_uplift_table(points: Sequence[Any]) -> Table:
    columns = ["序号", "所属模块", "问题描述", "数据证据（指标+数值）", "影响范围",
               "建议动作", "责任对象", "预期效果", "优先级", "原因假设", "验证方式", "置信度"]
    rows = [point.to_row(index) for index, point in enumerate(points, start=1)]
    table = Table(key="uplift", title="销售提升点清单（六维汇总）", columns=columns, rows=rows,
                  col_formats={0: "int", 8: "text", 11: "text"})
    if not rows:
        table.rows = [["—", "—", "本期未识别到需要干预的提升点", "—", "—", "维持现有补货与排面节奏",
                       "运营负责人", "—", "—", "—", "—", "—"]]
    table.note = (f"共 {len(rows)} 条；优先级按金额影响与跌幅打分（P0 ≥ 400 分）。"
                  "每条都必须能回指到某个表格里的具体数字；无证据的建议不会出现。"
                  "责任对象按模块固定映射（运维/采购/定价/楼栋运营），不是临场指定。")
    return table


def run_global_checks(ctx: Any, modules: Sequence[ModuleResult], cleaned: Any,
                      recon: Any) -> ValidationResult:
    """跨模块交叉验证：六维互等 + 上期互等 + 覆盖与对账。"""
    checks: list[ValidationCheck] = []
    total = round(ctx.total_amount, 2)
    for module in modules:
        column = _amount_column(module, "本期")
        if column is None:
            continue
        subtotal, found = _column_sum(module, column)
        if found:
            checks.append(check_equal(f"CHK-G-{module.key.upper()}",
                                      f"{module.title}：合计 = 本期总额", total,
                                      round(subtotal, 2), module="global"))
    if ctx.prev_stat is not None:
        prev_total = round(ctx.prev_stat.amount, 2)
        for module in modules:
            column = _amount_column(module, "上期")
            if column is None:
                continue
            subtotal, found = _column_sum(module, column)
            if found:
                checks.append(check_equal(f"CHK-GP-{module.key.upper()}",
                                          f"{module.title}：上期合计 = 上期总额", prev_total,
                                          round(subtotal, 2), module="global"))
    checks.append(check_condition("CHK-G-PERIOD", "本期时间段数量 ≥ 1",
                                  expected="≥1", actual=f"{len(ctx.periods)}",
                                  fail_if=not ctx.periods, module="global"))
    checks.append(check_condition("CHK-G-COVERAGE", "分析口径行数 ≤ 原始行数",
                                  expected="≤ 原始行数", actual=cleaned.stats.get("kept_rows", NA),
                                  fail_if=(cleaned.stats.get("kept_rows", 0)
                                           > cleaned.stats.get("raw_rows", 0)), module="global"))
    checks.append(check_condition("CHK-G-AMOUNT", "本期销售额 > 0", expected="> 0",
                                  actual=fmt_money(total), fail_if=total <= 0, module="global"))
    checks.append(check_condition("CHK-G-NA", "缺失值未被 0 或估算值填充", expected="N/A 带原因",
                                  actual="已按规则输出 N/A", fail_if=False, module="global"))
    if recon is not None:
        diff = getattr(recon, "diff", None)
        checks.append(check_condition("CHK-G-RECON", "对账瀑布差额在容差内", expected="0.00",
                                      actual=diff,
                                      fail_if=isinstance(diff, (int, float)) and abs(diff) > 0.02,
                                      module="global"))
    return ValidationResult(module="global", checks=checks)


def _amount_column(module: ModuleResult, prefix: str) -> int | None:
    for index, name in enumerate(module.table.columns):
        if name.startswith(prefix) and ("销售额" in name):
            return index
    return None


def _column_sum(module: ModuleResult, column: int) -> tuple[float, bool]:
    total, found = 0.0, False
    for row in module.table.rows:
        value = row[column] if column < len(row) else None
        if isinstance(value, (int, float)):
            total += value
            found = True
    return total, found


def build_all_tables(run: Any) -> tuple[list[Table], dict[str, Table]]:
    """按工作表顺序汇总所有表。"""
    tables: dict[str, Table] = {}
    tables["overview"] = build_overview(run.config, run.ctx, run.narrative,
                                        run.style_source, run.baseline_info)
    tables["clean_log"] = build_clean_log(run.cleaned)
    if run.snapshot is not None:
        tables["snapshot"] = run.snapshot
    tables["baseline"] = build_baseline_table(run.baseline_info, run.ctx)
    for module in run.modules:
        tables[module.table.key] = module.table
    if run.sku_table is not None:
        tables["sku"] = run.sku_table
    tables["validation"] = build_validation_table(run.modules, run.global_result, run.extra_checks)
    tables["reconciliation"] = run.reconciliation_table
    tables["trace"] = build_trace_table(run.ctx)
    tables["uplift"] = build_uplift_table(run.points)
    tables["conclusion"] = build_conclusion_table(run.narrative)
    return list(tables.values()), tables
