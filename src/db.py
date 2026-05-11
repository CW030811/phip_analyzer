"""SQLite 状态库：跟踪已发现/已下载/已分析的 PHIP"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional


SCHEMA = """
CREATE TABLE IF NOT EXISTS phip (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- 唯一标识：通常是 PDF URL
    source_url TEXT NOT NULL UNIQUE,
    company_name TEXT,
    stock_code TEXT,
    board TEXT,                -- main / gem
    document_type TEXT,        -- PHIP / AP
    publish_date TEXT,
    sponsor TEXT,
    pdf_path TEXT,
    pdf_size_bytes INTEGER,
    pdf_pages INTEGER,
    -- 状态机：DISCOVERED -> DOWNLOADED -> PARSED -> ANALYZED -> REPORTED -> FAILED
    status TEXT NOT NULL DEFAULT 'DISCOVERED',
    report_path TEXT,
    error_msg TEXT,
    discovered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_phip_status ON phip(status);
CREATE INDEX IF NOT EXISTS idx_phip_publish_date ON phip(publish_date);

CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    discovered_count INTEGER DEFAULT 0,
    analyzed_count INTEGER DEFAULT 0,
    failed_count INTEGER DEFAULT 0,
    notes TEXT
);
"""


class PhipDB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def conn(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        finally:
            c.close()

    def _init_schema(self) -> None:
        with self.conn() as c:
            c.executescript(SCHEMA)

    @staticmethod
    def _now() -> str:
        return datetime.utcnow().isoformat(timespec="seconds")

    # --- PHIP 记录 ---

    def upsert_discovered(self, *, source_url: str, company_name: str | None,
                          stock_code: str | None, board: str, document_type: str,
                          publish_date: str | None, sponsor: str | None) -> bool:
        """新增一条 PHIP。返回 True 表示是新发现的。"""
        now = self._now()
        with self.conn() as c:
            cur = c.execute("SELECT id FROM phip WHERE source_url = ?", (source_url,))
            if cur.fetchone():
                return False
            c.execute(
                """INSERT INTO phip (source_url, company_name, stock_code, board,
                                     document_type, publish_date, sponsor,
                                     status, discovered_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'DISCOVERED', ?, ?)""",
                (source_url, company_name, stock_code, board, document_type,
                 publish_date, sponsor, now, now),
            )
            return True

    def list_pending(self, statuses: Iterable[str] = ("DISCOVERED", "DOWNLOADED", "PARSED"),
                     since_days: int | None = None) -> list[dict]:
        statuses_list = list(statuses)
        ph = ",".join("?" * len(statuses_list))
        params: list = list(statuses_list)
        date_clause = ""
        if since_days is not None and since_days > 0:
            from datetime import datetime, timedelta
            cutoff = (datetime.utcnow() - timedelta(days=since_days)).date().isoformat()
            # publish_date 缺失时回退到 discovered_at
            date_clause = (" AND COALESCE(publish_date, substr(discovered_at,1,10)) "
                           ">= ?")
            params.append(cutoff)
        with self.conn() as c:
            rows = c.execute(
                f"""SELECT * FROM phip
                    WHERE status IN ({ph}){date_clause}
                    ORDER BY CASE status
                                 WHEN 'PARSED' THEN 0
                                 WHEN 'DOWNLOADED' THEN 1
                                 ELSE 2
                             END,
                             COALESCE(publish_date, substr(discovered_at,1,10)) DESC,
                             discovered_at DESC""",
                tuple(params),
            ).fetchall()
            return [dict(r) for r in rows]

    def get(self, source_url: str) -> Optional[dict]:
        with self.conn() as c:
            row = c.execute("SELECT * FROM phip WHERE source_url = ?", (source_url,)).fetchone()
            return dict(row) if row else None

    def update(self, source_url: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = self._now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [source_url]
        with self.conn() as c:
            c.execute(f"UPDATE phip SET {cols} WHERE source_url = ?", vals)

    def mark_failed(self, source_url: str, error: str) -> None:
        self.update(source_url, status="FAILED", error_msg=error[:1000])

    # --- 运行日志 ---

    def start_run(self) -> int:
        with self.conn() as c:
            cur = c.execute(
                "INSERT INTO run_log (started_at) VALUES (?)",
                (self._now(),),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, *, discovered: int, analyzed: int,
                   failed: int, notes: str = "") -> None:
        with self.conn() as c:
            c.execute(
                """UPDATE run_log
                   SET finished_at = ?, discovered_count = ?, analyzed_count = ?,
                       failed_count = ?, notes = ?
                   WHERE id = ?""",
                (self._now(), discovered, analyzed, failed, notes, run_id),
            )
