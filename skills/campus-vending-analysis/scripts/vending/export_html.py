"""HTML 导出：单文件交互报告（结论先行 + 每图配解读 + 三段式收尾）。

实现方式：assets/template 下的 html/css/js 三个文本资产在导出时内联，
图表数据以 data-chart 属性嵌入（不拼 JS 字符串），因此报告可离线打开、
可直接打印为 PDF，也能被 Office 的「网页转 Word」正常读取。
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .core import NA, ChartSpec, ExportError, Table, fmt_money, fmt_num, fmt_pct
from .pipeline import AnalysisRun

TEMPLATE_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "template"

SECTION_IDS = {
    "core": "core", "hourly": "hourly", "price": "price",
    "category": "category", "machine": "machine", "dorm": "dorm",
}
NAV_LABELS = {
    "core": "① 核心数据", "hourly": "② 时点", "price": "③ 价格带",
    "category": "④ 品类", "machine": "⑤ 机组", "dorm": "⑥ 用户",
}


def export_html(run: AnalysisRun, path: Path) -> Path:
    path = Path(path)
    try:
        template = (TEMPLATE_DIR / "report.html").read_text(encoding="utf-8")
        style = (TEMPLATE_DIR / "report.css").read_text(encoding="utf-8")
        script = (TEMPLATE_DIR / "report.js").read_text(encoding="utf-8")
    except OSError as exc:
        raise ExportError(f"报告模板缺失或不可读：{exc}",
                          hint="确认 skill 目录下 assets/template 三个文件完整。") from exc

    fragments = {
        "{{TITLE}}": html.escape(f"{run.config.platform_name} · 销售分析报告"),
        "{{BRAND}}": html.escape(run.config.platform_name),
        "{{STYLE}}": style,
        "{{SCRIPT}}": script,
        "{{NAV}}": _render_nav(run),
        "{{HERO}}": _render_hero(run),
        "{{KPIS}}": _render_kpis(run),
        "{{SECTIONS}}": _render_sections(run),
        "{{VERIFICATION}}": _render_verification(run),
        "{{CONCLUSION}}": _render_conclusion(run),
        "{{FOOTER}}": _render_footer(run),
    }
    document = template
    for token, value in fragments.items():
        document = document.replace(token, value)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(document, encoding="utf-8")
    return path


# ---------------------------------------------------------------- 片段

def _render_nav(run: AnalysisRun) -> str:
    links = [f'<a href="#{SECTION_IDS[m.key]}">{NAV_LABELS.get(m.key, m.title)}</a>'
             for m in run.modules if m.key in SECTION_IDS]
    links.append('<a href="#verify">校验</a>')
    links.append('<a href="#conclusion">结论</a>')
    links.append('<a href="#appendix">口径</a>')
    return "\n    ".join(links)


def _render_hero(run: AnalysisRun) -> str:
    periods = "；".join(f"{p.label}（{p.start}~{p.end}，{p.days} 天）"
                       for p in run.config.periods)
    headline = run.narrative.findings[0].title if run.narrative.findings else "本期数据不足以形成结论"
    checks = [c for m in run.modules for c in m.validation.checks] + run.global_result.checks
    passed = sum(1 for c in checks if c.status == "PASS")
    warned = sum(1 for c in checks if c.status == "WARN")
    badges = [
        f"明细 {run.cleaned.stats.get('raw_rows', 0):,} 行",
        f"分析口径 {run.cleaned.stats.get('kept_rows', 0):,} 行",
        f"单品 {run.cleaned.stats.get('sku_count', 0)} 个",
        f"机组 {run.cleaned.stats.get('machine_count', 0)} 台",
        f"校验 PASS {passed} / WARN {warned}",
        f"生成于 {datetime.now():%Y-%m-%d %H:%M}",
    ]
    badge_html = "".join(f'<span class="badge">{html.escape(b)}</span>' for b in badges)
    return f"""<header class="hero">
  <h1>{html.escape(run.config.platform_name)} · 销售分析报告</h1>
  <div class="sub">分析区间：{html.escape(periods)}　|　口径：{html.escape(run.config.dorm.mode)} 楼栋归属</div>
  <div class="headline"><b>结论先行：</b>{html.escape(headline)}。{html.escape(run.narrative.summary)}</div>
  <div class="badges">{badge_html}</div>
</header>"""


def _render_kpis(run: AnalysisRun) -> str:
    ctx = run.ctx
    core = next((m for m in run.modules if m.key == "core"), None)
    last = run.config.periods[-1]
    rows = {row[0]: row for row in core.table.rows} if core else {}
    n = len(run.config.periods)
    last_index = n

    def last_value(metric: str) -> Any:
        row = rows.get(metric)
        return row[last_index] if row and len(row) > last_index else None

    coverage = last_value("用户消费率")
    atv = last_value("客单价（元/单）")
    daily = last_value("日均销售额（元/天）")
    cards = [
        ("分析口径销售额", fmt_money(ctx.total_amount), f"{len(run.config.periods)} 期合计", ""),
        (f"{last.label} 日均销售额", fmt_money(daily), f"{last.days} 天口径", ""),
        ("消费人数（去重）", fmt_num(ctx.total_users), "按用户标识去重", ""),
        ("用户消费率", fmt_pct(coverage), "消费人数 ÷ 在住人数", ""),
        ("订单数（去重）", fmt_num(ctx.total_orders) if ctx.total_orders else NA,
         "订单标识去重" if ctx.total_orders else "订单标识缺失", "small"),
        ("客单价", fmt_money(atv), "销售额 ÷ 订单数", ""),
        ("销售数量", f"{fmt_num(ctx.total_qty)} 件", "全部时间段合计", ""),
        ("单品 / 品类", f"{run.cleaned.stats.get('sku_count', 0)} / "
                    f"{len({r.category for r in ctx.records})}", "单品判定见附录", "small"),
    ]
    html_cards = []
    for label, value, foot, klass in cards:
        html_cards.append(
            f'<div class="kpi"><div class="label">{html.escape(label)}</div>'
            f'<div class="value {klass}">{html.escape(str(value))}</div>'
            f'<div class="foot">{html.escape(foot)}</div></div>')
    return '<section class="kpis">' + "".join(html_cards) + "</section>"


def _render_sections(run: AnalysisRun) -> str:
    cards: list[str] = []
    for module in run.modules:
        section_id = SECTION_IDS.get(module.key, module.key)
        chips = _status_chips(module.validation.checks)
        chart_html = _render_chart(module.chart)
        table_html = _render_table(module.table, table_id=f"tbl-{module.key}")
        cards.append(f"""<section class="card" id="{section_id}">
  <h2><span class="chip-no">{module.order}</span>{html.escape(module.title)}</h2>
  <p class="lead">{html.escape(module.table.title)}</p>
  <div class="chart-box">{chart_html}</div>
  <div class="insight"><b>解读：</b>{html.escape(module.insight)}</div>
  <div class="table-tools">
    <span class="hint">共 {len(module.table.rows)} 行；点击表头可排序，数值列支持倒序核对</span>
    <span>
      <button class="btn" data-csv="tbl-{module.key}">导出该表 CSV</button>
    </span>
  </div>
  <div class="table-scroll">{table_html}</div>
  {f'<div class="note">{html.escape(module.table.note)}</div>' if module.table.note else ''}
  <div class="table-tools">{chips}</div>
</section>""")
    return "\n".join(cards)


def _render_chart(chart: ChartSpec | None) -> str:
    if chart is None or not chart.categories:
        return '<div class="muted">本模块无图表（数据不足以作图）。</div>'
    series = chart.series[0] if chart.series else {"name": "数值", "values": []}
    unit = "yuan" if ("元" in chart.title or "销售额" in (chart.y_label or "")) else "count"
    if "占比" in chart.title:
        unit = "pct"
    payload = {
        "kind": chart.kind,
        "categories": [str(c) for c in chart.categories],
        "series": [{"name": series.get("name", "数值"), "values": series.get("values", [])}],
        "highlight": chart.highlight,
        "unit": unit,
    }
    attribute = html.escape(json.dumps(payload, ensure_ascii=False), quote=True)
    note = f'<div class="note">{html.escape(chart.note)}</div>' if chart.note else ""
    return (f'<div class="chart-title">{html.escape(chart.title)}</div>'
            f'<div class="chart-holder" data-chart="{attribute}"></div>{note}')


def _render_table(table: Table, *, table_id: str) -> str:
    head = "".join(f"<th>{html.escape(column)}</th>" for column in table.columns)
    body_rows: list[str] = []
    for row_index, row in enumerate(table.rows):
        cells: list[str] = []
        for col_index, value in enumerate(row):
            fmt = table.format_of(row_index, col_index)
            cells.append(_cell(value, fmt))
        highlight = ' class="highlight"' if table.rows and row_index == 0 else ""
        body_rows.append(f"<tr{highlight}>{''.join(cells)}</tr>")
    foot = ""
    if table.total_row:
        foot_cells = "".join(_cell(value, table.format_of(max(len(table.rows) - 1, 0), index))
                             for index, value in enumerate(table.total_row))
        foot = f"<tfoot><tr>{foot_cells}</tr></tfoot>"
    return (f'<table id="{table_id}" data-title="{html.escape(table.title)}" '
            f'data-sortable="1"><thead><tr>{head}</tr></thead>'
            f"<tbody>{''.join(body_rows)}</tbody>{foot}</table>")


def _cell(value: Any, fmt: str) -> str:
    if value == NA or value is None:
        return '<td class="na">N/A</td>'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if fmt == "money":
            return f'<td class="num">{fmt_money(value)}</td>'
        if fmt == "pct":
            return f'<td class="num">{fmt_pct(value)}</td>'
        if fmt == "num":
            digits = 2 if abs(value - round(value)) > 1e-9 else 0
            return f'<td class="num">{fmt_num(value, digits)}</td>'
        return f'<td class="num">{fmt_num(value, 2)}</td>'
    text = str(value)
    if len(text) > 60:
        return f'<td class="muted">{html.escape(text)}</td>'
    return f"<td>{html.escape(text)}</td>"


def _status_chips(checks: Sequence[Any]) -> str:
    if not checks:
        return ""
    items = []
    for check in checks:
        items.append(
            f'<span class="status {check.status}" title="{html.escape(str(check.note))}">'
            f"{check.status} · {html.escape(check.id)}</span>")
    return " ".join(items)


def _render_verification(run: AnalysisRun) -> str:
    all_checks = [c for m in run.modules for c in m.validation.checks] + run.global_result.checks
    counts = {status: sum(1 for c in all_checks if c.status == status)
              for status in ("PASS", "WARN", "FAIL", "SKIP")}
    top = [
        ("PASS", counts["PASS"], "数值吻合（在容差内）"),
        ("WARN", counts["WARN"], "差额已被解释，且写进了报告"),
        ("FAIL", counts["FAIL"], "无法解释的差额（会阻断出表）"),
        ("SKIP", counts["SKIP"], "缺数据，无法对账（已注明原因）"),
    ]
    cards = "".join(
        f'<div class="check"><span class="status {status}">{status}</span>'
        f'<div class="name">{count}</div><div class="meta">{html.escape(note)}</div></div>'
        for status, count, note in top)

    detail_rows = []
    for check in all_checks:
        detail_rows.append(
            f"<tr><td>{html.escape(check.module or '全局')}</td>"
            f"<td>{html.escape(check.id)}</td><td>{html.escape(check.name)}</td>"
            f'<td class="num">{html.escape(_plain(check.expected))}</td>'
            f'<td class="num">{html.escape(_plain(check.actual))}</td>'
            f'<td><span class="status {check.status}">{check.status}</span></td>'
            f'<td class="muted">{html.escape(check.note or '')}</td></tr>')
    detail_table = (
        '<div class="table-scroll"><table id="tbl-checks" data-title="校验明细" data-sortable="1">'
        "<thead><tr><th>模块</th><th>校验项</th><th>校验内容</th><th>期望值</th>"
        "<th>实际值</th><th>结论</th><th>说明</th></tr></thead><tbody>"
        + "".join(detail_rows) + "</tbody></table></div>")

    recon = run.reconciliation
    recon_rows = "".join(
        f"<tr><td>{html.escape(name)}</td><td class='num'>{value}</td>"
        f"<td class='muted'>{html.escape(note)}</td></tr>"
        for name, value, note in [
            ("原始解析总额（去重后）", fmt_money(recon.raw_total), "分析口径 + 不可归属 + 排除项"),
            ("分析口径总销售额", fmt_money(recon.analysis_total), "六维分析共同使用"),
            ("非数字行", fmt_money(recon.invalid_numbers), "无法解析，未估算"),
            ("负数/退款行（剔除）", fmt_money(recon.negative_excluded), "按策略剔除"),
            ("时间无法归属/区间外", fmt_money(recon.unassigned), "进「未归属」桶"),
            ("对账结果", fmt_money(recon.expected_raw),
             f"与原始总额差异 {fmt_money(recon.diff)}"),
        ])
    recon_table = (
        '<div class="table-scroll"><table id="tbl-recon"><thead><tr><th>项目</th>'
        "<th>金额</th><th>说明</th></tr></thead><tbody>" + recon_rows + "</tbody></table></div>")

    clean_rows = "".join(
        f"<tr><td>{html.escape(e.step)}</td><td>{html.escape(e.title)}</td>"
        f"<td class='muted'>{html.escape(e.rule)}</td>"
        f"<td class='num'>{e.input_rows:,}</td><td class='num'>{e.affected_rows:,}</td>"
        f"<td class='num'>{fmt_money(e.amount_delta)}</td>"
        f"<td class='muted'>{html.escape(e.detail)}</td></tr>"
        for e in run.cleaned.log)
    clean_table = (
        '<div class="table-scroll"><table id="tbl-cleanlog" data-title="清洗日志" '
        'data-sortable="1"><thead><tr><th>步骤</th><th>环节</th><th>规则</th>'
        "<th>输入行数</th><th>影响行数</th><th>金额影响</th><th>结果说明</th></tr></thead>"
        f"<tbody>{clean_rows}</tbody></table></div>")

    return f"""<section class="card" id="verify">
  <h2><span class="chip-no">7</span>校验与对账</h2>
  <p class="lead">六个模块的表格生成后先跑校验，通过才进入下一个模块；全部分析结束后再做全局对账与交叉验证。</p>
  <div class="checks">{cards}</div>
  <h3 style="margin:22px 0 6px;font-size:15px;color:#1f3864">合计对账（瀑布）</h3>
  {recon_table}
  <h3 style="margin:22px 0 6px;font-size:15px;color:#1f3864">逐项校验明细</h3>
  <div class="table-tools"><span class="hint">FAIL 会中断执行，不会带病出表</span>
    <button class="btn" data-csv="tbl-checks">导出该校验表 CSV</button></div>
  {detail_table}
  <details style="margin-top:18px">
    <summary style="cursor:pointer;color:#2f5597;font-size:14px">展开清洗日志（每个数字的溯源起点）</summary>
    <div class="table-tools"><span class="hint">金额影响为负 = 该步骤从分析口径中剔除的金额</span>
      <button class="btn" data-csv="tbl-cleanlog">导出清洗日志 CSV</button></div>
    {clean_table}
  </details>
</section>"""


def _render_conclusion(run: AnalysisRun) -> str:
    """底部固定四段：问题清单 → 机会点 → 行动建议 → 风险预警。"""
    points = list(getattr(run, "points", []))
    problems = "".join(
        f'<li><b>{html.escape(p.id)}</b>　<span class="tag {p.priority}">{p.priority}</span>'
        f"{html.escape(p.problem)}<div class='muted' style='font-size:12.5px'>"
        f"影响范围：{html.escape(p.scope_type)}{html.escape('、'.join(p.scope) or '全平台')}"
        f"　证据：{html.escape('；'.join(e.text() for e in p.evidence))}</div></li>"
        for p in points) or '<li class="muted">本期未识别到需要干预的问题。</li>'
    findings = "".join(
        f'<div class="finding"><div class="rank">机会 {f.rank}</div>'
        f"<h3>{html.escape(f.title)}</h3><p>{html.escape(f.detail)}</p>"
        f'<div class="ev">证据：{html.escape(f.evidence)}</div></div>'
        for f in run.narrative.findings)
    actions = "".join(
        f'<li><span class="tag {a.priority}">{a.priority}</span>'
        f"{html.escape(a.action)}<div class='muted' style='font-size:12.5px'>"
        f"依据：{html.escape(a.basis)}"
        + (f"　预期：{html.escape(a.expected)}" if a.expected else "")
        + "</div></li>"
        for a in run.narrative.actions)
    risks = "".join(
        f'<li><span class="tag {r.level}">{r.level}</span>'
        f"{html.escape(r.risk)}<div class='muted' style='font-size:12.5px'>"
        f"影响：{html.escape(r.impact)}　处置：{html.escape(r.mitigation)}</div></li>"
        for r in run.narrative.risks)
    return f"""<section class="card" id="conclusion">
  <h2><span class="chip-no">★</span>结论与行动（问题清单 · 机会点 · 行动建议 · 风险预警）</h2>
  <p class="lead">四段固定收尾；行动建议与「13_销售提升点清单」同源生成，每条都能回指上方表格里的具体数字。
  导出：本页可直接另存为 PDF；Excel 报表（同名 xlsx）可另存为 Word。</p>
  <h3 style="margin:16px 0 8px;font-size:15px;color:#1f3864">一、问题清单（{len(points)} 条）</h3>
  <ul class="risks" style="padding-left:0;list-style:none">{problems}</ul>
  <h3 style="margin:22px 0 8px;font-size:15px;color:#1f3864">二、机会点（前 3 大发现）</h3>
  <div class="findings">{findings}</div>
  <h3 style="margin:22px 0 6px;font-size:15px;color:#1f3864">三、行动建议（责任对象 + 预期效果 + 验证方式）</h3>
  <ol class="actions" style="padding-left:20px">{actions}</ol>
  <h3 style="margin:22px 0 6px;font-size:15px;color:#1f3864">四、风险预警</h3>
  <ul class="risks" style="padding-left:0;list-style:none">{risks}</ul>
</section>"""


def _render_footer(run: AnalysisRun) -> str:
    cfg = run.config
    hashes = "；".join(f"{name}: {value[:12]}…" for name, value in cfg.sources.hashes.items())
    roster = fmt_num(run.ctx.roster_total) if run.ctx.roster_total else NA
    return f"""<footer id="appendix">
  <div class="row">
    <span>销售明细：{html.escape(Path(cfg.sources.sales).name)}</span>
    <span>分类表：{html.escape(Path(cfg.sources.categories).name) if cfg.sources.categories else NA}</span>
    <span>楼栋人数表：{html.escape(Path(cfg.sources.dorm_roster).name) if cfg.sources.dorm_roster else NA}</span>
    <span>覆盖在住人数：{html.escape(roster)}</span>
  </div>
  <div class="row" style="margin-top:8px">
    <span>口径版本：skill v{cfg.skill_version} / config v{cfg.schema_version}</span>
    <span>确认时间：{html.escape(cfg.confirmed_at or NA)}</span>
    <span>快照：{"已包含" if cfg.include_raw_snapshot else "按用户要求不包含"}</span>
  </div>
  <div class="row" style="margin-top:8px">
    <span>文件指纹：{html.escape(hashes or NA)}</span>
  </div>
  <p style="margin:12px 0 0">
    本报告由 WorkBuddy Skill <code>campus-vending-sales-analysis</code> 生成：先做输入完整性检查与用户确认，
    再按六个模块逐个校验后出表。缺失值一律显示 N/A 并注明原因，不使用估算值填充；
    分组表合计与分析口径总额的差额都在「校验与对账」中列明。
  </p>
  <p style="margin:6px 0 0">© <span id="build-year"></span> {html.escape(cfg.platform_name)} · 内部经营分析用途</p>
</footer>"""


def _plain(value: Any) -> str:
    if value is None:
        return NA
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, float):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)
