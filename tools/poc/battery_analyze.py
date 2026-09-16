# -*- coding: utf-8 -*-
"""一次性分析 battery_history.csv 的放电会话：斜率回归 / 分段斜率 / 电压斜率。"""
from __future__ import annotations

import csv
import sys
from datetime import datetime
from pathlib import Path

CSV = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("battery_history.csv")
FMT = "%Y-%m-%d %H:%M:%S"


def load():
    with CSV.open(encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f)]
    out = []
    for r in rows:
        out.append({
            "t": datetime.strptime(r["timestamp"], FMT),
            "pct": float(r["pct"]),
            "mv": float(r["mv"]),
            "discharge": r["vbus"].strip() == "false" and r["charging"].strip() == "false",
            "est": r["runtime_estimate_hours"],
        })
    return out


def lin(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sxx == 0:
        return None, None, None
    slope = sxy / sxx
    inter = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (slope * x + inter)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot else float("nan")
    return slope, inter, r2


def seg(name, samples):
    if len(samples) < 2:
        print(f"{name}: 样本不足")
        return
    t0 = samples[0]["t"]
    xs = [(s["t"] - t0).total_seconds() / 3600 for s in samples]
    ps = [s["pct"] for s in samples]
    vs = [s["mv"] for s in samples]
    span = xs[-1]
    sp, _, r2p = lin(xs, ps)
    sv, _, r2v = lin(xs, vs)
    drop = ps[0] - ps[-1]
    naive = drop / span if span else float("nan")
    print(f"\n[{name}] n={len(samples)}  {samples[0]['t']} → {samples[-1]['t']}  跨度={span:.2f}h")
    print(f"  pct {ps[0]:.0f}% → {ps[-1]:.0f}% (Δ{drop:.0f}pp)   简单速率={naive:.2f} %/h  →  满电续航={100/naive:.1f}h")
    print(f"  回归斜率={-sp:.2f} %/h (R²={r2p:.4f})  →  100%/{-sp:.1f} = {100/-sp:.1f}h")
    print(f"  电压 {vs[0]:.0f}mV → {vs[-1]:.0f}mV   压降斜率={-sv:.0f} mV/h (R²={r2v:.4f})")
    print(f"  实测容量消耗={(vs[0]-vs[-1]):.0f}mV / {drop:.0f}pp = {(vs[0]-vs[-1])/drop:.1f} mV/pp")


rows = load()
print(f"文件: {CSV.resolve()}")
print(f"总样本={len(rows)}  首={rows[0]['t']}  末={rows[-1]['t']}")
dis = [r for r in rows if r["discharge"]]
print(f"放电样本={len(dis)}  充电/其他={len(rows)-len(dis)}")
print(f"最后一条电表估算 runtime_estimate_hours={[r['est'] for r in rows if r['est']] or '(空)'}")

seg("整段放电 97%→22%", dis)
seg("前段 97%→62%", [s for s in dis if s["pct"] >= 62])
seg("中段 61%→41%", [s for s in dis if 41 <= s["pct"] <= 61])
seg("尾段 41%→22%", [s for s in dis if s["pct"] <= 41])
seg("排除首个60s抖动后", dis[3:])

print("\n[校准核查] 工具写入的 runtime_estimate_hours 是否等于当时 pct：")
tot = 0
same = 0
for r in rows:
    if not r["est"]:
        continue
    tot += 1
    if abs(float(r["est"]) - r["pct"]) < 0.06:
        same += 1
    else:
        print(f"  不等同: {r['t']} pct={r['pct']:.0f} est={r['est']}")
print(f"  折算出的估算行数={tot}，其中与 pct 数值相同的={same}"
      f"（{'完全吻合 → 估算≈pct/斜率，即估算实际等于剩余电量百分比' if tot and same == tot else '存在差异'}）")

print("\n[逐点]  时间   pct   mV   与上一采样 Δt(min) / Δpct")
prev = None
for r in rows:
    if prev is None:
        print(f"  {r['t'].strftime('%H:%M:%S')}  {r['pct']:>3.0f}%  {r['mv']:>4.0f}  (起始, {'放电' if r['discharge'] else 'USB/充电'})")
    else:
        dt = (r["t"] - prev["t"]).total_seconds() / 60
        print(f"  {r['t'].strftime('%H:%M:%S')}  {r['pct']:>3.0f}%  {r['mv']:>4.0f}  Δt={dt:>4.0f}min  Δpct={prev['pct']-r['pct']:>+3.0f}"
              + ("" if r["discharge"] else "   <- USB/充电"))
    prev = r
