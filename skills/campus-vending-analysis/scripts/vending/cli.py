"""命令行入口：preflight / confirm / analyze / demo / selftest。

退出码是给 AI 看的契约：
0  成功
2  输入问题（文件缺失、无法解析、不满足契约）
3  需要向用户提问并取得确认（preflight 有阻断性问题）——必须停下来问人
4  配置未确认却试图分析（禁止跳过确认）
5  模块校验未通过（禁止带病出表）
6  导出失败
7  本期归档已存在，需要用户确认覆盖（确认后用 --force 重跑）
"""

from __future__ import annotations

EXIT_OVERWRITE = 7

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .core import (
    EXIT_INPUT,
    EXIT_NEEDS_CONFIRM,
    EXIT_OK,
    AnalysisConfig,
    GateFailed,
    NeedsConfirmation,
    SkillError,
)
from . import deps
from .pipeline import analyze, archive_run, export, write_gate_failure
from .preflight import PreflightResult, Question, apply_answers, run_preflight


def _configure_stdout() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")   # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass


def _print(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------- 子命令

def _cmd_preflight(args: argparse.Namespace) -> int:
    result = run_preflight(sales=args.sales, categories=args.categories,
                           dorm_roster=args.dorm, machine_map=args.machine_map,
                           sheet=args.sheet, encoding=args.encoding, out_dir=args.out)
    artifacts = {}
    if args.out:
        artifacts = {
            "preflight_report": str(Path(args.out) / "preflight_report.json"),
            "analysis_config": str(Path(args.out) / "analysis_config.json"),
            "questions": str(Path(args.out) / "preflight_questions.md"),
        }
    payload = {
        "status": "NEEDS_CONFIRMATION" if result.unresolved else "READY",
        "diagnostics": result.diagnostics,
        "questions": [q.to_dict() for q in result.questions],
        "blocking_unresolved": [q.id for q in result.unresolved],
        "artifacts": artifacts,
        "next_step": ("逐条向用户提问（见 questions），把回答写成 answers.json，"
                      "再运行 `run.py confirm --config ... --preflight ... --answers ...`"
                      if result.unresolved else
                      "无阻断性问题，可直接 `run.py confirm --accept-proposals` 完成确认后分析"),
    }
    _print(payload)
    return EXIT_NEEDS_CONFIRM if result.unresolved else EXIT_OK


def _load_questions(path: Path) -> list[Question]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    questions: list[Question] = []
    for raw in payload.get("questions", []):
        questions.append(Question(
            id=str(raw["id"]), title=str(raw.get("title", "")),
            detail=str(raw.get("detail", "")), options=list(raw.get("options", [])),
            proposal=raw.get("proposal"), blocking=bool(raw.get("blocking", True)),
            answer=raw.get("answer"), evidence=list(raw.get("evidence", []))))
    return questions


def _cmd_confirm(args: argparse.Namespace) -> int:
    config_path = Path(args.config)
    config = AnalysisConfig.load(config_path)
    questions: list[Question] = []
    diagnostics: dict[str, Any] = {}
    if args.preflight and Path(args.preflight).exists():
        questions = _load_questions(Path(args.preflight))
        diagnostics = json.loads(Path(args.preflight).read_text(encoding="utf-8")).get(
            "diagnostics", {})
    answers: dict[str, Any] = {}
    if args.answers:
        answers = json.loads(Path(args.answers).read_text(encoding="utf-8"))
    result = PreflightResult(config=config, questions=questions, diagnostics=diagnostics,
                             sales_table=None)      # type: ignore[arg-type]
    confirmed = apply_answers(result, answers, accept_proposals=args.accept_proposals)
    out_path = Path(args.out) if args.out else config_path
    confirmed.save(out_path)
    _print({
        "status": "CONFIRMED",
        "config": str(out_path),
        "confirmed_at": confirmed.confirmed_at,
        "answers": confirmed.answers,
        "periods": [p.to_dict() for p in confirmed.periods],
        "next_step": f"run.py analyze --config {out_path} --out <输出目录>",
    })
    return EXIT_OK


def _cmd_analyze(args: argparse.Namespace) -> int:
    from .archive import ensure_root, period_dir, resolve_root

    config = AnalysisConfig.load(Path(args.config))
    effective_root = args.data_root or getattr(config, "archive_root", "") or ""
    root, root_source = resolve_root(effective_root)
    key = args.period_key.strip()
    if key and not args.force and period_dir(ensure_root(root), "reports", key).exists():
        _print({
            "status": "NEEDS_CONFIRMATION",
            "reason": f"本期归档 {key} 已存在（{root}）",
            "next_step": ("向用户确认是否覆盖本期归档；确认后加 --force 重跑，"
                          "旧文件会自动另存为 .bak"),
            "exit_code": EXIT_OVERWRITE,
        })
        return EXIT_OVERWRITE

    run = analyze(config, data_root=effective_root, period_kind=args.period_kind,
                  period_key=key)
    formats = tuple(part.strip() for part in (args.formats or "excel,html,docx").split(",")
                    if part.strip())
    prefix = args.prefix or (f"校园售货机_销售提升分析_{key}" if key else "")
    outputs = export(run, Path(args.out), prefix=prefix, formats=formats)
    archive: dict[str, Any] = {}
    if key and not args.no_archive:
        archive = archive_run(run, data_root=effective_root, force=True)
    summary = run.summary()
    summary.update({"status": "OK", "outputs": outputs, "archive": archive,
                    "data_root": str(root), "data_root_source": root_source})
    _print(summary)
    return EXIT_OK


def _cmd_demo(args: argparse.Namespace) -> int:
    """两期演示：先跑上期（无基准）并归档，再跑本期（带环比）。"""
    from .demo import build_demo_dataset

    out_dir = Path(args.out or "demo_output")
    demo_dir = out_dir / "demo_input"
    files = build_demo_dataset(demo_dir, seed=args.seed)
    root = out_dir / "archive"
    results: dict[str, Any] = {}
    for index, key in enumerate(files["periods"]):
        sales = files["sales"][key]
        preflight = run_preflight(sales=str(sales), categories=str(files["categories"]),
                                  dorm_roster=str(files["roster"]),
                                  machine_map=str(files["machine_map"]),
                                  out_dir=str(out_dir / key))
        config = apply_answers(preflight, {}, accept_proposals=True)
        config_path = out_dir / key / "analysis_config.json"
        config.save(config_path)
        run = analyze(config, data_root=str(root), period_kind="weekly", period_key=key)
        prefix = f"示例_校园售货机_销售提升分析_{key}"
        outputs = export(run, out_dir / "reports" / key, prefix=prefix,
                         formats=("excel", "html", "docx"))
        archived = archive_run(run, data_root=str(root), force=True)
        summary = run.summary()
        results[key] = {
            "status": "OK", "outputs": outputs,
            "current_total_amount": summary["current_total_amount"],
            "previous_total_amount": summary["previous_total_amount"],
            "baseline": summary["baseline"]["text"],
            "uplift_points": len(summary["uplift_points"]),
            "archive": archived.get("reports", {}),
        }
    _print({"status": "DEMO_OK", "data_root": str(root),
            "periods": files["periods"], "results": results,
            "demo_input": {"categories": str(files["categories"]),
                           "roster": str(files["roster"]),
                           "machine_map": str(files["machine_map"]),
                           "sales": {k: str(v) for k, v in files["sales"].items()}}})
    return EXIT_OK


def _cmd_selftest(args: argparse.Namespace) -> int:
    from .selftest import run_selftest

    report = run_selftest(verbose=args.verbose)
    _print(report)
    return EXIT_OK if report["ok"] else 1


# ---------------------------------------------------------------- 入口

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="校园自助售货机销售分析 Skill：preflight → confirm → analyze")
    parser.add_argument("--version", action="version", version="campus-vending-analysis")
    subparsers = parser.add_subparsers(dest="command", required=True)

    preflight = subparsers.add_parser("preflight", help="输入完整性检查并生成待确认问题")
    preflight.add_argument("--sales", required=True, help="销售明细文件（CSV/TSV/Excel/JSON）")
    preflight.add_argument("--categories", default="", help="商品分类表（商品名称 → 品类）")
    preflight.add_argument("--dorm", default="", help="寝室楼栋人数表（楼栋 → 新老生人数）")
    preflight.add_argument("--machine-map", default="", help="机器编号 → 楼栋 映射（可选）")
    preflight.add_argument("--sheet", default="", help="Excel 工作表名")
    preflight.add_argument("--encoding", default="auto", help="CSV 编码，默认自动探测")
    preflight.add_argument("--out", default="", help="产物目录")
    preflight.set_defaults(func=_cmd_preflight)

    confirm = subparsers.add_parser("confirm", help="写回用户回答并确认口径")
    confirm.add_argument("--config", required=True)
    confirm.add_argument("--preflight", default="")
    confirm.add_argument("--answers", default="")
    confirm.add_argument("--accept-proposals", action="store_true",
                         help="把建议答案视为已确认（仅用于演示/自测）")
    confirm.add_argument("--out", default="")
    confirm.set_defaults(func=_cmd_confirm)

    analyze_cmd = subparsers.add_parser("analyze", help="执行六维分析并导出")
    analyze_cmd.add_argument("--config", required=True)
    analyze_cmd.add_argument("--out", required=True)
    analyze_cmd.add_argument("--prefix", default="")
    analyze_cmd.add_argument("--formats", default="excel,html,docx")
    analyze_cmd.add_argument("--data-root", default="", help="归档数据根目录（首次确认后固定）")
    analyze_cmd.add_argument("--period-kind", default="weekly", choices=["weekly", "monthly"],
                             help="周报=ISO 周；月报=自然月")
    analyze_cmd.add_argument("--period-key", default="",
                             help="本期周期键，如 2026-W45 或 2026-11；给了才会算环比与归档")
    analyze_cmd.add_argument("--force", action="store_true",
                             help="本期归档已存在时覆盖（旧文件另存为 .bak）")
    analyze_cmd.add_argument("--no-archive", action="store_true", help="只出报告，不写归档")
    analyze_cmd.set_defaults(func=_cmd_analyze)

    demo = subparsers.add_parser("demo", help="生成示例数据并跑完整流程")
    demo.add_argument("--out", default="demo_output")
    demo.add_argument("--seed", type=int, default=20241020)
    demo.set_defaults(func=_cmd_demo)

    selftest = subparsers.add_parser("selftest", help="端到端自测（契约与校验断言）")
    selftest.add_argument("--verbose", action="store_true")
    selftest.set_defaults(func=_cmd_selftest)

    deps_cmd = subparsers.add_parser("deps", help="检查运行依赖是否齐全")
    deps_cmd.set_defaults(func=_cmd_deps)
    return parser


def _cmd_deps(args: argparse.Namespace) -> int:
    payload = deps.summary()
    warning = deps.optional_warning()
    if warning:
        payload["warning"] = warning
    guard = deps.guard()
    if guard is not None:
        payload.update({"status": guard["status"], "message": guard["message"],
                        "install_command": guard["install_command"],
                        "agent_instruction": guard["agent_instruction"]})
    else:
        payload["status"] = "OK"
    _print(payload)
    return EXIT_OK if guard is None else guard["exit_code"]


def main(argv: list[str] | None = None) -> int:
    _configure_stdout()
    guard = deps.guard()
    if guard is not None:                     # 缺库先讲清楚要装什么，不抛 traceback
        _print(guard)
        return guard["exit_code"]
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NeedsConfirmation as exc:
        _print({"status": "NEEDS_CONFIRMATION", **exc.to_dict()})
        return exc.exit_code
    except GateFailed as exc:
        payload = {"status": "VALIDATION_FAILED", **exc.to_dict()}
        if getattr(args, "out", ""):
            payload["artifacts"] = write_gate_failure(exc, Path(args.out))
        _print(payload)
        return exc.exit_code
    except SkillError as exc:
        _print({"status": "ERROR", **exc.to_dict()})
        return exc.exit_code
    except FileNotFoundError as exc:
        _print({"status": "ERROR", "error": "FileNotFoundError", "message": str(exc),
                "exit_code": EXIT_INPUT})
        return EXIT_INPUT
    except Exception as exc:                      # noqa: BLE001 - 兜底，绝不静默
        _print({"status": "ERROR", "error": type(exc).__name__, "message": str(exc),
                "hint": "这是未预期的错误，请把完整输出反馈给技能维护者。"})
        if getattr(args, "verbose", False):
            raise
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
