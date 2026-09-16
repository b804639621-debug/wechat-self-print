"""复位小屏并抓取启动日志：RTS 脉冲复位(EN)，随后捕获 N 秒串口输出。

用法：.venv/Scripts/python.exe tools/poc/reset_capture.py [秒数] [输出文件]
"""
import sys
import time
from pathlib import Path

import serial

SECS = float(sys.argv[1]) if len(sys.argv) > 1 else 45.0
OUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("trace_logs/reset_boot.log")


def main() -> None:
    with serial.Serial("COM3", 115200, timeout=1) as ser, OUT.open("wb", buffering=0) as out:
        # esptool hard_reset 同款：DTR 先显式拉低(IO9 高)，RTS=1 拉低 EN → 松回复位
        ser.dtr = False
        time.sleep(0.05)
        ser.rts = True
        time.sleep(0.3)
        ser.rts = False
        out.write(b"\n=== reset pulsed ===\n")
        deadline = time.monotonic() + SECS
        while time.monotonic() < deadline:
            data = ser.read(256)
            if data:
                out.write(data)


if __name__ == "__main__":
    main()
