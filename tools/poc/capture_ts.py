"""带时间戳的 COM3 被动监听（自动重连），输出到指定日志。

用法：.venv/Scripts/python.exe tools/poc/capture_ts.py <秒数> <输出文件>
"""
import sys
import time
from pathlib import Path

import serial
from serial.serialutil import SerialException

SECS = float(sys.argv[1])
OUT = Path(sys.argv[2])


def main() -> None:
    deadline = time.monotonic() + SECS
    with OUT.open("ab", buffering=0) as out:
        out.write(f"\n=== capture start {time.strftime('%H:%M:%S')} ===\n".encode())
        while time.monotonic() < deadline:
            try:
                with serial.Serial("COM3", 115200, timeout=1) as ser:
                    while time.monotonic() < deadline:
                        data = ser.readline()
                        if data:
                            ts = time.strftime("%H:%M:%S") + f".{int(time.time()*1000)%1000:03d}"
                            out.write(ts.encode() + b" " + data)
            except (SerialException, OSError) as exc:
                out.write(f"=== disconnected: {exc} ===\n".encode())
                time.sleep(1)


if __name__ == "__main__":
    main()
