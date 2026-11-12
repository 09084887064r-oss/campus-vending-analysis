#!/usr/bin/env python3
"""WorkBuddy Skill 入口：校园自助售货机销售分析。

常用
----
python scripts/run.py preflight --sales 明细.xlsx --categories 分类表.xlsx --dorm 楼栋人数.xlsx --out out
python scripts/run.py confirm   --config out/analysis_config.json --preflight out/preflight_report.json --answers answers.json
python scripts/run.py analyze   --config out/analysis_config.json --out out
python scripts/run.py demo      --out demo_output     # 合成数据跑通全流程
python scripts/run.py selftest  --verbose             # 契约与校验回归
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from vending.cli import main  # noqa: E402  (需要先补 sys.path)

if __name__ == "__main__":
    raise SystemExit(main())
