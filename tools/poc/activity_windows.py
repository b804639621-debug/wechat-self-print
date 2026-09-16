# -*- coding: utf-8 -*-
"""分析 2026-09-10 电池放电期间的"活跃窗口"（亮屏/顾客会话）与轮询节拍。
耗电对比必须扣掉亮屏时段，否则新旧数据不可比。"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path

LOG = Path(r"E:\kaifa\自助打印\dist\logs\printshop.log")
DAY = "2026-09-10"
lines = [ln for ln in LOG.read_text(encoding="utf-8", errors="replace").splitlines() if ln.startswith(DAY)]


def ts(ln):
    return datetime.strptime(ln[:19], "%Y-%m-%d %H:%M:%S")


# 放电窗口
DIS0 = datetime(2026, 9, 10, 9, 4, 35)
DIS1 = datetime(2026, 9, 10, 18, 13, 55)

# 1) 活跃事件（微信业务 + wake + jobs）
ACT = re.compile(r"jslogin\?appid|/qrcode/|/api/screen/wake|webwxnewloginpage|webwxinit|"
                 r"webwxsendmsg|webwxgetmedia|webwxgetmsgimg|webwxlogout|/api/jobs|/api/service/finish")
acts = [(ts(ln), ln) for ln in lines if ACT.search(ln)]
act_times = sorted({t for t, _ in acts})

# 把相邻 <15min 的事件聚成"会话簇"
clusters = []
for t in act_times:
    if clusters and (t - clusters[-1][-1]).total_seconds() <= 900:
        clusters[-1].append(t)
    else:
        clusters.append([t])

print(f"=== {DAY} 业务事件簇（放电窗口 {DIS0.strftime('%H:%M')}~{DIS1.strftime('%H:%M')}） ===")
total_active = timedelta()
for c in clusters:
    start, end = c[0], c[-1]
    in_dis = not (end < DIS0 or start > DIS1)
    # 亮屏到自动熄屏约 180s（计划 §2.3），每次会话后加 3 分钟尾巴
    tail = end + timedelta(seconds=180)
    dur = (tail - start).total_seconds() / 60
    if in_dis:
        total_active += (tail - start)
    print(f"  {start.strftime('%H:%M:%S')}~{end.strftime('%H:%M:%S')} (+180s 熄屏)  "
          f"事件 {len(c)} 条  约 {dur:.0f} 分钟  {'← 放电期内' if in_dis else '(窗口外)'}")

dis_h = (DIS1 - DIS0).total_seconds() / 3600
act_h = total_active.total_seconds() / 3600
print(f"\n放电时长 {dis_h:.2f}h，其中活跃(推断亮屏) {act_h:.2f}h = {act_h/dis_h*100:.0f}%；"
      f"熄屏静置 {dis_h-act_h:.2f}h = {(dis_h-act_h)/dis_h*100:.0f}%")

# 2) 轮询节拍：放电期内 vs 活跃期内
poll = [(ts(ln), ln) for ln in lines if "/api/screen/state" in ln]
in_dis = [t for t, _ in poll if DIS0 <= t <= DIS1]
gaps = [(b - a).total_seconds() for a, b in zip(in_dis, in_dis[1:])]
from collections import Counter
hist = Counter(int(round(g)) for g in gaps)
print(f"\n放电期轮询 {len(in_dis)} 次，间隔分布 {dict(sorted(hist.items(), key=lambda kv: -kv[1])[:6])}")
fast = [t for t, g in zip(in_dis, gaps) if g < 3]
print(f"出现 <3s（亮屏 1.5s 档）的时刻数={len(fast)}"
      + (f"，例如 {[t.strftime('%H:%M') for t in fast[:12]]}" if fast else " → 全天恒为熄屏 5s 档"))

# 3) 电压骤降点（亮屏瞬间负载压降）
samples = [(ts(ln), int(re.search(r"batt_mv=(\d+)", ln).group(1)), int(re.search(r"batt_pct=(\d+)", ln).group(1)))
           for ln in lines if "batt_mv=" in ln and "/api/screen/state" in ln]
dips = [(t, mv) for (t, mv, _), (t2, mv2, _) in zip(samples, samples[1:])
        if mv - mv2 >= 15 and DIS0 <= t <= DIS1]
print(f"\n放电期电压骤降(≥15mV)次数={len(dips)}（亮屏瞬间负载压降特征）")
for t, mv in dips[:20]:
    print(f"     {t.strftime('%H:%M:%S')}  →{mv}mV")
