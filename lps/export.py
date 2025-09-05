from __future__ import annotations

import csv
import sqlite3
import time
from typing import Literal, Optional

from lps.db import ensure_db


Group = Literal["exe", "pid"]


def export_csv(db_path: str, group: Group, since_ts: float, until_ts: float, out_path: str) -> int:
    """
    导出聚合后的 CSV。
    - group='exe'：按 exe_path 聚合
    - group='pid'：按 (pid, create_time) 会话聚合
    返回写入的行数（不含表头）。
    """
    conn = ensure_db(db_path)
    cur = conn.cursor()

    if group == "exe":
        sql = """
SELECT
  COALESCE(p.exe_path, p.name) AS exe_path,
  COUNT(*) AS samples,
  SUM(s.delta_cpu_s) AS cpu_s,
  SUM(s.dt_s) AS wall_s,
  SUM(CASE WHEN s.active=1 THEN s.dt_s ELSE 0 END) AS active_wall_s,
  CASE WHEN SUM(s.dt_s) > 0 THEN SUM(s.delta_cpu_s)/SUM(s.dt_s) ELSE NULL END AS avg_eff_cores,
  AVG(s.rss_bytes) AS avg_rss
FROM sample s
JOIN process p ON p.id = s.process_id
WHERE s.ts BETWEEN ? AND ?
GROUP BY COALESCE(p.exe_path, p.name)
ORDER BY cpu_s DESC
"""
        cur.execute(sql, (since_ts, until_ts))
        rows = cur.fetchall()
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "exe_path",
                    "samples",
                    "cpu_s",
                    "wall_s",
                    "active_wall_s",
                    "avg_eff_cores",
                    "avg_cpu_percent",
                    "avg_rss",
                    "since_ts",
                    "until_ts",
                ]
            )
            for r in rows:
                avg_eff = r["avg_eff_cores"]
                writer.writerow(
                    [
                        r["exe_path"],
                        r["samples"],
                        _f(r["cpu_s"]),
                        _f(r["wall_s"]),
                        _f(r["active_wall_s"]),
                        _f(avg_eff),
                        _f((avg_eff or 0.0) * 100.0 if avg_eff is not None else None),
                        _i(r["avg_rss"]),
                        _f(since_ts),
                        _f(until_ts),
                    ]
                )
        return len(rows)

    elif group == "pid":
        sql = """
SELECT
  p.pid AS pid,
  p.create_time AS create_time,
  MIN(COALESCE(p.exe_path, p.name)) AS exe_path,
  COUNT(*) AS samples,
  SUM(s.delta_cpu_s) AS cpu_s,
  SUM(s.dt_s) AS wall_s,
  SUM(CASE WHEN s.active=1 THEN s.dt_s ELSE 0 END) AS active_wall_s,
  CASE WHEN SUM(s.dt_s) > 0 THEN SUM(s.delta_cpu_s)/SUM(s.dt_s) ELSE NULL END AS avg_eff_cores,
  AVG(s.rss_bytes) AS avg_rss
FROM sample s
JOIN process p ON p.id = s.process_id
WHERE s.ts BETWEEN ? AND ?
GROUP BY p.pid, p.create_time
ORDER BY cpu_s DESC
"""
        cur.execute(sql, (since_ts, until_ts))
        rows = cur.fetchall()
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "pid",
                    "create_time",
                    "exe_path",
                    "samples",
                    "cpu_s",
                    "wall_s",
                    "active_wall_s",
                    "avg_eff_cores",
                    "avg_cpu_percent",
                    "avg_rss",
                    "since_ts",
                    "until_ts",
                ]
            )
            for r in rows:
                avg_eff = r["avg_eff_cores"]
                writer.writerow(
                    [
                        r["pid"],
                        _f(r["create_time"]),
                        r["exe_path"],
                        r["samples"],
                        _f(r["cpu_s"]),
                        _f(r["wall_s"]),
                        _f(r["active_wall_s"]),
                        _f(avg_eff),
                        _f((avg_eff or 0.0) * 100.0 if avg_eff is not None else None),
                        _i(r["avg_rss"]),
                        _f(since_ts),
                        _f(until_ts),
                    ]
                )
        return len(rows)

    else:
        raise ValueError("group must be 'exe' or 'pid'")


def _f(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    try:
        return f"{float(v):.6f}"
    except Exception:
        return None


def _i(v: Optional[float]) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None