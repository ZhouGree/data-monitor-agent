"""
D16 异常检测器: 用规则扫描全量数据, 筛出异常清单给 D17 的 AI

三条规则(对应 D15 剧本的三类异常):
  规则1 销量暴跌: 当日销售额 < 前7天日均 x 60%(用7天均值抗周末波动和噪音)
  规则2 退款激增: 当日退款率(退款/订单) > 15%(正常约3%)
  规则3 差评爆发: 单品类单日差评(评分<=2) >= 3 条(正常天几乎为0)

设计要点(面试可讲):
  1. 规则只报"哪里不对", 不猜"为什么" -> 归因是 D17 AI 的事, 职责分离
  2. 阈值集中到 Thresholds, 默认值放文件顶部; 评测脚本可按档位传入不同阈值
  3. 连续多天触发同一规则 -> 聚合成一个"事件",
     否则精华暴跌3天产生3条告警, AI 要重复分析同一事故3次
  4. 同一(品类, 日期区间)触发多条规则 -> 再合并一次,
     因为一个根因常常同时打穿几条规则(如过敏批次既压销量又爆差评),
     不合并的话 5 条告警其实只有 3 个事故, AI 会重复花钱分析同一个根因
"""

from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"


@dataclass(frozen=True)
class Thresholds:
    """三条规则的阈值组合, 供评测脚本按档位扫描

    默认值 = 线上一直在用的那一组, 不传就按默认跑, 老调用方零改动。
    """

    sales_drop_ratio: float = 0.60     # 规则1: 销量 < 前7天日均 x 60%
    refund_rate_limit: float = 0.15    # 规则2: 退款率 > 15%
    bad_review_count: int = 3          # 规则3: 单日差评 >= 3 条


DEFAULT_THRESHOLDS = Thresholds()

# ===== 兼容原有常量(外部若 import 过这三个名字, 行为不变) =====
SALES_DROP_RATIO = DEFAULT_THRESHOLDS.sales_drop_ratio
REFUND_RATE_LIMIT = DEFAULT_THRESHOLDS.refund_rate_limit
BAD_REVIEW_COUNT = DEFAULT_THRESHOLDS.bad_review_count


def load_data() -> tuple[pd.DataFrame, pd.DataFrame]:
    sales = pd.read_csv(DATA_DIR / "sales.csv", parse_dates=["date"])
    reviews = pd.read_csv(DATA_DIR / "reviews.csv", parse_dates=["date"])
    return sales, reviews


def detect_sales_drop(sales: pd.DataFrame,
                      thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """规则1: 销量环比暴跌(对比前7天日均, 而不是只对比昨天)"""
    hits = []
    ratio = thresholds.sales_drop_ratio
    for (cat, day), grp in sales.groupby(["category", "date"]):
        day_sales = grp["sales"].sum()
        prev = sales[(sales["category"] == cat)
                     & (sales["date"] < day)
                     & (sales["date"] >= day - timedelta(days=7))]["sales"].sum() / 7

        if prev > 0 and day_sales < prev * ratio:
            hits.append({
                "type": "销量暴跌", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日销售额 {day_sales:.0f} 元, 前7天日均 {prev:.0f} 元, "
                          f"仅为 {day_sales / prev:.0%}(阈值 {ratio:.0%})",
            })
    return hits


def detect_sales_drop_naive(sales: pd.DataFrame,
                            ratio: float = SALES_DROP_RATIO) -> list[dict]:
    """对照方案: 只和前一天比, 不看 7 日均值

    周末销量上浮 30%、大促当天放大, 第二天回落时很容易被误判成"暴跌",
    这条规则放在评测里当基线, 用来证明滑动均值的作用。
    """
    hits = []
    for cat, grp in sales.groupby("category"):
        grp = grp.sort_values("date").reset_index(drop=True)
        for i in range(1, len(grp)):
            cur, prev = grp.loc[i, "sales"], grp.loc[i - 1, "sales"]
            if prev > 0 and cur < prev * ratio:
                hits.append({
                    "type": "销量暴跌", "category": cat,
                    "date": grp.loc[i, "date"].strftime("%Y-%m-%d"),
                    "detail": f"当日销售额 {cur:.0f} 元, 前一日 {prev:.0f} 元, "
                              f"仅为 {cur / prev:.0%}(阈值 {ratio:.0%}, 单日环比)",
                })
    return hits


def detect_refund_spike(sales: pd.DataFrame,
                        thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """规则2: 退款率激增"""
    hits = []
    limit = thresholds.refund_rate_limit
    for (cat, day), grp in sales.groupby(["category", "date"]):
        orders = grp["orders"].sum()
        refunds = grp["refunds"].sum()
        rate = refunds / orders if orders else 0

        if rate > limit:
            hits.append({
                "type": "退款激增", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日退款率 {rate:.0%}(正常约3%), "
                          f"退款 {refunds:.0f} 单 / 订单 {orders:.0f} 单",
            })
    return hits


def detect_bad_reviews(reviews: pd.DataFrame,
                       thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """规则3: 差评爆发(销量查不出的口碑问题, 靠这条规则兜底)"""
    hits = []
    min_count = thresholds.bad_review_count
    bad = reviews[reviews["rating"] <= 2]
    for (cat, day), grp in bad.groupby(["category", "date"]):
        n = len(grp)
        if n >= min_count:
            hits.append({
                "type": "差评爆发", "category": cat,
                "date": day.strftime("%Y-%m-%d"),
                "detail": f"当日差评 {n} 条(正常天几乎为0, 阈值 {min_count} 条)",
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


def merge_cross_rule(events: list[dict]) -> list[dict]:
    """同一(品类, 起止日期)命中多条规则 -> 合并成一个事件

    一个根因往往同时打穿多条规则: 批次过敏既让销量掉, 又让差评爆,
    分开报就是同一事故被 AI 重复分析两次。
    """
    groups: dict[tuple, list[dict]] = {}
    for e in events:
        groups.setdefault((e["category"], e["start"], e["end"]), []).append(e)

    merged = []
    for (cat, start, end), items in groups.items():
        if len(items) == 1:
            merged.append(items[0])
            continue
        types = [i["type"] for i in items]
        merged.append({
            "category": cat,
            "type": " + ".join(types),      # 例如 "销量暴跌 + 差评爆发"
            "rules": types,                 # 明细留给报告渲染
            "start": start,
            "end": end,
            "detail": "; ".join(i["detail"] for i in items),
        })

    merged.sort(key=lambda e: (e["start"], e["category"]))
    return merged


def raw_hits(sales: pd.DataFrame, reviews: pd.DataFrame,
             naive: bool = False,
             thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """只跑规则、不做任何合并的原始告警, 用于评测里对比合并前后的数量"""
    sales_rule = (detect_sales_drop_naive(sales, thresholds.sales_drop_ratio) if naive
                  else detect_sales_drop(sales, thresholds))
    return (sales_rule
            + detect_refund_spike(sales, thresholds)
            + detect_bad_reviews(reviews, thresholds))


def detect_from(sales: pd.DataFrame, reviews: pd.DataFrame,
                naive: bool = False,
                thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """对给定数据跑完整流程, 方便评测脚本换数据集复用"""
    return merge_cross_rule(
        merge_consecutive(raw_hits(sales, reviews, naive, thresholds)))


def detect_all(thresholds: Thresholds = DEFAULT_THRESHOLDS) -> list[dict]:
    """主入口: 跑三条规则 -> 合并连续告警 -> 合并跨规则同根因, 返回事件清单给 D17"""
    sales, reviews = load_data()
    return detect_from(sales, reviews, thresholds=thresholds)


if __name__ == "__main__":
    events = detect_all()
    print(f"共检出 {len(events)} 个异常事件\n")
    for i, e in enumerate(events, 1):
        period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
        print(f"[{i}] {e['category']} | {e['type']} | {period}")
        print(f"    {e['detail']}\n")
