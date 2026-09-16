# -*- coding: utf-8 -*-
"""从 dist/logs/printshop.log 提取纯电池放电会话并做斜率回归。

用法: python tools/poc/battery_from_log.py [日志路径] [日期]
输出: 每个放电会话的时长/Δpct/平均速率/回归斜率/电压斜率。
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

LOG = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(r"E:\kaifa\自助打印\dist\logs\printshop.log")
DAY = sys.argv[2] if len(sys.argv) > 2 else None

PAT = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)"
)

samples: list[tuple[datetime, int, int]] = []
for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines():
    m = PAT.match(ln)
    if not m:
        continue
    if DAY and not ln.startswith(DAY):
        continue
    samples.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                    int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5))))

print(f"日志: {LOG}")
print(f"带电量参数的采样行: {len(samples)}")
if not samples:
    sys.exit(0)

# 电压修正：AXP2101 上报的 mv 含负载压降。用"静置电压"近似 = 滑窗最大值
print(f"时间范围: {samples[0][0]} → {samples[-1][0]}")


def is_dis(s):
    return s[4] == 0 and s[3] == 0


# 切分会话
sessions: list[list] = []
cur: list = []
for s in samples:
    if is_dis(s):
        cur.append(s)
    else:
        if len(cur) >= 10:
            sessions.append(cur)
        cur = []
if len(cur) >= 10:
    sessions.append(cur)

print(f"\n发现 {len(sessions)} 段放电会话（>=10 采样）")


def lin(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None, None
    slope = sxy / sxx
    inter = my - slope * mx
    ss = sum((y - my) ** 2 for y in ys)
    sr = sum((y - (slope * x + inter)) ** 2 for x, y in zip(xs, ys))
    return slope, (1 - sr / ss if ss else float("nan"))


for i, ses in enumerate(sessions, 1):
    t0 = ses[0][0]
    xs = [(s[0] - t0).total_seconds() / 3600 for s in ses]
    ps = [float(s[1]) for s in ses]
    vs = [float(s[2]) for s in ses]
    span = xs[-1]
    drop = ps[0] - ps[-1]
    sp, r2p = lin(xs, ps)
    sv, r2v = lin(xs, vs)
    print(f"\n=== 会话 {i}: {ses[0][0]} → {ses[-1][0]} ({span:.2f}h, n={len(ses)}) ===")
    print(f"  pct : {ps[0]:.0f}% → {ps[-1]:.0f}%  (Δ{drop:.0f}pp)  平均 {drop/span:.2f} %/h")
    print(f"  回归斜率: {-sp:.2f} %/h  (R²={r2p:.4f})")
    print(f"  电压: {vs[0]:.0f}mV → {vs[-1]:.0f}mV  斜率 {-sv:.0f} mV/h (R²={r2v:.4f})")
    print(f"  1% 对应电压 = {(vs[0]-vs[-1])/drop:.1f} mV/pp" if drop else "")
    print(f"  ↑ 折算满电续航 = {100/(drop/span):.1f}h（按平均速率）")
    # 每 1% 所耗时间
    print(f"  实测每 1% 耗时 = {span*60/drop:.1f} 分钟")
