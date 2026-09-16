"""§2.4 在线指示观察：连 /ws/admin，落盘 device/session 事件（含阈值、电量）。

用法：.venv/Scripts/python.exe tools/poc/ws_watch.py <秒数> <输出日志>
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import websockets

DUR = float(sys.argv[1])
OUT = Path(sys.argv[2])


async def main() -> None:
    async with websockets.connect("ws://127.0.0.1:8000/ws/admin") as ws:
        deadline = time.monotonic() + DUR
        with OUT.open("a", encoding="utf-8") as out:
            out.write(f"\n=== ws_watch start {time.strftime('%H:%M:%S')} dur={DUR:.0f}s ===\n")
            while True:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    break
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=remain)
                except asyncio.TimeoutError:
                    break
                d = json.loads(msg)
                ts = time.strftime("%H:%M:%S")
                if d.get("type") == "device":
                    s = d.get("screen") or {}
                    b = d.get("battery") or {}
                    out.write(f"{ts} device online={s.get('online')} thr={s.get('threshold_seconds')} "
                              f"last={s.get('last_seen_at')} pct={b.get('pct')} chg={b.get('charging')} "
                              f"vbus={b.get('vbus')} stale_after={b.get('stale_after')}\n")
                elif d.get("type") == "session":
                    out.write(f"{ts} session state={d.get('state')} display={d.get('display')}\n")
            out.write(f"=== ws_watch end {time.strftime('%H:%M:%S')} ===\n")


if __name__ == "__main__":
    asyncio.run(main())
