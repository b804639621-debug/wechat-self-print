# -*- coding: utf-8 -*-
"""会话4 逐小时诊断：找出 04:00~06:00 放电加速 3 倍的原因。
检查：每小时掉电速率 / 轮询数 / 业务事件 / 服务重启 / WiFi 与 BLE 相关日志。"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOGDIR = Path(r"E:\kaifa\自助打印\dist\logs")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
RE_BATT = re.compile(r"batt_pct=(\d+)&batt_mv=(\d+)")
RE_POLL = re.compile(r"GET /api/screen/state")
ACT = re.compile(r"jslogin\?appid|/qrcode/|api/screen/wake|webwxnewloginpage|webwxinit|"
                 r"webwxsendmsg|webwxgetmedia|webwxgetmsgimg|webwxlogout|/api/jobs|/api/service/finish")
NOTABLE = re.compile(r"BLE|ble|WebSocket|watchdog|空闲|熄屏|睡眠|maintenance|维护|重启|"
                     r"Started server|shutdown|disconnect|重连|离线")

T0 = datetime(2026, 9, 15, 14, 12)
T1 = datetime(2026, 9, 16, 8, 50)

samples, polls, acts, notable = [], [], [], []
for f in sorted(LOGDIR.glob("printshop.log*")):
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        mt = RE_TS.match(ln)
        if not mt:
            continue
        t = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S")
        if not (T0 - timedelta(hours=1) <= t <= T1):
            continue
        if RE_POLL.search(ln):
            polls.append(t)
            mb = RE_BATT.search(ln)
            if mb:
                samples.append((t, int(mb.group(1)), int(mb.group(2))))
        elif ACT.search(ln):
            acts.append(t)
        elif NOTABLE.search(ln):
            notable.append(ln[:170])

samples.sort()
polls.sort()
acts.sort()
samples = [s for i, s in enumerate(samples) if i == 0 or s[0] != samples[i - 1][0]]

print("=== 逐小时明细（会话4）===")
print(f"{'时段':<16}{'pct':>6}{'mV':>7}{'%/h':>7}{'mV/h':>7}{'轮询数':>7}{'事件':>6}")
hour = T0
while hour < T1:
    nxt = hour + timedelta(hours=1)
    ss = [s for s in samples if hour <= s[0] < nxt]
    np_ = sum(1 for p in polls if hour <= p < nxt)
    na = sum(1 for a in acts if hour <= a < nxt)
    if len(ss) >= 2:
        dp = ss[0][1] - ss[-1][1]
        dv = ss[0][2] - ss[-1][2]
        flag = ""
        if dp >= 8:
            flag = "  ← 加速"
        if np_ < 300:
            flag += "  ← 轮询稀疏"
        print(f"{hour:%m-%d %H:%M}~{nxt:%H:%M} {ss[0][1]:4d}%{ss[0][2]:7d}"
              f"{dp/1.0:7.1f}{dv/1.0:7.0f}{np_:7d}{na:6d}{flag}")
    hour = nxt

print("\n=== 会话4 期间值得注意的日志行 ===")
if notable:
    for ln in notable[:60]:
        print("  " + ln)
else:
    print("  （无 BLE/维护/重启类日志）")

# 轮询间隔变化（可能反映屏幕状态切换）
print("\n=== 轮询间隔分布：加速前后对比 ===")
for label, a, b in (("加速前 14:12~04:00", datetime(2026, 9, 15, 14, 12), datetime(2026, 9, 16, 4, 0)),
                    ("加速后 04:00~07:25", datetime(2026, 9, 16, 4, 0), datetime(2026, 9, 16, 7, 25))):
    ps = [p for p in polls if a <= p <= b]
    g = [round((y - x).total_seconds()) for x, y in zip(ps, ps[1:])]
    c = Counter(x for x in g if x <= 30)
    print(f"  {label}: {len(ps)} 次  间隔分布 {dict(sorted(c.items(), key=lambda kv: -kv[1])[:6])}")
