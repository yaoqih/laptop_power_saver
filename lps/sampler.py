from __future__ import annotations

import logging
import time
from typing import Dict, Iterable, List, Optional, Tuple

import psutil

from . import db as dbmod
from lps.utils import clamp

log = logging.getLogger(__name__)


ProcKey = Tuple[int, float]  # (pid, create_time)


class Sampler:
    def __init__(
        self,
        db_path: str,
        *,
        interval_s: float = 1.0,
        active_threshold: float = 0.005,  # 单核0.5%/s
        collect_mem: bool = True,
        collect_io: bool = True,
        retention_s: float = 30 * 86400.0,
    ) -> None:
        self.db_path = db_path
        self.interval_s = float(interval_s)
        self.active_threshold = float(active_threshold)
        self.collect_mem = bool(collect_mem)
        self.collect_io = bool(collect_io)
        self.retention_s = float(retention_s)

        self.cpu_count = max(1, psutil.cpu_count(logical=True) or 1)
        self._conn = dbmod.ensure_db(self.db_path)

        # 进程状态缓存
        self._prev_cpu: Dict[ProcKey, float] = {}  # total_cpu_time
        self._procid: Dict[ProcKey, int] = {}  # DB process.id
        self._missing_ticks: Dict[ProcKey, int] = {}
        self._last_mono: Optional[float] = None
        self._last_cleanup_ts: float = 0.0

    def run_loop(self) -> None:
        """
        主循环：按 interval 对齐采样并写入数据库。
        """
        log.info(
            "Sampler start: interval=%.3fs, active_threshold=%.3f, mem=%s, io=%s, retention=%.0fs",
            self.interval_s,
            self.active_threshold,
            self.collect_mem,
            self.collect_io,
            self.retention_s,
        )
        base = time.monotonic()
        tick_idx = 0
        try:
            while True:
                target = base + tick_idx * self.interval_s
                now = time.monotonic()
                sleep_s = target - now
                if sleep_s > 0:
                    time.sleep(sleep_s)
                # 执行一次采样
                try:
                    self.tick()
                except Exception as e:
                    log.exception("tick failed: %s", e)
                    # 确保前一次失败的事务被回滚，避免下一次循环处于脏事务状态
                    try:
                        if self._conn.in_transaction:
                            self._conn.rollback()
                    except Exception:
                        pass
                tick_idx += 1
        except KeyboardInterrupt:
            log.info("Sampler interrupted, exiting.")

    def tick(self) -> None:
        """
        单次采样：枚举进程，计算 Δcpu 与 eff_cores，批量写入样本。
        首次 tick 仅建立基线（不写样本）。
        """
        mono_now = time.monotonic()
        ts_now = time.time()
        # 若上一次 tick 发生异常导致事务未回滚，这里进行清理，避免后续写库出错
        try:
            if self._conn.in_transaction:
                self._conn.rollback()
        except Exception:
            pass

        # 第一次：建立基线，不写样本
        if self._last_mono is None:
            self._bootstrap_baseline(ts_now)
            self._last_mono = mono_now
            return

        dt = mono_now - self._last_mono
        dt = clamp(dt, 0.25, 5.0)
        self._last_mono = mono_now

        rows = []  # type: List[Tuple[float,int,float,float,float,int,Optional[int],Optional[int],Optional[int],Optional[int]]]
        seen: Dict[ProcKey, float] = {}

        attrs = [
            "pid",
            "name",
            "create_time",
            "exe",
            "cmdline",
            "username",
            "ppid",
            "cpu_times",
        ]
        if self.collect_mem:
            attrs.append("memory_info")
        if self.collect_io:
            attrs.append("io_counters")

        for proc in psutil.process_iter(attrs=attrs):
            info = proc.info
            try:
                pid = int(info.get("pid"))
                create_time = float(info.get("create_time"))
                key: ProcKey = (pid, create_time)
            except Exception:
                # 无法识别唯一键则跳过
                continue

            # 读取元数据
            exe_path = _safe_str(info.get("exe"))
            name = _safe_str(info.get("name"))
            # cmdline 可能是 list or str
            cmdline_val = info.get("cmdline")
            if isinstance(cmdline_val, (list, tuple)):
                cmdline = " ".join(map(str, cmdline_val))
            else:
                cmdline = _safe_str(cmdline_val)
            username = _safe_str(info.get("username"))
            try:
                ppid = int(info.get("ppid")) if info.get("ppid") is not None else None
            except Exception:
                ppid = None

            # CPU 时间
            total_cpu = _cpu_total_seconds(info.get("cpu_times"))
            if total_cpu is None:
                # 无 cpu_times 视为不可访问
                prev_total = None
                partial_meta = True
            else:
                prev_total = self._prev_cpu.get(key)
                partial_meta = False

            # 资源
            rss = vms = None  # type: Optional[int]
            if self.collect_mem:
                try:
                    mem = info.get("memory_info")
                    if mem is not None:
                        rss = int(getattr(mem, "rss", None)) if getattr(mem, "rss", None) is not None else None
                        vms = int(getattr(mem, "vms", None)) if getattr(mem, "vms", None) is not None else None
                except Exception:
                    pass

            read_b = write_b = None  # type: Optional[int]
            if self.collect_io:
                try:
                    io = info.get("io_counters")
                    if io is not None:
                        read_b = int(getattr(io, "read_bytes", None)) if getattr(io, "read_bytes", None) is not None else None
                        write_b = int(getattr(io, "write_bytes", None)) if getattr(io, "write_bytes", None) is not None else None
                except Exception:
                    pass

            # process id in DB
            process_id = dbmod.insert_or_get_process_id(
                self._conn,
                pid=pid,
                create_time=create_time,
                exe_path=exe_path,
                name=name,
                cmdline=cmdline,
                username=username,
                ppid=ppid,
                now_ts=ts_now,
                partial_meta=partial_meta,
            )
            self._procid[key] = process_id

            # 差分与活跃判断
            if prev_total is None or total_cpu is None:
                delta_cpu = 0.0
            else:
                delta_cpu = max(0.0, float(total_cpu) - float(prev_total))

            eff = delta_cpu / dt if dt > 0 else 0.0
            # 合理上限，避免异常 spike
            eff = min(eff, self.cpu_count * 1.5)
            active = 1 if delta_cpu >= self.active_threshold * dt else 0

            # 写行
            rows.append(
                (
                    ts_now,
                    process_id,
                    dt,
                    delta_cpu,
                    eff,
                    active,
                    rss,
                    vms,
                    read_b,
                    write_b,
                )
            )

            # 更新基线
            if total_cpu is not None:
                seen[key] = float(total_cpu)

        # 写库
        try:
            dbmod.batch_insert_samples(self._conn, rows)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

        # 标记丢失/结束
        self._handle_missing_and_ended(seen, ts_now)

        # 定期清理
        if ts_now - self._last_cleanup_ts >= 60.0:
            cutoff = ts_now - self.retention_s
            try:
                deleted = dbmod.prune_old_samples(self._conn, cutoff)
                if deleted:
                    log.debug("prune: deleted %d old samples older than %.0fs", deleted, self.retention_s)
            except Exception as e:
                log.warning("prune failed: %s", e)
            self._last_cleanup_ts = ts_now

    def _bootstrap_baseline(self, ts_now: float) -> None:
        """
        首次采样：建立 prev_cpu 与 procid 映射，写入/更新 process 元数据，不写 sample。
        """
        attrs = [
            "pid",
            "name",
            "create_time",
            "exe",
            "cmdline",
            "username",
            "ppid",
            "cpu_times",
        ]
        for proc in psutil.process_iter(attrs=attrs):
            info = proc.info
            try:
                pid = int(info.get("pid"))
                create_time = float(info.get("create_time"))
                key: ProcKey = (pid, create_time)
            except Exception:
                continue

            exe_path = _safe_str(info.get("exe"))
            name = _safe_str(info.get("name"))
            cmdline_val = info.get("cmdline")
            if isinstance(cmdline_val, (list, tuple)):
                cmdline = " ".join(map(str, cmdline_val))
            else:
                cmdline = _safe_str(cmdline_val)
            username = _safe_str(info.get("username"))
            try:
                ppid = int(info.get("ppid")) if info.get("ppid") is not None else None
            except Exception:
                ppid = None

            total_cpu = _cpu_total_seconds(info.get("cpu_times"))
            partial_meta = total_cpu is None

            process_id = dbmod.insert_or_get_process_id(
                self._conn,
                pid=pid,
                create_time=create_time,
                exe_path=exe_path,
                name=name,
                cmdline=cmdline,
                username=username,
                ppid=ppid,
                now_ts=ts_now,
                partial_meta=partial_meta,
            )
            self._procid[key] = process_id
            if total_cpu is not None:
                self._prev_cpu[key] = float(total_cpu)

    def _handle_missing_and_ended(self, seen: Dict[ProcKey, float], ts_now: float) -> None:
        # 更新 prev_cpu 与 missing 计数
        for key, total in seen.items():
            self._prev_cpu[key] = total
            self._missing_ticks.pop(key, None)

        # 对未见到的 key 累加缺失计数
        ended_ids: List[int] = []
        for key in list(self._prev_cpu.keys()):
            if key not in seen:
                cnt = self._missing_ticks.get(key, 0) + 1
                self._missing_ticks[key] = cnt
                if cnt >= 2:
                    # 认为已结束
                    self._missing_ticks.pop(key, None)
                    self._prev_cpu.pop(key, None)
                    pid = self._procid.pop(key, None)
                    if pid is not None:
                        ended_ids.append(pid)

        if ended_ids:
            try:
                dbmod.mark_process_ended(self._conn, ended_ids, ts_now)
                self._conn.commit()
            except Exception as e:
                self._conn.rollback()
                log.warning("mark ended failed: %s", e)


def _cpu_total_seconds(cpu_times_obj) -> Optional[float]:
    try:
        if cpu_times_obj is None:
            return None
        user = getattr(cpu_times_obj, "user", None)
        system = getattr(cpu_times_obj, "system", None)
        if user is None or system is None:
            return None
        return float(user) + float(system)
    except Exception:
        return None


def _safe_str(v) -> Optional[str]:
    try:
        if v is None:
            return None
        s = str(v)
        if not s:
            return None
        return s
    except Exception:
        return None