"""
D17 执行层: 给 AI 的数据查询工具

给 AI 配两个"查数"工具, 让它自主取证:
  query_sales    查某品类某时间段的每日销售数据(销售额/订单数/退款数)
  query_reviews  查某品类某时间段的用户评论(评分/内容)

和客服项目的区别: 工具背后是 pandas 查 CSV,
真实项目换成 SQL / 数据仓库, 工具说明书和 Agent 一行不用改
"""

import json
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

# ===== 一、工具说明书(AI 只读这个来决定调不调、怎么调) =====
tools = [
    {
        "type": "function",
        "function": {
            "name": "query_sales",
            "description": "查询指定品类在日期区间内的每日销售数据(销售额/订单数/退款数)。"
                           "需要对比正常与异常时期的销量、退款表现时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "品类名, 只能是: 精华、面膜、水乳、防晒"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "开始日期, 如 2026-10-01"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "结束日期, 如 2026-10-10"
                    }
                },
                "required": ["category", "start_date", "end_date"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_reviews",
            "description": "查询指定品类在日期区间内的用户评论(评分+评论内容)。"
                           "调查异常原因、寻找用户投诉线索时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "品类名, 只能是: 精华、面膜、水乳、防晒"
                    },
                    "start_date": {
                        "type": "string",
                        "description": "开始日期, 如 2026-10-08"
                    },
                    "end_date": {
                        "type": "string",
                        "description": "结束日期, 如 2026-10-10"
                    }
                },
                "required": ["category", "start_date", "end_date"]
            }
        }
    }
]


# ===== 二、函数实现(真正干活: pandas 查 CSV) =====
def _query_sales(category: str, start_date: str, end_date: str) -> str:
    df = pd.read_csv(DATA_DIR / "sales.csv", parse_dates=["date"])
    mask = ((df["category"] == category)
            & (df["date"] >= start_date) & (df["date"] <= end_date))
    rows = df[mask].sort_values("date")

    if rows.empty:
        return f"{category} 在 {start_date}~{end_date} 无销售数据"

    lines = ["日期 | 销售额 | 订单数 | 退款数"]
    for _, r in rows.iterrows():
        lines.append(f"{r['date'].date()} | {r['sales']:.0f} | {r['orders']:.0f} | {r['refunds']:.0f}")
    return "\n".join(lines)


def _query_reviews(category: str, start_date: str, end_date: str) -> str:
    df = pd.read_csv(DATA_DIR / "reviews.csv", parse_dates=["date"])
    mask = ((df["category"] == category)
            & (df["date"] >= start_date) & (df["date"] <= end_date))
    rows = df[mask].sort_values("rating")     # 差评排前面, 方便 AI 先看到投诉

    if rows.empty:
        return f"{category} 在 {start_date}~{end_date} 无评论数据"

    MAX_ROWS = 50                             # 保险丝: 防止返回内容超长撑爆上下文
    lines = [f"共 {len(rows)} 条评论(格式 评分 | 内容), 最多显示 {MAX_ROWS} 条:"]
    for _, r in rows.head(MAX_ROWS).iterrows():
        lines.append(f"{r['rating']}星 | {r['content']}")
    if len(rows) > MAX_ROWS:
        lines.append(f"...其余 {len(rows) - MAX_ROWS} 条省略")
    return "\n".join(lines)


# ===== 三、分发器(注册表模式, 和第一周完全相同) =====
TOOL_FUNCTIONS = {
    "query_sales": _query_sales,
    "query_reviews": _query_reviews,
}


def execute_tool(function_name: str, arguments: str) -> str:
    func = TOOL_FUNCTIONS.get(function_name)
    if func is None:
        return f"错误: 未知函数 {function_name}"
    args = json.loads(arguments) if arguments else {}   # AI 给的参数是 JSON 字符串
    return func(**args)                                  # ** 拆包自动分发
