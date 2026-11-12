"""依赖自检：缺库时给 WorkBuddy（和用户）一句能照做的话，而不是 Python traceback。

分工很关键：
- `detect()` 碰环境，只回答"缺哪个模块"；
- `format_missing()` 是纯函数，只负责把缺的东西拼成可执行的指令，
  这样回归测试不需要真的卸载 openpyxl 也能验证提示文本。
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any, Iterable

from .core import EXIT_INPUT

#: 必需依赖：(import 名, pip 包名, 用途)。缺任一即无法出 Excel 报表。
REQUIRED: tuple[tuple[str, str, str], ...] = (
    ("openpyxl", "openpyxl>=3.1", "读写 Excel（核心分析、Excel/HTML 导出必需）"),
)

#: 可选依赖：缺了只影响 Word 报告，Excel 与 HTML 照常产出。
OPTIONAL: tuple[tuple[str, str, str], ...] = (
    ("docx", "python-docx>=1.1", "导出 Word 报告"),
    ("matplotlib", "matplotlib>=3.7", "Word 报告里的图表 PNG"),
)

REQUIREMENTS_FILE = "scripts/requirements.txt"


def _missing(specs: Iterable[tuple[str, str, str]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for module, package, purpose in specs:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if not found:
            result[module] = {"package": package, "purpose": purpose}
    return result


def detect() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    """返回（缺失的必需依赖, 缺失的可选依赖）。"""
    return _missing(REQUIRED), _missing(OPTIONAL)


def install_command(missing: dict[str, dict[str, str]]) -> str:
    packages = " ".join(item["package"].split(">=")[0] for item in missing.values())
    return f"{sys.executable} -m pip install {packages}"


def format_missing(missing: dict[str, dict[str, str]], *,
                   requirements: str = REQUIREMENTS_FILE) -> dict[str, Any]:
    """拼成给 AI/用户看的载荷；纯函数，便于测试。"""
    packages = "、".join(item["package"] for item in missing.values())
    command = install_command(missing)
    return {
        "status": "MISSING_DEPENDENCIES",
        "missing": [{"module": module, **item} for module, item in missing.items()],
        "reasons": [f"{item['package']}：{item['purpose']}" for item in missing.values()],
        "message": f"运行本技能缺少依赖：{packages}。装好之后重跑即可，不需要重新安装技能。",
        "install_command": command,
        "install_command_all": f"{sys.executable} -m pip install -r {requirements}",
        "agent_instruction": (
            "请先执行 install_command 安装依赖，然后重新运行同一条命令；"
            "不要再拆解报错、也不要跳过依赖继续分析。"
        ),
        "exit_code": EXIT_INPUT,
    }


def guard() -> dict[str, Any] | None:
    """启动自检：缺必需依赖时返回载荷（调用方直接打印并退出），否则返回 None。"""
    required, _optional = detect()
    if not required:
        return None
    return format_missing(required)


def optional_warning() -> str:
    """可选依赖缺失时的一句提醒（不阻断）。"""
    _required, optional = detect()
    if not optional:
        return ""
    packages = "、".join(item["package"] for item in optional.values())
    return f"提示：缺少可选依赖 {packages}，本次只能出 Excel 与 HTML，Word 报告会跳过。"


def summary() -> dict[str, Any]:
    required, optional = detect()
    return {
        "python": sys.version.split()[0],
        "required_ok": not required,
        "missing_required": sorted(required),
        "missing_optional": sorted(optional),
    }
