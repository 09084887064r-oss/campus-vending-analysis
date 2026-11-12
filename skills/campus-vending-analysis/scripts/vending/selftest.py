"""端到端自测：契约、校验、环比、归档、样式、四段式结论。

跑法：python run.py selftest --verbose
每条断言都对应 SKILL.md 里的一条硬约束，通过即证据。
"""

from __future__ import annotations

import json
import re
import tempfile
import traceback
from datetime import date
from pathlib import Path
import shutil
from typing import Any, Callable

CL_PATTERN = re.compile(r"CL-\d{2}")


class Checker:
    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.results: list[dict[str, Any]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> bool:
        status = "PASS" if condition else "FAIL"
        self.results.append({"name": name, "status": status, "detail": detail})
        if self.verbose or not condition:
            print(f"{status} {name}" + (f" ｜ {detail}" if detail else ""))
        return condition

    def attempt(self, name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:                      # noqa: BLE001
            self.check(name, False, f"{type(exc).__name__}: {exc}")
            if self.verbose:
                traceback.print_exc()
            return None

    def summary(self) -> dict[str, Any]:
        passed = sum(1 for item in self.results if item["status"] == "PASS")
        return {"ok": passed == len(self.results), "passed": passed,
                "failed": len(self.results) - passed, "total": len(self.results),
                "checks": self.results}


def run_selftest(verbose: bool = False) -> dict[str, Any]:
    from openpyxl import load_workbook

    from .archive import SCHEMA, iso_week_key, month_key, prev_key, week_bounds
    from .aggregate import Group
    from .archive import fingerprint, load_baseline
    from .cli import main as cli_main
    from . import deps
    from .archive import backup_existing
    from .diagnose import MIN_HOUR_DEALS, OWNER_BY_MODULE
    from .modules import _dorm_alias, _merge_by_alias
    from .core import NA, Table
    from .demo import build_demo_dataset
    from .pipeline import analyze, archive_run, export
    from .preflight import apply_answers, run_preflight
    from .style import load_profile

    checker = Checker(verbose)
    workdir = Path(tempfile.mkdtemp(prefix="vending-selftest-"))

    checker.check("ISO 周键 = 2026-W44", iso_week_key(date(2026, 10, 26)) == "2026-W44")
    checker.check("自然月键 = 2026-11", month_key(date(2026, 11, 2)) == "2026-11")
    checker.check("上期周键回退一周", prev_key("weekly", "2026-W45") == "2026-W44")
    checker.check("上期月键回退一月", prev_key("monthly", "2026-01") == "2025-12")
    monday, sunday = week_bounds("2026-W45")
    checker.check("周区间为周一至周日（7 天）", (sunday - monday).days + 1 == 7)
    checker.check("缺失值自动写成 N/A",
                  Table(key="k", title="t", columns=["a"], rows=[[None]]).rows[0][0] == NA)

    files = build_demo_dataset(workdir / "demo", seed=20261108)
    runs: dict[str, Any] = {}
    for key in files["periods"]:
        preflight = run_preflight(sales=str(files["sales"][key]),
                                  categories=str(files["categories"]),
                                  dorm_roster=str(files["roster"]),
                                  machine_map=str(files["machine_map"]),
                                  out_dir=str(workdir / key))
        config = apply_answers(preflight, {}, accept_proposals=True)
        config.save(workdir / key / "config.json")
        checker.check(f"{key} 口径已确认", config.confirmed is True)
        run = analyze(config, data_root=str(workdir / "archive"),
                      period_kind="weekly", period_key=key)
        runs[key] = run
        export(run, workdir / "reports" / key, prefix=f"t_{key}",
               formats=("excel", "html", "docx"))
        archive_run(run, data_root=str(workdir / "archive"), force=True)

    first, last = files["periods"][0], files["periods"][1]
    run_first, run_last = runs[first], runs[last]

    checker.check("首期无上期基准 → 环比 N/A", run_first.ctx.prev_stat is None)
    checker.check("次期从归档装载上期基准", run_last.ctx.prev_stat is not None)
    checker.check("次期上期总额 = 首期本期总额",
                  run_last.ctx.prev_stat is not None
                  and abs(run_last.ctx.prev_stat.amount - run_first.ctx.total_amount) < 0.02)
    core_table = run_last.tables["core"]
    checker.check("核心表含变化额/变化率列",
                  any(c.endswith("变化额") for c in core_table.columns)
                  and any(c.endswith("变化率") for c in core_table.columns))

    for module in run_last.modules:
        checker.check(f"{module.title} 校验非 FAIL", module.status != "FAIL", module.status)
    checker.check("六模块全部产出", len(run_last.modules) == 6, str(len(run_last.modules)))
    failed = [c.id for c in run_last.global_result.checks if c.status == "FAIL"]
    checker.check("全局交叉验证无 FAIL", not failed, str(failed))

    points = run_last.points
    checker.check("识别到增长点 ≥3", len(points) >= 3, f"{len(points)} 条")
    checker.check("每条提升点有证据", all(p.evidence for p in points))
    checker.check("每条提升点原因含「假设」", all("假设" in p.hypothesis for p in points))
    checker.check("每条有验证方式与预期效果", all(p.verify and p.expected for p in points))
    checker.check("每条写清影响范围", all(p.scope and p.scope_type for p in points))
    checker.check("每条写清责任对象（角色+具体对象）",
                  all(p.owner and "（" in p.owner and p.owner.endswith("）") for p in points),
                  str([p.owner for p in points[:3]]))
    checker.check("责任对象按模块映射，不串岗",
                  all(p.owner.startswith(OWNER_BY_MODULE[p.module]) for p in points))
    checker.check("提升点编号连续",
                  [p.id for p in points] == [f"UP-{i:02d}" for i in range(1, len(points) + 1)])
    checker.check("提升点模块归属合法",
                  all(p.module in {"core", "hourly", "price", "category", "machine", "dorm"}
                      for p in points))

    for module in run_last.modules:
        checker.check(f"{module.title} 解读含问题/假设/动作",
                      all(token in module.insight for token in ("【问题】", "【假设】", "【动作】")))

    backup_dir = workdir / "_backup_probe"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True)
    (backup_dir / "报告.xlsx").write_text("v1", encoding="utf-8")
    first = backup_existing(backup_dir)
    (backup_dir / "报告.xlsx").write_text("v2", encoding="utf-8")
    second = backup_existing(backup_dir)
    names = sorted(p.name for p in backup_dir.iterdir())
    checker.check("重复覆盖只产生 .bak/.bak2，不叠加 .bak.bak",
                  "报告.xlsx.bak" in names and "报告.xlsx.bak2" in names
                  and not any(name.endswith(".bak.bak") or ".bak2.bak" in name for name in names),
                  str(names))
    checker.check("备份不把上一次的备份再备份", len(first) == 1 and len(second) == 1,
                  f"{first} / {second}")
    shutil.rmtree(backup_dir, ignore_errors=True)

    required_missing, optional_missing = deps.detect()
    checker.check("本机必需依赖齐全（不误报）", not required_missing, str(sorted(required_missing)))
    fake = deps.format_missing(
        {"openpyxl": {"package": "openpyxl>=3.1", "purpose": "读写 Excel"}})
    checker.check("缺依赖时状态可被 AI 识别", fake["status"] == "MISSING_DEPENDENCIES")
    checker.check("缺依赖提示含可执行安装命令",
                  "pip install" in fake["install_command"]
                  and "openpyxl" in fake["install_command"],
                  fake["install_command"])
    checker.check("缺依赖提示要求安装后重跑",
                  "安装" in fake["agent_instruction"] and "重新运行" in fake["agent_instruction"])
    checker.check("缺依赖退出码走输入类（2）", fake["exit_code"] == 2)
    checker.check("缺库不阻断可选功能提示",
                  "python-docx" in deps.format_missing(
                      {"docx": {"package": "python-docx>=1.1", "purpose": "Word"}})["message"])

    steps = {entry.step for entry in run_last.cleaned.log}
    refs: set[str] = set()
    for table_obj in run_last.tables.values():
        for row in getattr(table_obj, "rows", []):
            for cell in row:
                refs.update(CL_PATTERN.findall(str(cell)))
    checker.check("溯源引用全部指向真实日志步骤", not (refs - steps), str(sorted(refs - steps)))

    xlsx = Path(run_last.outputs["excel"])
    workbook = checker.attempt("Excel 可打开", lambda: load_workbook(xlsx))
    if workbook is not None:
        names = workbook.sheetnames
        for required in ("00_说明", "01_清洗日志", "03_环比基准与口径", "04_核心数据分析",
                         "05_时段销售分析", "06_价格带陈列分析", "07_品类数销售分析",
                         "08_机组销售环比", "09_用户消费分析", "11_校验汇总",
                         "12_口径与溯源", "13_销售提升点清单", "14_结论与建议"):
            checker.check(f"Excel 含工作表 {required}", required in names)
        hidden = [ws.title for ws in workbook.worksheets if ws.sheet_state == "hidden"]
        checker.check("图表数据表被隐藏", "zz_图表数据" in hidden)
        header_fill = None
        for row in workbook["04_核心数据分析"].iter_rows(min_row=1, max_row=6):
            for cell in row:
                if cell.fill and cell.fill.patternType and cell.font and cell.font.bold:
                    rgb = getattr(cell.fill.fgColor, "rgb", None)
                    header_fill = header_fill or (rgb if isinstance(rgb, str) else None)
        checker.check("表头底色来自模板 #FF534AB7", header_fill == "FF534AB7", str(header_fill))
        mom_rules = sum(len(sheet.conditional_formatting._cf_rules)       # noqa: SLF001
                        for sheet in workbook.worksheets
                        if sheet.conditional_formatting)
        checker.check("环比列有红绿条件格式", mom_rules >= 1, f"{mom_rules} 条")
        workbook.close()

    html = Path(run_last.outputs["html"]).read_text(encoding="utf-8")
    for section in ("问题清单", "机会点", "行动建议", "风险预警"):
        checker.check(f"HTML 底部含「{section}」", section in html)
    checker.check("HTML 含环比字样", "环比" in html)
    checker.check("HTML 含提升点编号", any(p.id in html for p in points[:3]))
    checker.check("HTML 单文件无外链",
                  not re.search(r'(src|href)="https?://', html))

    docx_path = run_last.outputs.get("docx", "")
    checker.check("Word 报告已生成", bool(docx_path) and Path(docx_path).exists())

    archive = workdir / "archive"
    for layer in ("processed", "reports"):
        checker.check(f"归档含 {layer}/{last}", (archive / layer / last).exists())
    meta = json.loads((archive / "processed" / last / "meta.json").read_text(encoding="utf-8"))
    checker.check("归档 meta 版本正确", meta["schema"] == SCHEMA)
    checker.check("归档 meta 有口径指纹", bool(meta.get("fingerprint")))
    checker.check("归档 manifest 有台账行", (archive / "manifest.jsonl").exists())

    fallen = load_profile(workdir / "not-exists.xlsx")
    checker.check("模板缺失回退默认并写明原因", "内置默认" in fallen.source)
    real = load_profile(Path(__file__).resolve().parents[2] / "assets" / "template.xlsx")
    checker.check("模板存在时读到 #FF534AB7", real.header_fill == "FF534AB7", real.header_fill)
    checker.check("样式来源标注为模板", "模板" in real.source, real.source)

    # ---------- 14. 本次修复的行为断言（每条对应一个真实缺陷）
    dorm_rows = run_last.tables["dorm"].rows
    first_dorm = dorm_rows[0] if dorm_rows else []
    checker.check("老生/新生人数已接线（不是 N/A）",
                  len(first_dorm) > 3 and isinstance(first_dorm[2], (int, float))
                  and isinstance(first_dorm[3], (int, float)),
                  f"老生={first_dorm[2] if first_dorm else None} 新生={first_dorm[3] if first_dorm else None}")
    checker.check("新生占比可计算",
                  len(first_dorm) > 5 and isinstance(first_dorm[5], (int, float)),
                  str(first_dorm[5] if len(first_dorm) > 5 else None))
    checker.check("性别列有值（人数表或按机组类型推断）",
                  bool(first_dorm) and first_dorm[1] in {"男", "女"},
                  str(first_dorm[1] if first_dorm else None))
    checker.check("表头用周期键标注本期（不是 preflight 时间段标签）",
                  run_last.ctx.cur_period.label == last,
                  run_last.ctx.cur_period.label)

    hourly = [p for p in points if p.module == "hourly"]
    small_sample = []
    for point in hourly:
        scope = point.scope[0] if point.scope else ""
        if "–" in scope:                       # 合并窗口：样本由窗口保证
            continue
        hour = int(scope.split(":")[0])
        prev_group = run_last.ctx.prev_stat.hours.get(hour) if run_last.ctx.prev_stat else None
        if prev_group is not None and prev_group.order_count < MIN_HOUR_DEALS:
            small_sample.append(f"{scope}({prev_group.order_count} 单)")
    checker.check("时点类不拿小样本下结论", not small_sample,
                  f"小样本时点：{small_sample}" if small_sample else f"{len(hourly)} 条时点均达标")

    alias = _dorm_alias({"十栋+十一栋": {"total": 1200}})
    checker.check("楼栋组按人数表写法展开别名", alias.get("十栋") == "十栋+十一栋", str(alias))
    merged = _merge_by_alias(
        {"十栋": Group(key="十栋", qty=2, amount=100.0, users={"a"}),
         "十一栋": Group(key="十一栋", qty=1, amount=50.0, users={"b"})},
        {"十栋": "十栋+十一栋", "十一栋": "十栋+十一栋"})
    only = next(iter(merged.values())) if merged else None
    checker.check("合并后金额与用户数正确",
                  len(merged) == 1 and only is not None
                  and abs(only.amount - 150.0) < 0.01 and only.user_count == 2,
                  f"{len(merged)} 组 / 金额 {only.amount if only else None}")

    checker.check("价格带改动会改变口径指纹",
                  fingerprint(run_last.config, "r1") != fingerprint(
                      type(run_last.config)(**{**run_last.config.__dict__,
                                               "price_bands": []}), "r1"))

    # 首期没有上期 → N/A 必须一路传到网页报告
    html_first = Path(run_first.outputs["html"]).read_text(encoding="utf-8")
    checker.check("无上期时 HTML 出现 N/A", "N/A" in html_first)

    # 重跑同一期 → 旧文件另存 .bak
    archive_run(run_last, data_root=str(workdir / "archive"), force=True)
    backups = list((workdir / "archive" / "reports" / last).glob("*.bak*"))
    checker.check("重跑同期归档自动另存 .bak", bool(backups), f"{len(backups)} 个")

    # 退出码 7：本期已存在且未加 --force 时必须停下来问人
    config_path2 = workdir / last / "config.json"
    code = cli_main(["analyze", "--config", str(config_path2), "--out", str(workdir / "again"),
                     "--period-key", last, "--data-root", str(workdir / "archive"),
                     "--formats", "excel"])
    checker.check("归档已存在时退出码 = 7", code == 7, f"实际 {code}")
    return checker.summary()
