"""
D18 报告落盘: 把"规则详情 + AI 归因"打包成 markdown 日报, 按日期存到 reports/

真实项目这一步通常还会接: 邮件 / 飞书 / 钉钉 webhook 推送,
让值班的人在聊天工具里直接收到日报 —— 存文件 + 推送是同一份数据两种出口
"""

import json
from datetime import datetime
from pathlib import Path

REPORT_DIR = Path(__file__).parent / "reports"


def build_report(results: list[dict]) -> str:
    """results: [{"event": 事件dict, "report": AI报告str}, ...] -> markdown 全文"""
    lines = [
        "# 平台数据监控日报",
        "",
        f"- 生成时间: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"- 异常事件: {len(results)} 个",
        "",
    ]

    for i, r in enumerate(results, 1):
        e = r["event"]
        period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
        lines += [
            f"## {i}. {e['category']} | {e['type']} | {period}",
            "",
            f"> 规则详情: {e['detail']}",
            "",
            "### AI 归因",
            "",
            r["report"],
            "",
            "---",
            "",
        ]

    return "\n".join(lines)


def save_report(md: str) -> Path:
    """按当天日期命名存盘 -> reports/监控报告-2026-10-18.md"""
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / f"监控报告-{datetime.now():%Y-%m-%d}.md"
    path.write_text(md, encoding="utf-8")
    return path


def save_analysis_json(results: list[dict]) -> Path:
    """结构化快照 -> reports/latest_analysis.json, 给 dashboard.py 监控大屏读取

    读写分离: 分析离线跑(analyze.py/定时任务), 大屏在线看(零API成本、秒开)
    真实项目里这个 JSON 换成写数据库、大屏从库里读, 道理相同
    """
    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "events": [r["event"] for r in results],
        "reports": [r["report"] for r in results],
    }
    REPORT_DIR.mkdir(exist_ok=True)
    path = REPORT_DIR / "latest_analysis.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
