"""COM3 串口监听：跨 USB 拔插自动重连，输出到 trace_logs/com3_boot.log。

烧录/重启实验期间抓固件 Serial 日志用， Ctrl-C 或 TaskStop 结束。
"""
import time
from pathlib import Path

import serial
from serial.serialutil import SerialException

LOG = Path("trace_logs/com3_boot.log")


def main() -> None:
    with LOG.open("ab", buffering=0) as out:
        while True:
            try:
                with serial.Serial("COM3", 115200, timeout=1) as ser:
                    out.write(b"\n=== connected ===\n")
                    while True:
                        data = ser.read(256)
                        if data:
                            out.write(data)
            except (SerialException, OSError) as exc:
                out.write(f"\n=== disconnected: {exc} ===\n".encode())
                time.sleep(1)


if __name__ == "__main__":
    main()
