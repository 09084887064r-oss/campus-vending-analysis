"""执行前检查（preflight）：把"缺什么、含糊什么"变成可回答的问题。

行为契约
- 只体检、不分析：不产出任何业务结论。
- 有任何**阻断性问题**未确认时，写出问题清单并以退出码 3 结束；
  下游 analyze 只有在配置 confirmed=true 时才允许执行（退出码 4 否则）。
- 所有判定都给"建议答案"，让用户一次确认即可，不必从零思考。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .archive import default_root
from .core import (
    FIELD_ALIASES,
    OPTIONAL_SALES_FIELDS,
    REQUIRED_SALES_FIELDS,
    AnalysisConfig,
    ColumnMap,
    DormStrategy,
    InputError,
    NeedsConfirmation,
    Period,
    PriceBand,
    SourceFiles,
    normalize_text,
    parse_datetime,
    parse_number,
    sha256_file,
)
from .product import CategoryResolver, build_category_mapping, build_identity, build_dorm_roster
from .sources import RawTable, read_machine_map, read_table

# 默认与成品表模板一致的三档；超出 8 元的商品会自动进「8 元以上」溢出档，
# 保证各档销售额之和仍然等于总额（模板只有三档，遇到高价商品不会静默丢数）。
#
# 边界规则固定为「左闭右开」[low, high)：即 3.00 元计入 3–5 元档，而不是 0–3 元档。
# 因此标签不能写成「≤3 元」——那会让用户以为 3.00 归第一档。要含 3.00 元，
# 请把第一档上界改成 3.01，而不是改标签。（规则见 references/validation-rules.md）
DEFAULT_PRICE_BANDS: tuple[tuple[float, float | None, str], ...] = (
    (0.0, 3.0, "0–3 元"),
    (3.0, 5.0, "3–5 元"),
    (5.0, 8.0, "5–8 元"),
    (8.0, None, "8 元以上"),
)

_DORM_PATTERN_CANDIDATES: tuple[tuple[str, str], ...] = (
    (r"^(?P<dorm>\d{1,2}号楼)", "以「N号楼」开头的用户标识"),
    (r"^(?P<dorm>\d{1,2}#)", "以「N#」开头的用户标识"),
    (r"^(?P<dorm>\d{1,2}舍)", "以「N舍」开头的用户标识"),
    (r"^(?P<dorm>[A-Za-z]?\d{1,2})-", "以「区/楼-」开头的用户标识"),
    (r"^(?P<dorm>[一二三四五六七八九十]+舍)", "以「N舍」开头的用户标识"),
    (r"^(?P<dorm>\d{1,2})", "用户标识前 1–2 位为楼栋号"),
)


@dataclass
class Question:
    id: str
    title: str
    detail: str
    options: list[str]
    proposal: Any
    blocking: bool = True
    answer: Any = None
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title, "detail": self.detail,
                "options": self.options, "proposal": self.proposal,
                "blocking": self.blocking, "answer": self.answer,
                "evidence": self.evidence}


@dataclass
class PreflightResult:
    config: AnalysisConfig
    questions: list[Question]
    diagnostics: dict[str, Any]
    sales_table: RawTable
    category_table: RawTable | None = None
    dorm_table: RawTable | None = None

    @property
    def blocking_questions(self) -> list[Question]:
        return [q for q in self.questions if q.blocking]

    @property
    def unresolved(self) -> list[Question]:
        return [q for q in self.questions if q.blocking and q.answer is None]


# ---------------------------------------------------------------- 列映射推断

def _score_header(header: str, logical: str, rows: list[dict[str, Any]]) -> float:
    name = normalize_text(header).lower()
    aliases = [normalize_text(a).lower() for a in FIELD_ALIASES.get(logical, ())]
    score = 0.0
    if name in aliases:
        score = 100.0
    else:
        for alias in aliases:
            if alias and (alias in name or name in alias):
                score = max(score, 60.0)
    samples = [row.get(header) for row in rows[:50] if row.get(header) not in (None, "")]
    if samples:
        if logical == "time":
            parsed = sum(1 for value in samples if parse_datetime(value) is not None)
            score += 40.0 * parsed / len(samples)
        elif logical in {"qty", "amount"}:
            parsed = sum(1 for value in samples if parse_number(value) is not None)
            score += 30.0 * parsed / len(samples)
    return score


def propose_column_map(table: RawTable) -> tuple[ColumnMap, list[Question], dict[str, Any]]:
    headers = list(table.headers)
    scores: dict[str, dict[str, float]] = {}
    for logical in list(REQUIRED_SALES_FIELDS) + list(OPTIONAL_SALES_FIELDS):
        scores[logical] = {header: _score_header(header, logical, table.rows)
                           for header in headers}

    assigned: dict[str, str] = {}
    used: set[str] = set()
    for logical in ("time", "machine", "product", "qty", "amount", "user", "order", "dorm"):
        ranked = sorted(scores[logical].items(), key=lambda kv: -kv[1])
        for header, score in ranked:
            if score >= 60.0 and header not in used:
                assigned[logical] = header
                used.add(header)
                break

    questions: list[Question] = []
    missing = [logical for logical in REQUIRED_SALES_FIELDS if logical not in assigned]
    if missing:
        detail_parts = []
        for logical in missing:
            ranked = sorted(scores[logical].items(), key=lambda kv: -kv[1])[:3]
            detail_parts.append(
                f"{REQUIRED_SALES_FIELDS[logical]}：未找到可信列（候选 "
                + "、".join(f"{h}({s:.0f}分)" for h, s in ranked) + "）")
        questions.append(Question(
            id="Q1_column_map",
            title="必需字段与明细列名的映射",
            detail="以下必需字段无法自动判定对应列：\n" + "\n".join(detail_parts)
                   + f"\n文件实际列名：{headers}",
            options=["按我给出的映射（请在回答中列出 字段←列名）"],
            proposal={logical: assigned.get(logical, "") for logical in
                      list(REQUIRED_SALES_FIELDS) + list(OPTIONAL_SALES_FIELDS)},
            blocking=True,
            evidence=[f"{h} 示例：{', '.join(str(r.get(h)) for r in table.rows[:3])}"
                      for h in headers[:8]]))

    low_confidence = [(logical, header, scores[logical][header])
                      for logical, header in assigned.items() if scores[logical][header] < 100]
    conflict = [logical for logical in assigned
                if assigned.get("amount") == assigned.get("qty")]
    if conflict:
        questions.append(Question(
            id="Q1b_amount_qty_conflict",
            title="金额列与数量列疑似指向同一列",
            detail=f"金额与数量都映射到「{assigned['amount']}」，请明确区分",
            options=["金额←X，数量←Y"],
            proposal={"amount": "", "qty": ""}, blocking=True))

    columns = ColumnMap(
        time=assigned.get("time", ""), machine=assigned.get("machine", ""),
        product=assigned.get("product", ""), qty=assigned.get("qty", ""),
        amount=assigned.get("amount", ""), user=assigned.get("user", ""),
        order=assigned.get("order"), dorm=assigned.get("dorm"))
    diagnostics = {"column_scores": {logical: sorted(scores[logical].items(),
                                                    key=lambda kv: -kv[1])[:3]
                                     for logical in scores},
                   "low_confidence": [(l, h, round(s, 1)) for l, h, s in low_confidence]}
    return columns, questions, diagnostics


# ---------------------------------------------------------------- 时间段提案

def _month_periods(dates: list[date]) -> list[Period]:
    buckets: dict[tuple[int, int], list[date]] = {}
    for value in dates:
        buckets.setdefault((value.year, value.month), []).append(value)
    periods: list[Period] = []
    for (year, month), values in sorted(buckets.items()):
        first, last = min(values), max(values)
        periods.append(Period(label=f"{year}-{month:02d}", start=first, end=last))
    return periods


def _equal_halves(dates: list[date]) -> list[Period]:
    start, end = min(dates), max(dates)
    span = (end - start).days + 1
    if span < 4:
        return [Period(label=f"全期 {start}~{end}", start=start, end=end)]
    numbers = span // 2
    mid = start + timedelta(days=numbers - 1)
    return [Period(label=f"前半段 {start}~{mid}", start=start, end=mid),
            Period(label=f"后半段 {mid + timedelta(days=1)}~{end}",
                   start=mid + timedelta(days=1), end=end)]


def propose_periods(daily_counts: dict[date, int]) -> tuple[list[Period], Question]:
    """按数据分布提出时间段方案，并把"疑似不足整期"的月份摆到台面上。

    判定：某个自然月的记录数 < max(5, 总量 1%) → 视为疑似脏数据/系统故障，
    建议剔除（相关记录进「未归属」并在对账表单列），而不是让它参与趋势对比。
    """
    if not daily_counts:
        raise InputError("销售明细里没有任何可解析的交易时间，无法划分时间段。",
                         hint="检查时间列映射（问题 Q1）或源文件时间格式。")
    unique = sorted(daily_counts)
    start, end = unique[0], unique[-1]
    total_rows = sum(daily_counts.values())
    months = {(value.year, value.month) for value in unique}
    thin: list[tuple[Period, int]] = []
    mode = "calendar_month"

    if len(months) >= 2:
        periods = _month_periods(unique)
        why = f"数据覆盖 {len(months)} 个自然月，建议按月对比"
        counts = {p.label: sum(daily_counts[d] for d in unique if p.contains_date(d))
                  for p in periods}
        threshold = max(5, total_rows * 0.01)
        thin = [(p, counts[p.label]) for p in periods if counts[p.label] < threshold]
    elif (end - start).days >= 13:
        periods = _equal_halves(unique)
        mode, why = "equal_halves", "数据不足两个月，建议按前后两段等分对比"
    else:
        periods = [Period(label=f"全期 {start}~{end}", start=start, end=end)]
        mode, why = "whole_range", "数据跨度太短，只能作为单期描述，无法做趋势对比"

    kept = [p for p in periods if all(p.label != thin_period.label for thin_period, _ in thin)]
    days = [p.days for p in kept]
    unequal = len(set(days)) > 1
    detail = (f"{why}。数据实际范围 {start} ~ {end}（{len(unique)} 个有交易日）。")
    if thin:
        mode = "custom_ranges"
        detail += ("\n⚠ 以下时间段记录数异常少，疑似脏数据或设备故障，"
                   "建议剔除后计入「未归属」并在对账表单列：\n"
                   + "\n".join(f"  · {p.label}（{p.start}~{p.end}，{p.days} 天，仅 {count} 行）"
                               for p, count in thin)
                   + f"\n剔除后分析区间："
                   + "；".join(f"{p.label} {p.start}~{p.end}（{p.days} 天）" for p in kept))
    if unequal and kept:
        detail += ("\n注意：各期天数不同（"
                   + " / ".join(f"{p.label}={p.days}天" for p in kept)
                   + "），对比将强制使用日均口径。")
    detail += ("\n可选：calendar_month（自然月）/ week（自然周）/ custom_ranges（自定义起止）。"
               f"提议方案：{mode}")

    question = Question(
        id="Q3_periods",
        title="时间段的划分口径",
        detail=detail,
        options=["calendar_month", "custom_ranges（剔除异常期）", "week", "whole_range"],
        proposal={"mode": mode, "periods": [p.to_dict() for p in kept],
                  "excluded_thin_periods": [p.to_dict() for p, _ in thin]},
        blocking=True,
        evidence=[f"{p.label}: {p.start} ~ {p.end}（{p.days} 天，"
                  f"{sum(daily_counts[d] for d in unique if p.contains_date(d))} 行）"
                  for p in periods])
    return kept, question


# ---------------------------------------------------------------- 楼栋归属提案

def propose_dorm_strategy(table: RawTable, columns: ColumnMap,
                          roster: dict[str, dict[str, Any]] | None,
                          machine_map_path: str) -> tuple[DormStrategy, Question]:
    roster_names = set(roster or {})

    if columns.dorm and columns.dorm in table.headers:
        strategy = DormStrategy(mode="column", note=f"使用明细列「{columns.dorm}」")
        values = {normalize_text(row.get(columns.dorm)) for row in table.rows
                  if normalize_text(row.get(columns.dorm))}
        question = Question(
            id="Q6_dorm_strategy", title="寝室楼栋归属策略",
            detail=(f"明细自带楼栋列「{columns.dorm}」，出现 {len(values)} 个取值。"
                    + (f"人数表覆盖：{sorted(values & roster_names)}；未覆盖：{sorted(values - roster_names)}"
                       if roster_names else "")),
            options=["column", "user_id_pattern", "machine_map"],
            proposal={"mode": "column", "column": columns.dorm,
                      "covered": sorted(values & roster_names),
                      "missing": sorted(values - roster_names)},
            blocking=False, evidence=sorted(values)[:10])
        return strategy, question

    if machine_map_path:
        mapping = read_machine_map(machine_map_path)
        strategy = DormStrategy(mode="machine_map", machine_map_path=machine_map_path,
                                note="通过机器-楼栋映射归属")
        mapped = set(mapping.values())
        question = Question(
            id="Q6_dorm_strategy", title="寝室楼栋归属策略",
            detail=(f"使用机器-楼栋映射（{len(mapping)} 条），映射出 {len(mapped)} 个楼栋；"
                    + (f"人数表未覆盖：{sorted(mapped - roster_names)}" if roster_names else "")),
            options=["machine_map", "user_id_pattern", "column"],
            proposal={"mode": "machine_map", "machine_map_path": machine_map_path,
                      "missing_in_roster": sorted(mapped - roster_names)},
            blocking=False, evidence=sorted(mapped)[:10])
        return strategy, question

    users = [normalize_text(row.get(columns.user)) for row in table.rows[:500]
             if columns.user and row.get(columns.user)]
    import re as _re

    best: tuple[str, str, float] | None = None
    for pattern, description in _DORM_PATTERN_CANDIDATES:
        try:
            compiled = _re.compile(pattern)
        except _re.error:
            continue
        hits = [compiled.search(user) for user in users]
        matched = [m.group("dorm") for m in hits if m]
        if not matched:
            continue
        ratio = len(matched) / max(len(users), 1)
        coverage = len(set(matched) & roster_names) / max(len(set(matched)), 1) if roster_names else ratio
        score = ratio * 0.6 + coverage * 0.4
        if best is None or score > best[2]:
            best = (pattern, description, score, sorted(set(matched))[:10])

    if best is not None and best[2] >= 0.5:
        pattern, description, score, examples = best
        strategy = DormStrategy(mode="user_id_pattern", pattern=pattern,
                                note=f"用户标识正则：{description}")
        question = Question(
            id="Q6_dorm_strategy", title="寝室楼栋归属策略（用户标识解析）",
            detail=(f"用户标识看起来带有楼栋信息（{description}，命中率 {score:.0%}）。"
                    f"提议正则：{pattern}；解析出的楼栋示例：{examples}。"
                    "请确认该正则是否符合你们的学号/用户 ID 规则。"),
            options=["user_id_pattern（同意该正则）", "machine_map（用机器-楼栋映射）",
                     "column（明细里另有楼栋列）"],
            proposal={"mode": "user_id_pattern", "pattern": pattern,
                      "examples": examples},
            blocking=True, evidence=users[:5])
        return strategy, question

    strategy = DormStrategy(mode="none")
    question = Question(
        id="Q6_dorm_strategy", title="寝室楼栋归属策略（必需，当前无法推断）",
        detail=("用户习惯分析需要把每笔消费归属到寝室楼栋，但当前无法自动判断："
                "明细没有楼栋列，用户标识中没有可识别的楼栋模式，也没有提供机器-楼栋映射。"
                "请选择一种归属方式（三选一）：\n"
                "1) user_id_pattern：给出从用户标识提取楼栋的正则（需含命名分组 dorm）\n"
                "2) machine_map：给出 机器编号→楼栋 的映射文件\n"
                "3) column：确认明细中某一列就是楼栋"),
        options=["user_id_pattern", "machine_map", "column"],
        proposal={"mode": "none"},
        blocking=True, evidence=users[:5])
    return strategy, question


# ---------------------------------------------------------------- 主流程

def run_preflight(*, sales: str, categories: str = "", dorm_roster: str = "",
                  machine_map: str = "", sheet: str = "", encoding: str = "auto",
                  out_dir: str = "") -> PreflightResult:
    sales_path = Path(sales)
    table = read_table(sales_path, sheet=sheet, encoding=encoding)
    columns, questions, column_diagnostics = propose_column_map(table)

    category_table = None
    roster: dict[str, dict[str, Any]] | None = None
    category_map: dict[str, str] = {}
    if categories:
        category_table = read_table(categories)
        category_map = build_category_mapping(category_table)
    if dorm_roster:
        dorm_table = read_table(dorm_roster)
        roster = build_dorm_roster(dorm_table)
    else:
        dorm_table = None

    # ---- 必需输入的硬性检查
    if not categories:
        questions.append(Question(
            id="Q2_categories_missing", title="缺少商品分类表（必需输入）",
            detail=("商品品类分析必须依赖「商品名称 → 品类」分类表。"
                    "按契约不允许跳过该模块继续执行，请提供分类表文件。"),
            options=["提供分类表文件路径"], proposal="", blocking=True))
    if not dorm_roster:
        questions.append(Question(
            id="Q2_roster_missing", title="缺少寝室楼栋人数表（必需输入）",
            detail=("用户消费率、用户习惯分析必须依赖「楼栋 → 新老生人数」表。"
                    "按契约不允许跳过该模块继续执行，请提供人数表文件。"),
            options=["提供寝室楼栋人数表路径"], proposal="", blocking=True))

    # ---- 数据体检
    rows = table.rows
    timestamps = []
    for row in rows[:200000]:
        moment = parse_datetime(row.get(columns.time)) if columns.time else None
        if moment is not None:
            timestamps.append(moment)
    amount_values = [parse_number(row.get(columns.amount)) for row in rows] if columns.amount else []
    qty_values = [parse_number(row.get(columns.qty)) for row in rows] if columns.qty else []
    duplicates = _count_duplicates(table)
    product_names: list[str] = []
    if columns.product:
        seen: set[str] = set()
        for row in rows:
            name = normalize_text(row.get(columns.product))
            if name and name not in seen:
                seen.add(name)
                product_names.append(name)

    diagnostics: dict[str, Any] = {
        "sales_file": str(sales_path),
        "sales_rows": len(rows),
        "sheet": table.sheet,
        "encoding": table.encoding,
        "headers": table.headers,
        "column_diagnostics": column_diagnostics,
        "time_min": min(timestamps).isoformat(sep=" ") if timestamps else None,
        "time_max": max(timestamps).isoformat(sep=" ") if timestamps else None,
        "time_parsed_ratio": round(len(timestamps) / max(len(rows), 1), 4),
        "amount_parsed_ratio": round(sum(1 for v in amount_values if v is not None)
                                     / max(len(amount_values), 1), 4),
        "qty_parsed_ratio": round(sum(1 for v in qty_values if v is not None)
                                  / max(len(qty_values), 1), 4),
        "negative_amount_rows": sum(1 for v in amount_values if v is not None and v < 0),
        "duplicate_rows": duplicates,
        "product_count": len(product_names),
        "machine_count": len({normalize_text(row.get(columns.machine)) for row in rows
                              if columns.machine and row.get(columns.machine)}),
        "missing_order_ratio": _missing_ratio(table, columns.order),
        "missing_user_ratio": _missing_ratio(table, columns.user),
        "category_entries": len(category_map),
        "roster_entries": len(roster or {}),
        "category_coverage": None,
        "roster_coverage": None,
    }

    # ---- 分类表覆盖检查
    if category_map and product_names:
        resolver = CategoryResolver(category_map)
        missing_names = [name for name in product_names
                         if resolver.resolve(name, build_identity(name)).level == "unmapped"]
        diagnostics["category_coverage"] = {
            "covered": len(product_names) - len(missing_names),
            "total": len(product_names),
            "missing_sample": sorted(missing_names)[:10],
        }
        if missing_names:
            amount = 0.0
            for row in rows:
                name = normalize_text(row.get(columns.product))
                if name in set(missing_names):
                    amount += parse_number(row.get(columns.amount)) or 0.0
            diagnostics["category_coverage"]["missing_amount"] = round(amount, 2)
            questions.append(Question(
                id="Q2_category_gap", title="分类表未覆盖明细中的部分商品",
                detail=(f"{len(missing_names)}/{len(product_names)} 个商品名未在分类表中"
                        f"（涉及金额 {amount:,.2f}），例如：{sorted(missing_names)[:8]}"),
                options=["归入「未分类」并单列（推荐）", "我补充分类表后重跑", "中止分析"],
                proposal="unclassified", blocking=True,
                evidence=sorted(missing_names)[:10]))

    # ---- 楼栋人数表覆盖检查
    strategy, dorm_question = propose_dorm_strategy(table, columns, roster, machine_map)
    questions.append(dorm_question)

    # ---- 时间段
    daily_counts: dict[date, int] = {}
    for moment in timestamps:
        daily_counts[moment.date()] = daily_counts.get(moment.date(), 0) + 1
    periods, period_question = propose_periods(daily_counts)
    questions.append(period_question)

    # ---- 数据质量已知问题 / 快照 / 策略类问题（非阻断）
    if diagnostics["missing_order_ratio"] > 0:
        questions.append(Question(
            id="Q7_orders", title="订单标识缺失或不可用",
            detail=(f"明细中有 {diagnostics['missing_order_ratio']:.1%} 的订单标识为空"
                    "（或没有订单标识列）。按契约，订单数/连带率/客单价将输出 N/A，"
                    "不会用明细行数替代。是否继续？"),
            options=["继续（相关指标输出 N/A）", "我补齐订单号后重跑"],
            proposal="continue_with_na", blocking=True))
    if duplicates:
        questions.append(Question(
            id="Q8_duplicates", title="检测到完全重复的记录",
            detail=(f"有 {duplicates} 行在全部列上完全一致。若确为重复导出应删除；"
                    "若只是两笔相同交易则应保留。"),
            options=["保留（keep，默认）", "删除重复行（drop_identical）"],
            proposal="keep", blocking=True))
    if diagnostics["negative_amount_rows"]:
        questions.append(Question(
            id="Q8_negative", title="存在负金额/负数量记录",
            detail=(f"{diagnostics['negative_amount_rows']} 行为负数（退款或冲正）。"
                    "是否从分析口径中剔除？"),
            options=["剔除（exclude，默认）", "计入（include，视为净销售额）"],
            proposal="exclude", blocking=True))
    questions.append(Question(
        id="Q4_snapshot", title="是否在报表中包含「原始数据快照」工作表",
        detail="数据敏感时可选择不包含；不包含时报表会注明原因。",
        options=["包含（默认，前 2000 行）", "不包含"], proposal=True, blocking=False))
    questions.append(Question(
        id="Q5_known_issues", title="是否存在需要提前规避的数据质量问题",
        detail=("例如：某台机器在特定时段离线、系统故障导致某天数据缺失、"
                "促销活动导致价格异常等。这些会写进清洗日志与风险预警。"),
        options=["无", "有（请在回答中按行列出）"], proposal=[], blocking=False))

    config = AnalysisConfig(
        sources=SourceFiles(
            sales=str(sales_path), categories=str(categories), dorm_roster=str(dorm_roster),
            sheet=table.sheet, encoding=table.encoding,
            hashes={name: sha256_file(Path(path))
                    for name, path in (("sales", sales), ("categories", categories),
                                       ("dorm_roster", dorm_roster)) if path}),
        columns=columns, dorm=strategy, periods=periods,
        price_bands=[PriceBand(low=low, high=high, label=label)
                     for low, high, label in DEFAULT_PRICE_BANDS],
        unmapped_products="unclassified",
        negative_policy="exclude",
        dedupe_policy="keep",
        include_raw_snapshot=True,
        known_data_issues=[],
        confirmed=False,
        preflight_summary=diagnostics,
    )

    # ---- 归档根目录：决定下期能否算环比，必须由用户确认一次
    questions.append(Question(
        id="Q0_archive_root", title="历史报告与归档数据放在哪个目录",
        detail=("每期分析会写入 <数据根>/raw|processed|reports/<周期键>，下期就从这里取上期做环比。"
                "请选一个不会被清理、能长期保留的目录；确认后固定，事后换根目录会导致环比断档。"),
        options=[f"用默认目录：{default_root()}", "自定义（回答里给出绝对路径）"],
        proposal=str(default_root()), blocking=True))

    result = PreflightResult(config=config, questions=questions, diagnostics=diagnostics,
                             sales_table=table, category_table=category_table,
                             dorm_table=dorm_table)
    if out_dir:
        write_preflight_artifacts(result, Path(out_dir))
    return result


def _count_duplicates(table: RawTable) -> int:
    seen: set[tuple] = set()
    duplicates = 0
    for row in table.rows:
        signature = tuple(normalize_text(row.get(header)) for header in table.headers)
        if signature in seen:
            duplicates += 1
        else:
            seen.add(signature)
    return duplicates


def _missing_ratio(table: RawTable, column: str | None) -> float:
    if not column:
        return 1.0
    filled = sum(1 for row in table.rows if normalize_text(row.get(column)))
    return round(1 - filled / max(len(table.rows), 1), 4)


# ---------------------------------------------------------------- 产物

def write_preflight_artifacts(result: PreflightResult, out_dir: Path) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / "preflight_report.json"
    config_path = out_dir / "analysis_config.json"
    questions_path = out_dir / "preflight_questions.md"

    report_path.write_text(json.dumps({
        "diagnostics": result.diagnostics,
        "questions": [q.to_dict() for q in result.questions],
        "blocking_unresolved": [q.id for q in result.unresolved],
        "next_step": ("把 questions 逐条问用户并写回 answers，然后运行 "
                      "`run.py confirm --config analysis_config.json --answers answers.json`"),
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    result.config.save(config_path)
    questions_path.write_text(render_questions_markdown(result), encoding="utf-8")
    return {"report": str(report_path), "config": str(config_path),
            "questions": str(questions_path)}


def render_questions_markdown(result: PreflightResult) -> str:
    diagnostics = result.diagnostics
    lines = ["# 执行前确认清单（未确认前不会执行任何分析）", "",
             "## 一、数据体检", ""]
    lines.append(f"- 销售明细：`{diagnostics.get('sales_file')}`，{diagnostics.get('sales_rows')} 行，"
                 f"{len(diagnostics.get('headers', []))} 列")
    lines.append(f"- 时间范围：{diagnostics.get('time_min')} ~ {diagnostics.get('time_max')}"
                 f"（可解析比例 {diagnostics.get('time_parsed_ratio'):.1%}）")
    lines.append(f"- 金额可解析比例：{diagnostics.get('amount_parsed_ratio'):.1%}；"
                 f"数量可解析比例：{diagnostics.get('qty_parsed_ratio'):.1%}")
    lines.append(f"- 商品名去重数：{diagnostics.get('product_count')}；机组数：{diagnostics.get('machine_count')}")
    lines.append(f"- 完全重复行：{diagnostics.get('duplicate_rows')}；负金额行："
                 f"{diagnostics.get('negative_amount_rows')}")
    lines.append(f"- 分类表条目：{diagnostics.get('category_entries')}；楼栋人数表条目："
                 f"{diagnostics.get('roster_entries')}")
    coverage = diagnostics.get("category_coverage")
    if coverage:
        lines.append(f"- 分类表覆盖：{coverage['covered']}/{coverage['total']} 个商品名"
                     + (f"，未覆盖示例：{coverage['missing_sample']}"
                        if coverage.get("missing_sample") else ""))
    lines += ["", "## 二、需要你确认的问题", ""]
    for index, question in enumerate(result.questions, start=1):
        flag = "【阻断】" if question.blocking else "【可默认】"
        lines.append(f"### {index}. {flag} {question.title}")
        lines.append("")
        lines.append(question.detail.replace("\n", "\n> "))
        lines.append("")
        lines.append(f"- 可选：{' / '.join(question.options)}")
        lines.append(f"- 建议答案：`{json.dumps(question.proposal, ensure_ascii=False)}`")
        if question.evidence:
            lines.append(f"- 依据：{question.evidence}")
        lines.append("")
    if result.unresolved:
        lines += ["## 三、下一步", "",
                  f"还有 {len(result.unresolved)} 个阻断性问题未确认："
                  f"{', '.join(q.id for q in result.unresolved)}。",
                  "确认后运行 `run.py confirm`，再运行 `run.py analyze`。", ""]
    return "\n".join(lines)


def apply_answers(result: PreflightResult, answers: dict[str, Any],
                  *, accept_proposals: bool = False) -> AnalysisConfig:
    """把用户回答写回配置；仍然缺答案的阻断问题会让 confirm 失败。"""
    config = result.config
    payload = {"answers": dict(answers)}
    for question in result.questions:
        answer = answers.get(question.id)
        if answer is None:
            if question.blocking and not accept_proposals:
                continue
            answer = question.proposal
        payload["answers"][question.id] = answer

        if question.id == "Q1_column_map" and isinstance(answer, dict):
            for logical in ("time", "machine", "product", "qty", "amount", "user",
                            "order", "dorm"):
                value = answer.get(logical)
                if value:
                    setattr(config.columns, logical, value or None)
        elif question.id == "Q0_archive_root":
            config.archive_root = (str(answer).strip()
                                   if isinstance(answer, str) and str(answer).strip() else "")
        elif question.id in {"Q1b_amount_qty_conflict"} and isinstance(answer, dict):
            for logical in ("amount", "qty"):
                if answer.get(logical):
                    setattr(config.columns, logical, answer[logical])
        elif question.id == "Q3_periods":
            if isinstance(answer, dict):
                periods = answer.get("periods")
                if isinstance(periods, list) and periods:
                    config.periods = [Period.from_dict(item) for item in periods]
                mode = answer.get("mode")
                if mode:
                    payload["answers"]["period_mode"] = mode
        elif question.id == "Q6_dorm_strategy":
            if isinstance(answer, dict):
                config.dorm = DormStrategy(
                    mode=str(answer.get("mode", config.dorm.mode)),
                    pattern=str(answer.get("pattern", config.dorm.pattern)),
                    machine_map_path=str(answer.get("machine_map_path",
                                                    config.dorm.machine_map_path)))
        elif question.id == "Q2_category_gap":
            config.unmapped_products = "abort" if answer == "中止分析" else "unclassified"
        elif question.id == "Q4_snapshot":
            config.include_raw_snapshot = bool(answer)
        elif question.id == "Q5_known_issues":
            config.known_data_issues = [str(item) for item in (answer or [])]
        elif question.id == "Q7_orders":
            payload["answers"][question.id] = answer
        elif question.id == "Q8_duplicates":
            config.dedupe_policy = "drop_identical" if str(answer).startswith("删除") else "keep"
        elif question.id == "Q8_negative":
            config.negative_policy = "include" if str(answer).startswith("计入") else "exclude"

    config.answers = payload["answers"]
    config.confirmed = True
    config.confirmed_at = datetime.now().isoformat(timespec="seconds")

    missing = [question.id for question in result.questions
               if question.blocking and config.answers.get(question.id) in (None, "", {})]
    if missing and not accept_proposals:
        raise NeedsConfirmation(
            "以下阻断性问题仍缺少明确回答：" + "、".join(missing),
            hint="逐条询问用户，把回答写入 answers.json 后重跑 confirm。",
            details={"missing": missing})
    unsupported = [question.id for question in result.questions
                   if question.id in {"Q2_categories_missing", "Q2_roster_missing"}
                   and config.answers.get(question.id) in (None, "")]
    if unsupported:
        raise NeedsConfirmation(
            "缺少必需输入（商品分类表 / 寝室楼栋人数表），按契约不能继续执行。",
            hint="补齐文件后重跑 preflight；这两个模块不允许跳过。",
            details={"missing_inputs": unsupported})
    if config.unmapped_products == "abort":
        raise NeedsConfirmation(
            "用户选择先补充分类表再分析：已停止。",
            hint="补全分类表后重跑 preflight。")
    return config
