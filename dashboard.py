"""
监控大屏: 展示数据趋势 + 关键指标 + 异常事件 + AI 归因报告

设计: 读写分离 —— 分析离线跑(analyze.py / 定时任务), 大屏在线看
大屏只读 reports/latest_analysis.json 快照, 不调 API, 打开秒开零成本

运行: D:\7python\Fir\venv\Scripts\python.exe -m streamlit run dashboard.py
前置: 先跑过一次 analyze.py (快照不存在时会给出引导提示)
"""

import json
from pathlib import Path

import pandas as pd
import streamlit as st

BASE = Path(__file__).parent
SNAPSHOT = BASE / "reports" / "latest_analysis.json"

st.set_page_config(page_title="花颜数据监控平台", layout="wide")
st.title("花颜平台数据监控")

# ---- 前置校验: 没跑过 analyze.py 时给引导而不是报错 ----
if not SNAPSHOT.exists():
    st.warning("还没有分析结果, 请先在项目目录运行: python analyze.py")
    st.stop()

with open(SNAPSHOT, encoding="utf-8") as f:
    snap = json.load(f)

sales = pd.read_csv(BASE / "data" / "sales.csv", parse_dates=["date"])
reviews = pd.read_csv(BASE / "data" / "reviews.csv", parse_dates=["date"])

st.caption(
    f"分析时间: {snap['generated_at']} | "
    f"数据区间: {sales['date'].min():%Y-%m-%d} ~ {sales['date'].max():%Y-%m-%d}"
)

# ---- 一、关键指标卡 ----
total_sales = sales["sales"].sum()
refund_rate = sales["refunds"].sum() / sales["orders"].sum() * 100
bad_reviews = int((reviews["rating"] <= 2).sum())

c1, c2, c3, c4 = st.columns(4)
c1.metric("30天总销售额", f"{total_sales:,.0f} 元")
c2.metric("整体退款率", f"{refund_rate:.1f}%")
c3.metric("差评总数(不高于2星)", f"{bad_reviews} 条")
c4.metric("检出异常事件", f"{len(snap['events'])} 个")

# ---- 二、趋势图 ----
left, right = st.columns(2)
with left:
    st.markdown("**各品类日销售额**")
    st.line_chart(sales.pivot_table(index="date", columns="category", values="sales"))
with right:
    st.markdown("**各品类每日退款数**")
    st.line_chart(sales.pivot_table(index="date", columns="category", values="refunds"))

# ---- 三、异常事件 + AI 归因 ----
st.subheader(f"异常事件与 AI 归因（{len(snap['events'])} 个）")
for e, r in zip(snap["events"], snap["reports"]):
    period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
    with st.expander(f"{e['category']} | {e['type']} | {period}"):
        st.markdown(f"> 规则详情: {e['detail']}")
        st.markdown(r)
