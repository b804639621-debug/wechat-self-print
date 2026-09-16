"""§2.4 唤醒时延自动化：持有 COM3 打时间戳日志，循环 N 次
POST /api/screen/wake → 等串口 "[lcd] screen on ok"（量唤醒时延）
→ 等 "[lcd] screen off ok"（180s 自动熄屏）。

用法：.venv/Scripts/python.exe tools/poc/wake_cycles.py [次数] [输出日志]
期间所有串口行均带时间戳落盘，兼作轮询间隔/花屏重绘([perf] drawQR)数据源。
"""
import sys
import threading
import time
import urllib.request
from pathlib import Path

import serial

CYCLES = int(sys.argv[1]) if len(sys.argv) > 1 else 10
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("trace_logs/wake_cycles.log")
WAKE_URL = "http://127.0.0.1:8000/api/screen/wake"

_lines: list[tuple[float, str]] = []   # (monotonic, text)
_lock = threading.Lock()
_stop = threading.Event()


def _reader(ser: serial.Serial, out) -> None:
    while not _stop.is_set():
        data = ser.readline()
        if data:
            mono = time.monotonic()
            wall = time.time()
            text = data.decode("utf-8", "replace").rstrip()
            with _lock:
                _lines.append((mono, text))
            ts = time.strftime("%H:%M:%S", time.localtime(wall)) + f".{int(wall * 1000) % 1000:03d}"
            out.write(f"{ts} {text}\n".encode("utf-8", "replace"))


def _wait_for(pattern: str, since: float, timeout: float, out) -> tuple[float, str] | None:
    deadline = time.monotonic() + timeout
    seen = 0
    while time.monotonic() < deadline:
        with _lock:
            for i in range(seen, len(_lines)):
                m, t = _lines[i]
                if m >= since and pattern in t:
                    return m, t
            seen = len(_lines)
        time.sleep(0.05)
    out.write(f"!! timeout waiting '{pattern}'\n".encode())
    return None


def main() -> None:
    with serial.Serial("COM3", 115200, timeout=1) as ser, OUT.open("ab", buffering=0) as out:
        out.write(f"\n=== wake_cycles start {time.strftime('%H:%M:%S')} cycles={CYCLES} ===\n".encode())
        th = threading.Thread(target=_reader, args=(ser, out), daemon=True)
        th.start()
        lat: list[float] = []
        for i in range(1, CYCLES + 1):
            t0 = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(WAKE_URL, method="POST"), timeout=5) as r:
                    r.read()
            except Exception as exc:
                out.write(f"WAKE_CYCLE {i} POST fail: {exc}\n".encode())
                time.sleep(5)
                continue
            hit = _wait_for("[lcd] screen on ok", t0, 20, out)
            if hit:
                m, _ = hit
                lat.append(m - t0)
                out.write(f"WAKE_CYCLE {i} on_latency={m - t0:.2f}s\n".encode())
            else:
                out.write(f"WAKE_CYCLE {i} on_latency=TIMEOUT\n".encode())
            off = _wait_for("[lcd] screen off ok", t0, 300, out)
            if off:
                out.write(f"WAKE_CYCLE {i} off_after={off[0] - t0:.1f}s\n".encode())
            else:
                out.write(f"WAKE_CYCLE {i} off TIMEOUT（中止后续循环）\n".encode())
                break
        if lat:
            lat.sort()
            out.write(f"WAKE_SUMMARY n={len(lat)} min={lat[0]:.2f}s median={lat[len(lat)//2]:.2f}s max={lat[-1]:.2f}s\n".encode())
        out.write(f"=== wake_cycles end {time.strftime('%H:%M:%S')} ===\n".encode())
        _stop.set()
        th.join(timeout=3)


if __name__ == "__main__":
    main()
