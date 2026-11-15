# 数据监控 Agent：规则检测 + AI 归因 + 日报调度

模拟电商平台的数据监控场景：规则引擎每天扫描销售 / 退款 / 评论数据，发现异常事件后交给 AI Agent 取证、归因、给建议，自动生成监控日报，可挂 Windows 任务计划或 GitHub Actions 无人值守。

核心主张：**规则负责「有没有出事」，AI 负责「为什么出事」**。规则零成本扫全量数据，只把筛出的异常喂给模型，避免用大模型跑全表。

---

## 目录

- [效果速览](#效果速览)
- [系统架构](#系统架构)
- [关键设计决策](#关键设计决策)
- [量化结果](#量化结果)
- [看板与日报](#看板与日报)
- [API 契约](#api-契约)
- [快速开始](#快速开始)
- [目录结构](#目录结构)
- [接入真实数据的替换点](#接入真实数据的替换点)
- [已知局限](#已知局限)

---

## 效果速览

| 维度 | 结果 | 出处 |
|---|---|---|
| 规则检出（剧本集 3 起事故） | 3/3，误报 0，重复 0 | `evaluate.py` → `reports/eval_report.md` |
| 规则检出（边界集 4 起） | 4/4，误报 0，重复 0 | 同上 |
| 告警去重 | 剧本集 12 条原始告警 → **3 个事件**；边界集 16 → **4** | 同上 |
| AI 归因方向命中 | **7/7**（对应 4 起独立事故） | `evaluate_attribution.py` → `reports/eval_attribution.md` |
| AI 成本 | 只处理事件，且同事件缓存命中不重复调用 | `analyze.py` |
| 调度 | 本地 bat + GitHub Actions cron 双通道 | `run_monitor.bat` / `monitor.yml` |

内置 3 起剧本事故，症状在销售流水里、原因线索埋在评论里：

| 剧本事故 | 规则层检出 | AI 归因结论 | 建议 |
|---|---|---|---|
| 精华 10-08 起销量暴跌 65% | 销量暴跌 + 差评爆发 | 差评集中「泛红刺痛、疑似过敏」，口碑崩塌压垮转化 | 下架质检 |
| 面膜 10-12 退款率 3% → 24% | 退款激增 + 差评爆发 | 「面膜袋漏气、盒子压扁」，包装 / 物流破损 | 核查批次 + 加固包装 |
| 水乳 10-15 差评集中爆发 | 差评爆发（销量仅跌 20%，未触发规则） | 「下单五天才发货」，物流延迟 | 排查发货环节 + 修正时效承诺 |

第三起是刻意设计的：规则漏掉了 20% 的下滑，AI 通过评论语义补上了这层解释，用来体现两层的互补关系。

---

## 系统架构

```
gen_data.py          模拟数据源（30/60 天销量流水 + 评论，固定随机种子可复现）
    │                真实场景替换为：定时拉平台 API / 查数据仓库
    ▼
detector.py          规则层：三条规则 + 连续/跨规则合并 → 事件
    │                规则1 销量暴跌: 当日 < 前 7 天日均 × 60%
    │                规则2 退款激增: 退款率 > 15%（正常约 3%）
    │                规则3 差评爆发: 单品类单日差评 ≥ 3 条
    ▼
analyze.py           流水线：事件 → 调查任务 → 归因（带重试 + 同事件缓存）
    │
    ▼
agent.py             决策层：DeepSeek + function calling 工具循环（30s 超时）
    │                自主决定查什么、要不要设对照组
    ▼
tools.py             执行层：query_sales / query_reviews（pandas 查 CSV）
    │                真实场景替换为：SQL / 数据仓库
    ▼
report.py            日报（markdown）+ JSON 快照（reports/）
    ▼
db.py                SQLite：runs / events / reports / report_cache
    ▼
dashboard.py         Streamlit 看板：KPI / 趋势 / 事件归因（读库与快照，零 API 成本）
api.py               FastAPI：健康检查 / 事件 / 手动触发 / 日报原文
```

---

## 关键设计决策

| 决策 | 选择 | 依据 / 代价 |
|---|---|---|
| 规则与 AI 的分工 | 规则筛异常，AI 只做归因 | 规则毫秒级、100% 可复现、零成本；AI 会漏判也可能编造，不适合扫全量 |
| 销量判断基准 | 对比前 7 天日均，而不是单日环比 | 抗周末波动；单日环比基线在边界集上误报 1 条、重复 2 条 |
| 告警合并 | 连续时间合并 + 跨规则合并 | 同一事故被 3 条规则各报一次是噪音；合并后 12 → 3、16 → 4，AI 只需调查 3 / 4 次 |
| 阈值档位 | 保守 / 当前 / 宽松三档全部跑评测 | 宽松档在边界集多 2 条误报、1 条重复；当前档与保守档结果一致，保留默认 60% / 15% / 3 条 |
| 重复调用控制 | 事件签名 `category+type+start+end` 命中缓存直接复用 | 定时任务重跑、人工补跑都不重复烧 token |
| 默认不调模型 | `POST /monitor/run` 默认 `ai=false`，只跑规则层 | 触发是零成本的；需要归因时显式开启 |
| 调度双通道 | 本地 bat + GitHub Actions cron | Actions 免费额度够用，但仓库 60 天不活跃会被停用，保留本机兜底 |
| 幂等落库 | 同一 run 重复写入不产生重复记录 | 调度重试、手动补跑都安全 |
| 看板解耦 | 只读 SQLite 与快照，不在页面里调模型 | 打开即用、零成本、断网也能看历史 |

---

## 量化结果

### 1. 检测方案对比（固定种子，可复现）

| 方案 | 剧本集事件数 | 剧本集召回 / 误报 / 重复 | 边界集事件数 | 边界集召回 / 误报 / 重复 |
|---|---:|---|---:|---|
| 原始告警（不合并） | 12 | 3/3 · 0 · 9 | 16 | 4/4 · 0 · 12 |
| 仅连续合并 | 5 | 3/3 · 0 · 2 | 7 | 4/4 · 0 · 3 |
| **连续 + 跨规则（当前）** | **3** | **3/3 · 0 · 0** | **4** | **4/4 · 0 · 0** |
| 单日环比 + 合并（基线） | 4 | 3/3 · 0 · 1 | 7 | 4/4 · 1 · 2 |

结论：召回全部 3/3、4/4 的前提下，合并策略把重复事件从 9/12 压到 0，误报保持 0。

### 2. 阈值扫描

| 档位 | 销量 / 退款 / 差评阈值 | 剧本集 | 边界集 |
|---|---|---|---|
| 保守 | 50% / 20% / 4 条 | 3 事件，3/3，误报 0 | 4 事件，4/4，误报 0 |
| **当前** | **60% / 15% / 3 条** | **3 事件，3/3，误报 0** | **4 事件，4/4，误报 0** |
| 宽松 | 70% / 10% / 2 条 | 4 事件，3/3，重复 1 | 8 事件，4/4，**误报 2**，重复 1 |

两套数据上三档召回相同，差别只在误报；宽松档会把水乳的正常波动报出来。弱信号（防晒 0.8 倍下滑）只有宽松档能检出，说明当前阈值会放过这一量级——这是明确记录下来的取舍，不是遗漏。

### 3. AI 归因评测

判定口径：同品类且日期区间有交集视为同一事件；AI 报告的【根本原因】中出现标准原因方向词即记为命中。

- 剧本集 3/3、边界集 4/4，合计 **7/7 方向命中**，判错 0、无法判定 0
- 这 11 条测试记录只对应 **4 起独立事故**（过敏 1、破损 2、物流 1），其中计分的 7 条全部命中；对外说「4 起事故、11 条记录」，不把记录数当事故数
- 加固集把评论里的「过敏 / 破损 / 物流」等线索词替换成纯症状描述，用来回答「100% 是不是因为答案写在评论里」

---

## 看板与日报

`streamlit run dashboard.py`，读本地 CSV / SQLite / 快照（`reports/latest_analysis.json`），不产生 API 费用：

- 顶部 5 个 KPI：销售额 / 订单量 / 退款率 / 差评数 / 异常事件（含 P1 数量），带环比
- 受影响 GMV 估算（标注「估算，非实际退款」）
- 销售与差评趋势图，异常区间高亮
- 事件列表：触发规则、严重级、AI 归因结论、建议
- 数据源状态提示；缺快照时给出明确指令（先跑 `analyze.py`）

日报样例：[reports/监控报告-2026-10-18.md](reports/监控报告-2026-10-18.md)，历史日报按日期存档于 `reports/`。

---

## API 契约

```bash
uvicorn api:app --port 8000
```

| 方法 | 路径 | 说明 | 成本 |
|---|---|---|---|
| GET | `/health` | 健康检查 + 数据库路径 | 零 |
| GET | `/monitor/events` | 最近一次 run 的事件清单（读 SQLite） | 零 |
| POST | `/monitor/run?ai=false` | 触发一轮监控，默认只跑规则层 | 零 |
| POST | `/monitor/run?ai=true` | 规则层 + LLM 归因（需要 `DEEPSEEK_API_KEY`） | 按 token |
| GET | `/reports/latest` | 最近一次日报 markdown 原文 | 零 |

```text
GET  /health                 -> 200 {"status":"ok","db_exists":true,...}
POST /monitor/run?ai=false   -> 200 {"event_count":3,"failed_count":0,...}
GET  /monitor/events         -> 200 {"run_id":"...","events":[...]}
GET  /reports/latest         -> 200 "# 平台数据监控日报 ..."
```

---

## 快速开始

```bash
pip install -r requirements.txt
copy .env.example .env        # 填入 DEEPSEEK_API_KEY（只跑规则层可不填）

python gen_data.py            # ① 生成模拟数据（固定种子，零成本）
python detector.py            # ② 规则层自检（零成本）
python analyze.py             # ③ 完整流水线：检测 → AI 归因 → 日报 → SQLite
python evaluate.py            # ④ 检测评测：四方案 + 阈值扫描（零成本）
python evaluate_attribution.py # ⑤ 归因评测（需 key）
uvicorn api:app --port 8000   # ⑥ 服务化接口
streamlit run dashboard.py    # ⑦ 监控看板（读快照，秒开）
```

### 调度

本地（Windows 任务计划程序）：

```powershell
schtasks /create /tn "DataMonitorAgent" /tr "项目路径\run_monitor.bat" /sc daily /st 09:00 /f
schtasks /delete /tn "DataMonitorAgent" /f
```

CI：`.github/workflows/monitor.yml` 每天 UTC 01:00（北京时间 09:00）跑 `detector.py` + `evaluate.py`；只有在仓库配置了 `DEEPSEEK_API_KEY` 且手动勾选 `run_ai` 时才执行完整归因，报告作为 artifact 上传。单次运行不到 2 分钟。

容器：

```bash
docker compose up --build
docker compose run --rm monitor-api python evaluate.py
```

---

## 目录结构

```
data-monitor-agent/
├── gen_data.py            # 数据源生成器（固定种子，剧本异常 + 线索埋点）
├── detector.py            # 规则层：三条规则 + Thresholds + 事件合并
├── tools.py               # 执行层：AI 数据查询工具 + 分发器
├── agent.py               # 决策层：分析师 prompt + 工具循环（30s 超时，不自动重试）
├── analyze.py             # 流水线：检测 → 归因（重试/缓存）→ 日报 / JSON / SQLite
├── evaluate.py            # 检测评测：四方案 + 阈值扫描
├── evaluate_attribution.py# 归因评测：方向命中判定
├── report.py              # 日报生成（markdown）+ JSON 快照
├── db.py                  # SQLite：runs / events / reports / report_cache
├── api.py                 # FastAPI：健康检查 / 事件 / 触发 / 日报
├── dashboard.py           # Streamlit 看板（读库，缺库回退 JSON 快照）
├── run_monitor.bat        # 本地定时调度入口
├── Dockerfile / docker-compose.yml
├── .github/workflows/monitor.yml
├── data/                  # sales.csv / reviews.csv / ground_truth.json / monitor.db
└── reports/               # 日报存档 + latest_analysis.json + 两份评测报告
```

---

## 接入真实数据的替换点

| 学习版 | 真实项目 | 改动范围 |
|---|---|---|
| `gen_data.py` 造数据 | 定时拉平台 API / 查数据库 | 只换数据源 |
| 规则扫描 CSV | 接入数据仓库 SQL | 只换 detector 输入 |
| `tools.query_*` 查 CSV | SQL / 数据仓库查询 | 只换函数体 |
| 日报存盘 | 加邮件 / 飞书 webhook 推送 | 只加 report 出口 |

Agent 决策层、工具循环、分发器一行不用改——三层解耦的价值。

字段契约（最小集）：

| 数据 | 来源 | 字段 | 改哪一处 |
|---|---|---|---|
| 销售 | 平台订单导出 / 开放 API | `date, category, sales, orders` | `tools.query_sales` 的函数体换成 SQL |
| 退款 | 售后 / 退款表 | `date, category, refunds` | `detector` 输入的退款率 |
| 评论 | 评价导出 / API | `date, category, rating, content` | `tools.query_reviews` 换成 SQL |

三个口径坑：

1. 按**支付时间**而不是发货时间聚合
2. 时区统一到 UTC+8
3. 同一订单多次部分退款先按订单去重，再算退款率

另外大促日要单独打标，否则活动当天的正常回落会被当成暴跌（边界集里埋的就是这个坑）。

---

## 已知局限

| 这份数据能验证 | 不能验证 |
|---|---|
| 规则触发、两级合并是否正确（有 `ground_truth.json`，可算召回 / 误报 / 重复） | 真实店铺的分布、长尾噪声与季节性 |
| 阈值档位之间的取舍（同一套数据只换阈值） | 真实业务里误报的实际代价（客服工时、信任损失） |
| 报告生成、幂等落库、调度与并发 | 规则在真实 GMV 量级下的性能 |

- **模拟数据是可控实验台，不是效果证明**。它的价值在于有标准答案、可复现、能比较方案；真实有效性必须接真实店铺数据复核，这一步还没做，不当作已完成。
- 归因评测是「方向命中」，不是完整因果链；判定用关键词 + 人工复核原文，关键词只覆盖过敏 / 破损 / 物流三类已知原因。
- 规则层依赖品类维度与固定阈值，品类粒度很细或季节波动强的业务需要按品类重标。
- 单机 SQLite + 单进程调度，未验证多实例并发写入与任务抢锁。
