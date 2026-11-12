"""按成品表（金标准）反向重建两期原始销售明细，用于端到端回归。

成品表只给了各维度的边际量（每小时、每品类、每价格带、每机组、每楼栋），
没给联合分布，因此按"结构 → 边际 → 残差修复"的顺序构造：

1. 品类×价格带 的整数销量矩阵（行=品类销量、列=价格带销量，权重偏向品类均价所在档）
2. 品类×价格带 的金额矩阵（行=品类金额、列=价格带金额，隐含单价必须落在档位区间）
3. 生成 SKU 与单价（数量=各价格带/品类单品数），精确命中销量与金额
4. 按"剩余期望单价最高者优先"把单品分到 24 小时（销量精确，金额再做交换修复）
5. 同一套贪心把单品分到机器，再做同小时交换修复
6. 造用户与订单：命中每台机器用户数、楼栋组用户数、订单数、连带率

输出 tests/fixture/，并打印每个边际家族的最大残差（保真度）。
"""

from __future__ import annotations

import csv
import json
import random
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

GOLDEN = Path(__file__).with_name("golden_raw.json")
OUT_DIR = Path(__file__).with_name("fixture")
PERIODS = {"2026-W44": ("2026-10-26", 1), "2026-W45": ("2026-11-02", 2)}
BAND_RANGES = {"≤3元": (1.0, 3.0), "3-5元": (3.0, 5.0), "5-8元": (5.0, 8.0)}
DAY_COUNT = 7


def load_golden() -> dict[str, Any]:
    raw = json.loads(GOLDEN.read_text(encoding="utf-8"))
    core: dict[str, tuple[float, float]] = {}
    for row in raw["核心数据分析"][1:]:
        if row[0] and row[2] is not None:
            core[str(row[0])] = (float(row[2]), float(row[3]) if row[3] is not None else 0.0)
    hours = {"p1": [], "p2": []}
    for row in raw["时段销售分析"][1:]:
        if str(row[0]) == "合计":
            continue
        hours["p1"].append((int(row[1]), float(row[4]), int(row[7])))
        hours["p2"].append((int(row[2]), float(row[5]), int(row[8])))
    bands = {"p1": {}, "p2": {}}
    for row in raw["价格带陈列分析"][1:]:
        if str(row[0]) == "合计":
            continue
        bands["p1"][str(row[0])] = (int(row[1]), int(row[4]), float(row[7]))
        bands["p2"][str(row[0])] = (int(row[2]), int(row[5]), float(row[8]))
    cats = {"p1": {}, "p2": {}}
    for row in raw["品类数销售分析"][1:]:
        if str(row[0]) == "合计":
            continue
        cats["p1"][str(row[0])] = (int(row[1]), int(row[3]), float(row[6]))
        cats["p2"][str(row[0])] = (int(row[2]), int(row[4]), float(row[7]))
    machines = {"p1": {}, "p2": {}}
    kinds: dict[str, str] = {}
    for row in raw["机组销售环比"][1:]:
        if str(row[0]) == "合计":
            continue
        kinds[str(row[0])] = str(row[1])
        machines["p1"][str(row[0])] = (float(row[2]), int(row[5]))
        machines["p2"][str(row[0])] = (float(row[3]), int(row[6]))
    dorms = {"p1": {}, "p2": {}}
    for row in raw["用户消费分析"][1:]:
        if str(row[0]) == "合计":
            continue
        dorms["p1"][str(row[0])] = (int(row[2]), int(row[3]), int(row[4]), int(row[6]), str(row[1]))
        dorms["p2"][str(row[0])] = (int(row[2]), int(row[3]), int(row[4]), int(row[7]), str(row[1]))
    return {"core": core, "hours": hours, "bands": bands, "cats": cats,
            "machines": machines, "kinds": kinds, "dorms": dorms}


# ------------------------------------------------------------------ 矩阵

def ipf_round(rows: dict[str, int], cols: dict[str, int],
              weights: dict[tuple[str, str], float] | None = None) -> dict[tuple[str, str], int]:
    """实数 IPF + 可控取整：行和与列和同时精确等于给定边际。

    取整用"最大余数一次分配"：每个格子最多进位 1，先满足所有行，再用列容差抵消，
    最后按余数从大到小回补，保证两个方向的边际都不破。
    """
    row_keys, col_keys = list(rows), list(cols)
    if sum(rows.values()) != sum(cols.values()):
        raise ValueError("边际不自洽")
    total = sum(rows.values())
    m = {r: {c: (weights or {}).get((r, c), rows[r] * cols[c] / max(total, 1)) for c in col_keys}
         for r in row_keys}
    for _ in range(500):
        for r in row_keys:
            scale = sum(m[r].values()) or 1.0
            for c in col_keys:
                m[r][c] *= rows[r] / scale
        for c in col_keys:
            scale = sum(m[r][c] for r in row_keys) or 1.0
            for r in row_keys:
                m[r][c] *= cols[c] / scale
    base = {(r, c): int(m[r][c]) for r in row_keys for c in col_keys}
    row_left = {r: rows[r] - sum(base[(r, c)] for c in col_keys) for r in row_keys}
    col_left = {c: cols[c] - sum(base[(r, c)] for r in row_keys) for c in col_keys}
    frac = {(r, c): m[r][c] - base[(r, c)] for r in row_keys for c in col_keys}
    while any(value > 0 for value in row_left.values()):
        row = next(r for r in row_keys if row_left[r] > 0)
        column = max((c for c in col_keys if col_left[c] > 0),
                     key=lambda c: (frac[(row, c)], m[row][c]))
        base[(row, column)] += 1
        row_left[row] -= 1
        col_left[column] -= 1
    assert all(value == 0 for value in row_left.values()), "行边际未对齐"
    assert all(value == 0 for value in col_left.values()), "列边际未对齐"
    return base


def amount_matrix(x: dict[tuple[str, str], int], cat_qty: dict[str, int],
                  band_qty: dict[str, int], cat_amount: dict[str, float],
                  band_amount: dict[str, float]) -> tuple[dict[tuple[str, str], int],
                                                          dict[tuple[str, str], float], float,
                                                          float]:
    """按品类把金额精确摊到各价格带；价格带总额只做披露，不强行凑。

    理由：一个 (品类,价格带) 格子的单价必须落在该档区间内，两个方向的金额边际
    不一定同时可行；品类表有 29 行、价格带只有 3 行，因此优先保品类金额。
    """
    cats, bands = list(cat_qty), list(band_qty)
    price: dict[tuple[str, str], float] = {}
    for cat in cats:
        mean = cat_amount[cat] / max(cat_qty[cat], 1)
        for band in bands:
            lo, hi = BAND_RANGES[band]
            price[(cat, band)] = min(max(mean, lo), hi)
    for _ in range(40):
        for cat in cats:
            cells = [(cat, b) for b in bands if x.get((cat, b), 0) > 0]
            if not cells:
                continue
            current = sum(x[k] * price[k] for k in cells)
            if current <= 0:
                continue
            factor = cat_amount[cat] / current
            for k in cells:
                lo, hi = BAND_RANGES[k[1]]
                price[k] = min(max(price[k] * factor, lo + 0.01), hi - 0.01)
    amount = {k: x[k] * price[k] for k in x if x[k] > 0}
    cat_residual = max(abs(sum(amount.get((c, b), 0.0) for b in bands) - cat_amount[c])
                       for c in cats)
    band_residual = max(abs(sum(amount.get((c, b), 0.0) for c in cats) - band_amount[b])
                        for b in bands)
    return x, amount, round(cat_residual, 2), round(band_residual, 2)


def make_skus(x: dict[tuple[str, str], int], y: dict[tuple[str, str], float],
              sku_counts: dict[tuple[str, str], int]) -> list[dict[str, Any]]:
    skus: list[dict[str, Any]] = []
    for key, qty in x.items():
        if qty <= 0:
            continue
        cat, band = key
        lo, hi = BAND_RANGES[band]
        count = max(1, min(sku_counts.get(key, 1), qty))
        base = qty // count
        counts = [base] * count
        for i in range(qty - base * count):
            counts[i] += 1
        prices = [round(lo + (hi - lo) * (i + 1) / (count + 1), 2) for i in range(count)]
        for _ in range(200):
            residual = y.get(key, 0.0) - sum(n * p for n, p in zip(counts, prices))
            if abs(residual) < 0.005:
                break
            i = max(range(count), key=lambda k: counts[k])
            candidate = prices[i] + residual / counts[i]
            prices[i] = round(min(max(candidate, lo), hi), 2)
        for i, (n, p) in enumerate(zip(counts, prices)):
            if n <= 0:
                continue
            price = round(p, 2)
            if price in (3.0, 5.0, 8.0):                 # 避开档位边界，保持口径无歧义
                price = round(price + 0.01, 2) if price < hi else round(price - 0.01, 2)
            skus.append({"cat": cat, "band": band, "name": f"{cat}-{band}{i:02d}",
                         "price": price, "qty": n})
    return skus


def topup_prices(skus: list[dict[str, Any]], amount_target: dict[tuple[str, str], float]) -> float:
    """在档位区间内调整单价，把每个 (品类,价格带) 的金额推回目标值。

    只动单价、不动数量，所以销量边际与档位归属都不变。
    """
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sku in skus:
        cells[(sku["cat"], sku["band"])].append(sku)
    for _ in range(120):
        worst = 0.0
        for key, group in cells.items():
            target = amount_target.get(key, 0.0)
            current = sum(item["price"] * item["qty"] for item in group)
            deficit = target - current
            worst = max(worst, abs(deficit))
            if abs(deficit) < 0.01 or current <= 0:
                continue
            lo, hi = BAND_RANGES[key[1]]
            qty = sum(item["qty"] for item in group)
            if deficit > 0:
                room = max((hi - 0.01) * qty - current, 0.0)
            else:
                room = max(current - (lo + 0.01) * qty, 0.0)
            step = min(abs(deficit), room)
            if step <= 0:
                continue
            factor = 1 + (step / current if deficit > 0 else -step / current)
            for item in group:
                item["price"] = round(min(max(item["price"] * factor, lo + 0.01), hi - 0.01), 2)
        if worst < 0.02:
            break
    return round(worst, 2)


def rebalance_bands(skus: list[dict[str, Any]], cat_amount: dict[str, float],
                    band_amount: dict[str, float]) -> float:
    """同一品类内部把金额从"超额档"挪到"不足档"，保持该品类合计不变。

    只沿档内价格区间微调单价：不动数量、不动档位归属，因此销量与单品数完全不变。
    """
    cells: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for sku in skus:
        cells[(sku["cat"], sku["band"])].append(sku)

    def amount(key: tuple[str, str]) -> float:
        return sum(item["price"] * item["qty"] for item in cells.get(key, []))

    def qty_of(key: tuple[str, str]) -> int:
        return sum(item["qty"] for item in cells.get(key, []))

    def room(key: tuple[str, str], direction: int) -> float:
        qty = qty_of(key)
        if qty <= 0:
            return 0.0
        lo, hi = BAND_RANGES[key[1]]
        current = amount(key)
        if direction > 0:
            return max((hi - 0.01) * qty - current, 0.0)
        return max(current - (lo + 0.01) * qty, 0.0)

    def apply(key: tuple[str, str], delta: float) -> None:
        qty = qty_of(key)
        if qty <= 0 or delta == 0:
            return
        lo, hi = BAND_RANGES[key[1]]
        for item in cells[key]:
            share = item["qty"] / qty
            item["price"] = round(min(max(item["price"] + delta * share / item["qty"],
                                           lo + 0.01), hi - 0.01), 2)

    worst = 0.0
    for _ in range(500):
        actual = {band: sum(amount((cat, band)) for cat in cat_amount)
                  for band in band_amount}
        need = {band: band_amount[band] - actual[band] for band in band_amount}
        worst = max(abs(value) for value in need.values())
        if worst < 1.0:
            break
        more = max(need, key=lambda b: need[b])
        less = min(need, key=lambda b: need[b])
        if need[more] <= 0.5 or need[less] >= -0.5:
            break
        moved = 0.0
        for cat in cat_amount:
            key_more, key_less = (cat, more), (cat, less)
            if not cells.get(key_more) or not cells.get(key_less):
                continue
            budget = min(need[more], -need[less])
            delta = min(budget, room(key_more, +1), room(key_less, -1))
            if delta <= 0.01:
                continue
            apply(key_more, delta)
            apply(key_less, -delta)
            need[more] -= delta
            need[less] += delta
            moved += delta
            if need[more] <= 0.5 or need[less] >= -0.5:
                break
        if moved <= 0.01:
            break
    return round(worst, 2)


def expand_items(skus: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for sku in skus:
        for _ in range(int(sku["qty"])):
            items.append({"name": sku["name"], "price": sku["price"],
                          "cat": sku["cat"], "band": sku["band"]})
    return items


def greedy_assign(items: list[dict[str, Any]], targets: dict[Any, tuple[int, float]],
                  *, fixed_qty: bool) -> dict[Any, list[dict[str, Any]]]:
    """剩余期望单价最高者优先：把单品分配到各组，尽量同时命中数量与金额。"""
    buckets: dict[Any, list[dict[str, Any]]] = {key: [] for key in targets}
    rem_amount = {key: value[1] for key, value in targets.items()}
    rem_qty = {key: value[0] for key, value in targets.items()}
    for item in sorted(items, key=lambda it: it["price"], reverse=True):
        def priority(key: Any) -> float:
            if fixed_qty and rem_qty[key] <= 0:
                return -1e9
            if rem_qty[key] <= 0:
                return rem_amount[key] - 1e9
            return rem_amount[key] / rem_qty[key]
        key = max(targets, key=priority)
        buckets[key].append(item)
        rem_amount[key] -= item["price"]
        rem_qty[key] -= 1
    return buckets


def repair_hours(buckets: dict[int, list[dict[str, Any]]],
                 targets: dict[int, tuple[int, float]]) -> float:
    def total(hour: int) -> float:
        return sum(item["price"] for item in buckets[hour])

    best = max(abs(total(h) - targets[h][1]) for h in range(24))
    for _ in range(40000):
        err = {h: total(h) - targets[h][1] for h in range(24)}
        worst = max(abs(v) for v in err.values())
        if worst < best:
            best = worst
        if worst <= 0.02:
            return worst
        surplus = max(err, key=lambda h: err[h])
        deficit = min(err, key=lambda h: err[h])
        need = min(err[surplus], -err[deficit])
        a_list = sorted(buckets[surplus], key=lambda it: it["price"])
        b_list = sorted(buckets[deficit], key=lambda it: it["price"])
        if not a_list or not b_list:
            return best
        candidate, gap = None, None
        j = 0
        for a in a_list:
            while j + 1 < len(b_list) and (a["price"] - b_list[j + 1]["price"]) >= need:
                j += 1
            for k in {j, min(j + 1, len(b_list) - 1), 0}:
                delta = abs((a["price"] - b_list[k]["price"]) - need)
                if gap is None or delta < gap:
                    candidate, gap = (a, b_list[k]), delta
        if candidate is None or gap is None or gap > need:
            return best
        a, b = candidate
        buckets[surplus].remove(a)
        buckets[deficit].remove(b)
        buckets[deficit].append(a)
        buckets[surplus].append(b)
    return best


def assign_machines(buckets: dict[int, list[dict[str, Any]]],
                    machine_amount: dict[str, float]) -> dict[tuple[int, str], list[dict[str, Any]]]:
    cells: dict[tuple[int, str], list[dict[str, Any]]] = {(h, m): []
                                                         for h in range(24) for m in machine_amount}
    rem = dict(machine_amount)
    for hour in range(24):
        for item in sorted(buckets[hour], key=lambda it: it["price"], reverse=True):
            machine = max(rem, key=lambda m: rem[m])
            cells[(hour, machine)].append(item)
            rem[machine] -= item["price"]

    def amount(machine: str) -> float:
        return sum(item["price"] for (h, m), bucket in cells.items() if m == machine for item in bucket)

    best = max(abs(amount(m) - machine_amount[m]) for m in machine_amount)
    for _ in range(20000):
        err = {m: amount(m) - machine_amount[m] for m in machine_amount}
        worst = max(abs(v) for v in err.values())
        if worst < best:
            best = worst
        if worst <= 0.02:
            return cells
        surplus = max(err, key=lambda m: err[m])
        deficit = min(err, key=lambda m: err[m])
        need = min(err[surplus], -err[deficit])
        for hour in range(24):
            a_list = sorted(cells[(hour, surplus)], key=lambda it: it["price"])
            b_list = sorted(cells[(hour, deficit)], key=lambda it: it["price"])
            if not a_list or not b_list:
                continue
            candidate, gap = None, None
            j = 0
            for a in a_list:
                while j + 1 < len(b_list) and (a["price"] - b_list[j + 1]["price"]) >= need:
                    j += 1
                for k in {j, min(j + 1, len(b_list) - 1), 0}:
                    delta = abs((a["price"] - b_list[k]["price"]) - need)
                    if gap is None or delta < gap:
                        candidate, gap = (a, b_list[k]), delta
            if candidate and gap is not None and gap <= need:
                a, b = candidate
                cells[(hour, surplus)].remove(a)
                cells[(hour, deficit)].remove(b)
                cells[(hour, deficit)].append(a)
                cells[(hour, surplus)].append(b)
                break
        else:
            return cells
    return cells


def build_users(machine_users: dict[str, int], dorm_users: dict[str, int],
                dorm_machines: dict[str, list[str]], total_users: int,
                rng: random.Random) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    next_id = 0
    used: set[str] = set()

    def fresh(n: int) -> list[str]:
        nonlocal next_id
        ids = [f"U{next_id + i:05d}" for i in range(n)]
        next_id += n
        used.update(ids)
        return ids

    for group, machines in dorm_machines.items():
        if len(machines) < 2:
            continue
        overlap = sum(machine_users[m] for m in machines) - dorm_users[group]
        shared = fresh(max(overlap, 0))
        for index, machine in enumerate(machines):
            extra = machine_users[machine] - len(shared)
            mapping[machine] = list(shared) + fresh(max(extra, 0))

    singles = [m for group, machines in dorm_machines.items() if len(machines) == 1 for m in machines]
    remaining = total_users - len(used)
    deck = [f"U{900000 + i:05d}" for i in range(max(remaining, 0))]
    for machine in singles:
        size = machine_users[machine]
        chosen : list[str] = []
        while len(chosen) < size:
            if deck:
                candidate = deck.pop()
                if candidate not in chosen:
                    chosen.append(candidate)
            else:
                pool = [u for u in used if u not in chosen] or list(used)
                chosen.append(rng.choice(pool))
        mapping[machine] = chosen
    return mapping


def write_csv(path: Path, header: list[str], rows: list[list[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def _shift(start_date: str, days: int) -> str:
    from datetime import date, timedelta
    return (date.fromisoformat(start_date) + timedelta(days=days)).isoformat()


def dorm_machines_of(machines: list[str], roster_names: list[str]) -> dict[str, list[str]]:
    """软件十一栋女 → 十一栋 → 属于「十栋+十一栋」组（按用户表里的写法合并）。"""
    result: dict[str, list[str]] = {name: [] for name in roster_names}
    for machine in machines:
        token = machine.replace("软件", "").rstrip("男女")
        for name in roster_names:
            if token in name.split("+"):
                result[name].append(machine)
                break
    return result


def build(golden: dict[str, Any], seed: int = 20261108) -> dict[str, Any]:
    rng = random.Random(seed)
    roster_names = list(golden["dorms"]["p1"])
    machines = list(golden["machines"]["p1"])
    dorm_machines = dorm_machines_of(machines, roster_names)
    report: dict[str, Any] = {"periods": {}, "dorm_machines": dorm_machines}
    category_rows: dict[str, str] = {}

    for period_key, (start_date, index) in PERIODS.items():
        tag = "p1" if index == 1 else "p2"
        cats, bands = golden["cats"][tag], golden["bands"][tag]
        hour_targets = golden["hours"][tag]
        machine_target = {m: golden["machines"][tag][m][0] for m in machines}
        machine_user = {m: golden["machines"][tag][m][1] for m in machines}

        cat_qty = {c: v[1] for c, v in cats.items()}
        cat_amount = {c: v[2] for c, v in cats.items()}
        band_qty = {b: v[1] for b, v in bands.items()}
        band_amount = {b: v[2] for b, v in bands.items()}

        # 权重 = 品类自身档位偏好 × 该档金额隐含的销量份额
        band_implied = {band: band_amount[band] / ((lo + hi) / 2)
                        for band, (lo, hi) in BAND_RANGES.items()}
        weights = {}
        for cat, (_sku, qty, amount) in cats.items():
            mean = amount / max(qty, 1)
            for band, (lo, hi) in BAND_RANGES.items():
                if lo <= mean <= hi:
                    preference = 1.0
                else:
                    distance = min(abs(mean - lo), abs(mean - hi))
                    preference = 0.02 ** (1 + distance)
                weights[(cat, band)] = preference * band_implied[band]
        x = ipf_round(cat_qty, band_qty, weights)
        x, y, amount_residual, band_residual = amount_matrix(
            x, cat_qty, band_qty, cat_amount, band_amount)
        sku_counts = ipf_round({c: v[0] for c, v in cats.items() if v[0] > 0},
                               {b: v[0] for b, v in bands.items()})
        sku_counts = {(c, b): min(v, x[(c, b)]) for (c, b), v in sku_counts.items() if x[(c, b)] > 0}
        sku_counts = {(c, b): min(v, x[(c, b)]) for (c, b), v in sku_counts.items() if x[(c, b)] > 0}
        skus = make_skus(x, y, sku_counts)
        topup_residual = topup_prices(skus, y)
        band_rebalance = rebalance_bands(skus, cat_amount, band_amount)
        for sku in skus:
            category_rows[sku["name"]] = sku["cat"]

        items = expand_items(skus)
        hour_buckets = greedy_assign(items, {h: (hour_targets[h][0], hour_targets[h][1])
                                             for h in range(24)}, fixed_qty=True)
        hour_error = repair_hours(hour_buckets, {h: (hour_targets[h][0], hour_targets[h][1])
                                                 for h in range(24)})
        cells = assign_machines(hour_buckets, machine_target)

        dorm_user_target = {name: golden["dorms"][tag][name][3] for name in roster_names}
        total_users = int(golden["core"]["消费人数"][index - 1])
        user_map = build_users(machine_user, dorm_user_target, dorm_machines, total_users, rng)
        orders_target = int(golden["core"]["订单数"][index - 1])

        all_items: list[dict[str, Any]] = []
        odd_baskets = 0
        for machine in machines:
            users = user_map[machine]
            machine_items: list[dict[str, Any]] = []
            for hour in range(24):
                for item in cells[(hour, machine)]:
                    record = dict(item)
                    record["hour"] = hour
                    record["machine"] = machine
                    machine_items.append(record)
            if len(machine_items) < 2 * len(users):
                raise RuntimeError(f"{machine} 件数 {len(machine_items)} 不足以覆盖 "
                                   f"{len(users)} 个用户（每人至少 2 件）")
            for index, record in enumerate(machine_items):
                record["user"] = users[(index // 2) % len(users)]
            baskets: dict[str, int] = defaultdict(int)
            for record in machine_items:
                baskets[record["user"]] += 1
            odd_baskets += sum(1 for size in baskets.values() if size % 2)
            all_items.extend(machine_items)

        # 订单：先按 (机器,用户) 两件一单，再拆出单件单凑够订单数
        by_user: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for item in all_items:
            by_user[(item["machine"], item["user"])].append(item)
        pairs: list[list[dict[str, Any]]] = []
        for key in sorted(by_user):
            bucket = by_user[key]
            for i in range(0, len(bucket), 2):
                pairs.append(bucket[i:i + 2])
        orders: list[list[dict[str, Any]]] = [list(pair) for pair in pairs]
        need = orders_target - len(orders)
        cursor = 0
        limit = len(orders)
        while need > 0 and cursor < limit:
            if len(orders[cursor]) == 2:
                orders.append([orders[cursor].pop()])
                need -= 1
            cursor += 1
        if need != 0 or len(orders) != orders_target:
            raise RuntimeError(f"订单数无法命中：{len(orders)} != {orders_target}（还差 {need} 单）")

        rows: list[list[Any]] = []
        for order_index, order in enumerate(orders, start=1):
            order_id = f"{period_key}-{order_index:06d}"
            day = rng.randrange(DAY_COUNT)
            for item in order:
                rows.append([f"{_shift(start_date, day)} {item['hour']:02d}:{rng.randrange(60):02d}:"
                             f"{rng.randrange(60):02d}", item["machine"], item["name"], 1,
                             round(item["price"], 2), item["user"], order_id])
        write_csv(OUT_DIR / period_key / "sales_detail.csv",
                  ["交易时间", "机器编号", "商品名称", "销售数量", "销售金额", "用户标识", "订单标识"],
                  rows)

        hour_sum = defaultdict(float)
        machine_sum = defaultdict(float)
        cat_sum = defaultdict(float)
        band_sum = defaultdict(float)
        for item in all_items:
            hour_sum[item["hour"]] += item["price"]
            machine_sum[item["machine"]] += item["price"]
            cat_sum[item["cat"]] += item["price"]
            band_sum[item["band"]] += item["price"]
        report["periods"][period_key] = {
            "rows": len(rows), "orders": len(orders), "items": len(all_items),
            "hour_amount_max_err": round(max(abs(hour_sum[h] - hour_targets[h][1])
                                             for h in range(24)), 2),
            "hour_qty_ok": all(len(hour_buckets[h]) == hour_targets[h][0] for h in range(24)),
            "machine_amount_max_err": round(max(abs(machine_sum[m] - machine_target[m])
                                                for m in machines), 2),
            "cat_amount_max_err": round(max(abs(cat_sum[c] - cat_amount[c]) for c in cat_qty), 2),
            "band_amount_max_err": round(max(abs(band_sum[b] - band_amount[b]) for b in bands), 2),
            "amount_residual_cat": amount_residual,
            "amount_residual_band": band_residual,
            "topup_residual": topup_residual,
            "band_rebalance_residual": band_rebalance,
            "odd_baskets": odd_baskets,
            "repair_hour_error": round(hour_error, 2),
        }

    write_csv(OUT_DIR / "categories.csv", ["商品名称", "品类"],
              [[name, cat] for name, cat in sorted(category_rows.items())])
    write_csv(OUT_DIR / "roster.csv", ["楼栋", "性别", "老生人数", "新生人数"],
              [[name, golden["dorms"]["p1"][name][4], golden["dorms"]["p1"][name][0],
                golden["dorms"]["p1"][name][1]] for name in roster_names])
    write_csv(OUT_DIR / "machine_map.csv", ["机器编号", "楼栋"],
              [[m, next((name.split("+")[0] for name in roster_names
                         if m.replace("软件", "").rstrip("男女") in name.split("+")), "")] for m in machines])
    return report


if __name__ == "__main__":
    print(json.dumps(build(load_golden()), ensure_ascii=False, indent=2))
