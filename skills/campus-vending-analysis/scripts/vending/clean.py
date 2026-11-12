"""清洗层：把 RawTable 变成可分析的 Record 列表，并逐条登记清洗日志。

原则
- 不静默丢弃：任何被排除的行都进清洗日志，附带行数与金额影响。
- 不估算：缺失的金额/数量/时间不会被补 0 或用均值替代，而是进"不可归属"桶并登记。
- 每个派生字段（机组号、单品、品类、楼栋、所属期间）都在日志里写明规则编号。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .core import (
    CleanLogEntry,
    ColumnMap,
    DormStrategy,
    InputError,
    Period,
    Record,
    parse_datetime,
    parse_number,
    normalize_text,
)
from .product import (
    UNCATEGORIZED,
    CategoryResolver,
    build_identity,
)
from .sources import RawTable

UNASSIGNED_PERIOD = "未归属"

_RE_DOOR = re.compile(r"^(.+?)[\s\-_#]*(?:门)?\s*(?:[左右lrLR])$")
_RE_TRAILING = re.compile(r"[\s\-_#]+$")


def normalize_machine(raw: Any) -> str:
    """机组号归一：左右门合并（A-01L / A-01R / A-01左 / A-01右 → A-01）。"""
    text = normalize_text(raw)
    if not text:
        return ""
    text = _RE_TRAILING.sub("", text)
    match = _RE_DOOR.match(text)
    if match:
        base = match.group(1).strip(" -_#")
        if len(base) >= 2:          # 保护形如 "L1" 的单字符主体
            return base
    return text


@dataclass
class CleanResult:
    records: list[Record]
    log: list[CleanLogEntry]
    stats: dict[str, Any]
    raw: RawTable
    sku_index: dict[str, dict[str, Any]] = field(default_factory=dict)
    product_names: list[str] = field(default_factory=list)

    @property
    def analyzed_records(self) -> list[Record]:
        return [r for r in self.records if r.period != UNASSIGNED_PERIOD]


class _Log:
    """清洗日志累加器：保证 step 编号稳定、可被表格引用。"""

    def __init__(self) -> None:
        self.entries: list[CleanLogEntry] = []

    def add(self, key: str, title: str, rule: str, input_rows: int,
            affected_rows: int, amount_delta: float = 0.0, detail: str = "",
            sample: list[str] | None = None) -> str:
        step = f"CL-{len(self.entries):02d}"
        self.entries.append(CleanLogEntry(step=step, title=title, rule=rule,
                                          input_rows=input_rows,
                                          affected_rows=affected_rows,
                                          amount_delta=round(amount_delta, 2),
                                          detail=detail, sample=(sample or [])[:8],
                                          stage=key))
        return step


def _resolve_dorm(user: str | None, machine: str, raw_dorm: Any,
                  strategy: DormStrategy, machine_map: dict[str, str],
                  pattern: re.Pattern[str] | None) -> tuple[str | None, str]:
    """返回（楼栋, 归属方式）。方式用于清洗日志与用户习惯模块的说明。"""
    if strategy.mode == "column":
        dorm = normalize_text(raw_dorm)
        return (dorm or None), "column"
    if strategy.mode == "machine_map":
        for key in (normalize_text(machine), normalize_machine(machine)):
            if key and key in machine_map:
                return machine_map[key], "machine_map"
        return None, "machine_map"
    if strategy.mode == "user_id_pattern" and pattern is not None and user:
        match = pattern.search(user)
        if match:
            value = match.groupdict().get("dorm") or (match.group(1) if match.groups() else "")
            return (normalize_text(value) or None), "user_id_pattern"
        return None, "user_id_pattern"
    return None, strategy.mode or "none"


def _period_of(moment: Any, periods: list[Period]) -> str:
    for period in periods:
        if period.contains(moment):
            return period.label
    return UNASSIGNED_PERIOD


def _empty_stats(raw: RawTable) -> dict[str, Any]:
    """清洗统计的口径骨架：所有"被排除/不可归属"的桶必须显式存在，便于对账。"""
    return {
        "raw_rows": len(raw.rows),
        "kept_rows": 0,
        "duplicate_rows": 0,
        "duplicate_amount": 0.0,
        "invalid_time_rows": 0,
        "invalid_time_amount": 0.0,
        "invalid_number_rows": 0,
        "invalid_number_amount": 0.0,
        "negative_rows": 0,
        "negative_amount": 0.0,
        "out_of_range_rows": 0,
        "out_of_range_amount": 0.0,
        "missing_user_rows": 0,
        "missing_order_rows": 0,
        "missing_order_amount": 0.0,
        "machine_renamed": 0,
        "spec_missing_rows": 0,
        "product_unrecognized": 0,
        "unmapped_rows": 0,
        "unmapped_amount": 0.0,
        "dorm_unassigned_rows": 0,
        "dorm_unassigned_amount": 0.0,
        "category_levels": {},
        "raw_total_amount": 0.0,
        "kept_total_amount": 0.0,
        "kept_total_qty": 0.0,
    }


def _detect_duplicates(raw: RawTable, columns: ColumnMap) -> tuple[set[int], float]:
    """找出逐列完全一致的行（1 基行号，含表头偏移）。返回（行号集合, 涉及金额）。"""
    seen: dict[tuple, int] = {}
    duplicate_indexes: set[int] = set()
    for index, row in enumerate(raw.rows):
        signature = tuple(normalize_text(row.get(header)) for header in raw.headers)
        if signature in seen:
            duplicate_indexes.add(index + 2)   # +2: 1 基行号且跳过表头
        else:
            seen[signature] = index
    amount = sum(parse_number(raw.rows[index - 2].get(columns.amount)) or 0.0
                 for index in duplicate_indexes)
    return duplicate_indexes, amount


def _write_log(log: _Log, *, record_count: int, stats: dict[str, Any], sku_count: int,
               resolver_size: int, strategy: DormStrategy, periods: list[Period],
               negative_policy: str, known_data_issues: list[str],
               renamed_samples: list[str], unmapped_samples: list[str],
               dorm_samples: list[str], spec_missing_samples: list[str]) -> None:
    """登记清洗日志。编号顺序即报表里的溯源依据（CL-00, CL-01…）。"""
    log.add("machine", "机组号归一（左右门合并）",
            "去掉机器编号末尾的 L/R/左/右/门 标记后合并为同一机组",
            record_count, stats["machine_renamed"], 0.0,
            f"归一后机组数={stats['machine_count']}；"
            f"示例：{', '.join(renamed_samples) or '无'}")

    log.add("product", "单品判定（规格切分 + 品牌/品名归一 + 同品别名合并）",
            "规则写死在 scripts/vending/product.py：规格进身份键；品牌/品名按词表归一；"
            "MERGE_ALIASES 合并同品异名；缺失规格标 未标注规格",
            record_count, stats["spec_missing_rows"], 0.0,
            f"识别出 {sku_count} 个单品；其中 {stats['spec_missing_rows']} 行商品名不含规格；"
            f"{stats['product_unrecognized']} 行未命中任何品名关键词（按原名单独成单品）",
            spec_missing_samples)

    log.add("category", "品类映射（只用用户提供的分类表）",
            "优先级：原始名称精确匹配 → 规范化名称 → 单品ID → 单品核心名；"
            "全部未命中记为「未分类」，不猜测",
            record_count, stats["unmapped_rows"], stats["unmapped_amount"],
            f"分类表条目={resolver_size}；映射命中分布={stats['category_levels']}；"
            f"未分类金额=¥{stats['unmapped_amount']:,.2f}",
            unmapped_samples)

    log.add("dorm", "寝室楼栋归属",
            f"策略={strategy.mode}"
            + (f"；正则={strategy.pattern}" if strategy.pattern else "")
            + (f"；机楼映射={strategy.machine_map_path}" if strategy.machine_map_path else ""),
            record_count, stats["dorm_unassigned_rows"], stats["dorm_unassigned_amount"],
            f"未归属楼栋金额=¥{stats['dorm_unassigned_amount']:,.2f}（单列为「未归属」行，不丢弃）",
            dorm_samples)

    log.add("period", "时间段归属",
            "；".join(f"{p.label}={p.start}~{p.end}({p.days}天)" for p in periods)
            + "；区间外与时间无法解析的记录统一进「未归属」桶",
            record_count, stats["unassigned_rows"], stats["unassigned_amount"],
            f"未归属（时间无效 {stats['invalid_time_rows']} 行 / 区间外 "
            f"{stats['out_of_range_rows']} 行）金额=¥{stats['unassigned_amount']:,.2f}")

    log.add("value", "数值与退款行处理",
            f"策略：非数字→不可归属；负数量/负金额（退款）→{negative_policy}",
            record_count, stats["invalid_number_rows"] + stats["negative_rows"],
            stats["invalid_number_amount"] + stats["negative_amount"],
            f"非数字 {stats['invalid_number_rows']} 行；负数 {stats['negative_rows']} 行")

    log.add("order", "订单标识完整性",
            "订单数只来自去重后的订单标识；任何缺失都会让该时间段订单数/连带率/客单价降级为 N/A",
            record_count, stats["missing_order_rows"], stats["missing_order_amount"],
            (f"缺失 {stats['missing_order_rows']} 行（金额 ¥{stats['missing_order_amount']:,.2f}）—"
             "相关指标将输出 N/A 并注明原因")
            if stats["missing_order_rows"] else "订单标识完整")

    log.add("user", "用户标识完整性",
            "用户数只来自去重后的用户标识；缺失行计入销售但无法计入消费人数",
            record_count, stats["missing_user_rows"], 0.0,
            f"缺失 {stats['missing_user_rows']} 行")

    for issue in known_data_issues:
        log.add("known_issue", "已知数据质量问题（用户预先告知）", issue, record_count, 0, 0.0,
                issue)


def clean(raw: RawTable, columns: ColumnMap, periods: list[Period],
          strategy: DormStrategy, *,
          category_map: dict[str, str] | None = None,
          machine_map: dict[str, str] | None = None,
          negative_policy: str = "exclude",
          dedupe_policy: str = "keep",
          known_data_issues: list[str] | None = None,
          unmapped_policy: str = "unclassified") -> CleanResult:
    log = _Log()
    resolver = CategoryResolver(category_map)
    machine_map = machine_map or {}
    pattern = None
    if strategy.mode == "user_id_pattern" and strategy.pattern:
        try:
            pattern = re.compile(strategy.pattern)
        except re.error as exc:
            raise InputError(f"楼栋归属正则无效：{strategy.pattern}（{exc}）",
                             hint="正则需含命名分组 dorm，例如 ^(?P<dorm>\\d+号楼)") from exc

    stats: dict[str, Any] = _empty_stats(raw)

    for warning in raw.warnings:
        log.add("header", "输入结构识别", warning, len(raw.rows), 0, 0.0, warning)
    log.add("source", "读取销售明细",
            f"格式={raw.kind}；工作表={raw.sheet or '-'}；编码={raw.encoding or '-'}；"
            f"分隔符={repr(raw.delimiter) if raw.delimiter else '-'}；表头行={raw.header_row}",
            len(raw.rows), 0, 0.0,
            f"共读取 {len(raw.rows)} 行，列名：{', '.join(raw.headers[:12])}"
            + ("…" if len(raw.headers) > 12 else ""))

    # ---- 列映射自检（防止执行期猜列）
    required = {"time": columns.time, "machine": columns.machine, "product": columns.product,
                "qty": columns.qty, "amount": columns.amount, "user": columns.user}
    missing_headers = [logical for logical, header in required.items()
                       if header and header not in raw.headers]
    if missing_headers:
        raise InputError(f"列映射指向的列在文件中不存在：{missing_headers}",
                         hint="重新运行 preflight 并确认列映射。",
                         details={"headers": raw.headers, "missing": missing_headers})

    # ---- 完全重复行
    duplicate_indexes, duplicate_amount = _detect_duplicates(raw, columns)
    if duplicate_indexes:
        stats["duplicate_rows"] = len(duplicate_indexes)
        stats["duplicate_amount"] = duplicate_amount
        log.add("dedupe", "完全重复行处理",
                f"策略={dedupe_policy}；逐列完全一致的记录视为重复，行号："
                f"{sorted(duplicate_indexes)[:8]}…",
                len(raw.rows), len(duplicate_indexes), duplicate_amount,
                ("已删除重复行（金额不计入分析口径）" if dedupe_policy == "drop_identical"
                 else "默认保留（未删除），但已在此登记，需人工确认是否为重复导出"))

    records: list[Record] = []
    skus: dict[str, dict[str, Any]] = {}
    product_names: list[str] = []
    seen_products: set[str] = set()
    renamed_samples: list[str] = []
    unmapped_samples: list[str] = []
    dorm_samples: list[str] = []
    spec_missing_samples: list[str] = []

    for index, row in enumerate(raw.rows):
        row_no = index + 2
        if dedupe_policy == "drop_identical" and row_no in duplicate_indexes:
            continue

        amount = parse_number(row.get(columns.amount))
        qty = parse_number(row.get(columns.qty))
        stats["raw_total_amount"] += amount or 0.0

        if amount is None or qty is None:
            stats["invalid_number_rows"] += 1
            stats["invalid_number_amount"] += amount or 0.0
            continue

        if amount < 0 or qty < 0:
            stats["negative_rows"] += 1
            stats["negative_amount"] += amount
            if negative_policy == "exclude":
                continue

        moment = parse_datetime(row.get(columns.time))
        product_raw = normalize_text(row.get(columns.product))
        user = normalize_text(row.get(columns.user)) or None
        order = normalize_text(row.get(columns.order)) if columns.order else ""
        order = order or None
        machine_raw = normalize_text(row.get(columns.machine))
        machine = normalize_machine(machine_raw)
        if machine and machine_raw and machine != machine_raw:
            stats["machine_renamed"] += 1
            if len(renamed_samples) < 6:
                renamed_samples.append(f"{machine_raw}→{machine}")

        identity = build_identity(product_raw, product_row=row_no)
        resolution = resolver.resolve(product_raw, identity)
        dorm, dorm_method = _resolve_dorm(user, machine_raw, row.get(columns.dorm) if columns.dorm else None,
                                          strategy, machine_map, pattern)

        flags: list[str] = list(identity.flags)
        period = UNASSIGNED_PERIOD
        if moment is None:
            stats["invalid_time_rows"] += 1
            stats["invalid_time_amount"] += amount
            flags.append("TIME_INVALID")
        else:
            period = _period_of(moment, periods)
            if period == UNASSIGNED_PERIOD:
                stats["out_of_range_rows"] += 1
                stats["out_of_range_amount"] += amount
                flags.append("OUT_OF_PERIOD")
        if user is None:
            stats["missing_user_rows"] += 1
            flags.append("USER_MISSING")
        if columns.order and order is None:
            stats["missing_order_rows"] += 1
            stats["missing_order_amount"] += amount
            flags.append("ORDER_MISSING")
        if "SPEC_MISSING" in flags:
            stats["spec_missing_rows"] += 1
            if len(spec_missing_samples) < 8:
                spec_missing_samples.append(product_raw)
        if "PRODUCT_UNRECOGNIZED" in flags:
            stats["product_unrecognized"] += 1
        category = resolution.category
        if category == UNCATEGORIZED:
            stats["unmapped_rows"] += 1
            stats["unmapped_amount"] += amount
            if len(unmapped_samples) < 10 and product_raw not in unmapped_samples:
                unmapped_samples.append(product_raw)
        if dorm is None:
            stats["dorm_unassigned_rows"] += 1
            stats["dorm_unassigned_amount"] += amount
        else:
            if len(dorm_samples) < 6 and dorm not in dorm_samples:
                dorm_samples.append(f"{user or machine_raw}→{dorm}")
        stats["category_levels"][resolution.level] = stats["category_levels"].get(resolution.level, 0) + 1

        if product_raw not in seen_products:
            seen_products.add(product_raw)
            product_names.append(product_raw)
        sku = skus.get(identity.sku_id)
        if sku is None:
            sku = {"sku_id": identity.sku_id, "name": identity.display,
                   "core": identity.core, "spec": identity.spec, "category": category,
                   "raw_names": [], "qty": 0.0, "amount": 0.0, "rows": 0}
            skus[identity.sku_id] = sku
        if product_raw not in sku["raw_names"]:
            sku["raw_names"].append(product_raw)
        sku["category"] = category if sku["category"] == UNCATEGORIZED else sku["category"]
        sku["qty"] += qty
        sku["amount"] += amount
        sku["rows"] += 1

        records.append(Record(
            row_no=row_no, ts=moment, machine_raw=machine_raw, machine=machine,
            product_raw=product_raw, sku_id=identity.sku_id, sku_name=identity.display,
            spec=identity.spec, category=category, qty=qty, amount=amount,
            user=user, order=order, dorm=dorm, period=period, flags=tuple(flags),
        ))

        if period != UNASSIGNED_PERIOD:
            stats["kept_rows"] += 1
            stats["kept_total_amount"] += amount
            stats["kept_total_qty"] += qty

    # ---- 无法归属记录（保留在 records 中，period=未归属）
    unassigned = [r for r in records if r.period == UNASSIGNED_PERIOD]
    stats["unassigned_rows"] = len(unassigned)
    stats["unassigned_amount"] = round(sum(r.amount or 0.0 for r in unassigned), 2)

    # ---- 汇总计数（须在写日志前算好，日志正文会引用）
    stats["kept_total_amount"] = round(stats["kept_total_amount"], 2)
    stats["kept_total_qty"] = round(stats["kept_total_qty"], 3)
    stats["sku_count"] = len(skus)
    stats["product_count"] = len(product_names)
    stats["machine_count"] = len({r.machine for r in records if r.machine})
    stats["dorm_count"] = len({r.dorm for r in records if r.dorm})
    stats["unclassified_names"] = unmapped_samples

    # ---- 登记日志（编号顺序即表格中的溯源依据）
    _write_log(log, record_count=len(records), stats=stats, sku_count=len(skus),
               resolver_size=resolver.size, strategy=strategy, periods=periods,
               negative_policy=negative_policy, known_data_issues=known_data_issues or [],
               renamed_samples=renamed_samples, unmapped_samples=unmapped_samples,
               dorm_samples=dorm_samples, spec_missing_samples=spec_missing_samples)

    return CleanResult(records=records, log=log.entries, stats=stats, raw=raw,
                       sku_index=skus, product_names=product_names)
