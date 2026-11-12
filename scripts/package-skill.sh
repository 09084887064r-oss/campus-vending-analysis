#!/usr/bin/env bash
# 打包 skills/<skill>/ 本身 + 仓库根的 INSTALL.md：ZIP 顶层是技能文件夹加一份说明，
# 技能文件夹本身仍是单层（收到 zip 的人解压即见安装说明，不用回仓库找）。
#
# 打包后端二选一，产出契约完全相同（顶层单目录 + 固定排除项 + 打包后自检）：
#   1) zip 命令存在 -> 用 zip
#   2) 否则用 python 的 zipfile（技能本身依赖 Python，一定有）
# Git Bash on Windows 默认没有 zip 与 unzip，会走第 2 条后端。
set -euo pipefail

NAME="campus-vending-analysis"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SKILL_DIR="$REPO_ROOT/skills/$NAME"
OUT="$REPO_ROOT/dist/$NAME.zip"
INSTALL_DOC="$REPO_ROOT/INSTALL.md"
GUIDE_DOC="$REPO_ROOT/使用说明.txt"

# Windows 上 python3 常指向 Microsoft Store 占位程序（存在但一跑就退出 1），
# 因此逐个试跑 `-c 'import sys'`，只接受真正能执行的解释器。
PY=()
pick_python() {
  local candidate
  for candidate in python3 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    if "$candidate" -c 'import sys' >/dev/null 2>&1; then PY=("$candidate"); return 0; fi
  done
  if command -v py >/dev/null 2>&1 && py -3 -c 'import sys' >/dev/null 2>&1; then
    PY=(py -3)
    return 0
  fi
  return 1
}
pick_python || true

die() { echo "FAIL: $*" >&2; exit "${2:-1}"; }

[ -f "$SKILL_DIR/SKILL.md" ] || die "缺少 SKILL.md"
name_in="$(grep -m1 '^name:' "$SKILL_DIR/SKILL.md" | sed 's/^name:[[:space:]]*//' | tr -d '\r')"
[ "$name_in" = "$NAME" ] || die "frontmatter name($name_in) 与目录名($NAME) 不一致"
for d in assets references scripts data; do
  [ -d "$SKILL_DIR/$d" ] || die "缺少 $d/ 目录"
done
[ -f "$SKILL_DIR/assets/template.xlsx" ] || echo "WARN: 缺 assets/template.xlsx，报表将回退内置样式" >&2
[ -f "$INSTALL_DOC" ] || echo "WARN: 缺 INSTALL.md，ZIP 里将没有安装说明" >&2
[ -f "$GUIDE_DOC" ] || echo "WARN: 缺 使用说明.txt，ZIP 里将没有大白话版说明" >&2

rm -f "$OUT"
mkdir -p "$REPO_ROOT/dist"

if command -v zip >/dev/null 2>&1; then
  BACKEND="zip"
  ( cd "$REPO_ROOT/skills" && zip -r -q "$OUT" "$NAME" \
      -x "*/__pycache__/*" "*.pyc" "*/.DS_Store" "*.bak" "*.bak[0-9]*" \
         "*/data/raw/*" "*/data/processed/*" "*/data/reports/*" )
  if [ -f "$INSTALL_DOC" ]; then
    ( cd "$REPO_ROOT" && zip -q -j "$OUT" INSTALL.md )
  fi
  if [ -f "$GUIDE_DOC" ]; then
    ( cd "$REPO_ROOT" && zip -q -s utf-8 -j "$OUT" "使用说明.txt" )
  fi
else
  BACKEND="python-zipfile"
  [ "${#PY[@]}" -gt 0 ] || die "系统既没有 zip 也没有可用 python，无法打包"
  "${PY[@]}" - "$REPO_ROOT" "$NAME" "$OUT" <<'PYEOF'
import sys, zipfile
from pathlib import Path

repo_root, name, out = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
skills_dir = repo_root / "skills"
skip_dirs = {"__pycache__"}
skip_suffixes = {".pyc", ".bak", ".bak2", ".bak3"}
out.parent.mkdir(parents=True, exist_ok=True)
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
    for path in sorted((skills_dir / name).rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(skills_dir).as_posix()
        parts = rel.split("/")
        if any(part in skip_dirs for part in parts):
            continue
        if path.name == ".DS_Store" or path.suffix in skip_suffixes:
            continue
        if len(parts) >= 4 and parts[1] == "data" and parts[2] in {"raw", "processed", "reports"}:
            continue
        archive.write(path, rel)
    for doc_name in ("INSTALL.md", "使用说明.txt"):
        doc = repo_root / doc_name
        if doc.is_file():
            archive.write(doc, doc_name)
PYEOF
fi

selfcheck() {
  "${PY[@]}" - "$OUT" "$NAME" <<'PYEOF'
import sys, zipfile
from pathlib import Path

out, name = Path(sys.argv[1]), sys.argv[2]
with zipfile.ZipFile(out) as archive:
    names = [n for n in archive.namelist() if not n.endswith("/")]
tops = {n.split("/")[0] for n in names}
problems = []
allowed = {name, "INSTALL.md", "使用说明.txt"}
if not tops.issubset(allowed):
    problems.append(f"ZIP 顶层有多余条目：{sorted(tops - allowed)}")
if name not in tops:
    problems.append("ZIP 里没有技能目录")
for doc_name in ("INSTALL.md", "使用说明.txt"):
    if doc_name not in names:
        problems.append(f"ZIP 顶层缺少 {doc_name}（说明文档）")
required = ["SKILL.md", "assets/template.xlsx", "scripts/run.py",
            "references/data-contract.md", "data/README.md"]
problems += [f"缺少 {item}" for item in required if f"{name}/{item}" not in names]
problems += [f"含缓存文件 {n}" for n in names if "__pycache__" in n or n.endswith(".pyc")]
problems += [f"含运行产物 {n}" for n in names
             if any(n.startswith(f"{name}/data/{layer}/") for layer in ("raw", "processed", "reports"))]
problems += [f"含反斜杠路径 {n}" for n in names if "\\" in n]
if problems:
    print("FAIL: " + "；".join(problems), file=sys.stderr)
    sys.exit(3)
print(f"OK {out.name}  {out.stat().st_size / 1024:.1f} KB  {len(names)} 个文件  顶层={sorted(tops)}")
PYEOF
}

if ! selfcheck; then
  rm -f "$OUT"
  exit 3
fi

echo "backend=$BACKEND"
echo "path=$OUT"
