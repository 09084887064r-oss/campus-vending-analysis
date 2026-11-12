"""检查仓库里存的使用说明.txt 是 CRLF 还是 LF，并按需修正 .gitattributes。"""

import subprocess
from pathlib import Path

repo = Path(r"D:\DeepSeek\campus-vending-analysis")
blob = subprocess.run(["git", "cat-file", "-p", "HEAD:使用说明.txt"], cwd=repo,
                      capture_output=True).stdout
print("仓库内 blob：CRLF =", blob.count(b"\r\n"), "| 裸 LF =", blob.count(b"\n") - blob.count(b"\r\n"),
      "| 字节 =", len(blob))
work = (repo / "使用说明.txt").read_bytes()
print("工作区文件：CRLF =", work.count(b"\r\n"), "| 裸 LF =", work.count(b"\n") - work.count(b"\r\n"),
      "| 字节 =", len(work))
