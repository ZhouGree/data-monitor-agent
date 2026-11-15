"""
D18 入口: 每日监控流水线(规则检测 -> AI 归因 -> 日报落盘)

完整数据流(监控闭环):
  detector.detect_all() 产出异常事件清单
    -> 每个事件包成一条"调查任务"发给 MonitorAgent
    -> AI 自主调 query_reviews / query_sales 取证, 返回归因报告
    -> report.build_report / save_report 打包成 markdown 日报存 reports/

可靠性(上线后才补的部分, 也是 D18 复盘里暴露的真问题):
  1. 单个事件失败隔离: 一个事件调查挂了只影响它自己, 不再拖垮整轮
  2. 失败重试: 网络抖动(如 getaddrinfo failed)重试一次通常就恢复
  3. 失败汇总: 跑完统计成功/失败数, 失败数>0 时打告警并以非 0 退出码结束,
     定时任务里这个退出码就是"有人该来看一眼"的信号

运行: python analyze.py   (调 DeepSeek API, 5 个事件约几分钱)
定时运行: 任务计划程序 -> run_monitor.bat (输出进 monitor.log)
"""

import time

import db
from detector import detect_all
from agent import MonitorAgent
from report import build_report, save_report, save_analysis_json

MAX_RETRIES = 2      # 单个事件调查最多尝试次数(第 1 次 + 1 次重试)
RETRY_DELAY = 5      # 重试前的等待秒数


def build_task(e: dict) -> str:
    """把一个异常事件包成给 AI 的调查任务"""
    return (
        f"监控规则发现异常事件, 请调查原因:\n"
        f"- 品类: {e['category']}\n"
        f"- 异常类型: {e['type']}\n"
        f"- 发生日期: {e['start']} 至 {e['end']}\n"
        f"- 规则详情: {e['detail']}\n"
        f"请按系统提示的格式输出归因报告。"
    )


def investigate(e: dict) -> tuple[str, Exception | None]:
    """调查单个事件: 失败不外抛, 重试后仍失败则返回失败说明

    这里刻意不让异常冒到 main(): 早期版本没有这层包裹,
    10/22 那次第 1 个事件网络解析失败就把整轮打断, 后面 4 个根本没跑。
    """
    cached = db.get_cached_report(e)
    if cached:
        print("  [缓存] 同事件已有结论, 跳过模型调用", flush=True)
        return cached, None

    task = build_task(e)
    last_err: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # 每个事件新建 agent -> 各事件调查独立, 上下文互不污染
            # (客服项目一个 agent 服务连续对话, 这里一个 agent 只办一个案子)
            report = MonitorAgent().chat(task)
            db.put_cached_report(e, report)      # 成功结论落库, 下次同事件直接复用
            return report, None
        except Exception as err:          # noqa: BLE001 —— 网络/API/解析异常统一兜住
            last_err = err
            print(f"  [失败] 第 {attempt}/{MAX_RETRIES} 次调查异常: "
                  f"{type(err).__name__}: {err}", flush=True)
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    return (f"AI 调查失败, 需人工复核。"
            f"错误: {type(last_err).__name__}: {last_err}"), last_err


def main() -> int:
    events = detect_all()
    print(f"规则层检出 {len(events)} 个异常事件, 逐个交给 AI 调查...\n")

    results: list[dict] = []
    failed = 0

    for i, e in enumerate(events, 1):
        period = e["start"] if e["start"] == e["end"] else f'{e["start"]} ~ {e["end"]}'
        print("=" * 60)
        print(f"事件 {i}/{len(events)}: {e['category']} | {e['type']} | {period}")
        print("=" * 60)

        report, err = investigate(e)
        results.append({"event": e, "report": report, "failed": err is not None})
        if err is not None:
            failed += 1
        print(f"\n{report}\n")

    # 日报落盘(markdown 存档) + 结构化快照(JSON, 给 dashboard.py 监控大屏读取)
    path = save_report(build_report(results))
    save_analysis_json(results)

    # 同时写 SQLite: 文件快照保留旧用法, 数据库给 API/看板按 run 追溯
    run_id = db.save_run(results, path, status="success" if failed == 0 else "partial")

    ok = len(results) - failed
    print("=" * 60)
    print(f"全部处理完成: {len(results)} 个事件, 成功 {ok} 个, 失败 {failed} 个")
    print(f"日报已保存: {path}")
    print(f"已写入 SQLite: run_id={run_id} ({db.DB_PATH})")
    if failed:
        print(f"[告警] {failed} 个事件调查失败, 已写入日报, 需人工复核", flush=True)

    # 非 0 退出码 = 定时任务侧的告警信号(不再需要人去翻 monitor.log 才知道)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
