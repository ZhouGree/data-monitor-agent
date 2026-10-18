# 数据监控 Agent（规则检测 + AI 归因）

模拟电商平台监控场景：规则引擎每日扫描销售/退款/评论数据发现异常事件 → AI Agent 自主查数据取证、归因、给出建议 → 自动生成监控日报，可挂 Windows 任务计划程序无人值守。

对标场景：**调用 AI 智能体监控平台数据变化 + 数据分析**。

## 核心设计：规则先行，AI 殿后

| | 规则层（detector.py） | AI 层（agent.py） |
|---|---|---|
| 职责 | 判断"有没有异常" | 判断"**为什么**异常" |
| 成本 | 零成本、毫秒级 | 每次调用烧 token |
| 确定性 | 100% 可复现 | 会漏判、可能编造 |
| 弱点 | 报得出"跌了65%"，说不出原因 | 不适合扫全量数据 |

规则当筛子，AI 当分析师：**规则零成本扫全量数据，只把筛出的异常喂给 AI 深挖**。实测中 AI 还抓到了规则漏掉的水乳销量下滑（跌 20% 藏在波动里，未触发 60% 阈值）——规则管确定性，AI 管语义和关联，两层互补。

## 数据流

```
gen_data.py          模拟数据源(30天销量流水 + 用户评论, 固定随机种子可复现)
    │                  真实场景替换为: 定时拉取平台API / 查数据库
    ▼
detector.py          规则层: 三条规则扫描 + 连续告警合并成事件
    │   规则1 销量暴跌: 当日 < 前7天日均 × 60% (对比均值抗周末波动)
    │   规则2 退款激增: 退款率 > 15% (正常约3%)
    │   规则3 差评爆发: 单品类单日差评 ≥ 3 条 (正常天几乎为0)
    ▼
analyze.py           流水线: 每个异常事件包成"调查任务"
    │
    ▼
agent.py             决策层: DeepSeek + function calling 工具循环
    │                  AI 自主决定调什么工具、要不要设对照组
    ▼
tools.py             执行层: query_sales / query_reviews (pandas 查 CSV)
    │                  真实场景替换为: SQL / 数据仓库
    ▼
report.py            归因报告 → markdown 日报 + JSON 快照 (reports/)
    ▼
dashboard.py         Streamlit 监控大屏: 趋势图/指标卡/事件归因 (读快照, 零API成本)
```

## 内置剧本：3 起事故，AI 全部查对

数据里按剧本埋了 3 类异常，"症状"在销售流水里，"原因线索"埋在评论里：

| 剧本事故 | 规则层检出 | AI 归因结论 |
|---|---|---|
| 精华 10-08 起销量暴跌 65% | 销量暴跌 + 差评爆发 | 差评集中出现"泛红刺痛、疑似过敏"，口碑崩塌压垮转化，建议下架质检 |
| 面膜 10-12 退款率 3%→24% | 退款激增 + 差评爆发 | "面膜袋漏气、盒子压扁"，包装/物流破损，建议核查批次+加固包装 |
| 水乳 10-15 差评集中爆发 | 差评爆发（销量仅跌20%规则未触发） | "下单五天才发货"，物流延迟，建议排查发货环节+修正时效承诺 |

报告样例见 [reports/监控报告-2026-10-18.md](reports/监控报告-2026-10-18.md)。

## 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env        # 填入 DeepSeek API Key

python gen_data.py            # ① 生成模拟数据(本地运行, 零成本)
python detector.py            # ② 单测规则层(零成本)
python analyze.py             # ③ 完整流水线: 检测→AI归因→日报(约几分钱)
streamlit run dashboard.py    # ④ 监控大屏: 读快照展示, 零成本秒开
```

无人值守（可选）：`run_monitor.bat` 是调度入口（输出追加到 monitor.log），注册每日任务：

```powershell
schtasks /create /tn "DataMonitorAgent" /tr "项目路径\run_monitor.bat" /sc daily /st 09:00 /f
schtasks /delete /tn "DataMonitorAgent" /f   # 删除任务
```

## 项目结构

```
data-monitor-agent/
├── gen_data.py      # 数据源生成器(固定种子, 剧本异常+线索埋点)
├── detector.py      # 规则层: 三条检测规则 + 事件合并
├── tools.py         # 执行层: AI 的数据查询工具 + 分发器
├── agent.py         # 决策层: 分析师 prompt + 工具调用循环
├── analyze.py       # 入口: 检测→归因→落盘 完整流水线
├── report.py        # 日报生成(markdown) + JSON 快照
├── dashboard.py     # Streamlit 监控大屏(趋势/指标/归因)
├── run_monitor.bat  # 定时调度入口
├── data/            # sales.csv / reviews.csv
└── reports/         # 日报存档 + latest_analysis.json 快照
```

## 真实业务替换点

| 学习版 | 真实项目 | 改动范围 |
|---|---|---|
| gen_data.py 造数据 | 定时拉平台 API / 查数据库 | 只换数据源 |
| bigram/规则扫描 CSV | 接入数据仓库 SQL | 只换 detector 输入 |
| pandas 查 CSV | SQL / 数据仓库查询 | 只换 tools 函数体 |
| markdown 日报存盘 | + 邮件 / 飞书 webhook 推送 | 只加 report 出口 |

Agent 决策层、工具循环、分发器**一行不用改**——三层架构解耦的价值。

## 三周演进脉络

| 周 | 项目 | 核心能力 |
|---|---|---|
| 1 | agent-demo | function calling：单工具 → 多工具 → 多轮 → 三层工程化 |
| 2 | customer-service-demo | RAG 客服：知识库检索 + 路由分发 + grounding |
| 3 | data-monitor-agent | 数据监控：规则检测 + AI 归因 + 日报调度 |
