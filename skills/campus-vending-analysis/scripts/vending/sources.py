"""数据接入层：把 CSV / TSV / Excel / JSON 统一读成 RawTable。

设计要点
- 只负责"读成表"，不做任何业务清洗；编码、分隔符、表头偏移的判定结果写入 warnings，
  由清洗日志（clean.py）登记，绝不静默处理。
- 表头偏移（导出文件常有标题行/空行）会被识别并登记，避免把标题当列名。
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .core import FIELD_ALIASES, InputError, normalize_text

SUPPORTED_SUFFIXES = {".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".json", ".jsonl"}
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")
_DELIMITERS = (",", "\t", ";", "|")


@dataclass
class RawTable:
    path: str
    kind: str
    headers: list[str]
    rows: list[dict[str, Any]]
    total_rows: int
    sheet: str = ""
    encoding: str = ""
    delimiter: str = ""
    header_row: int = 1
    warnings: list[str] = field(default_factory=list)

    def column_preview(self, limit: int = 8) -> list[dict[str, Any]]:
        """每个列名的前若干非空示例值，用于在 preflight 里向用户展示映射依据。"""
        preview: list[dict[str, Any]] = []
        for header in self.headers:
            samples: list[str] = []
            for row in self.rows:
                value = row.get(header)
                if value is None:
                    continue
                text = normalize_text(value)
                if text and text not in samples:
                    samples.append(text)
                if len(samples) >= limit:
                    break
            preview.append({"name": header, "samples": samples})
        return preview


def detect_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in {".csv", ".txt"}:
        return "csv"
    if suffix == ".tsv":
        return "tsv"
    if suffix in {".xlsx", ".xlsm", ".xls"}:
        return "excel"
    if suffix in {".json", ".jsonl"}:
        return "json"
    raise InputError(f"不支持的文件类型：{path.name}",
                     hint="支持 CSV / TSV / Excel(.xlsx/.xlsm) / JSON；.xls 请另存为 .xlsx。")


def list_sheets(path: Path) -> list[str]:
    path = Path(path)
    if detect_kind(path) != "excel":
        return []
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        return [sheet.title for sheet in workbook.worksheets]
    finally:
        workbook.close()


def _decode(raw_bytes: bytes, encoding: str) -> tuple[str, str]:
    candidates = (encoding,) if encoding and encoding != "auto" else _ENCODINGS
    for candidate in candidates:
        try:
            return raw_bytes.decode(candidate), candidate
        except (UnicodeDecodeError, LookupError):
            continue
    return raw_bytes.decode("utf-8", errors="replace"), "utf-8(replace)"


def _sniff_delimiter(sample: str) -> str:
    best, best_score = ",", 0
    for delimiter in _DELIMITERS:
        rows = list(csv.reader(io.StringIO(sample), delimiter=delimiter))
        rows = [r for r in rows if any(cell.strip() for cell in r)]
        if not rows:
            continue
        widths = [len(r) for r in rows[:20]]
        modal = max(set(widths), key=widths.count)
        if modal <= 1:
            continue
        score = modal * widths.count(modal)
        if score > best_score:
            best, best_score = delimiter, score
    return best


def _known_header_score(cells: Iterable[Any]) -> int:
    known: set[str] = set()
    for aliases in FIELD_ALIASES.values():
        known.update(normalize_text(a).lower() for a in aliases)
    score = 0
    for cell in cells:
        text = normalize_text(cell).lower()
        if text and text in known:
            score += 1
    return score


def _guess_header_index(rows: list[list[Any]], max_scan: int = 10) -> int:
    """返回表头所在行下标（0 基）。

    判定顺序：出现 >=2 个已知别名的第一行 > 非空单元格最多的第一行 > 第 0 行。
    """
    if not rows:
        return 0
    for index, row in enumerate(rows[:max_scan]):
        if _known_header_score(row) >= 2:
            return index
    for index, row in enumerate(rows[:max_scan]):
        if sum(1 for cell in row if normalize_text(cell)) >= 2:
            return index
    return 0


def _rows_to_dicts(headers: list[str], data: list[list[Any]]) -> list[dict[str, Any]]:
    width = len(headers)
    out: list[dict[str, Any]] = []
    for raw_row in data:
        cells = list(raw_row) + [None] * (width - len(raw_row))
        if not any(normalize_text(cell) for cell in cells[:width]):
            continue
        out.append({headers[i]: cells[i] for i in range(width)})
    return out


def _unique_headers(raw_headers: Iterable[Any]) -> list[str]:
    headers: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(raw_headers):
        text = normalize_text(cell) or f"列{index + 1}"
        if text in seen:
            seen[text] += 1
            text = f"{text}_{seen[text]}"
        else:
            seen[text] = 1
        headers.append(text)
    return headers


def _read_delimited(path: Path, encoding: str, delimiter: str | None) -> RawTable:
    raw_bytes = path.read_bytes()
    text, used_encoding = _decode(raw_bytes, encoding)
    if delimiter in (None, "", "auto"):
        delimiter = "\t" if path.suffix.lower() == ".tsv" else _sniff_delimiter(text[:65536])
    parsed = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    parsed = [row for row in parsed if any(normalize_text(cell) for cell in row)]
    if not parsed:
        raise InputError(f"文件为空或无法解析出任何数据行：{path.name}")
    header_index = _guess_header_index(parsed)
    headers = _unique_headers(parsed[header_index])
    rows = _rows_to_dicts(headers, parsed[header_index + 1:])
    warnings: list[str] = []
    if header_index > 0:
        warnings.append(f"表头位于第 {header_index + 1} 行，其上 {header_index} 行为标题/空行，已跳过并登记。")
    return RawTable(path=str(path), kind="csv", headers=headers, rows=rows,
                    total_rows=len(rows), encoding=used_encoding, delimiter=delimiter,
                    header_row=header_index + 1, warnings=warnings)


def _read_excel(path: Path, sheet: str) -> RawTable:
    from openpyxl import load_workbook

    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet:
            if sheet not in workbook.sheetnames:
                raise InputError(f"工作表不存在：{sheet}",
                                 hint=f"可选工作表：{', '.join(workbook.sheetnames)}",
                                 details={"sheets": list(workbook.sheetnames)})
            target = workbook[sheet]
        else:
            target = None
            for candidate in workbook.worksheets:
                target = candidate
                break
            if target is None:
                raise InputError(f"工作簿没有任何工作表：{path.name}")
        grid = [list(row) for row in target.iter_rows(values_only=True)]
    finally:
        workbook.close()

    grid = [row for row in grid if any(normalize_text(cell) for cell in row)]
    if not grid:
        raise InputError(f"工作表 {target.title} 为空：{path.name}")
    header_index = _guess_header_index(grid)
    headers = _unique_headers(grid[header_index])
    rows = _rows_to_dicts(headers, grid[header_index + 1:])
    warnings: list[str] = []
    if header_index > 0:
        warnings.append(f"表头位于第 {header_index + 1} 行，其上 {header_index} 行为标题/空行，已跳过并登记。")
    return RawTable(path=str(path), kind="excel", headers=headers, rows=rows,
                    total_rows=len(rows), sheet=target.title, header_row=header_index + 1,
                    warnings=warnings)


def _read_json(path: Path) -> RawTable:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    warnings: list[str] = []
    if path.suffix.lower() == ".jsonl":
        records = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise InputError(f"JSON 解析失败：{exc}") from exc
        if isinstance(payload, list):
            records = payload
        elif isinstance(payload, dict):
            records = []
            for key in ("data", "records", "rows", "items", "list", "detail"):
                value = payload.get(key)
                if isinstance(value, list):
                    records = value
                    warnings.append(f"JSON 顶层为对象，已取 '{key}' 数组作为明细。")
                    break
            else:
                raise InputError("JSON 结构无法识别：既不是数组，也不含 data/records/rows/items 数组。",
                                 hint="把明细导出为对象数组，或改存为 CSV。")
        else:
            raise InputError("JSON 结构无法识别：顶层必须是数组或对象。")

    rows: list[dict[str, Any]] = []
    headers: list[str] = []
    for item in records:
        if not isinstance(item, dict):
            raise InputError("JSON 数组元素必须是对象（每行一条记录）。")
        for key in item:
            if key not in headers:
                headers.append(str(key))
        rows.append({str(k): v for k, v in item.items()})
    return RawTable(path=str(path), kind="json", headers=headers, rows=rows,
                    total_rows=len(rows), encoding="utf-8", warnings=warnings)


def read_table(path: str | Path, *, sheet: str = "", encoding: str = "auto",
               delimiter: str | None = None) -> RawTable:
    """读取任意受支持格式为 RawTable。"""
    path = Path(path)
    if not path.exists():
        raise InputError(f"文件不存在：{path}")
    if not path.is_file():
        raise InputError(f"不是文件：{path}")
    if path.stat().st_size == 0:
        raise InputError(f"文件为空：{path.name}")
    kind = detect_kind(path)
    if kind == "excel":
        if path.suffix.lower() == ".xls":
            raise InputError(".xls 无法直接读取，请另存为 .xlsx 后重试。")
        return _read_excel(path, sheet)
    if kind == "json":
        return _read_json(path)
    return _read_delimited(path, encoding, delimiter)


def read_machine_map(path: str | Path) -> dict[str, str]:
    """读取 机器编号 -> 寝室楼栋 映射（JSON 对象或两列 CSV/Excel）。"""
    path = Path(path)
    if not path.exists():
        raise InputError(f"机器-楼栋映射文件不存在：{path}")
    if path.suffix.lower() == ".json":
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(payload, dict):
            raise InputError("机器-楼栋映射 JSON 必须是对象：{\"A-01\": \"1号楼\"}")
        return {normalize_text(k): normalize_text(v) for k, v in payload.items()}
    table = read_table(path)
    if len(table.headers) < 2:
        raise InputError("机器-楼栋映射需要至少两列：机器编号、楼栋")
    machine_key, dorm_key = table.headers[0], table.headers[1]
    mapping: dict[str, str] = {}
    for row in table.rows:
        machine = normalize_text(row.get(machine_key))
        dorm = normalize_text(row.get(dorm_key))
        if machine and dorm:
            mapping[machine] = dorm
    return mapping


def snapshot_rows(table: RawTable, limit: int) -> tuple[list[str], list[list[Any]], bool]:
    """返回原始数据快照（表头 + 前 limit 行 + 是否被截断）。"""
    headers = list(table.headers)
    rows = [[row.get(header) for header in headers] for row in table.rows[:limit]]
    return headers, rows, table.total_rows > limit
