# -*- coding: utf-8 -*-
"""会话4（09-15 14:12 → 09-16 07:25，100%→18%）逐段审计：
列出所有 >60s 的采样断档，并按电量段给出速率，判断"低电量是否加速"以及分桶是否被断档污染。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

LOGDIR = Path(r"E:\kaifa\自助打印\dist\logs")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
RE_BATT = re.compile(r"batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)")
RE_POLL = re.compile(r"GET /api/screen/state")
ACT = re.compile(r"jslogin\?appid|/qrcode/|api/screen/wake|webwxnewloginpage|webwxinit|"
                 r"webwxsendmsg|webwxgetmedia|webwxgetmsgimg|webwxlogout|/api/jobs|/api/service/finish")

samples, polls, acts, starts = [], [], [], []
for f in sorted(LOGDIR.glob("printshop.log*")):
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        mt = RE_TS.match(ln)
        if not mt:
            continue
        t = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S")
        if RE_POLL.search(ln):
            polls.append(t)
            mb = RE_BATT.search(ln)
            if mb:
                samples.append((t, int(mb.group(1)), int(mb.group(2))))
        elif ACT.search(ln):
            acts.append(t)
        elif "Started server process" in ln:
            starts.append(t)

for lst in (samples, polls, acts, starts):
    lst.sort()
samples = [s for i, s in enumerate(samples) if i == 0 or s[0] != samples[i - 1][0]]

T0 = datetime(2026, 9, 15, 14, 12)
T1 = datetime(2026, 9, 16, 8, 50)
ses = [s for s in samples if T0 <= s[0] <= T1]
print(f"会话4 采样 {len(ses)} 条：{ses[0][0]} {ses[0][1]}% → {ses[-1][0]} {ses[-1][1]}%")

print("\n=== 采样断档 >60s ===")
gaps = []
for a, b in zip(ses, ses[1:]):
    dt = (b[0] - a[0]).total_seconds()
    if dt > 60:
        gaps.append((a, b, dt))
        print(f"  {a[0]:%m-%d %H:%M:%S}({a[1]}%) → {b[0]:%m-%d %H:%M:%S}({b[1]}%)  "
              f"缺口 {dt/60:.1f}min  Δpct={a[1]-b[1]}")
if not gaps:
    print("  无（采样连续）")

print("\n=== 按 10pp 段统计（只算 Δt≤60s 的连续片段）===")
from collections import defaultdict
bh, bp = defaultdict(float), defaultdict(int)
for a, b in zip(ses, ses[1:]):
    dt = (b[0] - a[0]).total_seconds()
    dp = a[1] - b[1]
    if dt <= 0 or dp <= 0 or dt > 60:
        continue
    bh[b[1] // 10] += dt / 3600
    bp[b[1] // 10] += dp
for lo in sorted(bh, reverse=True):
    print(f"  {lo*10:3d}~{lo*10+10:3d}%: {bh[lo]:6.2f}h / {bp[lo]:3d}pp → "
          f"{bp[lo]/bh[lo]:5.2f} %/h   每1% {bh[lo]/bp[lo]*60:5.1f}min")

print("\n=== 会话4 期间的服务重启 / 业务事件 ===")
for t in starts:
    if T0 <= t <= T1 + timedelta(hours=1):
        print(f"  [重启] {t}")
for t in acts:
    if T0 <= t <= T1:
        print(f"  {t:%m-%d %H:%M:%S}  业务事件")
print(f"  业务事件总数(会话4内)={sum(1 for t in acts if T0 <= t <= T1)}")

# 抽样打印每 2 小时的读数，直观看曲线
print("\n=== 每 2 小时抽样 ===")
last = None
for s in ses:
    if last is None or (s[0] - last).total_seconds() >= 7200:
        print(f"  {s[0]:%m-%d %H:%M}  {s[1]:3d}%  {s[2]}mV")
        last = s[0]
print(f"  {ses[-1][0]:%m-%d %H:%M}  {ses[-1][1]:3d}%  {ses[-1][2]}mV (末)")
