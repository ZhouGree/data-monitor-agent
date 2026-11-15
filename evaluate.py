"""
D18 检测效果评测: 用带标准答案的数据集衡量检出率、误报和合并效果

两套数据:
  1. 剧本集: data/ 下的 30 天数据, 3 起事故, 用来证明"该检的能检出"
  2. 边界集: 临时生成 60 天, 额外放 1 起轻微下滑(允许漏报) + 1 次大促回落(容易误报),
     用来暴露规则的真实边界, 而不是只挑对自己有利的数据

四个方案对比:
  原始告警(不合并) / 仅连续合并 / 连续+跨规则(当前) / 单日环比+合并(基线)

阈值扫描(新增):
  保守 / 当前 / 宽松 三档, 两套数据集各跑一遍, 看漏报和误报此消彼长,
  用来回答"为什么选这组阈值", 而不是只报一组对自己有利的数字。

运行: python evaluate.py
产出: reports/eval_report.md + 控制台表格
"""

import json
from datetime import date
from pathlib import Path

import pandas as pd

import gen_data
from detector import (Thresholds, detect_from, merge_consecutive, raw_hits)

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
REPORT_PATH = BASE / "reports" / "eval_report.md"

SALES_COLUMNS = ["date", "category", "sales", "orders", "refunds"]
REVIEW_COLUMNS = ["date", "category", "rating", "content"]

# ===== 阈值扫描档位: 保守(少报) / 当前(线上默认) / 宽松(多报) =====
PROFILES = [
    ("保守", Thresholds(sales_drop_ratio=0.50, refund_rate_limit=0.20,
                        bad_review_count=4),
     "阈值收紧, 只报确定性高的事故"),
    ("当前", Thresholds(),
     "线上默认值"),
    ("宽松", Thresholds(sales_drop_ratio=0.70, refund_rate_limit=0.10,
                        bad_review_count=2),
     "阈值放宽, 用误报换召回"),
]


def _load_csv(dirpath: Path) -> tuple[pd.DataFrame, pd.DataFrame]:
    sales = pd.read_csv(dirpath / "sales.csv", parse_dates=["date"])
    reviews = pd.read_csv(dirpath / "reviews.csv", parse_dates=["date"])
    return sales, reviews


def _load_truth(dirpath: Path) -> list[dict]:
    with open(dirpath / "ground_truth.json", encoding="utf-8") as f:
        return json.load(f)


def _frame(rows: list[list], columns: list[str]) -> pd.DataFrame:
    df = pd.DataFrame(rows, columns=columns)
    df["date"] = pd.to_datetime(df["date"])
    return df


def build_edge_scenario() -> tuple[pd.DataFrame, pd.DataFrame, list[dict]]:
    """边界集: 在剧本基础上加两类容易翻车的样本

    1. 轻微下滑(0.8 倍且没有差评线索) -> 低于阈值, 允许漏报
    2. 大促当天放大 3 倍, 第二天回落 -> 单日环比方案会误报
    """
    start, days = date(2026, 9, 19), 60
    anomalies = list(gen_data.ANOMALIES) + [
        {
            "category": "防晒",
            "start": date(2026, 10, 26), "days": 2,
            "sales_factor": 0.80,
            "bad_review_reason": None,      # 没有差评线索, 只能靠销量发现
            "severity": "low",              # 边界样本, 不要求检出
        },
        {
            "category": "面膜",
            "start": date(2026, 11, 5), "days": 2,
            "sales_factor": 0.45,
            "bad_review_reason": "破损",
            "severity": "high",
        },
    ]
    promos = [
        {"category": "水乳", "start": date(2026, 11, 1), "days": 1, "factor": 3.0},
    ]

    sales_rows = gen_data.gen_sales(start, days, anomalies, promos, seed=7)
    review_rows = gen_data.gen_reviews(start, days, anomalies, seed=7)
    return (
        _frame(sales_rows, SALES_COLUMNS),
        _frame(review_rows, REVIEW_COLUMNS),
        gen_data.build_ground_truth(anomalies),
    )


def _overlaps(event: dict, truth: dict) -> bool:
    """同品类 + 日期区间有交集, 就算这次告警对上了这起事故

    原始告警只有单日字段 date, 合并后的事件才有 start/end, 这里统一处理。
    """
    start = event.get("start") or event["date"]
    end = event.get("end") or start
    return (event["category"] == truth["category"]
            and start <= truth["end"]
            and truth["start"] <= end)


def score(events: list[dict], truth: list[dict]) -> dict:
    must_catch = [t for t in truth if t["severity"] == "high"]
    hit = 0
    duplicates = 0
    for t in truth:
        matched = [e for e in events if _overlaps(e, t)]
        if t["severity"] == "high" and matched:
            hit += 1
        duplicates += max(0, len(matched) - 1)          # 同一事故被拆成多条 = 重复
    false_positive = [e for e in events
                      if not any(_overlaps(e, t) for t in truth)]

    return {
        "events": len(events),
        "hit": hit,
        "missed": len(must_catch) - hit,
        "false_positive": len(false_positive),
        "duplicates": duplicates,
        "recall": hit / len(must_catch) if must_catch else 1.0,
        "precision": (len(events) - len(false_positive)) / len(events)
        if events else 1.0,
    }


def modes() -> list[tuple[str, callable, str]]:
    """名字 / 跑法 / 一句话说明"""
    return [
        ("原始告警（不合并）", lambda s, r: raw_hits(s, r),
         "三条规则逐日命中, 不做任何聚合"),
        ("仅连续合并", lambda s, r: merge_consecutive(raw_hits(s, r)),
         "同一品类同一规则连续日期压成一个事件"),
        ("连续+跨规则（当前）", lambda s, r: detect_from(s, r),
         "再把同品类同区间的多条规则合并成一个根因"),
        ("单日环比+合并（基线）", lambda s, r: detect_from(s, r, naive=True),
         "销量只和前一天比, 用来对照滑动均值"),
    ]


def _table(sales, reviews, truth) -> tuple[list[str], dict[str, dict]]:
    lines = ["| 方案 | 事件数 | 命中/应检出 | 漏报 | 误报 | 重复事件 | 召回率 | 精确率 |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    results = {}
    for name, run, _ in modes():
        events = run(sales, reviews)
        s = score(events, truth)
        results[name] = s
        lines.append(
            f"| {name} | {s['events']} | {s['hit']}/{s['hit'] + s['missed']} | "
            f"{s['missed']} | {s['false_positive']} | {s['duplicates']} | "
            f"{s['recall']:.0%} | {s['precision']:.0%} |"
        )
    return lines, results


def _scan_table(sales, reviews, truth) -> tuple[list[str], dict[str, dict]]:
    """阈值扫描: 同一套数据只换阈值, 看召回和误报怎么移动"""
    lines = [
        "| 档位 | 销量阈值 | 退款率阈值 | 差评阈值 | 事件数 | 命中/应检出 | 漏报 | 误报 | 重复 | 召回率 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    results = {}
    for name, th, _ in PROFILES:
        events = detect_from(sales, reviews, thresholds=th)
        s = score(events, truth)
        # 低严重度边界样本是否被检出: 不计入召回, 但能说明"收紧阈值漏掉了什么"
        s["low_detected"] = sum(
            1 for t in truth if t["severity"] == "low"
            and any(_overlaps(e, t) for e in events))
        results[name] = s
        lines.append(
            f"| {name} | {th.sales_drop_ratio:.0%} | {th.refund_rate_limit:.0%} | "
            f"{th.bad_review_count} 条 | {s['events']} | "
            f"{s['hit']}/{s['hit'] + s['missed']} | {s['missed']} | "
            f"{s['false_positive']} | {s['duplicates']} | {s['recall']:.0%} |"
        )
    return lines, results


def main() -> None:
    sales, reviews = _load_csv(DATA_DIR)
    truth = _load_truth(DATA_DIR)

    edge_sales, edge_reviews, edge_truth = build_edge_scenario()

    report = [
        "# 异常检测评测报告",
        "",
        "固定随机种子生成, 重跑结果一致。`应检出` 指 `severity=high` 的事故;",
        "`low` 是边界样本(轻微下滑), 漏报不算失败。",
        "",
    ]

    all_results = {}
    scan_results = {}
    for title, (s, r, t) in {
        "剧本集（30 天 · 3 起事故）": (sales, reviews, truth),
        "边界集（60 天 · 4 起应检出 + 1 起边界 + 1 次大促）": (
            edge_sales, edge_reviews, edge_truth),
    }.items():
        report += [f"## {title}", ""]
        lines, results = _table(s, r, t)
        report += lines + [""]
        all_results[title] = results

        report += ["### 阈值扫描", ""]
        scan_lines, scans = _scan_table(s, r, t)
        report += scan_lines + [""]
        scan_results[title] = scans

    drama_title = "剧本集（30 天 · 3 起事故）"
    edge_title = "边界集（60 天 · 4 起应检出 + 1 起边界 + 1 次大促）"
    drama_scan = scan_results[drama_title]
    edge_scan = scan_results[edge_title]
    base = all_results[edge_title]
    now = base["连续+跨规则（当前）"]
    naive = base["单日环比+合并（基线）"]
    raw = base["原始告警（不合并）"]

    # ===== 结论: 用真实数字说话, 不写形容词 =====
    def _scan_line(name: str, th: Thresholds) -> str:
        d, e = drama_scan[name], edge_scan[name]
        return (
            f"- {name}档（销量 {th.sales_drop_ratio:.0%} / 退款 {th.refund_rate_limit:.0%} / "
            f"差评 {th.bad_review_count} 条）: 剧本集 {d['hit']}/{d['hit'] + d['missed']}、"
            f"漏报 {d['missed']}、误报 {d['false_positive']}；边界集 "
            f"{e['hit']}/{e['hit'] + e['missed']}、漏报 {e['missed']}、"
            f"误报 {e['false_positive']}。"
        )

    missed_names = [f"{n}档" for n, _, _ in PROFILES if edge_scan[n]["missed"] > 0]
    fp_names = [f"{n}档" for n, _, _ in PROFILES if edge_scan[n]["false_positive"] > 0]
    low_names = [f"{n}档" for n, _, _ in PROFILES if edge_scan[n]["low_detected"] > 0]

    con, cur, loo = PROFILES[0][1], PROFILES[1][1], PROFILES[2][1]
    report += [
        "## 阈值扫描结论",
        "",
        _scan_line("保守", con),
        _scan_line("当前", cur),
        _scan_line("宽松", loo),
        "",
        "- 取舍: "
        + (f"边界集上{'、'.join(missed_names)}出现漏报" if missed_names
           else "边界集上三档都没有漏报")
        + "；"
        + (f"{'、'.join(fp_names)}出现误报, 误报数 "
           f"{edge_scan['宽松']['false_positive']} 条" if fp_names
           else "三档都没有误报")
        + "。",
        f"- 弱信号: 低严重度边界样本（防晒 0.8 倍下滑）"
        + (f"只有{'、'.join(low_names)}检出" if low_names
           else "三档都没有检出")
        + "；它按设计不计入召回，只用来展示阈值档位对弱信号的实际差异。",
        f"- 当前档选型依据: 两套数据上分别为 "
        f"{drama_scan['当前']['hit']}/{drama_scan['当前']['hit'] + drama_scan['当前']['missed']}、"
        f"{edge_scan['当前']['hit']}/{edge_scan['当前']['hit'] + edge_scan['当前']['missed']}，"
        f"误报 {edge_scan['当前']['false_positive']} 条、重复 "
        f"{edge_scan['当前']['duplicates']} 条。收紧到 0.50/20%/4 条后误报仍为 "
        f"{edge_scan['保守']['false_positive']} 条、事件数仍为 "
        f"{edge_scan['保守']['events']} 个，没有可量化收益；放宽到 0.70/10%/2 条后"
        f"误报升到 {edge_scan['宽松']['false_positive']} 条、重复升到 "
        f"{edge_scan['宽松']['duplicates']} 条，成本明确。默认阈值同时与历史日报口径一致。",
        "",
        "## 合并与方案对比结论",
        "",
        f"- 当前方案在边界集召回 {now['recall']:.0%}（{now['hit']}/"
        f"{now['hit'] + now['missed']}）, 误报 {now['false_positive']} 条, "
        f"重复 {now['duplicates']} 条；这组数字只对应默认阈值, 不是全阈值下的表现。",
        f"- 单日环比基线误报 {naive['false_positive']} 条, "
        f"比当前方案多 {naive['false_positive'] - now['false_positive']} 条; "
        f"差异来自大促当天放大 3 倍、第二天回落被误判成暴跌。",
        f"- 合并把原始 {raw['events']} 条告警压成 {now['events']} 个事件, "
        f"AI 调查次数同比减少 {raw['events'] - now['events']} 次。",
        "",
    ]

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text("\n".join(report), encoding="utf-8")

    print("\n".join(report))
    print(f"\n[已写入] {REPORT_PATH}")


if __name__ == "__main__":
    main()
