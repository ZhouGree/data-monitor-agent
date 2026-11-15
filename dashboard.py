"""花颜平台数据监控看板（企业级布局）

启动: streamlit run dashboard.py
数据: data/sales.csv + data/reviews.csv + reports/latest_analysis.json
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import db

BASE = Path(__file__).parent
DATA_DIR = BASE / "data"
SNAPSHOT_PATH = BASE / "reports" / "latest_analysis.json"

RED = "#E5484D"
GREY = "#8C8C8C"
GREEN = "#2FA36B"
CAT_COLORS = {"精华": "#5B8FF9", "面膜": "#5AD8A6", "水乳": "#F6BD16", "防晒": "#E8684A"}
LOOKBACK = {"近 7 天": 7, "近 14 天": 14, "近 30 天": 30, "全部": None}

# 差评归因口径：关键词命中即归类，一条评论只计入第一个命中的类别
BAD_REVIEW_RULES = {
    "过敏/刺激": ["过敏", "泛红", "刺痛", "火辣", "闭口", "刺激", "不良反应", "小疙瘩"],
    "包装破损": ["破损", "漏气", "压扁", "破了", "裂", "干了", "干涸", "变形", "瘪"],
    "物流延迟": ["物流", "发货", "快递", "慢", "晚到", "迟迟", "不更新", "等了好久"],
    "售后/退款": ["退款", "售后", "客服", "退货", "不理", "没人"],
    "效果/体验不符": ["没用", "无效", "没效果", "味道", "难闻", "油腻", "干燥"],
}

st.set_page_config(page_title="花颜数据监控", page_icon="📈", layout="wide")

st.markdown(
    """
<style>
  .block-container { padding-top: 1.1rem; padding-bottom: 2rem; max-width: 1500px; }
  [data-testid="stMetricValue"] { font-size: 1.5rem; }
  [data-testid="stMetricDelta"] { font-size: 0.78rem; }
  [data-testid="stMetricLabel"] { font-size: 0.82rem; color: #5f6368; }
  h3 { margin-top: 0.2rem; margin-bottom: 0.1rem; }
  hr { margin: 0.6rem 0; }
</style>
""",
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------- 数据层
@st.cache_data(ttl=60)
def load_tables() -> tuple[pd.DataFrame, pd.DataFrame]:
    sales = pd.read_csv(DATA_DIR / "sales.csv", parse_dates=["date"])
    reviews = pd.read_csv(DATA_DIR / "reviews.csv", parse_dates=["date"])
    return sales, reviews


@st.cache_data(ttl=60)
def load_snapshot() -> dict:
    try:
        snap = db.latest_snapshot()
        if isinstance(snap, dict) and snap.get("events") and isinstance(snap.get("reports"), list):
            return snap
    except Exception:
        pass
    if SNAPSHOT_PATH.exists():
        return json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    return {"generated_at": "无数据", "events": [], "reports": [], "failed": []}


def parse_report(report: str) -> dict[str, str]:
    """把 AI 归因报告拆成四段，便于结构化展示。"""
    out = {"异常归因": "", "根本原因": "", "数据证据": "", "建议动作": ""}
    for head in out:
        m = re.search(rf"【{head}】(.*?)(?=【|$)", report or "", re.S)
        if m:
            out[head] = m.group(1).strip()
    return out


def merge_intervals(spans: list[tuple[pd.Timestamp, pd.Timestamp]]) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    if not spans:
        return []
    spans = sorted(spans)
    merged = [list(spans[0])]
    for s, e in spans[1:]:
        if s <= merged[-1][1] + pd.Timedelta(days=1):
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def event_impact(sales: pd.DataFrame, category: str, spans: list[tuple[pd.Timestamp, pd.Timestamp]]) -> tuple[float, int]:
    """受影响 GMV = 事件区间内实际销售额 与 「事件前 7 天日均 × 天数」的差额（下限 0）。"""
    d = sales[sales["category"] == category].sort_values("date")
    gap, days = 0.0, 0
    for start, end in merge_intervals(spans):
        cur = d[(d["date"] >= start) & (d["date"] <= end)]
        pre = d[(d["date"] >= start - pd.Timedelta(days=7)) & (d["date"] < start)]
        if cur.empty or pre.empty:
            days += len(cur)
            continue
        baseline = pre["sales"].mean() * len(cur)
        gap += max(0.0, baseline - cur["sales"].sum())
        days += len(cur)
    return gap, days


def severity(gap: float) -> str:
    if gap >= 10000:
        return "P1"
    if gap >= 3000:
        return "P2"
    return "P3"


def window_kpi(sales: pd.DataFrame, reviews: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp) -> dict:
    s = sales[(sales["date"] >= start) & (sales["date"] <= end)]
    r = reviews[(reviews["date"] >= start) & (reviews["date"] <= end)]
    orders = int(s["orders"].sum())
    refunds = int(s["refunds"].sum())
    return {
        "gmv": float(s["sales"].sum()),
        "orders": orders,
        "rate": refunds / orders * 100 if orders else 0.0,
        "bad": int((r["rating"] <= 2).sum()),
    }


def pct_delta(cur: float, prev: float) -> float | None:
    if not prev:
        return None
    return (cur - prev) / prev * 100


# ---------------------------------------------------------------- 页面
sales_all, reviews_all = load_tables()
snap = load_snapshot()

if sales_all.empty:
    st.error("data/sales.csv 为空，先跑 python gen_data.py")
    st.stop()

all_cats = sorted(sales_all["category"].unique())
date_max = sales_all["date"].max()
date_min = sales_all["date"].min()

st.title("花颜平台数据监控")
st.caption(f"数据区间 {date_min:%Y-%m-%d} ~ {date_max:%Y-%m-%d} ｜ 最近一次分析 {snap.get('generated_at', '-')}")

f1, f2, f3 = st.columns([1.2, 2, 1.6])
window_label = f1.selectbox("时间范围", list(LOOKBACK), index=2, label_visibility="collapsed")
cats = f2.multiselect("品类", all_cats, default=all_cats, label_visibility="collapsed", placeholder="选择品类")
window = LOOKBACK[window_label]
window_start = date_min if window is None else max(date_min, date_max - pd.Timedelta(days=window - 1))
with f3:
    if snap.get("events"):
        st.caption(f"🟢 数据源正常 ｜ 快照含 {len(snap['events'])} 起事件")
    else:
        st.caption("🟡 无分析快照，先跑 python analyze.py")

if not cats:
    cats = all_cats

sales = sales_all[sales_all["category"].isin(cats)]
reviews = reviews_all[reviews_all["category"].isin(cats)]
sales_win = sales[(sales["date"] >= window_start) & (sales["date"] <= date_max)]
reviews_win = reviews[(reviews["date"] >= window_start) & (reviews["date"] <= date_max)]

events = []
failed_flags = snap.get("failed") or []


def is_failed(idx: int) -> bool:
    return bool(failed_flags[idx]) if idx < len(failed_flags) else False

for i, ev in enumerate(snap.get("events", [])):
    if ev.get("category") not in cats:
        continue
    ev = dict(ev)
    ev["start_ts"] = pd.Timestamp(ev["start"])
    ev["end_ts"] = pd.Timestamp(ev["end"])
    if ev["end_ts"] < window_start or ev["start_ts"] > date_max:
        continue
    ev["report_index"] = i
    events.append(ev)
events.sort(key=lambda e: e["start_ts"])

# 受影响 GMV 按品类合并区间，避免同一事件的重复快照被重复计入
gap_by_cat, days_by_cat = {}, {}
for cat in cats:
    spans = [(e["start_ts"], e["end_ts"]) for e in events if e["category"] == cat]
    gap, days = event_impact(sales_all, cat, spans)
    gap_by_cat[cat], days_by_cat[cat] = gap, days
affected_gmv = sum(gap_by_cat.values())

# ---------------------------------------------------------------- KPI
kpi = window_kpi(sales_win, reviews_win, window_start, date_max)
prev_end = window_start - pd.Timedelta(days=1)
prev_len = (date_max - window_start).days + 1
prev_start = prev_end - pd.Timedelta(days=prev_len - 1)
prev = window_kpi(sales_all[sales_all["category"].isin(cats)], reviews_all[reviews_all["category"].isin(cats)], prev_start, prev_end)
has_prev = bool(sales_all["date"].min() <= prev_start)

k1, k2, k3, k4, k5, k6 = st.columns(6)
with k1, st.container(border=True):
    st.metric("销售额", f"{kpi['gmv']:,.0f} 元", None if not has_prev else f"{pct_delta(kpi['gmv'], prev['gmv']) or 0:+.1f}%")
with k2, st.container(border=True):
    st.metric("订单量", f"{kpi['orders']:,}", None if not has_prev else f"{pct_delta(kpi['orders'], prev['orders']) or 0:+.1f}%")
with k3, st.container(border=True):
    st.metric("退款率", f"{kpi['rate']:.1f}%", None if not has_prev else f"{kpi['rate'] - prev['rate']:+.1f} pp", delta_color="inverse")
with k4, st.container(border=True):
    st.metric("差评数（1-2 星）", f"{kpi['bad']}", None if not has_prev else f"{pct_delta(kpi['bad'], prev['bad']) or 0:+.1f}%", delta_color="inverse")
with k5, st.container(border=True):
    p1 = sum(1 for e in events if severity(gap_by_cat.get(e["category"], 0.0)) == "P1")
    st.metric("异常事件", f"{len(events)}", f"其中 P1 {p1} 起", delta_color="off")
with k6, st.container(border=True):
    st.metric("受影响 GMV", f"{affected_gmv:,.0f} 元", "估算，非实际退款", delta_color="off")

st.caption(
    f"环比口径：当前窗口 {window_start:%m-%d}~{date_max:%m-%d} vs 上一等长窗口 "
    f"{prev_start:%m-%d}~{prev_end:%m-%d}；退款率、差评数、异常事件为「越低越好」，箭头方向已反转"
)

# ---------------------------------------------------------------- 趋势区
st.markdown("### 趋势与阈值")

left, right = st.columns(2)

with left, st.container(border=True):
    fig = go.Figure()
    for cat in cats:
        d = sales_win[sales_win["category"] == cat]
        fig.add_trace(
            go.Scatter(
                x=d["date"], y=d["sales"], name=cat, mode="lines",
                line=dict(width=2.2, color=CAT_COLORS.get(cat, "#5B8FF9")),
                hovertemplate="%{x|%m-%d}　%{y:,.0f} 元<extra>" + cat + "</extra>",
            )
        )
    for ev in events:
        fig.add_vrect(
            x0=ev["start_ts"], x1=ev["end_ts"], fillcolor=RED, opacity=0.07, line_width=0, layer="below",
            annotation_text=ev["category"], annotation_position="top left",
            annotation_font_size=10, annotation_font_color=RED,
        )
    fig.update_layout(
        title=dict(text="销售额走势（红色底 = 检出异常区间）", font=dict(size=14)),
        height=330, margin=dict(l=10, r=10, t=42, b=10),
        template="plotly_white", hovermode="x unified",
        legend=dict(orientation="h", y=-0.18),
    )
    fig.update_xaxes(tickformat="%m-%d")
    fig.update_yaxes(tickformat=",.0f", title=None)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

with right, st.container(border=True):
    fig = go.Figure()
    for cat in cats:
        d = sales_win[sales_win["category"] == cat].copy()
        d["rate"] = d["refunds"] / d["orders"].replace(0, pd.NA) * 100
        fig.add_trace(
            go.Scatter(
                x=d["date"], y=d["rate"], name=cat, mode="lines",
                line=dict(width=1.8, color=CAT_COLORS.get(cat, "#5B8FF9")),
                hovertemplate="%{x|%m-%d}　%{y:.1f}%<extra>" + cat + "</extra>",
            )
        )
    fig.add_hline(y=15, line=dict(color=RED, width=1.2, dash="dash"),
                  annotation_text="阈值 15%", annotation_font_size=10, annotation_font_color=RED)
    fig.update_layout(
        title=dict(text="退款率走势（虚线 = 15% 规则阈值）", font=dict(size=14)),
        height=330, margin=dict(l=10, r=10, t=42, b=10),
        template="plotly_white", hovermode="x unified",
        legend=dict(orientation="h", y=-0.18),
    )
    fig.update_xaxes(tickformat="%m-%d")
    fig.update_yaxes(ticksuffix="%", title=None)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

# ---------------------------------------------------------------- 结构区
st.markdown("### 结构与文本洞察")

left, right = st.columns([1.35, 1])

with left, st.container(border=True):
    rate_matrix = (
        sales_win.assign(rate=lambda d: d["refunds"] / d["orders"].replace(0, pd.NA) * 100)
        .pivot(index="category", columns="date", values="rate")
        .reindex(cats)
    )
    fig = go.Figure(
        go.Heatmap(
            z=rate_matrix.values,
            x=[d.strftime("%m-%d") for d in rate_matrix.columns],
            y=rate_matrix.index,
            colorscale=[[0, "#F7F9FC"], [0.5, "#FFD8B0"], [1, "#C62828"]],
            zmin=0, zmax=max(15, float(pd.Series(rate_matrix.values.ravel()).max() or 15)),
            colorbar=dict(title="%", thickness=10, len=0.8),
            hovertemplate="%{y} %{x}<br>退款率 %{z:.1f}%<extra></extra>",
        )
    )
    fig.update_layout(
        title=dict(text="品类 × 日期 退款率热力图（越红越高）", font=dict(size=14)),
        height=300, margin=dict(l=10, r=10, t=42, b=10), template="plotly_white",
    )
    fig.update_yaxes(autorange="reversed")
    fig.update_xaxes(type="category")  # 防止 "09-19" 被解析成日期导致轴显示成 2010 年
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

with right, st.container(border=True):
    bad = reviews_win[reviews_win["rating"] <= 2]
    counts: dict[str, int] = {k: 0 for k in BAD_REVIEW_RULES}
    for text in bad["content"].astype(str):
        for label, words in BAD_REVIEW_RULES.items():
            if any(w in text for w in words):
                counts[label] += 1
                break
    items = sorted([(k, v) for k, v in counts.items() if v], key=lambda kv: kv[1])
    if not items:
        items = [("无差评", 0)]
    fig = go.Figure(
        go.Bar(
            x=[v for _, v in items], y=[k for k, _ in items], orientation="h",
            marker_color=[RED if v else "#E6E6E6" for _, v in items],
            text=[v for _, v in items], textposition="outside", cliponaxis=False,
            hovertemplate="%{y}: %{x} 条<extra></extra>",
        )
    )
    fig.update_layout(
        title=dict(text=f"差评归因分布（窗口内 {len(bad)} 条 1-2 星）", font=dict(size=14)),
        height=300, margin=dict(l=10, r=24, t=42, b=10), template="plotly_white",
    )
    fig.update_xaxes(showgrid=True, gridcolor="#F0F0F0", title=None)
    fig.update_yaxes(title=None)
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

# ---------------------------------------------------------------- 事件中心
st.markdown("### 事件中心")

if not events:
    st.info("当前筛选条件下没有检出异常事件。")
    st.stop()

rows, gaps = [], []
for ev in events:
    gap = gap_by_cat.get(ev["category"], 0.0)
    days = days_by_cat.get(ev["category"], 0)
    gaps.append((gap, days))
    rows.append(
        {
            "区间": f"{ev['start_ts']:%m-%d} ~ {ev['end_ts']:%m-%d}",
            "品类": ev["category"],
            "类型": ev["type"],
            "影响天数": days,
            "影响 GMV": round(gap),
            "严重度": severity(gap),
        }
    )

table = pd.DataFrame(rows)
selection = st.dataframe(
    table,
    hide_index=True,
    width="stretch",
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "区间": st.column_config.TextColumn("区间", width="small"),
        "类型": st.column_config.TextColumn("类型", width="medium"),
        "影响 GMV": st.column_config.NumberColumn("影响 GMV（元）", format="%d"),
        "影响天数": st.column_config.NumberColumn("影响天数", width="small"),
        "严重度": st.column_config.TextColumn("严重度", width="small", help="按影响 GMV 分档：≥1 万 P1，3 千-1 万 P2，其余 P3"),
    },
)
picked = selection.selection.rows[0] if selection.selection.rows else 0
ev = events[picked]
gap, days = gaps[picked]
report_raw = (snap.get("reports") or [""])[ev["report_index"]]
parts = parse_report(report_raw)

st.markdown(f"#### {ev['category']} · {ev['type']} · {ev['start_ts']:%m-%d} ~ {ev['end_ts']:%m-%d}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("严重度", severity(gap))
m2.metric("影响 GMV", f"{gap:,.0f} 元")
m3.metric("影响天数", f"{days} 天")
m4.metric("归因状态", "失败" if is_failed(ev["report_index"]) else "已完成")

c1, c2, c3 = st.columns([1.1, 1.4, 1.1])
with c1, st.container(border=True):
    st.markdown("**根本原因**")
    st.markdown(parts["根本原因"] or "_（本次未产出，见下方规则详情）_")
    st.caption("触发规则")
    for rule in (ev.get("rules") or [ev.get("type", "")]):
        st.markdown(f"- {rule}")
    st.markdown(f"<span style='color:#8C8C8C;font-size:0.82rem'>{ev.get('detail', '')}</span>", unsafe_allow_html=True)
with c2, st.container(border=True):
    st.markdown("**数据证据**")
    st.markdown(parts["数据证据"] or "_（无）_")
with c3, st.container(border=True):
    st.markdown("**建议动作**")
    st.markdown(parts["建议动作"] or "_（无）_")

with st.expander("查看完整 AI 报告（原始文本）"):
    st.markdown(report_raw.replace("~", "～") if report_raw else "_无报告_")

with st.expander("指标口径与已知限制"):
    st.markdown(
        """
- **退款率** = 当日退款笔数 / 当日订单笔数，是笔数口径，不是金额口径。
- **差评** = 1-2 星评论；「差评归因分布」按关键词命中归类，一条评论只计入第一个命中的类别，属于粗分类。
- **受影响 GMV** = 事件区间实际销售额 与 「事件前 7 天日均销售额 × 区间天数」的差额，下限为 0；按品类合并重叠区间，避免重复计入。它只衡量「卖得少了多少」，**退款激增类事件不会产生销售额缺口**，要看退款率趋势图。
- **严重度** 按受影响 GMV 分档（≥1 万 P1 / 3 千-1 万 P2 / 其余 P3），是运营视角的分级，不是模型输出。
- **归因状态** = AI 是否成功产出结构化归因；失败时回退到规则详情，「根本原因」会显示为空。
- 数据为模拟数据（`gen_data.py`，含 5 起注入事故），指标看趋势和口径，不代表真实业务规模。
        """
    )
