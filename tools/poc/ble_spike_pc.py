#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_spike_pc.py —— Task 10B Step S0：PC 侧 BLE 测量脚本（配合 firmware/ble_spike）

子命令：
  scan                          扫描 5s，列出周边 BLE 设备（找 PRINT-SCREEN 与地址/RSSI）
  latency --addr MAC [-n 20]    冷唤醒全链路测量（默认"缓存地址直连、不扫描"，
                                每次脚本启动都是新进程——本身就是"主控进程重启后首连"验证）：
                                  connect → 读协商 MTU → 订阅 link_status →
                                  write screen_state(display=on, qr_content) →
                                  等固件画完二维码的 drawn notify（全链路终点）→
                                  write display=off → disconnect → 回广播
                                输出每次迭代明细 + 成功率/avg/P95/max 与 Go/No-go 判定
  battery --addr MAC            连接 12s 打印 battery_report notify（验证电池上报链路）

依赖：.venv 内 bleak 3.x（Windows WinRT backend）。
用法：.venv/Scripts/python tools/poc/ble_spike_pc.py scan
"""

import argparse
import asyncio
import json
import statistics
import sys
import time

from bleak import BleakClient, BleakScanner

DEVICE_NAME = "PRINT-SCREEN"
SVC_UUID   = "6e410000-3f2b-4f5c-9a01-9d6a3b21c001"
CHR_STATE  = "6e410001-3f2b-4f5c-9a01-9d6a3b21c001"  # screen_state   Write
CHR_BATT   = "6e410002-3f2b-4f5c-9a01-9d6a3b21c001"  # battery_report Notify
CHR_LINK   = "6e410003-3f2b-4f5c-9a01-9d6a3b21c001"  # link_status    Read+Notify（drawn ack）

# 贴近真实微信登录二维码长度（约 48 字符）；v7+ECC_MEDIUM 容量 122B 足够
QR_SAMPLE = "http://weixin.qq.com/x/" + "AbC1" * 6 + "Qr9z"  # 22+27=49 字符


def pct95(xs):
    if not xs:
        return 0.0
    if len(xs) < 20:
        return max(xs)
    return statistics.quantiles(xs, n=100)[94]


async def cmd_scan(args):
    print("扫描 5s ...")
    devs = await BleakScanner.discover(timeout=5.0, return_adv=True)
    for addr, (dev, adv) in sorted(devs.items(), key=lambda kv: kv[1][1].rssi, reverse=True):
        name = dev.name or adv.local_name or ""
        mark = "  <== PRINT-SCREEN" if name == DEVICE_NAME else ""
        print(f"  {addr}  rssi={adv.rssi:4d}  name={name!r}{mark}")


async def one_round(addr, idx, scan_first=False):
    """一轮冷唤醒测量。返回 dict 或抛异常。"""
    t0 = time.perf_counter()
    target = addr
    if scan_first:
        dev = await BleakScanner.find_device_by_name(DEVICE_NAME, timeout=8.0)
        if dev is None:
            raise RuntimeError("scan 未找到设备")
        target = dev

    drawn_ev = asyncio.Event()
    fw_ms = None

    def on_link(_, data: bytearray):
        nonlocal fw_ms
        try:
            msg = json.loads(bytes(data).decode())
        except Exception:
            return
        if msg.get("drawn"):
            fw_ms = msg.get("ms")
            drawn_ev.set()

    async with BleakClient(target, timeout=10.0) as client:
        t_conn = time.perf_counter()
        await client.start_notify(CHR_LINK, on_link)
        mtu = getattr(client, "mtu_size", None)

        payload = json.dumps({
            "state": "IDLE", "display": "on", "qr_content": QR_SAMPLE,
            "file_count": 0, "message": "",
        })
        await client.write_gatt_char(CHR_STATE, payload.encode(), response=True)
        t_written = time.perf_counter()

        await asyncio.wait_for(drawn_ev.wait(), timeout=8.0)
        t_drawn = time.perf_counter()

        await client.write_gatt_char(CHR_STATE, json.dumps({"display": "off"}).encode(), response=True)

    return {
        "idx": idx,
        "connect_ms": (t_conn - t0) * 1000,
        "write_ms": (t_written - t_conn) * 1000,
        "drawn_ms": (t_drawn - t0) * 1000,   # 全链路：发起连接 → 屏上画完二维码
        "fw_ms": fw_ms,                       # 固件侧：ALDO3 上电 → 画完
        "mtu": mtu,
    }


async def cmd_latency(args):
    results, fails = [], 0
    for i in range(1, args.n + 1):
        try:
            r = await one_round(args.addr, i, scan_first=args.scan_first)
            results.append(r)
            print(f"[{i:2d}] ok  conn={r['connect_ms']:6.0f}ms  write={r['write_ms']:6.0f}ms  "
                  f"drawn={r['drawn_ms']:6.0f}ms  fw={r['fw_ms']}ms  mtu={r['mtu']}")
        except Exception as e:
            fails += 1
            print(f"[{i:2d}] FAIL  {type(e).__name__}: {e}")
        await asyncio.sleep(1.0)   # 等固件回广播（500ms 间隔，直连也需先收广播包）

    if not results:
        print("全部失败，无统计")
        return 1

    drawn = [r["drawn_ms"] for r in results]
    conn = [r["connect_ms"] for r in results]
    mtus = {r["mtu"] for r in results}
    print("\n===== 汇总 =====")
    print(f"成功率     : {len(results)}/{args.n} = {len(results)/args.n*100:.0f}%")
    print(f"建连       : avg={statistics.mean(conn):.0f}ms  p95={pct95(conn):.0f}ms  max={max(conn):.0f}ms")
    print(f"全链路drawn: avg={statistics.mean(drawn):.0f}ms  p95={pct95(drawn):.0f}ms  max={max(drawn):.0f}ms")
    print(f"协商 MTU   : {sorted(mtus)}")
    print("\n===== Go/No-go 参考 =====")
    print(f"连接成功率 ≥90%        : {'PASS' if len(results)/args.n >= 0.9 else 'FAIL'}")
    ok_typ = statistics.mean(drawn) <= 2500
    ok_max = max(drawn) <= 3000
    print(f"冷唤醒典型 ≤2.5s(avg)  : {'PASS' if ok_typ else 'FAIL'}")
    print(f"冷唤醒最坏 ≤3.0s(max)  : {'PASS' if ok_max else 'FAIL'}")
    print(f"MTU ≥185               : {'PASS' if all(m and m >= 185 for m in mtus) else 'FAIL'}")
    return 0


async def cmd_battery(args):
    def on_batt(_, data: bytearray):
        print(f"  battery_report: {bytes(data).decode(errors='replace')}")

    async with BleakClient(args.addr, timeout=10.0) as client:
        print(f"已连接，MTU={getattr(client, 'mtu_size', '?')}，等 12s 电池 notify ...")
        await client.start_notify(CHR_BATT, on_batt)
        link = await client.read_gatt_char(CHR_LINK)
        print(f"  link_status(read): {bytes(link).decode(errors='replace')}")
        await asyncio.sleep(12.0)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan")
    p = sub.add_parser("latency")
    p.add_argument("--addr", required=True, help="小屏 BLE MAC（scan 子命令获取）")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--scan-first", action="store_true", help="每轮先扫描再连（对照组；默认缓存地址直连）")
    p = sub.add_parser("battery")
    p.add_argument("--addr", required=True)
    args = ap.parse_args()

    fn = {"scan": cmd_scan, "latency": cmd_latency, "battery": cmd_battery}[args.cmd]
    rc = asyncio.run(fn(args))
    sys.exit(rc if isinstance(rc, int) else 0)


if __name__ == "__main__":
    main()
