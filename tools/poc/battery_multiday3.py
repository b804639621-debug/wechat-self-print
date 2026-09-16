# -*- coding: utf-8 -*-
"""多日放电分析 v3（自校验版）：估算真实续航。

数据: dist/logs/printshop.log*（6 个轮转文件，2026-09-07 ~ 09-16）
要点:
  * 相邻采样仅 5s，单点对点 Δpct 恒为 0/1 → 分桶必须用"累计耗时法"：
    从每段会话起点累加，记录跨过每个 1% 刻度所用的累计时间，再按桶汇总。
  * 亮屏占比由轮询间隔推断（<3s=亮屏 1.5s 档；≥4s=熄屏 5s 档）。
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOGDIR = Path(r"E:\kaifa\自助打印\dist\logs")
RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+")
RE_BATT = re.compile(r"batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)")
RE_POLL = re.compile(r"GET /api/screen/state")

samples, polls, acts = [], [], []
ACT_PAT = re.compile(r"jslogin\?appid|/qrcode/|api/screen/wake|webwxnewloginpage|webwxinit|"
                     r"webwxsendmsg|webwxgetmedia|webwxgetmsgimg|webwxlogout|/api/jobs|/api/service/finish")
n_files = 0
for f in sorted(LOGDIR.glob("printshop.log*")):
    n_files += 1
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        mt = RE_TS.match(ln)
        if not mt:
            continue
        t = datetime.strptime(mt.group(1), "%Y-%m-%d %H:%M:%S")
        if RE_POLL.search(ln):
            polls.append(t)
            mb = RE_BATT.search(ln)
            if mb:
                samples.append((t, int(mb.group(1)), int(mb.group(2)),
                                int(mb.group(3)), int(mb.group(4))))
        elif ACT_PAT.search(ln):
            acts.append(t)

samples.sort(key=lambda s: s[0])
polls.sort()
acts.sort()


def dedupe(seq, key):
    out, seen = [], set()
    for x in seq:
        k = key(x)
        if k not in seen:
            seen.add(k)
            out.append(x)
    return out


samples = dedupe(samples, lambda s: s[0])
polls = dedupe(polls, lambda p: p)
acts = dedupe(acts, lambda a: a)
print(f"轮转文件 {n_files} 个")
print(f"电池采样 {len(samples)}  轮询 {len(polls)}  业务事件 {len(acts)}")
print(f"覆盖 {samples[0][0]} → {samples[-1][0]}\n")


def screen_on_ratio(t0, t1):
    ps = [p for p in polls if t0 <= p <= t1]
    if len(ps) < 5:
        return None, 0, 0
    g = [(b - a).total_seconds() for a, b in zip(ps, ps[1:])]
    fast = sum(1 for x in g if x < 3)
    slow = sum(1 for x in g if 4 <= x <= 20)
    on_s, off_s = fast * 1.5, slow * 5.0
    tot = on_s + off_s
    return (on_s / tot if tot else 0.0), fast, slow


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
info = []
for i, ses in enumerate(sessions, 1):
    t0, t1 = ses[0][0], ses[-1][0]
    span = (t1 - t0).total_seconds() / 3600
    p0, p1 = ses[0][1], ses[-1][1]
    if span <= 0 or p0 <= p1:
        continue
    net = sum((b[0] - a[0]).total_seconds() for a, b in zip(ses, ses[1:])
              if (b[0] - a[0]).total_seconds() <= 120) / 3600
    ratio, fast, slow = screen_on_ratio(t0, t1)
    nact = sum(1 for a in acts if t0 <= a <= t1 + timedelta(seconds=180))
    info.append(dict(i=i, t0=t0, t1=t1, span=span, net=net, p0=p0, p1=p1,
                     drop=p0 - p1, rate=(p0 - p1) / net, on=ratio, fast=fast,
                     slow=slow, nact=nact))
    on_s = f"{ratio*100:4.1f}%" if ratio is not None else "  n/a"
    print(f"会话{i}: {t0:%m-%d %H:%M}→{t1:%m-%d %H:%M} 净{net:5.2f}h  {p0:3d}→{p1:3d}% "
          f"(Δ{p0-p1:3d})  速率 {(p0-p1)/net:5.2f} %/h  亮屏占比 {on_s} "
          f"[快{fast}/慢{slow}]  业务事件 {nact}")

# ---- 累计耗时法分桶 -------------------------------------------------------
print("\n=== 累计耗时法：跨越每个 10pp 桶所需时间 ===")
bucket_hours: dict[int, float] = defaultdict(float)
bucket_pp: dict[int, int] = defaultdict(int)
for ses in sessions:
    t0, p_start = ses[0][0], ses[0][1]
    marks: dict[int, datetime] = {p_start: t0}
    for s in ses[1:]:
        for lvl in range(p_start - 1, s[1] - 1, -1):   # 依次经过的每个刻度
            if lvl not in marks:
                marks[lvl] = s[0]
    levels = sorted(marks, reverse=True)
    for a, b in zip(levels, levels[1:]):
        dt = (marks[b] - marks[a]).total_seconds() / 3600
        if dt <= 0 or dt > 3:      # 断档保护
            continue
        bucket_hours[(b // 10) * 10] += dt
        bucket_pp[(b // 10) * 10] += (a - b)

total = 0.0
for lo in sorted(bucket_hours, reverse=True):
    # 桶内平均"每 1% 耗时"
    per_pp = bucket_hours[lo] / bucket_pp[lo]
    seg = per_pp * 10
    total += seg
    print(f"  {lo:3d}~{lo+10:3d}%: 累计 {bucket_hours[lo]:6.2f}h / {bucket_pp[lo]:4d}pp  "
          f"→ {bucket_pp[lo]/bucket_hours[lo]:5.2f} %/h   每1% {per_pp*60:5.1f}min  "
          f"该桶10pp={seg:5.2f}h")
print(f"\n  实测覆盖 {min(bucket_hours)}%~100%，累计耗时 {total:.2f}h")
low = min(bucket_hours)
per_pp_low = bucket_hours[low] / bucket_pp[low]
extra = per_pp_low * low
print(f"  未实测 0~{low}% 按最低桶速率外推 = {extra:.2f}h")
print(f"\n★ 真实续航（100%→0%）= {total:.2f} + {extra:.2f} = **{total+extra:.1f}h**")

# ---- 场景口径 -------------------------------------------------------------
print("\n=== 场景口径 ===")
for name, sel in (("全部会话", info),
                  ("无业务事件会话", [s for s in info if s["nact"] == 0])):
    if not sel:
        continue
    net = sum(s["net"] for s in sel)
    pp = sum(s["drop"] for s in sel)
    print(f"  {name}: {len(sel)} 段 净{net:.2f}h / {pp}pp → {pp/net:.2f} %/h "
          f"⇒ 100%→0% ≈ {100/(pp/net):.1f}h")
