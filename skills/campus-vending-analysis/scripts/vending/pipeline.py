"""编排：读入 → 清洗 → 六模块（含环比）→ 校验门禁 → 增长点 → 报表 → 导出 → 归档。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from .archive import (Baseline, Meta, fingerprint, load_baseline, resolve_root,
                      roster_digest, sha256_file, write_period, ensure_root, period_dir)
from .clean import clean
from .core import (NA, AnalysisConfig, ExportError, GateFailed, InputError, ModuleResult,
                   fmt_money)
from .diagnose import build_narrative, build_points, narrate_module
from .modules import MODULE_BUILDERS, AnalysisContext
from .product import build_category_mapping, build_dorm_roster
from .report import (build_all_tables, build_reconciliation as build_recon_table,
                     build_snapshot, run_global_checks)
from .sources import read_machine_map, read_table
from .style import load_profile
from .validation import GateFailed as _GateFailed, build_reconciliation, gate


@dataclass
class AnalysisRun:
    config: AnalysisConfig
    cleaned: Any
    ctx: AnalysisContext
    modules: list[ModuleResult]
    global_result: Any
    reconciliation: Any
    reconciliation_table: Any
    narrative: Any
    points: list[Any]
    sheets: list[Any]
    tables: dict[str, Any]
    baseline_info: dict[str, Any] = field(default_factory=dict)
    baseline: Baseline | None = None
    style_source: str = "内置默认"
    style: Any = None
    snapshot: Any = None
    sku_table: Any = None
    extra_checks: list[Any] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    archive: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (all(m.status != "FAIL" for m in self.modules)
                and self.global_result.status != "FAIL")

    def summary(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "period_key": self.ctx.period_key,
            "period_kind": self.ctx.period_kind,
            "baseline": self.baseline_info,
            "current_total_amount": self.ctx.total_amount,
            "all_periods_total_amount": self.ctx.all_amount,
            "previous_total_amount": (self.ctx.prev_stat.amount if self.ctx.prev_stat else None),
            "rows": {key: self.cleaned.stats.get(key) for key in
                     ("raw_rows", "kept_rows", "unassigned_rows")},
            "modules": [{"key": m.key, "title": m.title, "status": m.status,
                         "checks": [c.to_dict() for c in m.validation.checks]}
                        for m in self.modules],
            "global_checks": [c.to_dict() for c in self.global_result.checks],
            "uplift_points": [p.to_dict() for p in self.points],
            "style_source": self.style_source,
            "outputs": self.outputs,
            "archive": self.archive,
        }


def _load_inputs(config: AnalysisConfig) -> tuple[Any, dict[str, str], dict[str, Any]]:
    sales_table = read_table(config.sources.sales, sheet=config.sources.sheet,
                             encoding=config.sources.encoding or "auto")
    category_map: dict[str, str] = {}
    if config.sources.categories:
        category_map = build_category_mapping(read_table(config.sources.categories))
    roster: dict[str, Any] = {}
    if config.sources.dorm_roster:
        roster = build_dorm_roster(read_table(config.sources.dorm_roster))
    return sales_table, category_map, roster


def analyze(config: AnalysisConfig, *, data_root: str | None = None,
            period_kind: str = "weekly", period_key: str = "",
            baseline: Baseline | None = None) -> AnalysisRun:
    """执行完整分析；任何模块校验 FAIL 都抛 GateFailed，不产出报表。"""
    config.require_confirmed()
    if period_key:
        from .archive import bounds
        from .core import Period

        start, end = bounds(period_kind, period_key)
        # 周期键即本期口径：表头必须显示周期键，不能沿用 preflight 猜的时间段标签
        config.periods = [Period(label=period_key, start=start, end=end)]

    sales_table, category_map, roster = _load_inputs(config)
    if config.unmapped_products == "abort":
        raise InputError("配置要求「未匹配商品必须补充分类表后再分析」，当前已停止。",
                         hint="补全分类表后重跑 preflight/confirm。")

    machine_map: dict[str, str] = {}
    if config.dorm.mode == "machine_map":
        if not config.dorm.machine_map_path:
            raise InputError("楼栋归属策略为 machine_map，但未提供映射文件路径。")
        machine_map = read_machine_map(config.dorm.machine_map_path)

    cleaned = clean(sales_table, config.columns, config.periods, config.dorm,
                    category_map=category_map, machine_map=machine_map,
                    negative_policy=config.negative_policy,
                    dedupe_policy=config.dedupe_policy,
                    known_data_issues=config.known_data_issues,
                    unmapped_policy=config.unmapped_products)
    if not cleaned.analyzed_records:
        raise InputError("清洗后没有任何记录落在配置的时间段内，无法分析。",
                         hint=f"检查时间段划分与时间列映射；未归属 {cleaned.stats.get('unassigned_rows', 0)} 行。")

    root, root_source = resolve_root(data_root or getattr(config, "archive_root", "") or "")
    ensure_root(root)
    period_key = period_key or ""
    digest = roster_digest(roster)
    print_fingerprint = fingerprint(config, digest)
    if baseline is None and period_key:
        baseline = load_baseline(root, period_kind, period_key, print_fingerprint)

    ctx = AnalysisContext.build(cleaned, config, roster, baseline=baseline,
                               period_kind=period_kind, period_key=period_key)

    modules: list[ModuleResult] = []
    for _, builder in MODULE_BUILDERS:
        module = builder(ctx)
        gate(module.validation)
        modules.append(module)

    points = build_points(ctx)
    for module in modules:
        module.insight = narrate_module(module.key, points)

    recon = build_reconciliation(cleaned.stats, ctx.all_amount, config.negative_policy)
    global_result = run_global_checks(ctx, modules, cleaned, recon)
    gate(global_result)

    narrative = build_narrative(ctx, modules, points, cleaned)
    style = load_profile(_template_path())
    recon_table = build_recon_table(cleaned, ctx, config.negative_policy)

    snapshot = None
    if config.include_raw_snapshot:
        from .sources import snapshot_rows

        try:
            rows, columns = snapshot_rows(sales_table, config.snapshot_limit)
            snapshot = build_snapshot(rows, columns, config.snapshot_limit)
        except Exception:                                   # noqa: BLE001 - 快照失败不阻断分析
            snapshot = None

    sku_table = _build_sku_table(ctx, cleaned)

    baseline_info = _baseline_info(baseline, root, root_source, print_fingerprint, period_key)

    run = AnalysisRun(config=config, cleaned=cleaned, ctx=ctx, modules=modules,
                      global_result=global_result, reconciliation=recon,
                      reconciliation_table=recon_table, narrative=narrative,
                      points=points, sheets=[], tables={}, baseline_info=baseline_info,
                      baseline=baseline, style_source=style.source, style=style,
                      snapshot=snapshot, sku_table=sku_table)
    run.sheets, run.tables = build_all_tables(run)
    return run


def _template_path() -> Path | None:
    candidate = Path(__file__).resolve().parents[2] / "assets" / "template.xlsx"
    return candidate if candidate.exists() else None


def _build_sku_table(ctx: Any, cleaned: Any) -> Any:
    from .core import Table

    rows: list[list[Any]] = []
    for sku_id, info in sorted(cleaned.sku_index.items()):
        names = info.get("aliases") or info.get("raw_names") or []
        rows.append([sku_id, info.get("name", sku_id), info.get("spec", NA),
                     info.get("category", NA),
                     "、".join(names) if isinstance(names, (list, tuple)) else str(names),
                     info.get("rows", NA)])
    if not rows:
        return None
    table = Table(key="sku", title="单品追溯（单品 → 合并的原始名 → 行数）",
                  columns=["单品", "品名", "规格", "品类", "合并的原始名称", "原始行数"],
                  rows=rows, col_formats={5: "int"})
    table.note = "单品判定规则的可验证证据：规格不同算两个单品，同品异名在这里能看到合并过程。"
    return table


def _baseline_info(baseline: Baseline | None, root: Path, root_source: str,
                   print_fingerprint: str, period_key: str) -> dict[str, Any]:
    if baseline is None:
        return {"exists": False, "text": "未指定周期键，未装载上期基准（环比全部 N/A）",
                "reason": "未指定周期键", "source": "无", "root": str(root),
                "fingerprint": print_fingerprint, "baseline_key": NA,
                "compatible_text": NA, "counts": NA, "baseline_fingerprint": NA}
    counts = baseline.meta.get("counts") if baseline.meta else NA
    if baseline.ok:
        text = f"{baseline.key}（已装载 {len(baseline.records)} 行 facts）"
    else:
        text = f"{baseline.key}：{baseline.reason}（环比全部 N/A）"
    return {
        "exists": baseline.exists,
        "text": text,
        "reason": baseline.reason,
        "source": f"归档 processed/{baseline.key}/facts.csv（数据根来自 {root_source}）"
        if baseline.exists else f"归档 processed/{baseline.key}（不存在）",
        "root": str(root),
        "fingerprint": print_fingerprint,
        "baseline_key": baseline.key,
        "compatible_text": "可比" if baseline.compatible else "不可比（口径指纹不同）",
        "counts": counts,
        "baseline_fingerprint": (baseline.meta or {}).get("fingerprint", NA),
    }


def build_meta(run: AnalysisRun, sources: Sequence[Path] = ()) -> Meta:
    counts = {
        "raw_rows": run.cleaned.stats.get("raw_rows"),
        "kept_rows": run.cleaned.stats.get("kept_rows"),
        "unassigned_rows": run.cleaned.stats.get("unassigned_rows"),
        "current_amount": run.ctx.total_amount,
        "sku_count": run.cleaned.stats.get("sku_count"),
    }
    return Meta(period_kind=run.ctx.period_kind, period_key=run.ctx.period_key,
                periods=[p.to_dict() for p in run.config.periods],
                fingerprint=run.baseline_info.get("fingerprint", ""), counts=counts,
                sources=[{"path": str(path), "sha256": sha256_file(path)}
                         for path in sources if Path(path).exists()],
                baseline_key=(run.baseline.key if run.baseline else ""))


def export(run: AnalysisRun, out_dir: Path, *, prefix: str = "",
           formats: Sequence[str] = ("excel", "html", "docx")) -> dict[str, str]:
    """导出；单格式失败不影响其他格式，但会记在 outputs 里。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = prefix or f"{run.config.platform_name}_销售提升分析"
    outputs: dict[str, str] = {}
    if "excel" in formats:
        from .export_excel import export_excel

        path = out_dir / f"{stem}.xlsx"
        export_excel(run, path)
        outputs["excel"] = str(path)
    if "html" in formats:
        from .export_html import export_html

        path = out_dir / f"{stem}_交互报告.html"
        export_html(run, path)
        outputs["html"] = str(path)
    if "docx" in formats:
        from .export_docx import export_docx

        path = out_dir / f"{stem}.docx"
        try:
            export_docx(run, path)
            outputs["docx"] = str(path)
        except ExportError as exc:
            outputs["docx_error"] = f"{exc.message}｜{exc.hint or ''}"
    run.outputs = outputs
    manifest = out_dir / "analysis_run.json"
    manifest.write_text(json.dumps(run.summary(), ensure_ascii=False, indent=2), encoding="utf-8")
    outputs["manifest"] = str(manifest)
    return outputs


def archive_run(run: AnalysisRun, *, data_root: str | None = None,
                force: bool = False) -> dict[str, Any]:
    """把本期原始/清洗/报告写入归档；同目录已存在时由调用方先确认（force）。"""
    root, _ = resolve_root(data_root)
    key = run.ctx.period_key or "未命名期"
    sources = [Path(p) for p in (run.config.sources.sales, run.config.sources.categories,
                                 run.config.sources.dorm_roster) if p]
    meta = build_meta(run, sources)
    report_paths = {name: Path(path) for name, path in run.outputs.items()
                    if name in ("excel", "html", "docx")}
    uplift_rows = [point.to_row(index) for index, point in enumerate(run.points, start=1)]
    entry = write_period(root, run.ctx.period_kind, key, meta=meta,
                         records=run.cleaned.analyzed_records,
                         report_paths=report_paths, uplift_rows=uplift_rows, force=force)
    run.archive = entry
    return entry


def write_gate_failure(error: GateFailed, out_dir: Path) -> dict[str, str]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "validation_failure.json"
    md_path = out_dir / "validation_failure.md"
    payload = error.to_dict()
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 校验未通过，已停止出表", "",
             f"**模块**：{payload['details'].get('module', 'N/A')}", "",
             f"**原因**：{payload['message']}", ""]
    for item in payload["details"].get("failed", []):
        lines.append(f"- `{item['id']}` {item['name']}：期望 {item['expected']}，"
                     f"实际 {item['actual']}，说明：{item['note']}")
    lines += ["", f"**修复指引**：{payload.get('hint') or ''}"]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return {"failure_json": str(json_path), "failure_md": str(md_path)}
