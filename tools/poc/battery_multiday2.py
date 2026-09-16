# -*- coding: utf-8 -*-
"""多日放电分析 v2：修正分桶口径 + 用轮询节拍判定亮屏占比。

修正点（相对 v1）：
  - 分桶改用"相邻采样 Δt≤2min"的净放电区间，避免日志断档污染；
  - 用轮询间隔（5s=熄屏档 / 1.5s=亮屏档）量化每段放电的亮屏时长；
  - 给出"熄屏静置率"与"营业混合率"两套续航口径。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOGDIR = Path(r"E:\kaifa\自助打印\dist\logs")
BATT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?"
                  r"batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)")
POLL = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*GET /api/screen/state\?batt")
samples, polls = [], []
for f in sorted(LOGDIR.glob("printshop.log*")):
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = BATT.match(ln)
        if m:
            samples.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                            int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))))
            continue
        p = POLL.match(ln)
        if p:
            polls.append(datetime.strptime(p.group(1), "%Y-%m-%d %H:%M:%S"))
samples.sort(key=lambda s: s[0])
polls.sort()

# 去重（轮转边界可能重复）
samples = [s for i, s in enumerate(samples) if i == 0 or s[0] != samples[i - 1][0] or s[1] != samples[i - 1][1]]
print(f"电池采样 {len(samples)} 条  {samples[0][0]} → {samples[-1][0]}")
print(f"轮询记录 {len(polls)} 条\n")


def screen_ratio(t0, t1):
    """用轮询间隔估算该区间内"亮屏"时长占比：<3s 视为亮屏档(1.5s)，≥4s 视为熄屏档(5s)。"""
    ps = [p for p in polls if t0 <= p <= t1]
    if len(ps) < 3:
        return 0.0, 0, 0
    gaps = [(b - a).total_seconds() for a, b in zip(ps, ps[1:])]
    fast = sum(1 for g in gaps if g < 3)
    slow = sum(1 for g in gaps if g >= 4)
    total = fast + slow
    if not total:
        return 0.0, fast, slow
    # 亮屏时采样更密：亮屏时长占比 ≈ fast/(fast+slow) 再按采样率加权
    # 亮屏相邻对代表 1.5s，熄屏代表 5s；亮屏时间 = fast*1.5，熄屏 = slow*5
    on_s = fast * 1.5
    off_s = slow * 5.0
    return (on_s / (on_s + off_s) if (on_s + off_s) else 0.0), fast, slow

# ---- 切分放电会话 ---------------------------------------------------------
sessions, cur = [], []
for s in samples:
    if s[4] == 0 and s[3] == 0:
        cur.append(s)
    else:
        if len(cur) >= 30:
            sessions.append(cur)
        cur = []
if len(cur) >= 30:
    sessions.append(cur)

print("=== 放电会话明细 ===")
summary = []
for i, ses in enumerate(sessions, 1):
    t0, t1 = ses[0][0], ses[-1][0]
    span = (t1 - t0).total_seconds() / 3600
    p0, p1 = ses[0][1], ses[-1][1]
    drop = p0 - p1
    if span <= 0 or drop <= 0:
        continue
    # 净采样覆盖（相邻 ≤2min 的区间），排除日志断档
    net = sum((b[0] - a[0]).total_seconds() for a, b in zip(ses, ses[1:])
              if (b[0] - a[0]).total_seconds() <= 120) / 3600
    gaps = sum(1 for a, b in zip(ses, ses[1:]) if (b[0] - a[0]).total_seconds() > 120)
    on_ratio, fast, slow = screen_ratio(t0, t1)
    rate = drop / net if net > 0 else 0
    summary.append(dict(i=i, t0=t0, t1=t1, span=span, net=net, p0=p0, p1=p1,
                        drop=drop, rate=rate, on=on_ratio, gaps=gaps))
    print(f"会话{i}: {t0:%m-%d %H:%M}→{t1:%m-%d %H:%M} 墙钟{span:5.2f}h 净{net:5.2f}h "
          f"断档{gaps}  {p0:3d}→{p1:3d}%(Δ{drop})  "
          f"速率(净) {rate:5.2f} %/h  亮屏占比 {on_ratio*100:4.1f}%  [快{fast}/慢{slow}]")

# ---- 分桶：净区间积分 -----------------------------------------------------
print("\n=== 按电量分桶（仅用 Δt≤2min 的净放电区间）===")
buckets: dict[int, list] = defaultdict(list)
for ses in sessions:
    for a, b in zip(ses, ses[1:]):
        dt = (b[0] - a[0]).total_seconds() / 3600
        dp = a[1] - b[1]
        if dt <= 0 or dp <= 0 or dt > 2 / 60:
            continue
        buckets[(b[1] // 10) * 10].append((dt, dp))

total, covered = 0.0, 0
for lo in sorted(buckets, reverse=True):
    h = sum(d for d, _ in buckets[lo])
    pp = sum(p for _, p in buckets[lo])
    hpp = h / pp
    seg = 10 * hpp
    total += seg
    covered += 10
    print(f"  {lo:3d}~{lo+10:3d}%: {h:5.2f}h / {pp:4d}pp → {pp/h:5.2f} %/h   "
          f"每1% {hpp*60:5.1f}min  该桶10pp={seg:5.2f}h")
print(f"\n  实测覆盖 {covered}pp（{min(buckets)}%~100%），累加耗时 = {total:.2f}h")

# ---- 两种口径 -------------------------------------------------------------
idle = [s for s in summary if s["on"] < 0.05]
if idle:
    h = sum(s["net"] for s in idle)
    pp = sum(s["drop"] for s in idle)
    print(f"\n★ 口径A【熄屏静置】：{len(idle)} 段（亮屏<5%）净 {h:.2f}h / {pp}pp "
          f"→ {pp/h:.2f} %/h  ⇒ 100%→0% ≈ {100/(pp/h):.1f}h")

allh = sum(s["net"] for s in summary)
allpp = sum(s["drop"] for s in summary)
print(f"★ 口径B【全程混合含营业】：全部 {len(summary)} 段 净 {allh:.2f}h / {allpp}pp "
      f"→ {allpp/allh:.2f} %/h  ⇒ 100%→0% ≈ {100/(allpp/allh):.1f}h")

# 工作日场景：营业 10h 内有会话亮屏
b = allpp / allh
print(f"\n  → 营业场景（10h/天，含顾客会话亮屏）每日耗电 ≈ {b*10:.0f}%"
      f"（按口径B）；纯待机 ≈ {idle and (pp/h)*10:.0f}%")
