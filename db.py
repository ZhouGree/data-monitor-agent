"""
D18 落库层: 把每轮监控结果写入 SQLite, 给 API 和看板一个稳定的读取出口

为什么加这一层:
  1. reports/*.md 和 latest_analysis.json 是"文件快照", 只能回答"最近一次",
     无法按 run 追溯; SQLite 能保留每次运行的事件和报告, 也方便接口分页/筛选
  2. 文件快照继续保留(向后兼容), 大屏/日报的旧用法不受影响

三张主表(外加一张事件级缓存表):
  runs          每轮运行: run_id / started_at / status / event_count
  events        本轮事件: run_id / category / type / start / end / detail
                (扩展 report / failed 两列, 让大屏能从库里直接展示 AI 归因)
  reports       本轮日报文件: run_id / path / created_at
  report_cache  同事件缓存: signature -> report, 避免同一事件重复调模型
"""

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

DB_PATH = Path(__file__).parent / "data" / "monitor.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id      TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    status      TEXT NOT NULL,
    event_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id    TEXT NOT NULL,
    category  TEXT NOT NULL,
    type      TEXT NOT NULL,
    start     TEXT NOT NULL,
    end       TEXT NOT NULL,
    detail    TEXT NOT NULL,
    report    TEXT,
    failed    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id);
CREATE TABLE IF NOT EXISTS reports (
    run_id     TEXT PRIMARY KEY,
    path       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS report_cache (
    signature  TEXT PRIMARY KEY,
    report     TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


@contextmanager
def connect(path: Path = DB_PATH):
    """读写连接: 首次调用自动建库建表, 退出时统一提交"""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")      # 读写不互斥, 崩了不易损坏
    conn.execute("PRAGMA busy_timeout=5000")     # 写锁竞争最多等 5s
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def connect_readonly(path: Path = DB_PATH) -> sqlite3.Connection:
    """只读连接: 大屏/接口只读, 避免把正在写库的进程锁住

    mode=ro 是 SQLite 的 URI 参数; 路径里的中文和空格要做 URL 编码。
    """
    if not path.exists():
        raise FileNotFoundError(path)
    uri = f"file:{quote(path.as_posix(), safe='/:')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _event_signature(event: dict) -> str:
    return "|".join(str(event[k]) for k in ("category", "type", "start", "end"))


def get_cached_report(event: dict, path: Path = DB_PATH) -> str | None:
    """同一(category, type, start, end)的事件命中过就复用上次结论"""
    if not path.exists():
        return None
    with connect(path) as conn:
        row = conn.execute(
            "SELECT report FROM report_cache WHERE signature = ?",
            (_event_signature(event),),
        ).fetchone()
    return row["report"] if row else None


def put_cached_report(event: dict, report: str, path: Path = DB_PATH) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO report_cache(signature, report, created_at) "
            "VALUES (?, ?, ?)",
            (_event_signature(event), report, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


def save_run(results: list[dict], report_path: Path, status: str = "success",
             path: Path = DB_PATH) -> str:
    """把一轮 analyze/api 的结果整体落库, 返回 run_id

    results: [{"event": 事件dict, "report": AI报告str, "failed": bool}, ...]
    """
    run_id = datetime.now().strftime("%Y%m%d%H%M%S%f")
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO runs(run_id, started_at, status, event_count) VALUES (?, ?, ?, ?)",
            (run_id, started_at, status, len(results)),
        )
        for r in results:
            e = r["event"]
            conn.execute(
                "INSERT INTO events(run_id, category, type, start, end, detail, report, failed) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, e["category"], e["type"], e["start"], e["end"], e["detail"],
                 r.get("report", ""), int(bool(r.get("failed")))),
            )
        conn.execute(
            "INSERT INTO reports(run_id, path, created_at) VALUES (?, ?, ?)",
            (run_id, str(report_path), started_at),
        )
    return run_id


def latest_snapshot(path: Path = DB_PATH) -> dict | None:
    """最近一次 run 的事件 + 报告, 形状和 latest_analysis.json 保持一致"""
    if not path.exists():
        return None
    conn = connect_readonly(path)
    try:
        run = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            return None
        rows = conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run["run_id"],)
        ).fetchall()
    finally:
        conn.close()

    return {
        "run_id": run["run_id"],
        "generated_at": run["started_at"],
        "status": run["status"],
        "events": [
            {"category": r["category"], "type": r["type"],
             "start": r["start"], "end": r["end"], "detail": r["detail"]}
            for r in rows
        ],
        "reports": [r["report"] for r in rows],
        "failed": [bool(r["failed"]) for r in rows],
    }


def latest_report(path: Path = DB_PATH) -> dict | None:
    """最近一次日报: 从 reports 表拿路径并读回 markdown 文本"""
    if not path.exists():
        return None
    conn = connect_readonly(path)
    try:
        row = conn.execute(
            "SELECT r.run_id, r.path, r.created_at FROM reports r "
            "JOIN runs u ON u.run_id = r.run_id "
            "ORDER BY u.started_at DESC, u.run_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return None
    report_file = Path(row["path"])
    content = report_file.read_text(encoding="utf-8") if report_file.exists() else ""
    return {"run_id": row["run_id"], "path": row["path"],
            "created_at": row["created_at"], "content": content}
