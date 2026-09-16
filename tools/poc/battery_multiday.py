# -*- coding: utf-8 -*-
"""多日日志全量分析：提取所有纯电池放电会话，估算真实续航。

数据源: dist/logs/printshop.log*（RotatingFileHandler，含 .1~.5 历史）
方法  :
  1. 按时间排序合并所有轮转文件（注意 read order 与时间顺序不同）
  2. 切分放电会话（vbus=0 且 chg=0）
  3. 每段算 时长/Δpct/平均速率/回归斜率/电压斜率
  4. 按 pct 分桶统计"每 1% 耗时"，累加得到 100%→0% 的真实续航
     （避免线性外推：放电曲线在高段与低段速率不同）
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

LOGDIR = Path(r"E:\kaifa\自助打印\dist\logs")
PAT = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),\d+ .*?"
                 r"batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)")
ACT = re.compile(r"jslogin\?appid|/qrcode/|api/screen/wake|webwxnewloginpage|webwxinit|"
                 r"webwxsendmsg|webwxgetmedia|webwxgetmsgimg|webwxlogout|/api/jobs|/api/service/finish")

# ---- 读取全部轮转文件 -----------------------------------------------------
samples = []          # (t, pct, mv, chg, vbus)
acts = []             # 业务事件时间
files = sorted(LOGDIR.glob("printshop.log*"))
for f in files:
    for ln in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = PAT.match(ln)
        if m:
            samples.append((datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S"),
                            int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))))
            continue
        if ACT.search(ln) and re.match(r"^\d{4}-\d{2}-\d{2}", ln):
            acts.append(datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S"))

samples.sort(key=lambda s: s[0])
acts.sort()
print(f"轮转文件: {[f.name for f in files]}")
print(f"电池采样: {len(samples)} 条，覆盖 {samples[0][0]} → {samples[-1][0]}")
print(f"业务事件: {len(acts)} 条\n")

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

print(f"=== 检出 {len(sessions)} 段放电会话（>=30 采样）===\n")


def lin(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None, None
    sl = sxy / sxx
    it = my - sl * mx
    ss = sum((y - my) ** 2 for y in ys)
    sr = sum((y - (sl * x + it)) ** 2 for x, y in zip(xs, ys))
    return sl, (1 - sr / ss if ss else float("nan"))


rows = []
for i, ses in enumerate(sessions, 1):
    t0, t1 = ses[0][0], ses[-1][0]
    span = (t1 - t0).total_seconds() / 3600
    p0, p1 = ses[0][1], ses[-1][1]
    drop = p0 - p1
    if span <= 0 or drop <= 0:
        print(f"会话{i}: {t0} 数据异常 (span={span:.2f}h drop={drop}) 跳过\n")
        continue
    xs = [(s[0] - t0).total_seconds() / 3600 for s in ses]
    sp, r2 = lin(xs, [float(s[1]) for s in ses])
    sv, r2v = lin(xs, [float(s[2]) for s in ses])
    # 段内业务事件数（衡量亮屏干扰）
    nacts = sum(1 for a in acts if t0 <= a <= t1 + timedelta(seconds=180))
    rate = drop / span
    rows.append(dict(i=i, t0=t0, t1=t1, span=span, p0=p0, p1=p1, drop=drop,
                     rate=rate, slope=sp, r2=r2, sv=sv, nacts=nacts, n=len(ses)))
    print(f"会话{i}: {t0:%m-%d %H:%M} → {t1:%m-%d %H:%M}  {span:5.2f}h  "
          f"{p0:3d}%→{p1:3d}%  Δ{drop:3d}pp  平均 {rate:5.2f} %/h  "
          f"回归 {-sp:5.2f} %/h(R²={r2:.3f})  静置电压 {-sv:4.0f}mV/h  业务事件 {nacts}")

# ---- 汇总 -----------------------------------------------------------------
tot_h = sum(r["span"] for r in rows)
tot_pp = sum(r["drop"] for r in rows)
print(f"\n=== 汇总（全部 {len(rows)} 段）===")
print(f"  放电总时长 {tot_h:.2f}h，总消耗 {tot_pp}pp  →  全局平均 {tot_pp/tot_h:.2f} %/h")
print(f"  按全局平均外推 100%→0%: {100/(tot_pp/tot_h):.1f}h")

clean = [r for r in rows if r["nacts"] == 0]
if clean:
    ch = sum(r["span"] for r in clean)
    cp = sum(r["drop"] for r in clean)
    print(f"\n  【无业务事件(纯熄屏静置)的 {len(clean)} 段】{ch:.2f}h / {cp}pp "
          f"→ {cp/ch:.2f} %/h，外推 {100/(cp/ch):.1f}h")

# ---- 按 pct 分桶：真实续航积分 --------------------------------------------
print("\n=== 按电量分桶的实测速率（10pp 一桶）===")
buckets: dict[int, list] = defaultdict(list)   # 桶下界 -> [(hours, pp)]
for ses in sessions:
    for a, b in zip(ses, ses[1:]):
        dt = (b[0] - a[0]).total_seconds() / 3600
        dp = a[1] - b[1]
        if dt <= 0 or dp <= 0 or dt > 0.5:      # 跳过断档（>30min 视为日志缺失）
            continue
        buckets[(b[1] // 10) * 10].append((dt, dp))

total_hours = 0.0
for lo in sorted(buckets, reverse=True):
    data = buckets[lo]
    h = sum(d for d, _ in data)
    pp = sum(p for _, p in data)
    rate = pp / h if h else 0
    hours_per_pp = h / pp if pp else 0
    # 该桶跨越 10pp，耗时 = 10 * hours_per_pp
    seg_hours = 10 * hours_per_pp
    total_hours += seg_hours
    print(f"  {lo:3d}~{lo+10:3d}%: 实测 {h:5.2f}h 耗 {pp:4d}pp → {rate:5.2f} %/h   "
          f"每1% {hours_per_pp*60:5.1f}min   该桶10pp需 {seg_hours:5.2f}h")

print(f"\n  已实测覆盖桶累加 = {total_hours:.1f}h（对应 {sum(10 for _ in buckets)}pp 区间）")
low = min(buckets) if buckets else None
if low is not None and low > 0:
    # 低电量未实测区（0~low）按最低实测桶速率外推
    data = buckets[low]
    h = sum(d for d, _ in data)
    pp = sum(p for _, p in data)
    rate_low = pp / h if h else 0
    extra = low / rate_low if rate_low else 0
    print(f"  未实测低段 0~{low}% 按最低桶 {rate_low:.2f} %/h 外推 = {extra:.1f}h")
    print(f"\n  ★ 100%→0% 真实续航估算 = {total_hours:.1f} + {extra:.1f} = "
          f"{total_hours + extra:.1f}h")
else:
    print(f"\n  ★ 100%→0% 真实续航估算 = {total_hours:.1f}h")

# ---- 供电状态时间线 -------------------------------------------------------
print("\n=== 供电状态切换时间线（vbus/chg 变化）===")
prev = None
for s in samples:
    st = (s[3], s[4])
    if st != prev:
        kind = {0: "电池放电", 1: "充电中", 2: "USB(未充电)"}.get((s[4] << 1) | s[3], str(st))
        print(f"  {s[0]:%m-%d %H:%M}  pct={s[1]:3d}%  {s[2]}mV  chg={s[3]} vbus={s[4]}  → {kind}")
        prev = st
