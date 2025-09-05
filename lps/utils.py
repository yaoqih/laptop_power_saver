from __future__ import annotations

import datetime as _dt
import logging
import re
import time
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhdSMHD])\s*$")


def parse_duration_to_seconds(spec: str) -> float:
    """
    解析相对时长字符串到秒:
      - '30s' 秒
      - '15m' 分
      - '2h'  小时
      - '7d'  天
    支持大小写与小数。
    """
    m = _DURATION_RE.match(spec)
    if not m:
        raise ValueError(f"Invalid duration spec: {spec!r}")
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit == "s":
        return val
    if unit == "m":
        return val * 60.0
    if unit == "h":
        return val * 3600.0
    if unit == "d":
        return val * 86400.0
    raise ValueError(f"Invalid duration unit in spec: {spec!r}")


def parse_time_point(spec: str, now_ts: Optional[float] = None) -> float:
    """
    解析时间点到 Unix 时间戳（秒，float）。
      - 'now' 当前时间
      - 相对：例如 '24h' 表示 now-24h
      - 绝对：ISO 8601（例如 '2025-09-02T12:30:00' 或 '2025-09-02 12:30:00'）
      - 绝对：epoch（整数/浮点秒）
    """
    if now_ts is None:
        now_ts = time.time()
    s = spec.strip().lower()
    if s == "now":
        return now_ts
    # 相对时间：X[s|m|h|d]，解释为 now - duration
    try:
        dur = parse_duration_to_seconds(s)
        return now_ts - dur
    except Exception:
        pass

    # epoch（允许小数）
    try:
        return float(s)
    except Exception:
        pass

    # ISO 格式
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = _dt.datetime.strptime(spec, fmt)
            # 视为本地时间
            return dt.timestamp()
        except Exception:
            continue

    # fromisoformat（更宽松）
    try:
        dt = _dt.datetime.fromisoformat(spec)
        return dt.timestamp()
    except Exception:
        pass

    raise ValueError(f"Invalid time point spec: {spec!r}")


def parse_since_until(
    since_spec: Optional[str],
    until_spec: Optional[str],
    now_ts: Optional[float] = None,
) -> Tuple[float, float]:
    """
    返回 (since_ts, until_ts)，默认 since=24h 前，until=now。
    """
    if now_ts is None:
        now_ts = time.time()
    since_ts = parse_time_point(since_spec, now_ts) if since_spec else now_ts - 86400.0
    until_ts = parse_time_point(until_spec, now_ts) if until_spec else now_ts
    if until_ts < since_ts:
        raise ValueError("until must be >= since")
    return since_ts, until_ts


def clamp(v: float, lo: float, hi: float) -> float:
    return lo if v < lo else hi if v > hi else v