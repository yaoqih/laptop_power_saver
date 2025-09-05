from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
import time
from typing import Literal, Optional

from lps.db import ensure_db, connect
from lps.export import export_csv as do_export_csv
from lps.sampler import Sampler
from lps.utils import parse_duration_to_seconds, parse_since_until

log = logging.getLogger("lps")


def main() -> None:
    parser = argparse.ArgumentParser(prog="lps", description="Laptop Power Saver - 进程CPU/时间统计（Windows 轻量采样档）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # run
    p_run = sub.add_parser("run", help="启动采样器，按固定间隔采样并写入SQLite")
    p_run.add_argument("--db", default="./lps.db", help="SQLite 数据库文件路径")
    p_run.add_argument("--interval", type=float, default=1.0, help="采样间隔（秒）")
    p_run.add_argument("--active-threshold", type=float, default=0.005, help="活跃阈值（CPU秒/秒），默认0.005=0.5%单核")
    p_run.add_argument("--retention", default="30d", help="原始样本保留期，默认30d（支持 s/m/h/d）")
    p_run.add_argument("--no-mem", action="store_true", help="不采集内存（rss/vms）")
    p_run.add_argument("--no-io", action="store_true", help="不采集IO（read_bytes/write_bytes）")
    p_run.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    p_run.set_defaults(func=cmd_run)

    # export csv
    p_exp = sub.add_parser("export", help="导出数据")
    sub_exp = p_exp.add_subparsers(dest="exp_cmd", required=True)
    p_csv = sub_exp.add_parser("csv", help="导出CSV（按exe或pid会话聚合）")
    p_csv.add_argument("--db", default="./lps.db", help="SQLite 数据库文件路径")
    p_csv.add_argument("--group", choices=["exe", "pid"], default="exe", help="聚合维度：exe 或 pid")
    p_csv.add_argument("--since", default="24h", help="起始时间点（相对如24h、绝对ISO/epoch、或now）")
    p_csv.add_argument("--until", default="now", help="结束时间点（相对/绝对/now）")
    p_csv.add_argument("--out", required=True, help="输出CSV文件路径")
    p_csv.set_defaults(func=cmd_export_csv)

    # top
    p_top = sub.add_parser("top", help="终端查看热点（按窗口聚合）")
    p_top.add_argument("--db", default="./lps.db", help="SQLite 数据库文件路径")
    p_top.add_argument("--window", default="10m", help="观察窗口（例如 10m/1h）")
    p_top.add_argument("--group", choices=["exe", "pid"], default="exe")
    p_top.add_argument("--limit", type=int, default=20)
    p_top.set_defaults(func=cmd_top)

    # maintenance
    p_vac = sub.add_parser("vacuum", help="VACUUM 压缩数据库")
    p_vac.add_argument("--db", default="./lps.db")
    p_vac.set_defaults(func=cmd_vacuum)

    p_reset = sub.add_parser("reset", help="清空数据（sample与process），并VACUUM")
    p_reset.add_argument("--db", default="./lps.db")
    p_reset.set_defaults(func=cmd_reset)

    args = parser.parse_args()

    # logging
    logging.basicConfig(
        level=getattr(logging, getattr(args, "log_level", "INFO")),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    args.func(args)


def cmd_run(args: argparse.Namespace) -> None:
    db_path: str = args.db
    retention_s: float = parse_duration_to_seconds(args.retention) if isinstance(args.retention, str) else float(args.retention)

    sampler = Sampler(
        db_path=db_path,
        interval_s=float(args.interval),
        active_threshold=float(args.active_threshold),
        collect_mem=not args.no_mem,
        collect_io=not args.no_io,
        retention_s=float(retention_s),
    )
    sampler.run_loop()


def cmd_export_csv(args: argparse.Namespace) -> None:
    db_path: str = args.db
    group: Literal["exe", "pid"] = args.group
    now_ts = time.time()
    since_ts, until_ts = parse_since_until(args.since, args.until, now_ts=now_ts)
    out_path: str = args.out

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    n = do_export_csv(db_path, group, since_ts, until_ts, out_path)
    print(f"CSV written: {out_path} ({n} rows)")


def cmd_top(args: argparse.Namespace) -> None:
    db_path: str = args.db
    window_s: float = parse_duration_to_seconds(args.window)
    group: Literal["exe", "pid"] = args.group
    limit: int = max(1, int(args.limit))

    conn = ensure_db(db_path)
    cur = conn.cursor()
    now_ts = time.time()
    since_ts = now_ts - window_s

    if group == "exe":
        sql = """
SELECT
  COALESCE(p.exe_path, p.name, '<unknown>') AS key,
  COUNT(*) AS samples,
  SUM(s.delta_cpu_s) AS cpu_s,
  SUM(s.dt_s) AS wall_s,
  SUM(CASE WHEN s.active=1 THEN s.dt_s ELSE 0 END) AS active_wall_s,
  CASE WHEN SUM(s.dt_s) > 0 THEN SUM(s.delta_cpu_s)/SUM(s.dt_s) ELSE NULL END AS avg_eff_cores,
  AVG(s.rss_bytes) AS avg_rss
FROM sample s
JOIN process p ON p.id = s.process_id
WHERE s.ts BETWEEN ? AND ?
GROUP BY COALESCE(p.exe_path, p.name, '<unknown>')
ORDER BY cpu_s DESC
LIMIT ?
"""
        cur.execute(sql, (since_ts, now_ts, limit))
        rows = cur.fetchall()
        header = ("exe_or_name", "cpu_s", "avg_eff", "avg_cpu%", "active_s", "samples")
        print(f"{header[0]:<50} {header[1]:>10} {header[2]:>8} {header[3]:>8} {header[4]:>10} {header[5]:>8}")
        for r in rows:
            avg_eff = r["avg_eff_cores"] or 0.0
            print(
                f"{str(r['key'])[:50]:<50} {r['cpu_s'] or 0.0:>10.3f} {avg_eff:>8.3f} {(avg_eff*100):>8.1f} {(r['active_wall_s'] or 0):>10.1f} {r['samples'] or 0:>8}"
            )
    else:
        sql = """
SELECT
  printf('%d@%.0f', p.pid, p.create_time) AS key,
  COUNT(*) AS samples,
  MIN(p.exe_path) AS exe_path,
  SUM(s.delta_cpu_s) AS cpu_s,
  SUM(s.dt_s) AS wall_s,
  SUM(CASE WHEN s.active=1 THEN s.dt_s ELSE 0 END) AS active_wall_s,
  CASE WHEN SUM(s.dt_s) > 0 THEN SUM(s.delta_cpu_s)/SUM(s.dt_s) ELSE NULL END AS avg_eff_cores
FROM sample s
JOIN process p ON p.id = s.process_id
WHERE s.ts BETWEEN ? AND ?
GROUP BY p.pid, p.create_time
ORDER BY cpu_s DESC
LIMIT ?
"""
        cur.execute(sql, (since_ts, now_ts, limit))
        rows = cur.fetchall()
        header = ("pid@ctime", "cpu_s", "avg_eff", "avg_cpu%", "active_s", "samples")
        print(f"{header[0]:<22} {header[1]:>10} {header[2]:>8} {header[3]:>8} {header[4]:>10} {header[5]:>8}")
        for r in rows:
            avg_eff = r["avg_eff_cores"] or 0.0
            print(
                f"{str(r['key'])[:22]:<22} {r['cpu_s'] or 0.0:>10.3f} {avg_eff:>8.3f} {(avg_eff*100):>8.1f} {(r['active_wall_s'] or 0):>10.1f} {r['samples'] or 0:>8}"
            )


def cmd_vacuum(args: argparse.Namespace) -> None:
    db_path: str = args.db
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("VACUUM")
    conn.commit()
    print("VACUUM done.")


def cmd_reset(args: argparse.Namespace) -> None:
    db_path: str = args.db
    conn = connect(db_path)
    cur = conn.cursor()
    cur.execute("DELETE FROM sample")
    cur.execute("DELETE FROM process")
    conn.commit()
    cur.execute("VACUUM")
    conn.commit()
    print("Database reset and vacuumed.")


if __name__ == "__main__":
    main()