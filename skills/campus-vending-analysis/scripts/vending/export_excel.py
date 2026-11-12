"""Excel 导出：多工作表 + 原生可编辑图表 + 完整口径留痕。

工作表顺序
00_说明 / 01_清洗日志 / 02_原始数据快照(可关) / 03_核心数据项 / 04_时点分析 /
05_价格带分析 / 06_商品品类分析 / 06b_单品明细 / 07_机组效能 / 08_用户习惯分析 /
10_校验汇总 / 10b_对账瀑布 / 11_口径与溯源 / 12_结论与建议 / zz_图表数据(隐藏)
"""

from __future__ import annotations

from pathlib import Path

from openpyxl import Workbook
from openpyxl.chart import BarChart, DoughnutChart, LineChart, Reference
from openpyxl.chart.label import DataLabelList
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .core import NA, ChartSpec, ExportError, Table
from .pipeline import AnalysisRun
from .sources import snapshot_rows

TITLE_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FILL = PatternFill("solid", fgColor="2F5597")
SECTION_FILL = PatternFill("solid", fgColor="D9E2F3")
TOTAL_FILL = PatternFill("solid", fgColor="EDEDED")
WARN_FILL = PatternFill("solid", fgColor="FFF2CC")
FAIL_FILL = PatternFill("solid", fgColor="F8CBAD")
PASS_FILL = PatternFill("solid", fgColor="E2EFDA")
NOTE_FILL = PatternFill("solid", fgColor="FBFBF7")

WHITE_BOLD = Font(name="微软雅黑", size=11, bold=True, color="FFFFFF")
TITLE_FONT = Font(name="微软雅黑", size=14, bold=True, color="FFFFFF")
BODY_FONT = Font(name="微软雅黑", size=10, color="262626")
BOLD_FONT = Font(name="微软雅黑", size=10, bold=True, color="1F3864")
NOTE_FONT = Font(name="微软雅黑", size=9, italic=True, color="595959")

MONEY_FMT = "#,##0.00"
INT_FMT = "#,##0"
NUM_FMT = "#,##0.00"
PCT_FMT = '#,##0.00"%"'

SHEET_NAMES = {
    "overview": "00_说明",
    "clean_log": "01_清洗日志",
    "snapshot": "02_原始数据快照",
    "baseline": "03_环比基准与口径",
    "core": "04_核心数据分析",
    "hourly": "05_时段销售分析",
    "price": "06_价格带陈列分析",
    "category": "07_品类数销售分析",
    "machine": "08_机组销售环比",
    "dorm": "09_用户消费分析",
    "sku": "10_单品追溯",
    "validation": "11_校验汇总",
    "reconciliation": "11b_对账瀑布",
    "trace": "12_口径与溯源",
    "uplift": "13_销售提升点清单",
    "conclusion": "14_结论与建议",
}

CHART_DATA_SHEET = "zz_图表数据"
_FORMAT_MAP = {"money": MONEY_FMT, "pct": PCT_FMT, "num": NUM_FMT, "text": "@"}


def export_excel(run: AnalysisRun, path: Path) -> Path:
    path = Path(path)
    try:
        workbook = Workbook()
        workbook.remove(workbook.active)

        _write_overview_sheet(workbook, run)
        _write_table_sheet(workbook, run.tables["clean_log"])
        if run.config.include_raw_snapshot:
            _write_snapshot_sheet(workbook, run)
        if "baseline" in run.tables:
            _write_table_sheet(workbook, run.tables["baseline"])

        chart_blocks = _prepare_chart_data(workbook, run)
        for module in run.modules:
            _write_table_sheet(workbook, module.table, chart=module.chart,
                               chart_rows=chart_blocks.get(module.key))

        _write_table_sheet(workbook, run.tables["sku"])
        _write_table_sheet(workbook, run.tables["validation"])
        _write_table_sheet(workbook, run.tables["reconciliation"])
        _write_table_sheet(workbook, run.tables["trace"])
        if "uplift" in run.tables:
            _write_table_sheet(workbook, run.tables["uplift"])
        _write_conclusion_sheet(workbook, run)

        data_sheet = workbook[CHART_DATA_SHEET]
        data_sheet.sheet_state = "hidden"

        _apply_template_style(workbook, run)
        _apply_sheet_order(workbook)
        workbook.save(path)
        return path
    except ExportError:
        raise
    except Exception as exc:                      # noqa: BLE001 - 兜底转成明确错误
        raise ExportError(f"Excel 导出失败：{exc}",
                          hint="检查文件是否被 Excel 占用（请先关闭同名文件）。") from exc


# ---------------------------------------------------------------- 通用表写入

def _write_table_sheet(workbook: Workbook, table: Table, *,
                       chart: ChartSpec | None = None,
                       chart_rows: tuple[int, int] | None = None) -> Worksheet:
    name = SHEET_NAMES.get(table.key, table.key[:28])
    worksheet = workbook.create_sheet(name)
    worksheet.sheet_view.showGridLines = False

    last_row = _write_table_body(worksheet, table, start_row=1)
    _write_note(worksheet, table.note, start_row=last_row + 2, width=len(table.columns))
    worksheet.freeze_panes = "B3" if len(table.columns) > 3 else "A3"
    _autosize(worksheet, table)

    if chart is not None and chart_rows is not None:
        _attach_chart(worksheet, chart, *chart_rows,
                      anchor_col=min(len(table.columns) + 1, 12))
    worksheet.sheet_properties.tabColor = "2F5597"
    return worksheet


def _write_table_body(worksheet: Worksheet, table: Table, start_row: int = 1) -> int:
    worksheet.cell(row=start_row, column=1, value=table.title).font = TITLE_FONT
    worksheet.cell(row=start_row, column=1).fill = TITLE_FILL
    title_end = get_column_letter(max(len(table.columns), 2))
    worksheet.merge_cells(f"A{start_row}:{title_end}{start_row}")
    worksheet.cell(row=start_row, column=1).alignment = Alignment(vertical="center")
    worksheet.row_dimensions[start_row].height = 24

    header_row = start_row + 1
    for index, column in enumerate(table.columns, start=1):
        cell = worksheet.cell(row=header_row, column=index, value=column)
        cell.font = WHITE_BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    worksheet.row_dimensions[header_row].height = 30

    row_index = header_row + 1
    for offset, row in enumerate(table.rows):
        fmt = table.format_of(offset, 0)
        for col_index, value in enumerate(row, start=1):
            cell_format = table.format_of(offset, col_index - 1)
            cell = worksheet.cell(row=row_index, column=col_index)
            if value == NA:
                cell.value = NA
                cell.alignment = Alignment(horizontal="center")
                cell.font = Font(name="微软雅黑", size=10, italic=True, color="808080")
            elif value is None or value == "":
                cell.value = ""
            else:
                cell.value = value
                cell.font = BODY_FONT
                number_format = _FORMAT_MAP.get(cell_format)
                if number_format and number_format != "@":
                    cell.number_format = number_format
                if isinstance(value, str) and len(value) > 24:
                    cell.alignment = Alignment(wrap_text=True, vertical="top")
                elif isinstance(value, (int, float)):
                    cell.alignment = Alignment(horizontal="right")
        if fmt == "money":
            pass
        row_index += 1

    if table.total_row:
        for col_index, value in enumerate(table.total_row, start=1):
            cell = worksheet.cell(row=row_index, column=col_index, value=value)
            cell.font = BOLD_FONT
            cell.fill = TOTAL_FILL
            cell.border = Border(top=Side(style="thin", color="8EA9DB"))
            cell_format = table.format_of(max(len(table.rows) - 1, 0), col_index - 1)
            number_format = _FORMAT_MAP.get(cell_format)
            if number_format and number_format != "@" and isinstance(value, (int, float)):
                cell.number_format = number_format
            if isinstance(value, (int, float)):
                cell.alignment = Alignment(horizontal="right")
        row_index += 1
    return row_index


def _write_note(worksheet: Worksheet, note: str, *, start_row: int, width: int) -> None:
    if not note:
        return
    end_col = get_column_letter(max(width, 2))
    worksheet.merge_cells(f"A{start_row}:{end_col}{start_row}")
    cell = worksheet.cell(row=start_row, column=1, value=f"说明：{note}")
    cell.font = NOTE_FONT
    cell.fill = NOTE_FILL
    cell.alignment = Alignment(wrap_text=True, vertical="top")
    lines = max(1, len(note) // 90 + 1)
    worksheet.row_dimensions[start_row].height = min(15 * lines, 90)


def _autosize(worksheet: Worksheet, table: Table) -> None:
    for index, column in enumerate(table.columns, start=1):
        samples = [len(str(column))]
        for row in table.rows[:200]:
            if index - 1 < len(row):
                value = row[index - 1]
                if value is None:
                    continue
                samples.append(min(len(str(value)), 60))
        width = min(max(max(samples) * 1.35 + 2, 9), 52)
        worksheet.column_dimensions[get_column_letter(index)].width = width


# ---------------------------------------------------------------- 特殊工作表

def _write_overview_sheet(workbook: Workbook, run: AnalysisRun) -> Worksheet:
    table = run.tables["overview"]
    worksheet = workbook.create_sheet(SHEET_NAMES["overview"])
    worksheet.sheet_view.showGridLines = False

    worksheet.cell(row=1, column=1,
                   value=f"{run.config.platform_name} · 销售分析报告").font = Font(
        name="微软雅黑", size=18, bold=True, color="1F3864")
    worksheet.merge_cells("A1:B1")
    worksheet.row_dimensions[1].height = 30

    worksheet.cell(row=2, column=1,
                   value=f"结论先行：{run.narrative.findings[0].title}"
                   if run.narrative.findings else "结论先行").font = BOLD_FONT
    worksheet.merge_cells("A2:B2")
    worksheet.cell(row=3, column=1, value=run.narrative.summary).font = BODY_FONT
    worksheet.merge_cells("A3:B3")
    worksheet.cell(row=3, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    worksheet.row_dimensions[3].height = 90

    directory_row = 5
    worksheet.cell(row=directory_row, column=1, value="工作表目录").font = BOLD_FONT
    worksheet.cell(row=directory_row, column=1).fill = SECTION_FILL
    worksheet.cell(row=directory_row, column=2, value="内容").font = BOLD_FONT
    worksheet.cell(row=directory_row, column=2).fill = SECTION_FILL
    directory = [
        ("01_清洗日志", "每一步清洗的规则、影响行数与金额影响（数字溯源的起点）"),
        ("02_原始数据快照", "前若干行原始记录，用于抽查核对（可按需关闭）"),
        ("03_核心数据项", "十指标 × 时间段对比（天数不同强制给日均口径）"),
        ("04_时点分析", "0–23 点逐小时，零销售时段保留为 0"),
        ("05_价格带分析", "件单价分区间的单品数/销量/销售额/占比"),
        ("06_商品品类分析", "品类结构，未分类单列不丢弃"),
        ("06b_单品明细", "单品判定结果（规格分开、同品异名合并）"),
        ("07_机组效能", "左右门合并后的机组销售额与占比"),
        ("08_用户习惯分析", "按寝室楼栋的用户数/频次/金额/消费率"),
        ("10_校验汇总", "模块门禁 + 全局对账 + 交叉验证的全部结论"),
        ("10b_对账瀑布", "分析口径、不可归属、排除项的逐项对齐"),
        ("11_口径与溯源", "每个指标的公式与清洗日志步骤"),
        ("12_结论与建议", "前 3 大发现 + 行动建议 + 风险预警"),
    ]
    row = directory_row + 1
    for name, description in directory:
        worksheet.cell(row=row, column=1, value=name).font = BODY_FONT
        worksheet.cell(row=row, column=2, value=description).font = BODY_FONT
        row += 1

    row += 1
    row = _write_table_body_at(worksheet, table, start_row=row, skip_title=True)
    worksheet.freeze_panes = "A6"
    worksheet.column_dimensions["A"].width = 26
    worksheet.column_dimensions["B"].width = 110
    worksheet.sheet_properties.tabColor = "1F3864"
    return worksheet


def _write_table_body_at(worksheet: Worksheet, table: Table, *, start_row: int,
                         skip_title: bool = False) -> int:
    header_row = start_row
    for index, column in enumerate(table.columns, start=1):
        cell = worksheet.cell(row=header_row, column=index, value=column)
        cell.font = WHITE_BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

    row_index = header_row + 1
    for row in table.rows:
        for col_index, value in enumerate(row, start=1):
            cell = worksheet.cell(row=row_index, column=col_index)
            cell.value = "" if value is None else value
            cell.font = BODY_FONT
            cell.alignment = Alignment(wrap_text=col_index == 2, vertical="top")
        row_index += 1
    _write_note(worksheet, table.note, start_row=row_index + 1, width=len(table.columns))
    return row_index + 2


def _write_snapshot_sheet(workbook: Workbook, run: AnalysisRun) -> Worksheet:
    worksheet = workbook.create_sheet(SHEET_NAMES["snapshot"])
    headers, rows, truncated = snapshot_rows(run.cleaned.raw, run.config.snapshot_limit)
    worksheet.cell(row=1, column=1, value="原始数据快照（未做任何清洗）").font = TITLE_FONT
    worksheet.cell(row=1, column=1).fill = TITLE_FILL
    worksheet.merge_cells(start_row=1, start_column=1, end_row=1,
                          end_column=max(len(headers), 2))
    for index, header in enumerate(headers, start=1):
        cell = worksheet.cell(row=2, column=index, value=header)
        cell.font = WHITE_BOLD
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", wrap_text=True)
    for r_index, row in enumerate(rows, start=3):
        for c_index, value in enumerate(row, start=1):
            cell = worksheet.cell(row=r_index, column=c_index)
            if isinstance(value, (int, float)):
                cell.value = value
                cell.number_format = NUM_FMT
            else:
                cell.value = "" if value is None else str(value)
            cell.font = BODY_FONT
    note = ("共 " + str(run.cleaned.stats.get("raw_rows", 0)) + " 行，此处展示前 "
            + str(len(rows)) + " 行；" + ("已截断，完整数据见源文件。" if truncated else "已全部展示。")
            + ("原始快照按用户要求关闭。" if not run.config.include_raw_snapshot else ""))
    _write_note(worksheet, note, start_row=len(rows) + 4, width=max(len(headers), 2))
    worksheet.freeze_panes = "A3"
    worksheet.auto_filter.ref = (f"A2:{get_column_letter(max(len(headers), 1))}"
                                f"{2 + len(rows)}")
    worksheet.sheet_properties.tabColor = "808080"
    return worksheet


def _write_conclusion_sheet(workbook: Workbook, run: AnalysisRun) -> Worksheet:
    worksheet = workbook.create_sheet(SHEET_NAMES["conclusion"])
    worksheet.sheet_view.showGridLines = False
    row = 1
    worksheet.cell(row=row, column=1, value="结论：前 3 大发现 + 行动建议 + 风险预警").font = TITLE_FONT
    worksheet.cell(row=row, column=1).fill = TITLE_FILL
    worksheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 2

    sections = [
        ("一、前 3 大发现", [(f"{f.rank}. {f.title}", f.detail, f"证据：{f.evidence}")
                       for f in run.narrative.findings]),
        ("二、行动建议", [(f"{a.priority} · {a.action}", f"依据：{a.basis}",
                      f"预期：{a.expected}") for a in run.narrative.actions]),
        ("三、风险预警", [(f"{r.level}风险 · {r.risk}", f"影响：{r.impact}",
                      f"处置：{r.mitigation}") for r in run.narrative.risks]),
    ]
    for title, items in sections:
        worksheet.cell(row=row, column=1, value=title).font = Font(
            name="微软雅黑", size=12, bold=True, color="FFFFFF")
        worksheet.cell(row=row, column=1).fill = TITLE_FILL
        worksheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 1
        for headline, detail, extra in items:
            worksheet.cell(row=row, column=1, value=headline).font = BOLD_FONT
            worksheet.cell(row=row, column=1).alignment = Alignment(wrap_text=True, vertical="top")
            worksheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
            worksheet.cell(row=row, column=3, value=detail).font = BODY_FONT
            worksheet.cell(row=row, column=3).alignment = Alignment(wrap_text=True, vertical="top")
            worksheet.merge_cells(start_row=row, start_column=3, end_row=row, end_column=4)
            worksheet.row_dimensions[row].height = 46
            row += 1
            worksheet.cell(row=row, column=1, value=extra).font = NOTE_FONT
            worksheet.cell(row=row, column=1).alignment = Alignment(wrap_text=True, vertical="top")
            worksheet.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
            row += 2
    for letter, width in (("A", 42), ("B", 18), ("C", 46), ("D", 26)):
        worksheet.column_dimensions[letter].width = width
    worksheet.sheet_properties.tabColor = "C00000"
    return worksheet


# ---------------------------------------------------------------- 图表

def _prepare_chart_data(workbook: Workbook, run: AnalysisRun) -> dict[str, tuple[int, int]]:
    """把每个模块的图表数据写成 tidy 块（隐藏表），返回 key -> (首行, 末行)。"""
    worksheet = workbook.create_sheet(CHART_DATA_SHEET)
    blocks: dict[str, tuple[int, int]] = {}
    row = 1
    for module in run.modules:
        chart = module.chart
        if chart is None or not chart.categories:
            continue
        series = chart.series[0] if chart.series else {"name": "数值", "values": []}
        worksheet.cell(row=row, column=1, value="类别")
        worksheet.cell(row=row, column=2, value=series.get("name", "数值"))
        header_row = row
        row += 1
        for category, value in zip(chart.categories, series.get("values", [])):
            worksheet.cell(row=row, column=1, value=str(category))
            cell = worksheet.cell(row=row, column=2)
            cell.value = value if isinstance(value, (int, float)) else 0
            cell.number_format = NUM_FMT
            row += 1
        blocks[module.key] = (header_row, row - 1)
        row += 2
    worksheet.column_dimensions["A"].width = 28
    worksheet.column_dimensions["B"].width = 18
    return blocks


def _attach_chart(worksheet: Worksheet, chart: ChartSpec, header_row: int, last_row: int,
                  *, anchor_col: int) -> None:
    data_ref = Reference(worksheet.parent[CHART_DATA_SHEET], min_col=2,
                         min_row=header_row, max_row=last_row)
    category_ref = Reference(worksheet.parent[CHART_DATA_SHEET], min_col=1,
                             min_row=header_row + 1, max_row=last_row)

    if chart.kind == "line":
        graphic = LineChart()
        graphic.height, graphic.width = 8.2, 15.5
    elif chart.kind == "donut":
        graphic = DoughnutChart(holeSize=52)
        graphic.height, graphic.width = 8.6, 13.5
    elif chart.kind == "hbar":
        graphic = BarChart()
        graphic.type = "bar"
        graphic.height, graphic.width = 0.9 * max(last_row - header_row, 3) + 4, 15.5
    else:
        graphic = BarChart()
        graphic.type = "col"
        graphic.height, graphic.width = 8.2, 15.5

    graphic.title = chart.title
    graphic.style = 10
    if chart.kind in {"bar", "hbar", "line"}:
        graphic.y_axis.title = chart.y_label or "数值"
        graphic.x_axis.title = chart.x_label or ""
        graphic.y_axis.majorGridlines = None
    if chart.kind == "donut":
        graphic.dataLabels = DataLabelList()
        graphic.dataLabels.showPercent = True
    graphic.add_data(data_ref, titles_from_data=True)
    graphic.set_categories(category_ref)
    worksheet.add_chart(graphic, f"{get_column_letter(anchor_col)}2")


def _apply_sheet_order(workbook: Workbook) -> None:
    order = ["00_说明", "01_清洗日志", "02_原始数据快照", "03_环比基准与口径",
             "04_核心数据分析", "05_时段销售分析", "06_价格带陈列分析", "07_品类数销售分析",
             "08_机组销售环比", "09_用户消费分析", "10_单品追溯", "11_校验汇总",
             "11b_对账瀑布", "12_口径与溯源", "13_销售提升点清单", "14_结论与建议",
             CHART_DATA_SHEET]
    existing = {name: index for index, name in enumerate(workbook.sheetnames)}
    desired = [name for name in order if name in existing]
    workbook._sheets = [workbook[name] for name in desired]   # noqa: SLF001


# ---------------------------------------------------------------- 模板样式

MOM_HEADER_HINT = ("环比", "变化")


def _header_row(worksheet: Worksheet) -> int | None:
    """在表头上方 6 行里找含“环比/变化”的那一行。"""
    for row_index in range(1, min(worksheet.max_row, 6) + 1):
        for cell in worksheet[row_index]:
            text = str(cell.value or "")
            if any(hint in text for hint in MOM_HEADER_HINT) and "销售额" not in text:
                return row_index
    return None


def _apply_template_style(workbook: Workbook, run: AnalysisRun) -> None:
    """把模板样式套到除隐藏数据表外的所有工作表，并给环比列加红绿条件格式。"""
    from .style import add_mom_rules, apply_profile

    profile = getattr(run, "style", None)
    if profile is None:
        return
    for worksheet in workbook.worksheets:
        if worksheet.title == CHART_DATA_SHEET:
            continue
        header_row = _header_row(worksheet)
        total_row = None
        for row_index in range(worksheet.max_row, 1, -1):
            if str(worksheet.cell(row=row_index, column=1).value or "").strip() == "合计":
                total_row = row_index
                break
        apply_profile(worksheet, profile, header_row=1 if worksheet.title == "00_说明" else 1,
                      n_cols=max(worksheet.max_column, 2), total_row=total_row)
        if header_row is None:
            continue
        mom_columns = [cell.column for cell in worksheet[header_row]
                       if any(hint in str(cell.value or "") for hint in MOM_HEADER_HINT)]
        if mom_columns:
            add_mom_rules(worksheet, mom_columns, profile, header_row + 1, worksheet.max_row - 1)
