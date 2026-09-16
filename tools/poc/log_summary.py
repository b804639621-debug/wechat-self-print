# -*- coding: utf-8 -*-
"""摘要 dist/logs/printshop.log 中指定日期的事件（自动过滤轮询噪声）。"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

LOG = Path(r"E:\kaifa\自助打印\dist\logs\printshop.log")
DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-09-10"

lines = LOG.read_text(encoding="utf-8", errors="replace").splitlines()
today = [ln for ln in lines if ln.startswith(DAY)]
print(f"日志文件: {LOG}")
print(f"{DAY} 总行数: {len(today)}")
if not today:
    sys.exit(0)


def ts(line: str) -> datetime:
    return datetime.strptime(line[:19], "%Y-%m-%d %H:%M:%S")


print(f"时段: {today[0][:19]}  →  {today[-1][:19]}")

# ---- 分类统计 -------------------------------------------------------------
NOISE = ("synccheck", "webwxsync", "/api/screen/state", "api.screen.state")
buckets: Counter[str] = Counter()
events: list[str] = []
for ln in today:
    low = ln.lower()
    if any(n in low for n in NOISE):
        buckets["轮询/微信sync噪声"] += 1
        continue
    buckets["其他"] += 1
    events.append(ln)

print("\n=== 非噪声行分类 ===")
for k, v in buckets.most_common():
    print(f"  {k}: {v}")

# ---- 关键业务事件 ---------------------------------------------------------
print("\n=== 业务/生命周期事件 ===")
PATTERNS = [
    ("启动/关闭", r"Started server process|Application startup complete|Waiting for application|Uvicorn running|shutdown|Uvicorn running"),
    ("GUI窗口", r"printshop\.gui"),
    ("二维码生成(jslogin)", r"jslogin\?appid"),
    ("二维码图片", r"login\.weixin\.qq\.com/qrcode"),
    ("亮屏(wake)", r"/api/screen/wake"),
    ("扫码登录(login page)", r"webwxnewloginpage"),
    ("初始化(webwxinit)", r"webwxinit"),
    ("发消息给顾客(sendmsg)", r"webwxsendmsg"),
    ("收文件(getmedia/getmsgimg)", r"webwxgetmedia|webwxgetmsgimg"),
    ("登出(logout)", r"webwxlogout"),
    ("管理页/admin", r"GET /admin|POST /api/"),
    ("打印/submit", r"print|submit|xps", ),
    ("错误/异常", r"ERROR|Traceback|Exception|失败|error"),
    ("WARNING", r"WARNING|警告"),
]
for label, pat in PATTERNS:
    hits = [ln for ln in events if re.search(pat, ln, re.I)]
    if hits:
        print(f"\n--- {label}  ({len(hits)} 条) ---")
        for h in hits[:40]:
            print("   " + h[:200])
        if len(hits) > 40:
            print(f"   ... 其余 {len(hits)-40} 条省略")

# ---- 轮询节拍分析 ---------------------------------------------------------
print("\n=== 屏轮询节拍（判定省电档2 是否生效） ===")
poll = [ln for ln in today if "/api/screen/state" in ln]
if len(poll) > 2:
    times = [ts(p) for p in poll]
    gaps = [(b - a).total_seconds() for a, b in zip(times, times[1:])]
    hist: dict[int, int] = defaultdict(int)
    for g in gaps:
        hist[int(round(g))] += 1
    print(f"  轮询总数={len(poll)}  间隔分布(秒:次数)={dict(sorted(hist.items(), key=lambda kv: -kv[1])[:8])}")
    print(f"  最小={min(gaps):.2f}s 最大={max(gaps):.2f}s 中位={sorted(gaps)[len(gaps)//2]:.2f}s")
    big = [(t0, g) for t0, g in zip(times, gaps) if g > 10]
    print(f"  >10s 的空档数={len(big)}")
    for t0, g in big[:15]:
        print(f"     空档 {t0.strftime('%H:%M:%S')} 起 {g:.0f}s")

# ---- 电量采样点（只打印变化） --------------------------------------------
print("\n=== 电量采样（仅打印 pct/vbus 变化点） ===")
prev = None
for ln in poll:
    m = re.search(r"batt_pct=(\d+)&batt_mv=(\d+)&batt_chg=(\d+)&batt_vbus=(\d+)", ln)
    if not m:
        continue
    cur = m.groups()
    if cur != prev:
        print(f"  {ln[:19]}  pct={cur[0]}%  {cur[1]}mV  chg={cur[2]}  vbus={cur[3]}")
        prev = cur
