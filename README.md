# campus-vending-analysis

一个给 WorkBuddy 用的技能：**读校园自助售货机的销售明细，找出销售提升点**，输出多工作表 Excel、单文件交互网页报告和 Word 报告。

它不做"把数据摆出来"的报告。每张对比表后面都要回答三个问题：哪里低于上期／低于其他分组、最可能的原因是什么（写成"假设"）、针对这个原因具体做什么（哪台机器、哪个时段、哪个品类、哪栋楼，看哪个指标验证）。最终汇总成一份**销售提升点清单**，每条都带数据证据、影响范围、责任对象、预期效果和验证方式。

三个输入：销售明细（CSV / Excel / JSON / TSV）、商品分类表、寝室楼栋新老生人数表。
六维分析：核心数据项、时点、价格带、品类、机组、用户习惯。
周环比 / 月环比：按期归档，自动取上期对比；没上期就老实写 N/A，不自比、不估算。

## 安装

看 **[INSTALL.md](INSTALL.md)**（写操作步骤，不用懂技术）。

## 打包成 ZIP

```bash
bash scripts/package-skill.sh          # 产出 dist/campus-vending-analysis.zip
```

ZIP 顶层就是 `campus-vending-analysis/` 本身，不含仓库根目录、不多套一层 `skills/`；
`assets/`、`references/`、`scripts/` 全部包含；自动排除 `__pycache__`、`.bak`、`data/raw` 等运行产物。

打包后端二选一，产出契约相同（都是"顶层单目录 + 固定排除项 + 打包后自检"）：

1. 有 `zip` 命令 → 用 `zip`；
2. 没有 `zip`（Git Bash for Windows 默认就没有）→ 用 `python` 的 `zipfile`。

打包脚本会自检：顶层是否单一技能目录、关键文件是否齐全、是否混入 `__pycache__`/`.bak`/`data/raw`、
路径是否用了反斜杠；任一条不满足就删掉产物并以退出码 3 失败，不交付半成品。

Windows 实测（Git Bash 5.3，无 `zip`）：`bash scripts/package-skill.sh` → `backend=python-zipfile`，
产出 34 个文件、顶层 `campus-vending-analysis`。

## 仓库结构

```
campus-vending-analysis/
├── README.md
├── LICENSE
├── INSTALL.md
├── .gitignore
├── scripts/
│   └── package-skill.sh
└── skills/
    └── campus-vending-analysis/        # 目录名必须与 SKILL.md 的 name 完全一致
        ├── SKILL.md
        ├── LICENSE.txt
        ├── assets/
        │   ├── template.xlsx           # 成品表样式模板（唯一格式权威）
        │   └── template/               # HTML 报告模板（html/css/js）
        ├── references/
        │   ├── data-contract.md
        │   ├── diagnosis-playbook.md
        │   ├── archive-and-style.md
        │   └── validation-rules.md
        ├── scripts/
        │   ├── run.py
        │   ├── requirements.txt
        │   └── vending/                # 内核（19 个模块）
        └── data/                        # 归档结构示例（不放真实数据）
```

## 依赖

- 必需：Python 3.10+、`openpyxl`（MIT）
- 可选：`python-docx`（MIT，Word 报告）、`matplotlib`（PSF/BSD，Word 里的图表图片）

```bash
pip install -r skills/campus-vending-analysis/scripts/requirements.txt
```

## 自测

```bash
cd skills/campus-vending-analysis/scripts
python run.py selftest --verbose     # 77 项断言
python run.py demo --out demo_output # 两周示例，含环比与提升点
```

## License

MIT，见 [LICENSE](LICENSE)。
