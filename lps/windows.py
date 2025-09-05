from __future__ import annotations

from typing import Optional

import psutil


def get_battery_percent() -> Optional[float]:
    """
    返回当前电池电量百分比（0..100），若无电池或无法获取则返回 None。
    """
    try:
        b = psutil.sensors_battery()
        if not b:
            return None
        return float(b.percent)
    except Exception:
        return None