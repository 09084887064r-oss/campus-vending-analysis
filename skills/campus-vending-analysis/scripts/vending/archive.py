"""归档与环比基准。

数据根结构：
    <root>/raw/<key>/        原始文件
    <root>/processed/<key>/  facts.csv + sku_index.csv + meta.json
    <root>/reports/<key>/    xlsx / html / docx / uplift_points.csv
    <root>/manifest.jsonl    一期一行的运行台账

周期键：周 `2026-W45`（ISO 周），月 `2026-11`（自然月）。两套键空间互不覆盖。
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

SCHEMA = 2
FACTS_FIELDS = ["period_label", "ts", "machine", "sku_id", "product_raw", "spec",
                "category", "qty", "amount", "user", "order", "dorm", "row_no"]
MANIFEST = "manifest.jsonl"


# ---------------------------------------------------------------- 周期键

def iso_week_key(value: date) -> str:
    year, week, _ = value.isocalendar()
    return f"{year}-W{week:02d}"


def month_key(value: date) -> str:
    return f"{value.year}-{value.month:02d}"


def week_bounds(key: str) -> tuple[date, date]:
    year, week = key.split("-W")
    monday = date.fromisocalendar(int(year), int(week), 1)
    return monday, monday + timedelta(days=6)


def month_bounds(key: str) -> tuple[date, date]:
    year, month = (int(part) for part in key.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return start, end


def prev_key(kind: str, key: str) -> str:
    if kind == "weekly":
        monday, _ = week_bounds(key)
        return iso_week_key(monday - timedelta(days=7))
    start, _ = month_bounds(key)
    return month_key(start - timedelta(days=1))


def bounds(kind: str, key: str) -> tuple[date, date]:
    return week_bounds(key) if kind == "weekly" else month_bounds(key)


def period_key(kind: str, start: date) -> str:
    return iso_week_key(start) if kind == "weekly" else month_key(start)


def prev_complete_key(kind: str, key: str, reference: date) -> dict[str, Any]:
    """返回上期键与它是否已经结束（未结束的基准不可用于对比）。"""
    candidate = prev_key(kind, key)
    _, end = bounds(kind, candidate)
    return {"key": candidate, "complete": end < reference}


# ---------------------------------------------------------------- 设置

def settings_dir() -> Path:
    import os
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / ".workbuddy" / "campus-vending"


def settings_file() -> Path:
    return settings_dir() / "settings.json"


def default_root() -> Path:
    import os
    home = Path(os.environ.get("USERPROFILE") or Path.home())
    return home / "Documents" / "售货机数据归档"


def load_settings() -> dict[str, Any]:
    path = settings_file()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(data: dict[str, Any]) -> Path:
    path = settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def resolve_root(explicit: str | None) -> tuple[Path, str]:
    """确定数据根：命令行 > 设置 > 默认。返回 (根目录, 来源)。"""
    if explicit:
        return Path(explicit).expanduser(), "命令行 --data-root"
    stored = load_settings().get("root")
    if stored:
        return Path(stored).expanduser(), "settings.json"
    return default_root(), "默认值（首次运行）"


def ensure_root(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".archive.json"
    if not marker.exists():
        marker.write_text(json.dumps({
            "schema": SCHEMA,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "layout": {"raw": "raw/<key>", "processed": "processed/<key>",
                       "reports": "reports/<key>"},
            "note": "本目录由 campus-vending-analysis 技能自动创建；每期一个子目录，互不覆盖。",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
    return root


def period_dir(root: Path, layer: str, key: str) -> Path:
    return root / layer / key


# ---------------------------------------------------------------- 指纹

def fingerprint(cfg: Any, roster_digest: str) -> str:
    """口径指纹：档位/单品规则/人数表/列映射任一变，环比就不可比。"""
    payload = {
        "bands": [(getattr(b, "label", ""), getattr(b, "low", None), getattr(b, "high", None))
                  for b in cfg.price_bands],
        "sku_rule": "v2",
        "roster": roster_digest,
        "columns": sorted(
            f"{name}={getattr(cfg.columns, name, None)}"
            for name in ("time", "machine", "product", "qty", "amount", "user", "order")
        ),
        "dorm_mode": getattr(cfg.dorm, "mode", ""),
        "unmapped": cfg.unmapped_products,
        "negative": cfg.negative_policy,
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- facts

def write_facts(path: Path, records: Iterable[Any]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FACTS_FIELDS)
        writer.writeheader()
        for record in records:
            writer.writerow({
                "period_label": record.period,
                "ts": record.ts.isoformat(sep=" ") if record.ts else "",
                "machine": record.machine or "",
                "sku_id": record.sku_id,
                "product_raw": record.product_raw,
                "spec": record.spec,
                "category": record.category,
                "qty": "" if record.qty is None else record.qty,
                "amount": "" if record.amount is None else record.amount,
                "user": record.user or "",
                "order": record.order or "",
                "dorm": record.dorm or "",
                "row_no": record.row_no,
            })
            count += 1
    return count


def read_facts(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def facts_rows_to_records(rows: Iterable[dict[str, Any]]) -> list[Any]:
    """把归档 facts 还原成 Record（环比只需要这几个字段）。"""
    from .core import Record

    out: list[Record] = []
    for row in rows:
        ts = None
        if row.get("ts"):
            try:
                ts = datetime.fromisoformat(row["ts"])
            except ValueError:
                ts = None
        out.append(Record(
            row_no=int(row.get("row_no") or 0),
            ts=ts,
            machine_raw=row.get("machine", ""),
            machine=row.get("machine", ""),
            product_raw=row.get("product_raw", ""),
            sku_id=row.get("sku_id", ""),
            sku_name=row.get("sku_id", ""),
            spec=row.get("spec", ""),
            category=row.get("category", ""),
            qty=float(row["qty"]) if row.get("qty") else None,
            amount=float(row["amount"]) if row.get("amount") else None,
            user=row.get("user") or None,
            order=row.get("order") or None,
            dorm=row.get("dorm") or None,
            period=row.get("period_label", ""),
        ))
    return out


# ---------------------------------------------------------------- meta

@dataclass
class Meta:
    period_kind: str
    period_key: str
    periods: list[dict[str, Any]]
    fingerprint: str
    counts: dict[str, Any]
    sources: list[dict[str, str]] = field(default_factory=list)
    schema: int = SCHEMA
    generated_at: str = ""
    baseline_key: str = ""
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "period_kind": self.period_kind,
            "period_key": self.period_key,
            "periods": self.periods,
            "fingerprint": self.fingerprint,
            "counts": self.counts,
            "sources": self.sources,
            "generated_at": self.generated_at or datetime.now().isoformat(timespec="seconds"),
            "baseline_key": self.baseline_key,
            "notes": self.notes,
        }


# ---------------------------------------------------------------- 写入

#: 已经是备份的文件名（报告.xlsx.bak / 报告.xlsx.bak2 …），不再参与二次备份
_BACKUP_NAME = re.compile(r"\.bak\d*$")


def backup_existing(path: Path) -> list[str]:
    """同名文件先原样另存为 .bak（已存在则 .bak2/.bak3…），返回改名清单。

    目录模式下只备份“本次会被覆盖的正式文件”：已经是备份的文件（``*.bak`` /
    ``*.bakN``）跳过。否则每重跑一次就会把上一次的备份再备份一次，
    文件名滚成 ``报告.xlsx.bak.bak.bak`` 这种没法读的样子。
    """
    moved: list[str] = []
    if not path.exists():
        return moved
    targets = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
    for target in targets:
        if _BACKUP_NAME.search(target.name):
            continue
        backup = target.with_suffix(target.suffix + ".bak")
        index = 2
        while backup.exists():
            backup = target.with_suffix(target.suffix + f".bak{index}")
            index += 1
        shutil.move(str(target), str(backup))
        moved.append(f"{target.name} → {backup.name}")
    return moved


def write_period(root: Path, kind: str, key: str, *, meta: Meta,
                 records: Iterable[Any], report_paths: dict[str, Path],
                 uplift_rows: list[list[Any]], force: bool = False) -> dict[str, Any]:
    """把一期结果写入归档；已存在则备份为 .bak（由调用方先确认）。"""
    ensure_root(root)
    processed = period_dir(root, "processed", key)
    reports = period_dir(root, "reports", key)
    backup: list[str] = []
    if processed.exists():
        backup += backup_existing(processed)
    if reports.exists():
        backup += backup_existing(reports)
    processed.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)

    fact_count = write_facts(processed / "facts.csv", records)
    (processed / "meta.json").write_text(
        json.dumps(meta.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")

    copied: dict[str, str] = {}
    for fmt, source in report_paths.items():
        if Path(source).exists():
            target = reports / Path(source).name
            shutil.copy2(source, target)
            copied[fmt] = str(target)
    with (reports / "uplift_points.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["序号", "所属模块", "问题描述", "数据证据", "影响范围",
                         "建议动作", "预期效果", "优先级", "原因假设", "验证方式", "置信度"])
        writer.writerows(uplift_rows)

    entry = {"at": datetime.now().isoformat(timespec="seconds"), "kind": kind,
             "key": key, "facts": fact_count, "reports": copied,
             "baseline": meta.baseline_key, "backups": backup}
    with (root / MANIFEST).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


# ---------------------------------------------------------------- 基准

@dataclass
class Baseline:
    key: str
    exists: bool = False
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)
    records: list[Any] = field(default_factory=list)
    compatible: bool = True

    @property
    def ok(self) -> bool:
        return self.exists and bool(self.records)


def load_baseline(root: Path, kind: str, key: str, current_fingerprint: str) -> Baseline:
    """上期归档不存在 / schema 不兼容 / 口径指纹变了，都要显式说明原因。"""
    candidate = prev_key(kind, key)
    processed = period_dir(root, "processed", candidate)
    meta_path = processed / "meta.json"
    facts_path = processed / "facts.csv"
    if not meta_path.exists() or not facts_path.exists():
        return Baseline(key=candidate, reason=f"归档中没有上期（{candidate}）")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Baseline(key=candidate, reason=f"上期 meta.json 无法读取（{exc.__class__.__name__}）")
    if int(meta.get("schema", 0)) != SCHEMA:
        return Baseline(key=candidate, reason=f"上期 schema={meta.get('schema')} 与本期 {SCHEMA} 不兼容")
    try:
        rows = read_facts(facts_path)
    except (OSError, ValueError) as exc:
        return Baseline(key=candidate, reason=f"上期 facts.csv 无法读取（{exc.__class__.__name__}）")
    same = meta.get("fingerprint") == current_fingerprint
    reason = "" if same else "上期口径指纹与本期不同（价格带/单品规则/人数表/列映射有变更）"
    return Baseline(key=candidate, exists=True, reason=reason, meta=meta,
                    records=facts_rows_to_records(rows), compatible=same)


def roster_digest(roster: dict[str, Any]) -> str:
    blob = json.dumps({k: sorted((str(i), str(v)) for i, v in entry.items())
                       for k, entry in roster.items()}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
