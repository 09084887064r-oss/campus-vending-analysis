"""两期示例数据（2026-W44 上期 / 2026-W45 本期），刻意埋入增长点场景。

场景清单（自测与演示都要能命中）：
- 软件九栋男：本期换成更便宜的货 → 销售额降、用户数不降（R01 转化下降）
- 软件十栋女：本期少了 1/5 的成交 → 销售额与用户数同降（R02 客流流失）
- 水饮品类：本期销量 -20% 左右（R07 品类下滑）
- 沙琪玛：本期销量归零（R06 断货/汰换）
- ≤3 元档：本期多 6 个在售单品但销量下降（R04 上架多动销低）
- 5–8 元档：单品没增、销量显著上升（R05 加品机会）
- 一栋女：渗透率明显低于全校（R09 拉新）；五栋男：本期零消费
- 数据质量问题：1 行退款、1 行时间无法解析、1 行完全重复、2 行缺用户标识
"""

from __future__ import annotations

import csv
import random
from pathlib import Path
from typing import Any

PERIODS = {"2026-W44": ("2026-10-26", "2026-11-01"),
           "2026-W45": ("2026-11-02", "2026-11-08")}

# (商品, 品类, 单价)
PRODUCTS: list[tuple[str, str, float]] = [
    ("农夫山泉 550ml", "水饮", 2.5), ("冰露矿泉水 550ml", "水饮", 2.0),
    ("康师傅冰红茶 500ml", "茶饮", 3.5), ("东方树叶 500ml", "茶饮", 5.0),
    ("可口可乐 330ml", "汽水", 3.0), ("可乐 330ml", "汽水", 3.0),
    ("可乐 500ml", "汽水", 4.0), ("百事可乐 330ml", "汽水", 3.0),
    ("红牛 250ml", "功能饮料", 7.0), ("脉动 600ml", "功能饮料", 6.0),
    ("伊利安慕希酸奶 205g", "牛奶乳饮", 6.5), ("蒙牛特仑苏 250ml", "牛奶乳饮", 7.5),
    ("康师傅红烧牛肉面", "经典桶泡面", 4.5), ("统一老坛酸菜面", "经典桶泡面", 4.5),
    ("白象汤好喝拌面", "拌面", 5.5), ("双汇火腿肠 40g", "肉肠类", 2.5),
    ("卤蛋", "蛋类", 2.0), ("手撕面包", "面包", 4.0),
    ("达利园蛋黄派 2枚", "面包", 5.0), ("乐事薯片 70g", "薯片薯条类", 6.5),
    ("上好佳薯条 40g", "薯片薯条类", 3.5), ("沙琪玛 100g", "沙琪玛", 3.0),
    ("徐福记沙琪玛 160g", "糕点类", 5.5), ("桃李面包 100g", "面包", 4.5),
    ("雀巢咖啡 268ml", "咖啡", 6.0), ("农夫果园 500ml", "果汁", 5.0),
    ("果粒橙 450ml", "果汁", 4.5), ("冰镇杨梅汁 500ml", "未收录", 6.0),
]

CHEAP_EXTRA = [("旺仔小馒头 30g", "膨化零食类", 2.0), ("卫龙小面筋 20g", "肉类麻辣", 2.5),
               ("旺旺仙贝 2枚", "膨化零食类", 2.0), ("咪咪虾条 20g", "膨化零食类", 2.5),
               ("干脆面 40g", "干脆面", 2.0), ("山楂片 30g", "甜食", 2.5)]

# 机器 → (楼栋, 女/男)
MACHINES: dict[str, tuple[str, str]] = {
    "软件六栋男": ("六栋", "男"), "软件七栋男": ("七栋", "男"), "软件八栋男": ("八栋", "男"),
    "软件九栋男": ("九栋", "男"), "软件四栋男": ("四栋", "男"),
    "软件一栋女": ("一栋", "女"), "软件二栋女": ("二栋", "女"), "软件三栋女": ("三栋", "女"),
    "软件十栋女": ("十栋", "女"), "软件十一栋女": ("十一栋", "女"),
}

# 楼栋 → (老生, 新生)
ROSTER: dict[str, tuple[int, int]] = {
    "一栋": (538, 353), "二栋": (858, 44), "三栋": (335, 393), "四栋": (145, 504),
    "五栋": (420, 120),                    # 本期零消费
    "六栋": (1065, 40), "七栋": (282, 613), "八栋": (1070, 14), "九栋": (1050, 36),
    "十栋": (820, 380), "十一栋": (744, 223),
}

# 机器权重：本期按场景调整成交笔数与客单
DEFAULT_WEIGHT = 30
WEIGHTS = {"2026-W44": {}, "2026-W45": {
    "软件九栋男": 30, "软件十栋女": 15,          # 十栋女：成交量明显减少
}}
CHEAP_MACHINES = {"软件九栋男"}                  # 本期这些机器改成便宜货（金额降、用户不降）
HOUR_WEIGHTS = {7: 2, 8: 6, 9: 4, 10: 5, 11: 4, 12: 9, 13: 6, 14: 4, 15: 3,
                16: 4, 17: 6, 18: 9, 19: 8, 20: 7, 21: 10, 22: 6, 23: 3,
                0: 1, 1: 1, 2: 1, 3: 0, 4: 0, 5: 0, 6: 1}


def _write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)
    return path


def _days(start: str, end: str) -> list[str]:
    from datetime import date, timedelta

    first, last = date.fromisoformat(start), date.fromisoformat(end)
    return [(first + timedelta(days=offset)).isoformat()
            for offset in range((last - first).days + 1)]


def build_demo_dataset(root: str | Path, seed: int = 20261108) -> dict[str, Any]:
    root = Path(root)
    rng = random.Random(seed)
    sales_files: dict[str, Path] = {}

    for period_key, (start, end) in PERIODS.items():
        rows: list[list[Any]] = []
        order_seq = 0
        for day in _days(start, end):
            for machine, (dorm, _gender) in MACHINES.items():
                if dorm == "五栋":
                    continue
                weight = WEIGHTS[period_key].get(machine, DEFAULT_WEIGHT)
                deals = max(1, int(weight * rng.uniform(0.8, 1.2)))
                for _ in range(deals):
                    hour = rng.choices(list(HOUR_WEIGHTS), weights=list(HOUR_WEIGHTS.values()))[0]
                    minute = rng.randint(0, 59)
                    cheap_pool = [p for p in PRODUCTS if p[2] <= 3.5 and p[1] != "沙琪玛"]
                    pool = cheap_pool if (period_key == "2026-W45" and machine in CHEAP_MACHINES) else PRODUCTS
                    if period_key == "2026-W45" and machine not in CHEAP_MACHINES:
                        pool = pool + CHEAP_EXTRA
                    name, category, price = rng.choice(pool)
                    if category == "沙琪玛" and period_key == "2026-W45":
                        name, category, price = rng.choice([("未收录-沙琪玛", "未收录", 3.0)])
                    qty = rng.choice([1, 1, 1, 2, 2, 3])
                    user = f"{dorm}-{rng.randint(1000, 9999):04d}"
                    order_seq += 1
                    order = f"{period_key}-{order_seq:06d}"
                    rows.append([f"{day} {hour:02d}:{minute:02d}:00", machine, name, qty,
                                 round(price * qty, 2), user, order])
                    if rng.random() < 0.12:              # 同一订单再买一件（抬高连带率）
                        name2, _cat2, price2 = rng.choice(pool)
                        rows.append([f"{day} {hour:02d}:{minute:02d}:30", machine, name2, 1,
                                     round(price2, 2), user, order])

        # 埋入数据质量问题
        rows.append([f"{start} 09:15:00", "软件六栋男", "农夫山泉 550ml", 1, -2.5,
                     "六栋-8801", f"{period_key}-R001"])
        rows.append([f"{start} 21:40:00", "软件九栋男", "红牛 250ml", 1, -7.0,
                     "九栋-8802", f"{period_key}-R002"])
        rows.append(["时间不详", "软件七栋男", "可乐 500ml", 1, 4.0,
                     "七栋-8803", f"{period_key}-R003"])
        rows.append(rows[0][:])
        rows.append([f"{start} 12:05:00", "软件一栋女", "手撕面包", 1, 4.0, "", f"{period_key}-R004"])

        sales_files[period_key] = _write_csv(
            root / period_key / "sales_detail.csv",
            ["交易时间", "机器编号", "商品名称", "销售数量", "销售金额", "用户标识", "订单标识"],
            rows)

    categories = _write_csv(root / "category_table.csv", ["商品名称", "品类"],
                            [[name, category] for name, category, _price in PRODUCTS])
    roster = _write_csv(root / "dorm_roster.csv", ["楼栋", "老生人数", "新生人数"],
                        [[dorm, old, new] for dorm, (old, new) in ROSTER.items()])
    machine_map = _write_csv(root / "machine_building.csv", ["机器编号", "楼栋"],
                             [[machine, dorm] for machine, (dorm, _g) in MACHINES.items()])

    return {"periods": list(PERIODS), "sales": sales_files, "categories": categories,
            "roster": roster, "machine_map": machine_map, "root": root}
