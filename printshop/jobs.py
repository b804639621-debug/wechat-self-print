import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


class JobStatus:
    PENDING = "pending"
    PRINTING = "printing"
    DONE = "done"
    FAILED = "failed"
    DISCARDED = "discarded"

    _ACTIVE = (PENDING, PRINTING, FAILED)


@dataclass
class Job:
    id: int
    msg_id: str
    file_name: str
    file_path: str
    category: str            # "pdf"|"image"|"office"|"other"
    size_bytes: int
    page_count: int
    status: str
    options_json: str
    error: str
    created_at: str
    printed_at: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS print_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    msg_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    category TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    printed_at TEXT
);
CREATE TABLE IF NOT EXISTS print_history (
    id INTEGER PRIMARY KEY,
    msg_id TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_path TEXT NOT NULL,
    category TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    page_count INTEGER NOT NULL,
    status TEXT NOT NULL,
    options_json TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    printed_at TEXT
);
"""

_COLUMNS = (
    "id", "msg_id", "file_name", "file_path", "category",
    "size_bytes", "page_count", "status", "options_json",
    "error", "created_at", "printed_at",
)


class JobStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    def add(self, msg_id, file_name, file_path, category, size_bytes, page_count) -> int:
        now = datetime.now().isoformat(sep=" ", timespec="seconds")
        cur = self.conn.execute(
            "INSERT INTO print_jobs "
            "(msg_id, file_name, file_path, category, size_bytes, page_count, "
            " status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, file_name, file_path, category, size_bytes, page_count,
             JobStatus.PENDING, now),
        )
        self.conn.commit()
        return cur.lastrowid

    def list_active(self) -> list[Job]:
        marks = ",".join("?" * len(JobStatus._ACTIVE))
        rows = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM print_jobs "
            f"WHERE status IN ({marks}) ORDER BY id",
            JobStatus._ACTIVE,
        ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def get(self, job_id: int) -> Job | None:
        row = self.conn.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM print_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        return self._row_to_job(row) if row else None

    def set_status(self, job_id: int, status: str, error: str = "", options_json: str = "") -> None:
        self.conn.execute(
            "UPDATE print_jobs SET status = ?, error = ?, options_json = ? WHERE id = ?",
            (status, error, options_json, job_id),
        )
        self.conn.commit()

    def archive_all(self) -> int:
        self.conn.execute(
            f"INSERT INTO print_history ({', '.join(_COLUMNS)}) "
            f"SELECT {', '.join(_COLUMNS)} FROM print_jobs"
        )
        cur = self.conn.execute("DELETE FROM print_jobs")
        self.conn.commit()
        return cur.rowcount

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(**{k: row[k] for k in _COLUMNS})
