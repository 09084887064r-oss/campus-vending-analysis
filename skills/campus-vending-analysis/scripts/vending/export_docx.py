"""Word 导出：与 Excel/HTML 同源同数，图表以 PNG 嵌入，文字与表格全可编辑。

用途：需要发给不看 Excel 的老师/后勤领导时，直接用 Word 出正式报告。
若环境缺少 python-docx 或中文字体，会抛出 ExportError 并说明缺什么，
而不是悄悄导出一份没图或乱码的文档。
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from .core import NA, ChartSpec, ExportError, Table, fmt_money, fmt_num, fmt_pct
from .pipeline import AnalysisRun

CJK_FONT = "微软雅黑"
_CJK_FALLBACKS = ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "PingFang SC")


def export_docx(run: AnalysisRun, path: Path) -> Path:
    try:
        import docx
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        from docx.oxml.ns import qn
        from docx.shared import Cm, Pt, RGBColor
    except ImportError as exc:      # pragma: no cover - 环境相关
        raise ExportError("缺少 python-docx，无法导出 Word（Excel 与 HTML 不受影响）。",
                          hint="pip install python-docx") from exc

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager
    except ImportError as exc:      # pragma: no cover
        raise ExportError("缺少 matplotlib，无法为 Word 渲染图表（Excel 图表为原生图表，不受影响）。",
                          hint="pip install matplotlib") from exc

    font_name = _pick_cjk_font(font_manager)
    plt.rcParams["font.sans-serif"] = [font_name, "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False

    document = docx.Document()
    _setup_styles(document, qn, Pt, RGBColor)

    document.add_heading(f"{run.config.platform_name} · 销售分析报告", level=0)
    periods = "；".join(f"{p.label}（{p.start}~{p.end}，{p.days} 天）" for p in run.config.periods)
    subtitle = document.add_paragraph()
    subtitle.add_run(f"分析区间：{periods}　|　分析口径销售额：{fmt_money(run.ctx.total_amount)}　|　"
                     f"生成时间：{run.config.confirmed_at or NA}").italic = True

    document.add_heading("结论先行", level=1)
    document.add_paragraph(run.narrative.summary)

    _add_kpi_table(document, run, Pt)
    chart_dir = Path(tempfile.mkdtemp(prefix="vending-docx-"))
    for module in run.modules:
        document.add_heading(f"{module.order}. {module.title}", level=1)
        if module.chart is not None:
            image = _render_chart_png(module.chart, chart_dir, plt)
            if image is not None:
                document.add_picture(str(image), width=Cm(15.5))
                document.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
        insight = document.add_paragraph()
        run_bold = insight.add_run("解读：")
        run_bold.bold = True
        insight.add_run(module.insight)
        _add_table(document, module.table, Pt)
        if module.table.note:
            note = document.add_paragraph()
            note.add_run(f"说明：{module.table.note}").font.size = Pt(8.5)

    document.add_heading("校验与对账", level=1)
    _add_validation_summary(document, run, Pt)

    document.add_heading("结论：前 3 大发现 · 行动建议 · 风险预警", level=1)
    document.add_heading("一、前 3 大发现", level=2)
    for finding in run.narrative.findings:
        paragraph = document.add_paragraph(style="List Number")
        paragraph.add_run(f"{finding.title}：").bold = True
        paragraph.add_run(f"{finding.detail}（证据：{finding.evidence}）")
    document.add_heading("二、行动建议", level=2)
    for action in run.narrative.actions:
        paragraph = document.add_paragraph(style="List Number")
        paragraph.add_run(f"[{action.priority}] {action.action}").bold = True
        paragraph.add_run(f"　依据：{action.basis}" + (f"　预期：{action.expected}" if action.expected else ""))
    document.add_heading("三、风险预警", level=2)
    for risk in run.narrative.risks:
        paragraph = document.add_paragraph(style="List Bullet")
        paragraph.add_run(f"[{risk.level}] {risk.risk}").bold = True
        paragraph.add_run(f"　影响：{risk.impact}　处置：{risk.mitigation}")

    closing = document.add_paragraph()
    closing.add_run(
        "数据来源与口径：所有数字来自销售明细、商品分类表、寝室楼栋人数表；"
        "缺失值一律输出 N/A 并注明原因，未使用估算值填充；"
        "分组表合计与分析口径总额的差额已在「校验与对账」中列明。").font.size = Pt(8.5)

    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(path))
    return path


# ---------------------------------------------------------------- 样式

def _setup_styles(document: Any, qn: Any, Pt: Any, RGBColor: Any) -> None:
    style = document.styles["Normal"]
    style.font.name = CJK_FONT
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)
    for section in document.sections:
        section.left_margin = section.right_margin = Pt(56)
        section.top_margin = section.bottom_margin = Pt(48)


def _pick_cjk_font(font_manager: Any) -> str:
    available = {font.name for font in font_manager.fontManager.ttflist}
    for candidate in _CJK_FALLBACKS:
        if candidate in available:
            return candidate
    for candidate in _CJK_FALLBACKS:
        if CJK_FONT in candidate:
            return candidate
    return "DejaVu Sans"


def _add_kpi_table(document: Any, run: AnalysisRun, Pt: Any) -> None:
    ctx = run.ctx
    pairs = [
        ("分析口径销售额", fmt_money(ctx.total_amount)),
        ("销售数量", f"{fmt_num(ctx.total_qty)} 件"),
        ("消费人数（去重）", f"{fmt_num(ctx.total_users)} 人"),
        ("订单数（去重）", fmt_num(ctx.total_orders) if ctx.total_orders else NA),
        ("单品数", str(run.cleaned.stats.get("sku_count", 0))),
        ("机组数", str(run.cleaned.stats.get("machine_count", 0))),
        ("覆盖在住人数", fmt_num(ctx.roster_total) if ctx.roster_total else NA),
        ("校验结论", "全部通过" if run.ok else "存在未通过项"),
    ]
    table = document.add_table(rows=len(pairs) // 2, cols=4)
    table.style = "Table Grid"
    for index, (label, value) in enumerate(pairs):
        row, column = index // 2, (index % 2) * 2
        table.cell(row, column).text = label
        table.cell(row, column + 1).text = str(value)
        for cell in (table.cell(row, column), table.cell(row, column + 1)):
            cell.paragraphs[0].runs[0].font.size = Pt(9.5)
    document.add_paragraph()


def _add_table(document: Any, table: Table, Pt: Any) -> None:
    rows = len(table.rows) + (1 if table.total_row else 0) + 1
    docx_table = document.add_table(rows=rows, cols=len(table.columns))
    docx_table.style = "Table Grid"
    docx_table.autofit = True
    for index, column in enumerate(table.columns):
        cell = docx_table.cell(0, index)
        cell.text = str(column)
        run = cell.paragraphs[0].runs[0]
        run.bold = True
        run.font.size = Pt(9)
    for r_index, row in enumerate(table.rows, start=1):
        for c_index, value in enumerate(row):
            cell = docx_table.cell(r_index, c_index)
            cell.text = _cell_text(value, table.format_of(r_index - 1, c_index))
            for run in cell.paragraphs[0].runs:
                run.font.size = Pt(8.5)
    if table.total_row:
        last = len(table.rows) + 1
        for c_index, value in enumerate(table.total_row):
            cell = docx_table.cell(last, c_index)
            cell.text = _cell_text(value, "text")
            for run in cell.paragraphs[0].runs:
                run.bold = True
                run.font.size = Pt(8.5)
    document.add_paragraph()


def _add_validation_summary(document: Any, run: AnalysisRun, Pt: Any) -> None:
    checks = [c for m in run.modules for c in m.validation.checks] + run.global_result.checks
    counts = {status: sum(1 for c in checks if c.status == status)
              for status in ("PASS", "WARN", "FAIL", "SKIP")}
    paragraph = document.add_paragraph()
    paragraph.add_run(
        f"共 {len(checks)} 项校验：PASS {counts['PASS']}，WARN {counts['WARN']}，"
        f"FAIL {counts['FAIL']}，SKIP {counts['SKIP']}。"
        "WARN 表示差额已被解释并写入报告；FAIL 会阻断出表。").font.size = Pt(9.5)
    recon = run.reconciliation
    table = document.add_table(rows=6, cols=2)
    table.style = "Table Grid"
    pairs = [
        ("原始解析总额（去重后）", fmt_money(recon.raw_total)),
        ("分析口径总销售额", fmt_money(recon.analysis_total)),
        ("非数字行", fmt_money(recon.invalid_numbers)),
        ("负数/退款行（剔除）", fmt_money(recon.negative_excluded)),
        ("时间无法归属/区间外", fmt_money(recon.unassigned)),
        ("对账结果", f"{fmt_money(recon.expected_raw)}（差异 {fmt_money(recon.diff)}）"),
    ]
    for index, (label, value) in enumerate(pairs):
        table.cell(index, 0).text = label
        table.cell(index, 1).text = value
        for column in (0, 1):
            for run in table.cell(index, column).paragraphs[0].runs:
                run.font.size = Pt(9)
    document.add_paragraph()


def _cell_text(value: Any, fmt: str) -> str:
    if value == NA or value is None:
        return NA
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if fmt == "money":
            return fmt_money(value)
        if fmt == "pct":
            return fmt_pct(value)
        digits = 2 if abs(value - round(value)) > 1e-9 else 0
        return fmt_num(value, digits)
    return str(value)


# ---------------------------------------------------------------- 图表

def _render_chart_png(chart: ChartSpec, out_dir: Path, plt: Any) -> Path | None:
    if not chart.categories:
        return None
    values = chart.series[0]["values"] if chart.series else []
    numeric = [value if isinstance(value, (int, float)) else 0 for value in values]
    unit = "yuan" if ("元" in chart.title or "销售额" in chart.title) else "count"
    figsize = (9.2, 3.6) if chart.kind != "hbar" else (9.2, max(2.4, 0.42 * len(chart.categories)))
    figure, axes = plt.subplots(figsize=figsize, dpi=170)

    if chart.kind == "line":
        axes.plot(chart.categories, numeric, marker="o", color="#2f5597", linewidth=2)
        axes.fill_between(range(len(numeric)), numeric, color="#2f5597", alpha=0.12)
        axes.set_xticks(range(len(chart.categories)))
        axes.set_xticklabels([str(c) for c in chart.categories], rotation=0, fontsize=8)
    elif chart.kind == "donut":
        colors = ["#2f5597", "#0f766e", "#b45309", "#7c3aed", "#be123c",
                  "#0369a1", "#4d7c0f", "#a16207"]
        wedges, _texts, autotexts = axes.pie(
            numeric, labels=None, autopct="%1.1f%%", startangle=90,
            colors=colors[:len(numeric)], pctdistance=0.78,
            wedgeprops={"width": 0.42, "edgecolor": "white"})
        for text in autotexts:
            text.set_fontsize(8)
        axes.legend(chart.categories, loc="center left", bbox_to_anchor=(1.0, 0.5), fontsize=8)
        axes.axis("equal")
    elif chart.kind == "hbar":
        axes.barh(chart.categories[::-1], numeric[::-1], color="#0f766e")
        axes.set_xlabel(unit_label(unit))
        axes.tick_params(labelsize=8)
    else:
        colors = ["#b45309" if str(c) == chart.highlight else "#2f5597"
                  for c in chart.categories]
        axes.bar([str(c) for c in chart.categories], numeric, color=colors)
        axes.tick_params(axis="x", labelsize=8, rotation=0)
        axes.set_ylabel(unit_label(unit))

    axes.set_title(chart.title, fontsize=11)
    if chart.kind not in {"donut"}:
        axes.grid(axis="y" if chart.kind != "hbar" else "x", color="#e5e7eb", linewidth=0.8)
        axes.set_axisbelow(True)
        for spine in ("top", "right"):
            axes.spines[spine].set_visible(False)
    figure.tight_layout()
    path = out_dir / f"chart_{abs(hash(chart.title)) % 100000}.png"
    figure.savefig(path, bbox_inches="tight")
    plt.close(figure)
    return path


def unit_label(unit: str) -> str:
    return "金额（元）" if unit == "yuan" else "数量"
