from __future__ import annotations
import sqlite3
from typing import Iterable, List, Optional, Sequence, Tuple
import time

PRAGMAS = (
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("temp_store", "MEMORY"),
    ("foreign_keys", "ON"),
)

def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn

def ensure_db(db_path: str) -> sqlite3.Connection:
    conn = connect(db_path)
    cur = conn.cursor()
    for key, val in PRAGMAS:
        try:
            cur.execute(f"PRAGMA {key}={val}")
        except sqlite3.DatabaseError:
            pass
    cur.executescript("""
CREATE TABLE IF NOT EXISTS process (
    id INTEGER PRIMARY KEY,
    pid INTEGER NOT NULL,
    create_time REAL NOT NULL,
    exe_path TEXT,
    name TEXT,
    cmdline TEXT,
    username TEXT,
    ppid INTEGER,
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    ended INTEGER NOT NULL DEFAULT 0,
    partial_meta INTEGER NOT NULL DEFAULT 0,
    UNIQUE(pid, create_time)
);
CREATE TABLE IF NOT EXISTS sample (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    process_id INTEGER NOT NULL,
    dt_s REAL NOT NULL,
    delta_cpu_s REAL NOT NULL,
    eff_cores REAL NOT NULL,
    active INTEGER NOT NULL,
    rss_bytes INTEGER,
    vms_bytes INTEGER,
    io_read_bytes INTEGER,
    io_write_bytes INTEGER,
    FOREIGN KEY(process_id) REFERENCES process(id)
);
CREATE INDEX IF NOT EXISTS idx_process_exe_path ON process(exe_path);
CREATE INDEX IF NOT EXISTS idx_process_ended ON process(ended);
CREATE INDEX IF NOT EXISTS idx_sample_ts ON sample(ts);
CREATE INDEX IF NOT EXISTS idx_sample_process_ts ON sample(process_id, ts);
""")
    conn.commit()
    return conn

def insert_or_get_process_id(
    conn: sqlite3.Connection,
    pid: int,
    create_time: float,
    *,
    exe_path: Optional[str],
    name: Optional[str],
    cmdline: Optional[str],
    username: Optional[str],
    ppid: Optional[int],
    now_ts: float,
    partial_meta: bool = False,
) -> int:
    cur = conn.cursor()
    cur.execute(
        """
INSERT INTO process
(pid, create_time, exe_path, name, cmdline, username, ppid, first_seen, last_seen, ended, partial_meta)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
ON CONFLICT(pid, create_time) DO UPDATE SET
  last_seen=excluded.last_seen,
  exe_path=COALESCE(exe_path, excluded.exe_path),
  name=COALESCE(name, excluded.name),
  cmdline=COALESCE(cmdline, excluded.cmdline),
  username=COALESCE(username, excluded.username),
  ppid=COALESCE(ppid, excluded.ppid),
  partial_meta=(partial_meta OR excluded.partial_meta)
""",
        (
            pid,
            create_time,
            exe_path,
            name,
            cmdline,
            username,
            ppid,
            now_ts,
            now_ts,
            1 if partial_meta else 0,
        ),
    )
    # fetch id
    cur.execute("SELECT id FROM process WHERE pid=? AND create_time=?", (pid, create_time))
    row = cur.fetchone()
    assert row is not None
    return int(row["id"])

def batch_insert_samples(
    conn: sqlite3.Connection,
    rows: Iterable[Tuple[float, int, float, float, float, int, Optional[int], Optional[int], Optional[int], Optional[int]]],
) -> None:
    cur = conn.cursor()
    cur.executemany(
        """
INSERT INTO sample
(ts, process_id, dt_s, delta_cpu_s, eff_cores, active, rss_bytes, vms_bytes, io_read_bytes, io_write_bytes)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
""",
        rows,
    )

def prune_old_samples(conn: sqlite3.Connection, cutoff_ts: float) -> int:
    cur = conn.cursor()
    cur.execute("DELETE FROM sample WHERE ts < ?", (cutoff_ts,))
    return cur.rowcount

def mark_process_ended(conn: sqlite3.Connection, process_ids: Sequence[int], last_seen_ts: float) -> int:
    if not process_ids:
        return 0
    placeholders = ",".join("?" for _ in process_ids)
    sql = f"UPDATE process SET ended=1, last_seen=? WHERE id IN ({placeholders})"
    cur = conn.cursor()
    cur.execute(sql, (last_seen_ts, *process_ids))
    return cur.rowcount