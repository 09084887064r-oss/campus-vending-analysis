"""商品归一化：单品判定规则（写死，不允许执行期自行发挥）。

锁定规则（与 SKILL.md / references/product-normalization.md 一一对应）
1. 规格先切分：从商品名称中提取容量/重量/包装数，规格进单品的身份键。
   "可乐 330ml" 与 "可乐 500ml" 是**两个**单品。
2. 名称去规格后，按"品牌词表 + 品名词表"重组核心名；
   品牌与品名顺序颠倒、带不带空格、全角半角、大小写差异都会归一。
3. 合并别名表 MERGE_ALIASES 处理"同品不同名"：如 可口可乐 / 可乐 → 同一个单品。
   不同品牌不合并：百事可乐 ≠ 可乐，康师傅冰红茶 ≠ 统一冰红茶。
4. 规格缺失时标注 "未标注规格"（SPEC_MISSING），宁可分成两个单品，也不猜规格。
5. 品类映射只认用户提供的分类表（精确名 → 规范化名 → 单品核心名）；
   映射不到就是"未分类"，不做关键词猜测。

以上规则是常量，AI 执行期不得改写；如需扩充词表，改本文件并重跑自测。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from typing import Sequence

from .core import (
    CATEGORY_FIELD_ALIASES,
    DORM_ROSTER_ALIASES,
    InputError,
    normalize_text,
    parse_number,
)
from .sources import RawTable

UNSPEC = "未标注规格"
UNCATEGORIZED = "未分类"

# ---------------------------------------------------------------- 规格切分

_UNIT_CANON = {
    "ml": "ml", "毫升": "ml", "cc": "ml",
    "l": "L", "升": "L",
    "g": "g", "克": "g",
    "kg": "kg", "千克": "kg", "公斤": "kg",
}

_RE_SIZE = re.compile(r"(\d+(?:\.\d+)?)\s*(ml|毫升|cc|l|升|kg|千克|公斤|g|克)")
_RE_PACK = re.compile(r"[*×]\s*(\d+)")
_RE_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class ProductIdentity:
    raw: str
    core: str
    spec: str
    sku_id: str
    brand: str = ""
    product: str = ""
    flags: tuple[str, ...] = ()

    @property
    def display(self) -> str:
        if self.spec and self.spec != UNSPEC:
            return f"{self.core} {self.spec}"
        return self.core


# ---------------------------------------------------------------- 词表（写死）

#: 品牌词表：按长度降序匹配，命中后从名称中剥离，用于区分"不同品牌"。
BRAND_VOCAB: tuple[str, ...] = (
    "可口可乐", "百事可乐", "康师傅", "统一", "农夫山泉", "怡宝", "娃哈哈",
    "元气森林", "东方树叶", "三得利", "维他", "尖叫", "脉动", "东鹏", "红牛",
    "乐虎", "宝矿力", "佳得乐", "依云", "百岁山", "昆仑山", "恒大冰泉",
    "光明", "伊利", "蒙牛", "君乐宝", "认养一头牛", "特仑苏", "安慕希",
    "乐事", "可比克", "上好佳", "品客", "奥利奥", "趣多多", "好丽友",
    "达利园", "盼盼", "三只松鼠", "良品铺子", "百草味", "卫龙", "旺旺",
    "银鹭", "加多宝", "王老吉", "和其正", "香飘飘", "优乐美", "雀巢",
    "星巴克", "costa", "瑞幸", "永璞", "三顿半", "士力架", "德芙",
    "费列罗", "金丝猴", "大白兔", "南街村", "白象", "今麦郎", "汤达人",
    "统一企业", "青岛", "雪花", "百威", "科罗娜", "燕京", "哈尔滨",
    "维达", "心相印", "清风", "洁柔", "苏菲", "七度空间", "护舒宝",
    "云南白药", "中华", "高露洁", "佳洁士", "海飞丝", "飘柔", "潘婷",
    "南孚", "公牛", "小米", "绿联", "安克", "晨光", "得力",
    "百事", "七喜", "美年达", "芬达", "雪碧",
)

#: 品名词表：按长度降序匹配，命中后作为"品名"。—— 只描述商品是什么，不做品类判断。
PRODUCT_VOCAB: tuple[str, ...] = (
    "饮用天然水", "天然矿泉水", "纯净水", "矿泉水", "苏打水", "气泡水", "电解质水",
    "冰红茶", "柠檬茶", "绿茶", "奶茶", "乌龙茶", "茉莉花茶", "东方树叶",
    "美式咖啡", "拿铁", "咖啡", "能量饮料", "功能饮料", "运动饮料", "维生素饮料",
    "豆奶", "纯牛奶", "酸奶", "乳饮料", "椰汁", "果汁", "果味饮料", "可乐",
    "薯片", "饼干", "曲奇", "威化", "巧克力", "糖果", "口香糖", "果冻",
    "辣条", "方便面", "泡面", "自热米饭", "自热火锅", "面包", "蛋糕", "吐司",
    "冰淇淋", "雪糕", "冰棍", "啤酒", "口罩", "纸巾", "湿巾", "卫生巾",
    "牙刷", "洗发水", "洗衣液", "电池", "数据线", "打火机", "雨伞", "暖宝宝",
)

#: 同品不同名合并：键=规范化后的核心名，值=统一单品名。
MERGE_ALIASES: dict[str, str] = {
    "可口可乐": "可乐",
    "可乐可乐": "可乐",
    "cocacola": "可乐",
    "coke": "可乐",
    "百事": "百事可乐",
    "pepsi": "百事可乐",
    "农夫山泉饮用天然水": "农夫山泉",
    "怡宝纯净水": "怡宝",
    "王老吉凉茶": "王老吉",
    "加多宝凉茶": "加多宝",
    "康师傅冰红茶": "康师傅 冰红茶",
    "统一冰红茶": "统一 冰红茶",
    "东方树叶": "东方树叶",
    "元气森林气泡水": "元气森林",
    "东鹏特饮": "东鹏特饮",
    "红牛维生素功能饮料": "红牛",
    "雪碧汽水": "雪碧",
    "芬达汽水": "芬达",
    "伊利安慕希酸奶": "伊利安慕希",
    "蒙牛特仑苏": "特仑苏",
}

_BRAND_BY_LEN = sorted(BRAND_VOCAB, key=len, reverse=True)
_PRODUCT_BY_LEN = sorted(PRODUCT_VOCAB, key=len, reverse=True)


def _canonical_spec(number: str, unit: str) -> str:
    canonical_unit = _UNIT_CANON.get(unit.lower(), unit.lower())
    value = number.rstrip("0").rstrip(".") if "." in number else number
    return f"{value}{canonical_unit}"


def split_spec(name: str) -> tuple[str, str, tuple[str, ...]]:
    """把名称拆成（去规格主体, 规格串, 标记）。规格串包含包装数，如 330ml 或 330ml×6。"""
    flags: list[str] = []
    text = normalize_text(name)
    sizes = _RE_SIZE.findall(text)
    packs = _RE_PACK.findall(text)
    if not sizes:
        return text, UNSPEC, ("SPEC_MISSING",)
    number, unit = sizes[-1]
    spec = _canonical_spec(number, unit)
    if packs:
        spec = f"{spec}×{packs[-1]}"
    base = _RE_SIZE.sub(" ", text)
    base = _RE_PACK.sub(" ", base)
    base = _RE_SPACE.sub("", base).strip("-_/|·.,，。")
    if not base:
        flags.append("NAME_EMPTY_AFTER_SPEC")
    return base, spec, tuple(flags)


def _strip_brand(base: str) -> tuple[str, str]:
    """返回（命中品牌, 去掉品牌后的剩余文本）。"""
    for brand in _BRAND_BY_LEN:
        if base.startswith(brand):
            return brand, base[len(brand):]
        if base.endswith(brand):
            return brand, base[: -len(brand)]
    for brand in _BRAND_BY_LEN:
        if brand in base:
            return brand, base.replace(brand, "", 1)
    return "", base


def _match_product(text: str) -> str:
    for product in _PRODUCT_BY_LEN:
        if product and product in text:
            return product
    return ""


def build_identity(raw_name: str, *, product_row: int = 0) -> ProductIdentity:
    """按写死规则生成单品身份。同一名称永远得到同一结果（纯函数）。"""
    text = normalize_text(raw_name)
    if not text:
        return ProductIdentity(raw=str(raw_name or ""), core="(空商品名)", spec=UNSPEC,
                               sku_id="(空商品名)@未标注规格", flags=("NAME_EMPTY",))

    base, spec, flags = split_spec(text)
    brand, rest = _strip_brand(base)
    product = _match_product(rest) or _match_product(base)

    merge_target = MERGE_ALIASES.get(base)
    if merge_target:
        core = merge_target
        brand_out = brand
    elif brand:
        if product and product in brand:
            core = brand                     # 百事可乐：品牌本身已包含品名
        elif product:
            core = f"{brand}{product}"
        else:
            core = base or brand
        brand_out = brand
    elif product and product == base:
        core = product
        brand_out = ""
    else:
        core = base or text                  # 无品牌时不轻易做泛化，保留原始信息量
        brand_out = ""

    core = re.sub(r"[（(][^）)]*[）)]", "", core).strip() or core
    core = MERGE_ALIASES.get(core, core)
    core = re.sub(r"\s+", " ", core).strip()

    if not product and not brand:
        flags = flags + ("PRODUCT_UNRECOGNIZED",)
    sku_id = f"{core}@{spec}"
    return ProductIdentity(raw=text, core=core, spec=spec, sku_id=sku_id,
                           brand=brand_out, product=_match_product(core) or product,
                           flags=flags)


# ---------------------------------------------------------------- 规格/包装标准化

def normalize_spec_only(raw_name: str) -> str:
    _, spec, _ = split_spec(raw_name)
    return spec


# ---------------------------------------------------------------- 品类映射

@dataclass
class CategoryResolution:
    category: str
    level: str          # exact | normalized | sku | core | unmapped


class CategoryResolver:
    """按用户提供的分类表做映射；映射不到就是"未分类"，不猜测。"""

    def __init__(self, mapping: dict[str, str] | None) -> None:
        self.exact: dict[str, str] = {}
        self.normalized: dict[str, str] = {}
        for raw_name, category in (mapping or {}).items():
            name = str(raw_name).strip()
            category = str(category).strip()
            if not name or not category:
                continue
            self.exact[name] = category
            self.normalized[normalize_text(name)] = category

    @property
    def size(self) -> int:
        return len(self.exact)

    def resolve(self, raw_name: str, identity: ProductIdentity) -> CategoryResolution:
        name = str(raw_name or "").strip()
        if name in self.exact:
            return CategoryResolution(self.exact[name], "exact")
        normalized = normalize_text(name)
        if normalized in self.normalized:
            return CategoryResolution(self.normalized[normalized], "normalized")
        if identity.sku_id in self.exact:
            return CategoryResolution(self.exact[identity.sku_id], "sku")
        if identity.core in self.exact:
            return CategoryResolution(self.exact[identity.core], "core")
        if identity.core in self.normalized:
            return CategoryResolution(self.normalized[identity.core], "normalized")
        return CategoryResolution(UNCATEGORIZED, "unmapped")

    def covered_products(self, names: list[str]) -> dict[str, list[str]]:
        """返回分类表对明细商品名的覆盖情况。"""
        missing = sorted({name for name in names
                          if self.resolve(name, build_identity(name)).level == "unmapped"})
        covered = sorted({name for name in names} - set(missing))
        return {"covered": covered, "missing": missing}


def _pick_column(headers: Sequence[str], aliases: Sequence[str]) -> str | None:
    """在表头里按别名找列：先精确匹配，再按规范化文本匹配。"""
    for alias in aliases:
        if alias in headers:
            return alias
    lowered = {normalize_text(header).lower(): header for header in headers}
    for alias in aliases:
        hit = lowered.get(normalize_text(alias).lower())
        if hit is not None:
            return hit
    return None


def build_category_mapping(table: RawTable) -> dict[str, str]:
    """从 RawTable 解析 商品名称 -> 品类 映射。"""
    product_key = _pick_column(table.headers, CATEGORY_FIELD_ALIASES["product"])
    category_key = _pick_column(table.headers, CATEGORY_FIELD_ALIASES["category"])
    if not product_key or not category_key:
        raise InputError(
            "商品分类表缺少必需列：需要（商品名称, 品类）。",
            hint=f"当前列名：{table.headers}；可用别名见 references/data-contract.md。",
            details={"headers": table.headers})
    mapping: dict[str, str] = {}
    for row in table.rows:
        name = normalize_text(row.get(product_key))
        category = normalize_text(row.get(category_key))
        if name and category:
            mapping[name] = category
    if not mapping:
        raise InputError("商品分类表解析后没有任何有效行（商品名称/品类为空）。")
    return mapping


GENDER_ALIASES: tuple[str, ...] = ("性别", "男女", "gender", "sex")
_GROUP_SPLIT = re.compile(r"[+＋/、,，;；]")


def _dorm_parts(label: str) -> list[str]:
    """把人数表里的「楼栋组」写法展开：十栋+十一栋 → [十栋, 十一栋]。"""
    parts = [part.strip() for part in _GROUP_SPLIT.split(str(label or ""))]
    return [part for part in parts if part]


def build_dorm_roster(table: RawTable) -> dict[str, dict[str, Any]]:
    """从 RawTable 解析 楼栋(组) -> {old, new, total, gender, parts}。

    键名用 old/new 与用户消费表里的「老生(人)/新生(人)」一一对应；
    人数缺失保留 None（N/A），性别缺失保留 None（后续按机组类型推断）。
    """
    headers = table.headers
    dorm_key = _pick_column(headers, DORM_ROSTER_ALIASES["dorm"])
    freshman_key = _pick_column(headers, DORM_ROSTER_ALIASES["freshman"])
    senior_key = _pick_column(headers, DORM_ROSTER_ALIASES["senior"])
    total_key = _pick_column(headers, DORM_ROSTER_ALIASES["total"])
    if not dorm_key:
        raise InputError("寝室楼栋人数表缺少「楼栋」列。", hint=f"当前列名：{headers}")
    if not (freshman_key or senior_key or total_key):
        raise InputError("寝室楼栋人数表缺少人数列（新生人数/老生人数/总人数 至少一列）。",
                         hint=f"当前列名：{headers}")
    gender_key = _pick_column(headers, GENDER_ALIASES)
    roster: dict[str, dict[str, Any]] = {}
    for row in table.rows:
        dorm = normalize_text(row.get(dorm_key))
        if not dorm:
            continue
        freshman = parse_number(row.get(freshman_key)) if freshman_key else None
        senior = parse_number(row.get(senior_key)) if senior_key else None
        total = parse_number(row.get(total_key)) if total_key else None
        if total is None and (freshman is not None or senior is not None):
            total = (freshman or 0) + (senior or 0)
        gender = normalize_text(row.get(gender_key)) if gender_key else ""
        roster[dorm] = {
            "old": senior,                 # 老生
            "new": freshman,               # 新生
            "total": total,
            "gender": gender if gender in {"男", "女"} else None,
            "parts": _dorm_parts(dorm),    # 楼栋组拆解（单栋时就是自己）
        }
    if not roster:
        raise InputError("寝室楼栋人数表解析后没有任何有效行。")
    return roster
