"""
D18 入口: 每日监控流水线(规则检测 -> AI 归因 -> 日报落盘)

完整数据流(监控闭环):
  detector.detect_all() 产出异常事件清单
    -> 每个事件包成一条"调查任务"发给 MonitorAgent
    -> AI 自主调 query_reviews / query_sales 取证, 返回归因报告
    -> report.build_report / save_report 打包成 markdown 日报存 reports/

运行: python analyze.py   (调 DeepSeek API, 5 个事件约几分钱)
定时运行: 任务计划程序 -> run_monitor.bat (输出进 monitor.log)
"""

from detector import detect_all
from agent import MonitorAgent
from report import build_report, save_report, save_analysis_json


def main():
    events = detect_all()
    print(f"规则层检出 {len(events)} 个异常事件, 逐个交给 AI 调查...\n")

    results = []
    for i, e in enumerate(events, 1):
        period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
        print("=" * 60)
        print(f"事件 {i}/{len(events)}: {e['category']} | {e['type']} | {period}")
        print("=" * 60)

        # 每个事件新建 agent -> 各事件调查独立, 上下文互不污染
        # (客服项目一个 agent 服务连续对话, 这里一个 agent 只办一个案子)
        agent = MonitorAgent()
        task = (
            f"监控规则发现异常事件, 请调查原因:\n"
            f"- 品类: {e['category']}\n"
            f"- 异常类型: {e['type']}\n"
            f"- 发生日期: {e['start']} 至 {e['end']}\n"
            f"- 规则详情: {e['detail']}\n"
            f"请按系统提示的格式输出归因报告。"
        )
        report = agent.chat(task)
        results.append({"event": e, "report": report})
        print(f"\n{report}\n")

    # 日报落盘(markdown 存档) + 结构化快照(JSON, 给 dashboard.py 监控大屏读取)
    path = save_report(build_report(results))
    save_analysis_json(results)

    print("=" * 60)
    print(f"全部调查完成: {len(results)} 个事件已归因")
    print(f"日报已保存: {path}")


if __name__ == "__main__":
    main()
