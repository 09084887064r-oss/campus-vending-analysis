"""校验层：模块级校验 + 全局对账 + 交叉验证。

三类状态
- PASS：数值吻合（在容差内）
- WARN：差额已被明确解释（未分类金额、未归属记录、天数不等…），且解释已写进报告
- FAIL：出现无法解释的差额 → 门禁拦截，停止后续模块

FAIL 一律阻断，绝不"调低阈值让它通过"。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .core import (
    GateFailed,
    ModuleResult,
    ValidationCheck,
    ValidationResult,
    fmt_money,
)

MONEY_TOL = 0.01
PCT_TOL = 0.1


def money_total(values: Iterable[Any]) -> float:
    total = 0.0
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += float(value)
    return round(total, 2)


def sum_column(table: "object", index: int) -> float:
    rows = getattr(table, "rows", [])
    return money_total(row[index] if index < len(row) else None for row in rows)


def check_equal(check_id: str, name: str, expected: Any, actual: Any, *,
                module: str = "", tol: float = MONEY_TOL,
                explained_by: str = "", note: str = "",
                fmt: str = "money") -> ValidationCheck:
    """数值相等校验。差额存在但已被解释 → WARN；无法解释 → FAIL。"""
    if not isinstance(expected, (int, float)) or not isinstance(actual, (int, float)):
        return ValidationCheck(id=check_id, name=name, expected=expected, actual=actual,
                               diff=None, status="SKIP", note=note or "存在 N/A，无法做数值对账",
                               module=module)
    diff = round(float(actual) - float(expected), 2)
    formatter = fmt_money if fmt == "money" else (lambda v: f"{v:,.2f}")
    base_note = note
    if abs(diff) <= tol:
        return ValidationCheck(id=check_id, name=name, expected=expected, actual=actual,
                               diff=0.0, status="PASS", note=base_note, module=module)
    if explained_by:
        return ValidationCheck(id=check_id, name=name, expected=expected, actual=actual,
                               diff=diff, status="WARN",
                               note=f"差额 {formatter(diff)} 已解释：{explained_by}"
                                    + (f"；{base_note}" if base_note else ""),
                               module=module)
    return ValidationCheck(id=check_id, name=name, expected=expected, actual=actual,
                           diff=diff, status="FAIL",
                           note=f"无法解释的差额 {formatter(diff)}"
                                + (f"；{base_note}" if base_note else ""),
                           module=module)


def check_pct_sum(check_id: str, name: str, total_pct: float, *,
                  module: str = "", tol: float = PCT_TOL,
                  explained_by: str = "",
                  note: str = "") -> ValidationCheck:
    diff = round(float(total_pct) - 100.0, 4)
    if abs(diff) <= tol:
        return ValidationCheck(id=check_id, name=name, expected=100.0, actual=round(total_pct, 4),
                               diff=diff, status="PASS", note=note, module=module)
    if explained_by:
        return ValidationCheck(id=check_id, name=name, expected=100.0, actual=round(total_pct, 4),
                               diff=diff, status="WARN",
                               note=f"占比合计 {total_pct:.2f}% 与 100% 的差异已解释：{explained_by}",
                               module=module)
    return ValidationCheck(id=check_id, name=name, expected=100.0, actual=round(total_pct, 4),
                           diff=diff, status="FAIL",
                           note=f"占比合计 {total_pct:.2f}%，超出 ±{tol}% 容差；四舍五入不足以解释",
                           module=module)


def check_condition(check_id: str, name: str, expected: Any, actual: Any, *,
                    module: str = "", status: str = "PASS", note: str = "",
                    fail_if: bool = False) -> ValidationCheck:
    """非数值校验（行数保留、来源正确性、口径提示等）。

    fail_if=True 时直接判 FAIL —— 用于"这条规则不满足就不许出表"的硬约束。
    """
    if fail_if:
        status = "FAIL"
    return ValidationCheck(id=check_id, name=name, expected=expected, actual=actual,
                           diff=None, status=status, note=note, module=module)


def gate(result: ValidationResult) -> None:
    """模块门禁：校验未通过立即中断，不进入下一个模块。"""
    if result.status == "FAIL":
        failed = result.blockers
        detail = "；".join(f"{c.id} {c.name}（{c.note}）" for c in failed)
        raise GateFailed(
            f"模块「{result.module}」校验未通过，已停止执行：{detail}",
            hint="先定位差额来源（清洗日志 + 10_校验汇总），修正数据或口径后重跑；"
                 "不要跳过校验继续出表。",
            details={"module": result.module,
                     "failed": [c.to_dict() for c in failed]},
        )


# ---------------------------------------------------------------- 全局对账

@dataclass
class Reconciliation:
    raw_total: float
    analysis_total: float
    invalid_numbers: float
    negative_excluded: float
    unassigned: float

    @property
    def expected_raw(self) -> float:
        return round(self.analysis_total + self.invalid_numbers
                     + self.negative_excluded + self.unassigned, 2)

    @property
    def diff(self) -> float:
        return round(self.raw_total - self.expected_raw, 2)


def build_reconciliation(stats: dict[str, Any], analysis_total: float,
                         negative_policy: str) -> Reconciliation:
    return Reconciliation(
        raw_total=round(float(stats.get("raw_total_amount", 0.0)), 2),
        analysis_total=round(analysis_total, 2),
        invalid_numbers=round(float(stats.get("invalid_number_amount", 0.0)), 2),
        negative_excluded=round(float(stats.get("negative_amount", 0.0))
                                if negative_policy == "exclude" else 0.0, 2),
        unassigned=round(float(stats.get("unassigned_amount", 0.0)), 2),
    )


def cross_module_totals(modules: list[ModuleResult], keys: list[str],
                        column: int = 1) -> dict[str, float]:
    """交叉验证用：从各模块表格里取出"总额"列，看六个维度是否互等。"""
    totals: dict[str, float] = {}
    for module in modules:
        if module.key in keys:
            totals[module.key] = sum_column(module.table, column)
    return totals
