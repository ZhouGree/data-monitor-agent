"""
D15 数据源生成器: 模拟化妆品电商平台的监控数据流

产出两份 CSV(存在 data/ 目录):
  sales.csv    每日每品类: 日期, 品类, 销售额, 订单数, 退款数
  reviews.csv  每日评论:   日期, 品类, 评分, 评论内容
另外产出 ground_truth.json: 每起剧本事故的品类/起止日期/原因/严重度,
供 evaluate.py 计算检出率和误报率。

三个设计决策(面试可讲):
  1. random.Random(seed) 固定随机种子 -> 每次生成一模一样的数据,
     后面检测/归因/评测的结果才可复现
  2. 按"剧本"注入异常(销量暴跌/退款激增/差评集中), 每起都把
     "原因线索"埋在评论里 -> AI 归因时有据可查, 而不是凭空编
  3. 这个文件是整个项目唯一"造假"的地方: 真实场景换成定时拉取
     平台 API / 查数据库, 后面的检测层和 AI 层一行不用改
"""

import csv
import json
import random
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"

# ===== 品类基线: 正常情况下每个品类每天的销售水平 =====
CATEGORIES = {
    "精华": {"sales": 8000, "orders": 40},
    "面膜": {"sales": 6000, "orders": 60},
    "水乳": {"sales": 5000, "orders": 30},
    "防晒": {"sales": 3000, "orders": 20},
}

START_DATE = date(2026, 9, 19)       # 生成 30 天: 9-19 ~ 10-18
DAYS = 30

# ===== 异常剧本: 什么时间, 哪个品类, 出什么事, 持续几天 =====
# severity=high 的事故必须被检出; low 是边界样本, 允许漏报
ANOMALIES = [
    {
        "category": "精华",
        "start": date(2026, 10, 8), "days": 3,
        "sales_factor": 0.35,          # 销量跌到平时的 35%(症状)
        "bad_review_reason": "过敏",    # 线索: 评论里集中出现过敏投诉
        "severity": "high",
    },
    {
        "category": "面膜",
        "start": date(2026, 10, 12), "days": 2,
        "refund_factor": 8,            # 退款率 3% -> 24%, 退款数激增(症状)
        "bad_review_reason": "破损",    # 线索: 包装破损投诉
        "severity": "high",
    },
    {
        "category": "水乳",
        "start": date(2026, 10, 15), "days": 2,
        "sales_factor": 0.8,           # 销量只跌 20%, 藏在正常波动边缘
        "bad_review_reason": "物流",    # 线索: 物流太慢投诉(主要靠差评发现)
        "severity": "high",
    },
]

NORMAL_GOOD = [
    "很好用，已经回购第三次了",
    "质地清爽不油腻，适合夏天",
    "包装很好，物流也快",
    "用了一个月，皮肤状态明显变好",
    "客服态度很好，购物体验不错",
]

BAD_REVIEWS = {
    "过敏": [
        "用了两天脸上泛红刺痛，停用后才好转",
        "涂完火辣辣的，疑似过敏，再也不买了",
        "敏感肌慎入！用完起了一脸闭口",
    ],
    "破损": [
        "面膜袋漏气，精华液全干了",
        "收到时盒子压扁了，里面的膜布破了",
        "包装破损完全没法用，已申请退款",
    ],
    "物流": [
        "下单五天才发货，物流太慢了",
        "等了一周才到，比承诺时间晚三天",
        "物流信息三天不更新，体验很差",
    ],
}


def _find_anomaly(day: date, category: str, anomalies: list[dict]) -> dict | None:
    """查剧本: 这天这个品类有没有异常, 有就返回剧本条目"""
    for a in anomalies:
        end = a["start"] + timedelta(days=a["days"] - 1)
        if a["category"] == category and a["start"] <= day <= end:
            return a
    return None


def _find_promo(day: date, category: str, promos: list[dict]) -> dict | None:
    """查大促日历: 大促日销售额按倍数放大, 用于制造"大促后回落"的误报样本"""
    for p in promos:
        end = p["start"] + timedelta(days=p["days"] - 1)
        if p["category"] == category and p["start"] <= day <= end:
            return p
    return None


def gen_sales(start: date, days: int, anomalies: list[dict],
              promos: list[dict] | None = None, seed: int = 42) -> list[list]:
    """生成销售流水: 基线 x 周末系数 x 随机波动 x 异常系数 x 大促系数"""
    rng = random.Random(seed)
    promos = promos or []
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)
        weekend = day.weekday() >= 5            # 5=周六 6=周日

        for cat, base in CATEGORIES.items():
            factor = 1.3 if weekend else 1.0     # 周末销量上浮 30%
            noise = rng.uniform(0.85, 1.15)      # 每天 ±15% 随机波动

            anomaly = _find_anomaly(day, cat, anomalies)
            if anomaly and "sales_factor" in anomaly:
                factor *= anomaly["sales_factor"]

            promo = _find_promo(day, cat, promos)
            if promo:
                factor *= promo["factor"]

            sales = base["sales"] * factor * noise
            orders = base["orders"] * factor * noise

            refund_rate = 0.03                   # 正常退款率 3%
            if anomaly and "refund_factor" in anomaly:
                refund_rate *= anomaly["refund_factor"]
            refunds = orders * refund_rate

            rows.append([day.isoformat(), cat,
                         round(sales), round(orders), round(refunds)])
    return rows


def gen_reviews(start: date, days: int, anomalies: list[dict],
                seed: int = 42) -> list[list]:
    """生成评论: 正常期以好评为主, 异常期差评集中爆发"""
    rng = random.Random(seed)
    rows = []
    for i in range(days):
        day = start + timedelta(days=i)

        for cat in CATEGORIES:
            anomaly = _find_anomaly(day, cat, anomalies)

            if anomaly and anomaly.get("bad_review_reason"):
                # 异常期: 差评集中爆发(评分 1-2 星, 内容全是投诉线索)
                for _ in range(rng.randint(5, 8)):
                    rows.append([day.isoformat(), cat,
                                 rng.choice([1, 1, 2]),
                                 rng.choice(BAD_REVIEWS[anomaly["bad_review_reason"]])])
                # 混一条正常好评, 更接近真实数据
                rows.append([day.isoformat(), cat, 5, rng.choice(NORMAL_GOOD)])
            else:
                # 正常期: 好评为主
                for _ in range(rng.randint(2, 4)):
                    rating = rng.choices([5, 4, 3], weights=[7, 2, 1])[0]
                    rows.append([day.isoformat(), cat, rating, rng.choice(NORMAL_GOOD)])
    return rows


def build_ground_truth(anomalies: list[dict]) -> list[dict]:
    """把剧本转成评测用的标准答案, 严重度决定是否计入召回率"""
    return [
        {
            "id": f"A{i + 1}",
            "category": a["category"],
            "start": a["start"].isoformat(),
            "end": (a["start"] + timedelta(days=a["days"] - 1)).isoformat(),
            "reason": a.get("bad_review_reason") or "",
            "severity": a.get("severity", "high"),
        }
        for i, a in enumerate(anomalies)
    ]


def write_dataset(out_dir: Path, sales_rows: list[list],
                  review_rows: list[list], truth: list[dict]) -> None:
    """写 CSV + ground_truth.json, 让检测结果和标准答案放在同一目录"""
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(out_dir / "sales.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "category", "sales", "orders", "refunds"])
        writer.writerows(sales_rows)
    with open(out_dir / "reviews.csv", "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["date", "category", "rating", "content"])
        writer.writerows(review_rows)
    with open(out_dir / "ground_truth.json", "w", encoding="utf-8") as f:
        json.dump(truth, f, ensure_ascii=False, indent=2)


def generate(out_dir: Path = DATA_DIR, start: date = START_DATE, days: int = DAYS,
             anomalies: list[dict] | None = None,
             promos: list[dict] | None = None, seed: int = 42) -> tuple[list, list, list]:
    """生成一套完整数据 + 标准答案, 返回 (sales_rows, review_rows, truth)"""
    anomalies = ANOMALIES if anomalies is None else anomalies
    sales_rows = gen_sales(start, days, anomalies, promos, seed)
    review_rows = gen_reviews(start, days, anomalies, seed)
    truth = build_ground_truth(anomalies)
    write_dataset(out_dir, sales_rows, review_rows, truth)
    return sales_rows, review_rows, truth


def main():
    sales_rows, review_rows, truth = generate()
    print(f"已生成 {len(sales_rows)} 条销售记录, {len(review_rows)} 条评论, "
          f"{len(truth)} 条标准答案")

    # ===== 自检: 三处剧本异常是否按预期落进数据 =====
    print("\n[1] 精华 10-06~10-11 销售额(预期 10-08 起跌到三成左右):")
    for r in sales_rows:
        if r[1] == "精华" and "2026-10-06" <= r[0] <= "2026-10-11":
            print(f"    {r[0]}  销售额 {r[2]:>6}  订单 {r[3]}")

    print("\n[2] 面膜 10-11~10-14 退款数(预期 10-12 起激增):")
    for r in sales_rows:
        if r[1] == "面膜" and "2026-10-11" <= r[0] <= "2026-10-14":
            print(f"    {r[0]}  退款 {r[4]}")

    print("\n[3] 水乳 10-14~10-17 差评数(预期 10-15 起差评集中):")
    bad = Counter(r[0] for r in review_rows
                  if r[1] == "水乳" and r[2] <= 2 and "2026-10-14" <= r[0] <= "2026-10-17")
    for day in sorted(bad):
        print(f"    {day}  差评 {bad[day]} 条")


if __name__ == "__main__":
    main()
