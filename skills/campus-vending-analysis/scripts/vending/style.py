"""样式引擎：从模板 xlsx 读取格式，套用到新报表。

优先级（SKILL.md 写死）：① 用户明文规格（金额千分位两位小数、占比两位小数、
环比正负红绿）② 模板实测样式（字体/底色/边框/列宽/冻结/对齐）③ 内置默认。
模板只提供格式，绝不提供数据。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

# —— ① 用户明文规格：数字格式以此为准（模板里的 0.00_) 没有千分位，故不采用）
NUMFMT = {
    "money": "#,##0.00",
    "num": "#,##0.00",
    "int": "#,##0",
    "pct": "0.00%",
    "ratio": "0.00",
    "delta_money": "#,##0.00;[Red]-#,##0.00",
    "text": "@",
}

DEFAULT = {
    "header_fill": "FF534AB7",
    "header_font": "微软雅黑",
    "header_size": 10,
    "header_bold": True,
    "header_color": "FFFFFFFF",
    "header_wrap": True,
    "body_font": "微软雅黑",
    "body_size": 10,
    "body_color": "FF2C2C2A",
    "border_style": "thin",
    "align_h": "center",
    "align_v": "center",
    "freeze": "B3",
    "widths": {},
    "mom_positive": "FFC00000",
    "mom_negative": "FF217346",
}


@dataclass
class StyleProfile:
    header_fill: str = DEFAULT["header_fill"]
    header_font: str = DEFAULT["header_font"]
    header_size: float = DEFAULT["header_size"]
    header_bold: bool = True
    header_color: str = DEFAULT["header_color"]
    header_wrap: bool = True
    body_font: str = DEFAULT["body_font"]
    body_size: float = DEFAULT["body_size"]
    body_color: str = DEFAULT["body_color"]
    border_style: str = DEFAULT["border_style"]
    align_h: str = DEFAULT["align_h"]
    align_v: str = DEFAULT["align_v"]
    freeze: str = DEFAULT["freeze"]
    widths: dict[str, float] = field(default_factory=dict)
    mom_positive: str = DEFAULT["mom_positive"]
    mom_negative: str = DEFAULT["mom_negative"]
    numfmt: dict[str, str] = field(default_factory=lambda: dict(NUMFMT))
    source: str = "内置默认"
    template_formats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _rgb(color: Any) -> str | None:
    try:
        value = color.rgb
    except AttributeError:
        return None
    return value if isinstance(value, str) and len(value) == 8 else None


def read_template(path: str | Path) -> StyleProfile:
    """从成品表推断统一表头样式；模板结构不一致时抛 ValueError 由调用方回退。"""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path))
    headers: list[Any] = []
    body: list[Any] = []
    formats: set[str] = set()
    widths: dict[str, float] = {}
    freeze = ""
    try:
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(min_row=1, max_row=min(sheet.max_row, 6)):
                for cell in row:
                    fill = _rgb(cell.fill.fgColor) if cell.fill and cell.fill.patternType else None
                    if fill and cell.font and cell.font.bold and cell.value is not None:
                        headers.append((fill, cell))
            for row in sheet.iter_rows(min_row=2, max_row=min(sheet.max_row, 12)):
                for cell in row:
                    if cell.value is not None and not (cell.font and cell.font.bold):
                        body.append(cell)
                    if cell.number_format and cell.number_format != "General":
                        formats.add(cell.number_format)
            for column, dimension in sheet.column_dimensions.items():
                if dimension.width and column not in widths:
                    widths[column] = round(float(dimension.width), 2)
            freeze = freeze or (sheet.freeze_panes or "")
    finally:
        workbook.close()

    if not headers:
        raise ValueError("模板里没有可识别的表头样式（找不到加粗白字填充行）")

    profile = StyleProfile()
    count: dict[str, int] = {}
    for fill, _ in headers:
        count[fill] = count.get(fill, 0) + 1
    profile.header_fill = max(count, key=count.get)
    sample = next((cell for fill, cell in headers if fill == profile.header_fill), headers[0][1])
    profile.header_font = sample.font.name or profile.header_font
    profile.header_size = float(sample.font.sz or profile.header_size)
    profile.header_bold = bool(sample.font.bold)
    profile.header_color = _rgb(sample.font.color) or profile.header_color
    profile.header_wrap = bool(sample.alignment and sample.alignment.wrap_text)
    profile.align_h = (sample.alignment.horizontal if sample.alignment else None) or profile.align_h
    profile.align_v = (sample.alignment.vertical if sample.alignment else None) or profile.align_v
    if sample.border and sample.border.left and sample.border.left.style:
        profile.border_style = sample.border.left.style

    if body:
        first = body[0]
        profile.body_font = first.font.name or profile.body_font
        profile.body_size = float(first.font.sz or profile.body_size)
        profile.body_color = _rgb(first.font.color) or profile.body_color
        if first.border and first.border.left and first.border.left.style:
            profile.border_style = profile.border_style or first.border.left.style

    profile.widths = widths
    profile.freeze = freeze or "B3"
    profile.template_formats = sorted(formats)
    profile.source = f"模板 {Path(path).name}"
    return profile


def default_profile(reason: str = "") -> StyleProfile:
    profile = StyleProfile()
    profile.source = "内置默认" + (f"（模板不可用：{reason}）" if reason else "")
    return profile


def dump_fallback(path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(default_profile().to_dict(), ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target


def load_profile(template: str | Path | None) -> StyleProfile:
    """模板存在就照抄；缺失/损坏则回退默认，并把原因写进 source（不静默）。"""
    if not template:
        return default_profile("未提供模板路径")
    candidate = Path(template)
    if not candidate.exists():
        return default_profile(f"文件不存在 {candidate.name}")
    try:
        return read_template(candidate)
    except Exception as exc:                      # noqa: BLE001 - 样式失败绝不能断报表
        return default_profile(f"{exc.__class__.__name__}: {exc}")


# ---------------------------------------------------------------- 套用

def apply_profile(worksheet: Any, profile: StyleProfile, *, header_row: int = 1,
                  n_cols: int | None = None, total_row: int | None = None) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    side = Side(style=profile.border_style, color="FFD0D0D8")
    border = Border(left=side, right=side, top=side, bottom=side)
    header_fill = PatternFill("solid", fgColor=profile.header_fill)
    header_font = Font(name=profile.header_font, size=profile.header_size,
                       bold=profile.header_bold, color=profile.header_color)
    body_font = Font(name=profile.body_font, size=profile.body_size, color=profile.body_color)
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=profile.header_wrap)
    body_align = Alignment(horizontal=profile.align_h, vertical=profile.align_v)

    max_row = worksheet.max_row
    max_col = n_cols or worksheet.max_column
    for row in worksheet.iter_rows(min_row=header_row, max_row=max_row, max_col=max_col):
        for cell in row:
            cell.border = border
            if cell.row == header_row:
                cell.fill = header_fill
                cell.font = header_font
                cell.alignment = header_align
            else:
                cell.font = body_font
                cell.alignment = body_align
    if profile.freeze:
        worksheet.freeze_panes = profile.freeze
    for index, (column, width) in enumerate(profile.widths.items()):
        if index < max_col:
            worksheet.column_dimensions[column].width = width
    if total_row:
        fill = PatternFill("solid", fgColor="FFEDEBFA")
        for cell in worksheet[total_row]:
            if cell.column <= max_col:
                cell.fill = fill
                cell.font = Font(name=profile.body_font, size=profile.body_size, bold=True,
                                 color=profile.body_color)


def add_mom_rules(worksheet: Any, columns: list[int], profile: StyleProfile,
                  first_row: int, last_row: int) -> int:
    """环比列的红绿用条件格式规则实现（改数后颜色自动跟随）。"""
    from openpyxl.formatting.rule import CellIsRule
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    if not columns or last_row < first_row:
        return 0
    up = Font(color=profile.mom_positive, bold=True)
    down = Font(color=profile.mom_negative, bold=True)
    added = 0
    for column in columns:
        ref = (f"{get_column_letter(column)}{first_row}:"
               f"{get_column_letter(column)}{last_row}")
        worksheet.conditional_formatting.add(
            ref, CellIsRule(operator="greaterThan", formula=["0"], font=up))
        worksheet.conditional_formatting.add(
            ref, CellIsRule(operator="lessThan", formula=["0"], font=down))
        added += 2
    return added
