"""核心契约层：错误类型、配置模型、记录模型、结果表格、解析与格式化工具。

本模块是纯逻辑层（无 IO、无第三方依赖），所有下游模块只依赖这里的类型定义，
以保证"数据契约"只有一个真源（single source of truth）。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterable

SKILL_VERSION = "1.0.0"
CONFIG_SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 退出码
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_INPUT = 2          # 输入缺失 / 无法解析 / 不满足契约
EXIT_NEEDS_CONFIRM = 3  # 需要向用户提问并确认后才能继续
EXIT_UNCONFIRMED = 4    # 配置未确认，拒绝执行分析
EXIT_GATE_FAILED = 5    # 模块校验未通过，门禁拦截
EXIT_EXPORT = 6         # 导出失败

NA = "N/A"


class SkillError(Exception):
    """所有可预期错误的基类，携带退出码、修复指引与结构化细节。"""

    exit_code = EXIT_USAGE

    def __init__(self, message: str, *, hint: str | None = None,
                 details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": type(self).__name__, "message": self.message,
                "hint": self.hint, "exit_code": self.exit_code,
                "details": self.details}


class InputError(SkillError):
    exit_code = EXIT_INPUT


class NeedsConfirmation(SkillError):
    exit_code = EXIT_NEEDS_CONFIRM


class UnconfirmedConfig(SkillError):
    exit_code = EXIT_UNCONFIRMED


class GateFailed(SkillError):
    exit_code = EXIT_GATE_FAILED


class ExportError(SkillError):
    exit_code = EXIT_EXPORT


# ---------------------------------------------------------------- 契约常量

#: 销售明细的六个必需字段（逻辑名 -> 中文名）。别名映射见 contract 文件。
REQUIRED_SALES_FIELDS: dict[str, str] = {
    "time": "交易时间",
    "machine": "机器编号",
    "product": "商品名称",
    "qty": "销售数量",
    "amount": "销售金额",
    "user": "用户标识",
}

#: 可选字段（缺失时对应指标输出 N/A，绝不用行数或估算值替代）。
OPTIONAL_SALES_FIELDS: dict[str, str] = {
    "order": "订单标识",
    "dorm": "寝室楼栋",
}

FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "time": ("交易时间", "交易日期", "下单时间", "支付时间", "销售时间", "时间",
             "transaction_time", "time", "datetime", "date", "created_at", "pay_time"),
    "machine": ("机器编号", "设备编号", "机组编号", "设备号", "机器号", "点位编号",
                "machine_id", "machine", "device_id", "device", "vending_machine"),
    "product": ("商品名称", "商品", "产品名称", "品名", "货品名称",
                "product_name", "product", "sku_name", "item", "goods"),
    "qty": ("销售数量", "数量", "购买数量", "件数",
            "qty", "quantity", "sale_qty", "count", "num"),
    "amount": ("销售金额", "金额", "实付金额", "支付金额", "销售额", "应收金额",
               "amount", "sale_amount", "revenue", "total_amount", "pay_amount", "price"),
    "user": ("用户标识", "用户ID", "用户id", "用户编号", "会员标识", "买家标识", "学号",
             "user_id", "userid", "user", "member_id", "buyer_id", "customer_id"),
    "order": ("订单标识", "订单号", "订单ID", "订单id", "交易号", "流水号",
              "order_id", "orderid", "order_no", "order", "trade_no", "trade_id"),
    "dorm": ("寝室楼栋", "楼栋", "宿舍楼", "楼宇", "所属楼栋",
             "dorm", "building", "dormitory", "building_name"),
}

#: 商品分类表 / 楼栋人数表的字段别名。
CATEGORY_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "product": FIELD_ALIASES["product"],
    "category": ("品类", "分类", "商品分类", "类目", "类别", "一级分类",
                 "category", "class", "category_name", "type"),
}

DORM_ROSTER_ALIASES: dict[str, tuple[str, ...]] = {
    "dorm": FIELD_ALIASES["dorm"],
    "freshman": ("新生人数", "新生", "大一", "freshman", "freshmen", "new_students"),
    "senior": ("老生人数", "老生", "非新生", "senior", "seniors", "old_students"),
    "total": ("总人数", "人数", "在住人数", "total", "students", "headcount"),
}

# ---------------------------------------------------------------- 解析工具

_TRUE_TOKENS = {"1", "true", "yes", "y", "是", "有"}
_FALSE_TOKENS = {"0", "false", "no", "n", "否", "无"}

_CURRENCY_JUNK = re.compile(r"[¥￥$€元人民币,，\s]")
_EXCEL_EPOCH = datetime(1899, 12, 30)

_DATETIME_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d",
    "%Y.%m.%d %H:%M:%S", "%Y.%m.%d %H:%M", "%Y.%m.%d",
    "%Y%m%d%H%M%S", "%Y%m%d", "%Y-%m-%dT%H:%M:%S",
    "%Y年%m月%d日 %H:%M:%S", "%Y年%m月%d日 %H:%M", "%Y年%m月%d日",
    "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %H:%M", "%d/%m/%Y %H:%M",
)


def normalize_text(value: Any) -> str:
    """NFKC 规范化：全角转半角、去首尾空白、压缩内部空白、统一小写。"""
    if value is None:
        return ""
    text = unicodedata.normalize("NFKC", str(value)).strip()
    text = re.sub(r"\s+", " ", text)
    return text


def parse_number(value: Any) -> float | None:
    """把常见的金额/数量写法解析为 float；无法解析返回 None（不猜、不填 0）。"""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
            return None
        return float(value)
    text = normalize_text(value)
    if not text:
        return None
    if text.lower() in {"na", "n/a", "null", "none", "-", "--", "无", "未知"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    cleaned = _CURRENCY_JUNK.sub("", text.strip("()"))
    cleaned = cleaned.replace("−", "-").replace("—", "-")
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def parse_datetime(value: Any) -> datetime | None:
    """解析多种时间写法；无法解析返回 None（由调用方登记为"时间无法解析"）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        # Excel 序列号：仅接受 2000-01-01 ~ 2100-01-01 之间的合理区间
        if 36526 <= float(value) <= 73050:
            return _EXCEL_EPOCH + timedelta(days=float(value))
        return None
    text = normalize_text(value)
    if not text:
        return None
    candidate = text.replace("Z", "").replace("T", " ")
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        pass
    for fmt in _DATETIME_FORMATS:
        try:
            return datetime.strptime(candidate, fmt)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------- 格式化

def fmt_money(value: Any, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return NA
    try:
        return f"¥{float(value):,.{digits}f}"
    except (TypeError, ValueError):
        return NA


def fmt_pct(value: Any, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return NA
    try:
        return f"{float(value):.{digits}f}%"
    except (TypeError, ValueError):
        return NA


def fmt_num(value: Any, digits: int = 0) -> str:
    if value is None or (isinstance(value, float) and value != value):
        return NA
    try:
        number = float(value)
    except (TypeError, ValueError):
        return NA
    if digits == 0:
        return f"{number:,.0f}"
    return f"{number:,.{digits}f}"


def safe_div(numerator: Any, denominator: Any, digits: int = 2) -> float | None:
    """安全除法：分母为 0 / None 时返回 None，由上层输出 N/A 并注明原因。"""
    try:
        n = float(numerator)
        d = float(denominator)
    except (TypeError, ValueError):
        return None
    if d == 0:
        return None
    return round(n / d, digits)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 配置模型

@dataclass
class Period:
    """一个对比时间段，起止日期均为闭区间。"""

    label: str
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def contains(self, moment: datetime) -> bool:
        return self.contains_date(moment.date())

    def contains_date(self, value: date) -> bool:
        return self.start <= value <= self.end

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "start": self.start.isoformat(),
                "end": self.end.isoformat(), "days": self.days}

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "Period":
        return Period(label=str(raw["label"]),
                      start=date.fromisoformat(str(raw["start"])),
                      end=date.fromisoformat(str(raw["end"])))


@dataclass
class ColumnMap:
    """逻辑字段 -> 源文件列名。用户确认后的映射在此固化，脚本不再自行猜测。"""

    time: str
    machine: str
    product: str
    qty: str
    amount: str
    user: str
    order: str | None = None
    dorm: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "ColumnMap":
        return ColumnMap(
            time=str(raw["time"]), machine=str(raw["machine"]),
            product=str(raw["product"]), qty=str(raw["qty"]),
            amount=str(raw["amount"]), user=str(raw["user"]),
            order=str(raw["order"]) if raw.get("order") else None,
            dorm=str(raw["dorm"]) if raw.get("dorm") else None,
        )

    def as_pairs(self) -> list[tuple[str, str]]:
        """返回 [(逻辑字段, 中文名, 源列名)]，用于说明表与校验。"""
        names = {**REQUIRED_SALES_FIELDS, **OPTIONAL_SALES_FIELDS}
        out: list[tuple[str, str, str]] = []
        for key, name in names.items():
            value = getattr(self, key, None)
            if value:
                out.append((key, name, value))
        return out


@dataclass
class DormStrategy:
    """寝室楼栋归属策略：三条受支持路径，未配置即阻断分析。"""

    mode: str = "none"            # user_id_pattern | machine_map | column | none
    pattern: str = ""             # 正则，需含命名分组 dorm
    machine_map_path: str = ""    # JSON/CSV: 机器编号 -> 楼栋
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "DormStrategy":
        return DormStrategy(mode=str(raw.get("mode", "none")),
                            pattern=str(raw.get("pattern", "")),
                            machine_map_path=str(raw.get("machine_map_path", "")),
                            note=str(raw.get("note", "")))


@dataclass
class PriceBand:
    low: float
    high: float | None
    label: str

    def contains(self, price: float) -> bool:
        if self.high is None:
            return price >= self.low
        return self.low <= price < self.high

    def to_dict(self) -> dict[str, Any]:
        return {"low": self.low, "high": self.high, "label": self.label}


@dataclass
class SourceFiles:
    sales: str
    categories: str = ""
    dorm_roster: str = ""
    sheet: str = ""
    encoding: str = "auto"
    hashes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "SourceFiles":
        return SourceFiles(
            sales=str(raw.get("sales", "")), categories=str(raw.get("categories", "")),
            dorm_roster=str(raw.get("dorm_roster", "")), sheet=str(raw.get("sheet", "")),
            encoding=str(raw.get("encoding", "auto")),
            hashes=dict(raw.get("hashes", {})),
        )


@dataclass
class AnalysisConfig:
    """一次分析的完整口径。只有 confirmed=True 时分析才会执行。"""

    sources: SourceFiles
    columns: ColumnMap
    dorm: DormStrategy
    periods: list[Period]
    price_bands: list[PriceBand]
    unmapped_products: str = "unclassified"   # unclassified | abort
    negative_policy: str = "exclude"          # exclude | include
    dedupe_policy: str = "keep"               # keep | drop_identical
    include_raw_snapshot: bool = True
    snapshot_limit: int = 2000
    known_data_issues: list[str] = field(default_factory=list)
    platform_name: str = "校园自助售货机"
    archive_root: str = ""            # 归档数据根目录（preflight 向用户确认后固定）
    confirmed: bool = False
    confirmed_at: str = ""
    answers: dict[str, Any] = field(default_factory=dict)
    preflight_summary: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CONFIG_SCHEMA_VERSION
    skill_version: str = SKILL_VERSION

    # -- 序列化 ---------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "skill_version": self.skill_version,
            "sources": self.sources.to_dict(),
            "columns": self.columns.to_dict(),
            "dorm": self.dorm.to_dict(),
            "periods": [p.to_dict() for p in self.periods],
            "price_bands": [b.to_dict() for b in self.price_bands],
            "unmapped_products": self.unmapped_products,
            "negative_policy": self.negative_policy,
            "dedupe_policy": self.dedupe_policy,
            "include_raw_snapshot": self.include_raw_snapshot,
            "snapshot_limit": self.snapshot_limit,
            "known_data_issues": list(self.known_data_issues),
            "platform_name": self.platform_name,
            "archive_root": self.archive_root,
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at,
            "answers": self.answers,
            "preflight_summary": self.preflight_summary,
        }

    def save(self, path: Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
                              encoding="utf-8")

    @staticmethod
    def from_dict(raw: dict[str, Any]) -> "AnalysisConfig":
        try:
            columns = ColumnMap.from_dict(raw["columns"])
            sources = SourceFiles.from_dict(raw.get("sources", {}))
            dorm = DormStrategy.from_dict(raw.get("dorm", {}))
            periods = [Period.from_dict(p) for p in raw.get("periods", [])]
            bands = [PriceBand(low=float(b["low"]),
                               high=(None if b.get("high") is None else float(b["high"])),
                               label=str(b["label"])) for b in raw.get("price_bands", [])]
        except (KeyError, TypeError, ValueError) as exc:
            raise InputError(f"配置文件结构不合法：{exc}",
                             hint="重新运行 `run.py preflight` 生成配置，不要手工删改必需字段。") from exc
        return AnalysisConfig(
            sources=sources, columns=columns, dorm=dorm, periods=periods,
            price_bands=bands,
            unmapped_products=str(raw.get("unmapped_products", "unclassified")),
            negative_policy=str(raw.get("negative_policy", "exclude")),
            dedupe_policy=str(raw.get("dedupe_policy", "keep")),
            include_raw_snapshot=bool(raw.get("include_raw_snapshot", True)),
            snapshot_limit=int(raw.get("snapshot_limit", 2000)),
            known_data_issues=list(raw.get("known_data_issues", [])),
            platform_name=str(raw.get("platform_name", "校园自助售货机")),
            archive_root=str(raw.get("archive_root", "")),
            confirmed=bool(raw.get("confirmed", False)),
            confirmed_at=str(raw.get("confirmed_at", "")),
            answers=dict(raw.get("answers", {})),
            preflight_summary=dict(raw.get("preflight_summary", {})),
            schema_version=int(raw.get("schema_version", CONFIG_SCHEMA_VERSION)),
            skill_version=str(raw.get("skill_version", SKILL_VERSION)),
        )

    @staticmethod
    def load(path: Path) -> "AnalysisConfig":
        path = Path(path)
        if not path.exists():
            raise InputError(f"配置文件不存在：{path}",
                             hint="先运行 `run.py preflight ...` 生成并确认配置。")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise InputError(f"配置文件不是合法 JSON：{exc}") from exc
        return AnalysisConfig.from_dict(raw)

    def require_confirmed(self) -> None:
        if not self.confirmed or not self.confirmed_at:
            raise UnconfirmedConfig(
                "分析配置尚未确认：缺少 confirmed=true / confirmed_at。",
                hint="把 preflight 提出的问题逐条问用户并写回 answers，"
                     "再运行 `run.py confirm --config ... --answers ...` 完成确认。",
                details={"config_confirmed": self.confirmed})
        missing = [name for name in ("sales",) if not getattr(self.sources, name)]
        if missing:
            raise UnconfirmedConfig(f"配置缺少必需输入文件：{missing}")
        if not self.periods:
            raise UnconfirmedConfig("配置未包含任何时间段，无法做期间对比。")


# ---------------------------------------------------------------- 记录模型

@dataclass
class Record:
    """一行清洗后的销售明细。所有派生字段都可回指原始行号。"""

    row_no: int
    ts: datetime | None
    machine_raw: str
    machine: str
    product_raw: str
    sku_id: str
    sku_name: str
    spec: str
    category: str
    qty: float | None
    amount: float | None
    user: str | None
    order: str | None
    dorm: str | None
    period: str
    flags: tuple[str, ...] = ()

    @property
    def unit_price(self) -> float | None:
        return safe_div(self.amount, self.qty, digits=4)

    @property
    def is_valid(self) -> bool:
        return self.flags == () or set(self.flags) <= {"SPEC_MISSING", "CATEGORY_UNMAPPED"}


@dataclass
class CleanLogEntry:
    """清洗日志条目：每个数字都能溯源到具体步骤。"""

    step: str            # CL-01
    title: str
    rule: str
    input_rows: int
    affected_rows: int
    amount_delta: float
    detail: str
    sample: list[str] = field(default_factory=list)
    stage: str = ""       # 环节键（period/dorm/order…），供报表按环节反查真实步骤号

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 结果表格

@dataclass
class Table:
    """统一的表格契约：所有导出器只消费这个结构。"""

    key: str
    title: str
    columns: list[str]
    rows: list[list[Any]] = field(default_factory=list)
    note: str = ""
    col_formats: dict[int, str] = field(default_factory=dict)  # "money"|"pct"|"num"|"text"
    row_formats: dict[int, str] = field(default_factory=dict)  # 行级格式优先于列级
    total_row: list[Any] | None = None
    # 指标行的数值列格式随指标变化（销售额要钱、消费率要百分比），
    # value_cols 标记这些列，row_value_formats 给出每行该用的格式。
    value_cols: list[int] = field(default_factory=list)
    row_value_formats: dict[int, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # 缺失值一律以 N/A 呈现，绝不留空、也不填 0
        self.rows = [[NA if value is None else value for value in row] for row in self.rows]

    def format_of(self, row_index: int, col_index: int) -> str:
        if col_index in self.value_cols and row_index in self.row_value_formats:
            return self.row_value_formats[row_index]
        if row_index in self.row_formats:
            return self.row_formats[row_index]
        return self.col_formats.get(col_index, "text")

    def add_total(self, values: list[Any]) -> None:
        self.total_row = [NA if value is None else value for value in values]

    def numeric_column(self, index: int) -> list[float]:
        out: list[float] = []
        for row in self.rows:
            value = row[index] if index < len(row) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                out.append(float(value))
        return out


@dataclass
class ValidationCheck:
    id: str
    name: str
    expected: Any
    actual: Any
    diff: float | None
    status: str          # PASS | WARN | FAIL | SKIP
    note: str = ""
    module: str = ""

    @property
    def passed(self) -> bool:
        return self.status in {"PASS", "WARN", "SKIP"}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ValidationResult:
    module: str
    checks: list[ValidationCheck] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(c.status == "FAIL" for c in self.checks):
            return "FAIL"
        if any(c.status == "WARN" for c in self.checks):
            return "WARN"
        return "PASS"

    @property
    def blockers(self) -> list[ValidationCheck]:
        return [c for c in self.checks if c.status == "FAIL"]

    def extend(self, checks: Iterable[ValidationCheck]) -> None:
        self.checks.extend(checks)


@dataclass
class ChartSpec:
    """与渲染器无关的图表契约，三个导出器各自实现。"""

    kind: str                       # bar | hbar | line | donut | scatter
    title: str
    categories: list[str] = field(default_factory=list)
    series: list[dict[str, Any]] = field(default_factory=list)
    y_label: str = ""
    x_label: str = ""
    highlight: str = ""             # 需要强调的分类/点位
    note: str = ""


@dataclass
class ModuleResult:
    key: str
    order: int
    title: str
    sheet: str
    table: Table
    validation: ValidationResult
    insight: str = ""
    chart: ChartSpec | None = None
    trace: list[str] = field(default_factory=list)   # 关联的清洗日志 step id

    @property
    def status(self) -> str:
        return self.validation.status


# ---------------------------------------------------------------- 增长点

@dataclass
class Evidence:
    """一条可以指回具体表格单元格的证据：指标名 + 两期数值 + 变化。"""

    metric: str
    prev: Any
    cur: Any
    delta_text: str = ""
    table: str = ""          # 证据所在工作表
    locator: str = ""        # 行定位（机器名/时段/品类…）

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def text(self) -> str:
        base = f"{self.metric} {self.prev}→{self.cur}"
        if self.delta_text:
            base += f"（{self.delta_text}）"
        if self.locator:
            base = f"{self.locator}：{base}"
        return base


@dataclass
class UpliftPoint:
    """销售提升点：问题 + 证据 + 影响范围 + 动作 + 预期效果 + 验证方式。"""

    id: str
    module: str                       # core/hourly/price/category/machine/dorm
    module_title: str
    problem: str
    scope_type: str                   # 机器/时段/品类/楼栋/价格带/整体
    scope: list[str]                  # 具体对象（机器编号、时段…）
    hypothesis: str                   # 原因假设（必须带“假设”字样）
    action: str                       # 可执行动作（含对象与数量）
    expected: str                     # 预期效果（可量化）
    verify: str                       # 验证方式：看哪个指标、下期比什么、门槛
    owner: str = ""                   # 责任对象：谁去做（角色+具体对象）
    evidence: list[Evidence] = field(default_factory=list)
    rule_id: str = ""
    score: float = 0.0
    priority: str = "P2"
    confidence: str = "中"            # 高/中/低

    def to_row(self, index: int) -> list[Any]:
        return [
            index,
            self.module_title,
            self.problem,
            "；".join(item.text() for item in self.evidence) or NA,
            f"{self.scope_type}：" + "、".join(self.scope) if self.scope else NA,
            self.action,
            self.owner or NA,
            self.expected,
            self.priority,
            self.hypothesis,
            self.verify,
            self.confidence,
        ]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["evidence"] = [item.to_dict() for item in self.evidence]
        return data

    def has_three_parts(self) -> bool:
        return bool(self.problem and self.hypothesis and self.action and self.owner
                    and "假设" in self.hypothesis and self.evidence and self.verify)
