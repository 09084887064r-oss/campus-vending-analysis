"""打包示例报告（给非技术用户先看效果），并抽 13_销售提升点清单 前几行做证据。

只收本次正式产出，跳过历史备份（*.bak / *.bakN）。
"""

import re
import shutil
import zipfile
from pathlib import Path

import openpyxl

REPO = Path(r"D:\DeepSeek\campus-vending-analysis")
SRC = REPO / "tests" / "archive" / "reports"
OUT_DIR = REPO / "dist" / "示例报告_模拟数据"
ZIP = REPO / "dist" / "示例报告_模拟数据.zip"

INTRO = """这些报告是用「模拟数据」跑出来的示例，不是真实经营数据。

数据来源：按成品结果表反推生成的两期原始明细（2026-W44、2026-W45），
         销量/订单/人数/单品数等边际与成品表一致，价格带金额有 1-3.5% 的构造残差。

用途：
  1. 装技能之前，先看看它到底会给你什么；
  2. 装好以后，对照 13_销售提升点清单 学会怎么读；
  3. 两个周期放在一起，能看出「环比」是怎么呈现的。

每个周期四份文件：
  ..._销售提升分析.xlsx          十几个工作表，重点看 13 / 14
  ..._销售提升分析_交互报告.html   双击用浏览器打开，可点可筛选
  ..._销售提升分析.docx           用 Word 打开，可直接改字
  uplift_points.csv             提升点清单的 CSV，方便进 Excel 二次加工
"""

BACKUP = re.compile(r"\.bak\d*$")

if OUT_DIR.exists():
    shutil.rmtree(OUT_DIR)
OUT_DIR.mkdir(parents=True)
for period in ("2026-W44", "2026-W45"):
    dst_dir = OUT_DIR / period
    dst_dir.mkdir()
    for path in sorted((SRC / period).iterdir()):
        if path.is_file() and not BACKUP.search(path.name):
            shutil.copy2(path, dst_dir / path.name)
(OUT_DIR / "说明.txt").write_text(INTRO.replace("\n", "\r\n"), encoding="utf-8-sig")

with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted(OUT_DIR.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(OUT_DIR).as_posix())
print("zip:", ZIP.name, ZIP.stat().st_size, "字节")

wb = openpyxl.load_workbook(SRC / "2026-W45" / "校园自助售货机_销售提升分析.xlsx")
print("工作表:", len(wb.sheetnames))
ws = wb["13_销售提升点清单"]
rows = list(ws.iter_rows(values_only=True))
print("提升点行数:", len(rows) - 2)
for row in rows[2:5]:
    print("-", " | ".join(str(cell)[:30] if cell is not None else "" for cell in row[:8]))
