"""
D16 异常检测器: 用规则扫描全量数据, 筛出异常清单给 D17 的 AI

三条规则(对应 D15 剧本的三类异常):
  规则1 销量暴跌: 当日销售额 < 前7天日均 x 60%(用7天均值抗周末波动和噪音)
  规则2 退款激增: 当日退款率(退款/订单) > 15%(正常约3%)
  规则3 差评爆发: 单品类单日差评(评分<=2) >= 3 条(正常天几乎为0)

设计要点(面试可讲):
  1. 规则只报"哪里不对", 不猜"为什么" -> 归因是 D17 AI 的事, 职责分离
  2. 阈值全部提成正 常量放文件顶部, 调参不用翻代码
  3. 连续多天触发同一规则 -> 聚合成一个"事件",
     否则精华暴跌3天产生3条告警, AI 要重复分析同一事故3次
"""

from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

# ===== 阈值集中放这里, 调参只改这一处 =====
SALES_DROP_RATIO = 0.60     # 规则1: 销量 < 前7天日均 x 60%
REFUND_RATE_LIMIT = 0.15    # 规则2: 退款率 > 15%
BAD_REVIEW_COUNT = 3        # 规则3: 单日差评 >= 3 条


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    sales = pd.read_csv(DATA_DIR / "sales.csv", parse_dates=["date"])
    reviews = pd.read_csv(DATA_DIR / "reviews.csv", parse_dates=["date"])
    return sales, reviews


def detect_sales_drop(sales: pd.DataFrame) -> list[dict]:
    """规则1: 销量环比暴跌(对比前7天日均, 而不是只对比昨天)"""
    hits = []
    for (cat, day), grp in sales.groupby(["category", "date"]):
        day_sales = grp["sales"].sum()
        prev = sales[(sales["category"] == cat)
                     & (sales["date"] < day)
                     & (sales["date"] >= day - timedelta(days=7))]["sales"].sum() / 7

        if prev > 0 and day_sales < prev * SALES_DROP_RATIO:
            hits.append({
                "type": "销量暴跌", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日销售额 {day_sales:.0f} 元, 前7天日均 {prev:.0f} 元, "
                          f"仅为 {day_sales / prev:.0%}(阈值 {SALES_DROP_RATIO:.0%})",
            })
    return hits


def detect_refund_spike(sales: pd.DataFrame) -> list[dict]:
    """规则2: 退款率激增"""
    hits = []
    for (cat, day), grp in sales.groupby(["category", "date"]):
        orders = grp["orders"].sum()
        refunds = grp["refunds"].sum()
        rate = refunds / orders if orders else 0

        if rate > REFUND_RATE_LIMIT:
            hits.append({
                "type": "退款激增", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日退款率 {rate:.0%}(正常约3%), "
                          f"退款 {refunds:.0f} 单 / 订单 {orders:.0f} 单",
            })
    return hits


def detect_bad_reviews(reviews: pd.DataFrame) -> list[dict]:
    """规则3: 差评爆发(销量查不出的口碑问题, 靠这条规则兜底)"""
    hits = []
    bad = reviews[reviews["rating"] <= 2]
    for (cat, day), grp in bad.groupby(["category", "date"]):
        n = len(grp)
        if n >= BAD_REVIEW_COUNT:
            hits.append({
                "type": "差评爆发", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日差评 {n} 条(正常天几乎为0)",
            })
    return hits


def merge_consecutive(hits: list[dict]) -> list[dict]:
    """同一(品类, 规则)连续日期的告警 -> 合并成一个事件"""
    groups: dict[tuple, list[dict]] = {}
    for h in hits:
        groups.setdefault((h["category"], h["type"]), []).append(h)

    events = []
    for (cat, typ), items in groups.items():
        items.sort(key=lambda x: x["date"])
        event = {"category": cat, "type": typ,
                 "start": items[0]["date"], "end": items[0]["date"],
                 "detail": items[0]["detail"]}
        for h in items[1:]:
            gap = (date.fromisoformat(h["date"])
                   - date.fromisoformat(event["end"])).days
            if gap == 1:
                event["end"] = h["date"]        # 日期连续 -> 拉长事件区间
            else:
                events.append(event)            # 断开 -> 开新事件
                event = {"category": cat, "type": typ,
                         "start": h["date"], "end": h["date"],
                         "detail": h["detail"]}
        events.append(event)

    events.sort(key=lambda e: e["start"])
    return events


def detect_all() -> list[dict]:
    """主入口: 跑三条规则 + 合并连续告警, 返回事件清单给 D17"""
    sales, reviews = load_data()
    hits = (detect_sales_drop(sales)
            + detect_refund_spike(sales)
            + detect_bad_reviews(reviews))
    return merge_consecutive(hits)


if __name__ == "__main__":
    events = detect_all()
    print(f"共检出 {len(events)} 个异常事件\n")
    for i, e in enumerate(events, 1):
        period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
        print(f"[{i}] {e['category']} | {e['type']} | {period}")
        print(f"    {e['detail']}\n")
