"""
D18 服务化: FastAPI 接口层(本地 uvicorn / Docker 都能起)

四个接口:
  GET  /health          健康检查
  GET  /monitor/events  最近一次 run 的事件清单(读 SQLite)
  POST /monitor/run     触发一轮监控, ?ai=false 只跑规则层(零成本, 默认)
                        ?ai=true 才调 LLM, 需要 DEEPSEEK_API_KEY
  GET  /reports/latest  最近一次日报 markdown 原文

运行: uvicorn api:app --host 127.0.0.1 --port 8000
"""

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

import analyze
import db
import detector
import report

BASE = Path(__file__).parent
REPORT_DIR = BASE / "reports"

app = FastAPI(title="数据监控 Agent API", version="1.0.0")


def _run_pipeline(ai: bool) -> dict:
    """跑一轮监控并把结果落库; ai=False 时不碰模型, 成本为零"""
    events = detector.detect_all()

    results = []
    if ai:
        for e in events:
            text, err = analyze.investigate(e)
            results.append({"event": e, "report": text, "failed": err is not None})
    else:
        for e in events:
            results.append({
                "event": e,
                "report": "仅规则层检出, 未调用 AI 归因(ai=false)。"
                          "需要归因请用 POST /monitor/run?ai=true 并配置 DEEPSEEK_API_KEY。",
                "failed": False,
            })

    md = report.build_report(results)
    path = report.save_report(md)
    report.save_analysis_json(results)          # 旧快照继续写, 大屏回退时还能用
    failed = sum(1 for r in results if r["failed"])
    run_id = db.save_run(results, path, status="success" if failed == 0 else "partial")

    return {
        "run_id": run_id,
        "ai": ai,
        "event_count": len(events),
        "failed_count": failed,
        "report_path": str(path),
        "events": events,
    }


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "db_path": str(db.DB_PATH),
        "db_exists": db.DB_PATH.exists(),
    }


@app.get("/monitor/events")
def monitor_events() -> dict:
    snapshot = db.latest_snapshot()
    if snapshot is None:
        raise HTTPException(status_code=404,
                            detail="还没有监控记录, 先运行 python analyze.py 或 POST /monitor/run")
    return snapshot


@app.post("/monitor/run")
def monitor_run(ai: bool = Query(False, description="true 时调用 LLM 归因, 默认只跑规则层")) -> dict:
    return _run_pipeline(ai)


@app.get("/reports/latest", response_class=PlainTextResponse)
def reports_latest() -> str:
    latest = db.latest_report()
    if latest and latest["content"]:
        return latest["content"]

    # 库里还没有记录时, 回退到 reports/ 下最新的日报文件(兼容只跑过 analyze.py 的环境)
    files = sorted(REPORT_DIR.glob("监控报告-*.md"))
    if not files:
        raise HTTPException(status_code=404, detail="还没有日报, 先运行 python analyze.py")
    return files[-1].read_text(encoding="utf-8")
