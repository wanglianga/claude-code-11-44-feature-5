"""离线计数 → 采样时间 的映射。

只允许使用同一设备、同一启动批次下的可信锚点：
  - 命中锚点：直接用锚点时间与误差；
  - 被两个锚点夹住：线性插值，误差取较大锚点误差 + 距最近锚点的漂移累积；
  - 单侧外推：按契约标称采样间隔外推，误差 = 锚点误差 + 步数 × 每步漂移；
  - 没有锚点：保持 pending_anchor，不猜时间。
"""
from datetime import timedelta

from .models import ClockAnchor


def map_counter_to_time(counter: int, anchors: list[ClockAnchor],
                        interval_s: float, drift_s_per_step: float):
    """返回 (sample_time, error_s, info)；无锚点返回 (None, None, info)。"""
    if not anchors:
        return None, None, {"mode": "counter", "reason": "同一启动批次下没有可信锚点"}

    anchors = sorted(anchors, key=lambda a: a.counter)
    prev = None
    nxt = None
    for a in anchors:
        if a.counter <= counter:
            prev = a
        if a.counter >= counter and nxt is None:
            nxt = a

    def info(mode, anchor_refs, extra=""):
        return {"mode": "counter", "method": mode, "anchors": anchor_refs, "note": extra}

    if prev and prev.counter == counter:
        return prev.server_time, prev.error_s, info("anchor_hit", [prev.id])
    if nxt and nxt.counter == counter:
        return nxt.server_time, nxt.error_s, info("anchor_hit", [nxt.id])

    if prev and nxt:
        span = nxt.counter - prev.counter
        ratio = (counter - prev.counter) / span
        dt = (nxt.server_time - prev.server_time).total_seconds()
        t = prev.server_time + timedelta(seconds=dt * ratio)
        dist = min(counter - prev.counter, nxt.counter - counter)
        err = max(prev.error_s, nxt.error_s) + dist * drift_s_per_step
        return t, err, info("interpolate", [prev.id, nxt.id],
                            f"两锚点间线性插值，距最近锚点 {dist} 步")

    near = prev or nxt
    steps = counter - near.counter
    t = near.server_time + timedelta(seconds=steps * interval_s)
    err = near.error_s + abs(steps) * drift_s_per_step
    return t, err, info("extrapolate", [near.id],
                        f"按标称间隔 {interval_s}s 外推 {steps} 步")
