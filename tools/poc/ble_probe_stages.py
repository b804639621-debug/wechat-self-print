#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ble_probe_stages.py —— Task 10B S0 辅助：分段计时定位 WinRT BLE 建连慢的来源。

背景：bleak WinRT connect() 实测 5~13s（远超预算 0.5s）。bleak 内部流程 =
  ① find_device_by_address（广告 watcher 扫描等待）
  ② BluetoothLEDevice.from_bluetooth_address_async（设备对象创建）
  ③ GattSession.from_device_id_async + maintain_connection=True → 等 ACTIVE
  ④ GetGattServicesAsync（服务发现，CACHED/UNCACHED）
本脚本直接调 WinRT API 逐段计时，N 轮统计每段耗时，定位瓶颈。

用法：.venv/Scripts/python tools/poc/ble_probe_stages.py 20:6E:F1:16:D9:2E [-n 8]
"""

import asyncio
import statistics
import sys
import time

from bleak import BleakScanner
from winrt.windows.devices.bluetooth import (
    BluetoothAddressType,
    BluetoothCacheMode,
    BluetoothLEDevice,
)
from winrt.windows.devices.bluetooth.genericattributeprofile import (
    GattSession,
    GattSessionStatus,
)

UUID16_FMT = "0000{:04x}-0000-1000-8000-00805f9b34fb"


def mac_to_int(mac: str) -> int:
    return int(mac.replace(":", ""), 16)


async def probe_round(mac: str, idx: int, cache: bool, gap: float, foruuid: bool):
    t = {}
    t0 = time.perf_counter()

    # ① 扫描等待（bleak connect 的第一步，缓存地址直连时也会执行）
    dev = await BleakScanner.find_device_by_address(mac, timeout=10.0)
    t["scan"] = time.perf_counter() - t0
    if dev is None:
        raise RuntimeError("scan timeout")

    # ② 设备对象创建
    t1 = time.perf_counter()
    ble_dev = await BluetoothLEDevice.from_bluetooth_address_with_bluetooth_address_type_async(
        mac_to_int(mac), BluetoothAddressType.PUBLIC
    )
    t["from_addr"] = time.perf_counter() - t1
    if ble_dev is None:
        raise RuntimeError("from_bluetooth_address_async returned None")

    # ③ GattSession 建立 + 等 ACTIVE
    t2 = time.perf_counter()
    session = await GattSession.from_device_id_async(ble_dev.bluetooth_device_id)
    loop = asyncio.get_running_loop()
    active_ev = asyncio.Event()

    def on_status(sender, args):
        if args.status == GattSessionStatus.ACTIVE:
            loop.call_soon_threadsafe(active_ev.set)

    token = session.add_session_status_changed(on_status)
    if session.session_status == GattSessionStatus.ACTIVE:
        active_ev.set()
    session.maintain_connection = True
    t["session_create"] = time.perf_counter() - t2
    await asyncio.wait_for(active_ev.wait(), timeout=10.0)
    t["session_active"] = time.perf_counter() - t2

    # ④ 服务发现（UNCACHED = 真实 GATT 全枚举；CACHED = Windows 缓存【实测对未配对
    #    设备不生效】；foruuid = 只按 UUID 查自定义服务 + 其 characteristics）
    t3 = time.perf_counter()
    if foruuid:
        import uuid as _uuid
        res = await ble_dev.get_gatt_services_for_uuid_async(
            _uuid.UUID("6e410000-3f2b-4f5c-9a01-9d6a3b21c001"))
        svc = res.services[0]
        ch = await svc.get_characteristics_async()
        n_svc = f"1svc/{len(ch.characteristics)}chr"
    else:
        mode = BluetoothCacheMode.CACHED if cache else BluetoothCacheMode.UNCACHED
        res = await ble_dev.get_gatt_services_with_cache_mode_async(mode)
        n_svc = len(res.services) if res.services else 0
    t["services"] = time.perf_counter() - t3

    t["total"] = time.perf_counter() - t0

    print(f"[{idx:2d}] scan={t['scan']*1000:6.0f}  from_addr={t['from_addr']*1000:6.0f}  "
          f"sess_create={t['session_create']*1000:5.0f}  sess_active={t['session_active']*1000:6.0f}  "
          f"services={t['services']*1000:6.0f}({n_svc}svc)  total={t['total']*1000:6.0f}ms")

    # 清理：断开 session
    session.maintain_connection = False
    session.remove_session_status_changed(token)
    session.close()
    ble_dev.close()
    return t


async def probe_round_direct(mac: str, idx: int):
    """极简路径：不开扫描、不开 GattSession——from_addr → for_uuid 服务查询
    （内部触发连接）→ characteristic 查询 → write 一次。验证"连接与发现并行"。"""
    import uuid as _uuid
    from winrt.windows.storage.streams import DataWriter

    SVC = _uuid.UUID("6e410000-3f2b-4f5c-9a01-9d6a3b21c001")
    CHR = _uuid.UUID("6e410001-3f2b-4f5c-9a01-9d6a3b21c001")
    t = {}
    t0 = time.perf_counter()
    ble_dev = await BluetoothLEDevice.from_bluetooth_address_with_bluetooth_address_type_async(
        mac_to_int(mac), BluetoothAddressType.PUBLIC
    )
    t["from_addr"] = time.perf_counter() - t0
    if ble_dev is None:
        raise RuntimeError("from_bluetooth_address_async returned None")

    t1 = time.perf_counter()
    # 查询触发建连；连接未就绪时 Windows 直接报 0x80070016（ERROR_BAD_COMMAND），
    # 不等待——已知 workaround：重试直至建连完成（此处测的正是"触发建连→ATT 可用"耗时）
    svc = None
    for attempt in range(30):
        try:
            res = await ble_dev.get_gatt_services_for_uuid_async(SVC)
            if res.services:
                svc = res.services[0]
                break
        except OSError:
            pass
        await asyncio.sleep(0.2)
    if svc is None:
        raise RuntimeError("svc query retry exhausted (connect never became ready)")
    t["svc"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    chres = await svc.get_characteristics_async()
    ch = [c for c in chres.characteristics if c.uuid == CHR][0]
    t["chr"] = time.perf_counter() - t2

    t3 = time.perf_counter()
    w = DataWriter()
    w.write_bytes(b'{"display":"off"}')
    wr = await ch.write_value_with_result_async(w.detach_buffer())
    t["write"] = time.perf_counter() - t3

    t["total"] = time.perf_counter() - t0
    print(f"[{idx:2d}] from_addr={t['from_addr']*1000:5.0f}  svc={t['svc']*1000:6.0f}  "
          f"chr={t['chr']*1000:6.0f}  write={t['write']*1000:5.0f}(st={wr.status})  "
          f"total={t['total']*1000:6.0f}ms")

    ble_dev.close()
    return t


async def probe_round_e2e(mac: str, idx: int):
    """端到端冷唤醒（生产路径原型）：from_addr → 建连+发现 → 开 drawn notify →
    write display=on（含 qr_content）→ 等固件画完二维码 → write display=off → 断开。
    全链路 = 托盘点击 → 屏上二维码画完。QR 用贴近真实的 49 字符微信登录 URL。"""
    import json
    import uuid as _uuid
    from winrt.windows.storage.streams import DataWriter
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattClientCharacteristicConfigurationDescriptorValue,
    )

    SVC = _uuid.UUID("6e410000-3f2b-4f5c-9a01-9d6a3b21c001")
    CHR_STATE = _uuid.UUID("6e410001-3f2b-4f5c-9a01-9d6a3b21c001")
    CHR_LINK = _uuid.UUID("6e410003-3f2b-4f5c-9a01-9d6a3b21c001")
    QR_SAMPLE = "http://weixin.qq.com/x/" + "AbC1" * 6 + "Qr9z"

    t = {}
    t0 = time.perf_counter()
    ble_dev = await BluetoothLEDevice.from_bluetooth_address_with_bluetooth_address_type_async(
        mac_to_int(mac), BluetoothAddressType.PUBLIC
    )
    t["from_addr"] = time.perf_counter() - t0
    if ble_dev is None:
        raise RuntimeError("from_bluetooth_address_async returned None")

    drawn_ev = asyncio.Event()
    fw_ms = None
    loop = asyncio.get_running_loop()

    def on_link(sender, args):
        nonlocal fw_ms
        try:
            msg = json.loads(bytes(bytearray(args.characteristic_value)).decode())
        except Exception:
            return
        if msg.get("drawn"):
            fw_ms = msg.get("ms")
            loop.call_soon_threadsafe(drawn_ev.set)

    # 建连 + 服务/特征发现（重试直至建连完成）
    t1 = time.perf_counter()
    svc = None
    for _ in range(30):
        try:
            res = await ble_dev.get_gatt_services_for_uuid_async(SVC)
            if res.services:
                svc = res.services[0]
                break
        except OSError:
            pass
        await asyncio.sleep(0.2)
    if svc is None:
        raise RuntimeError("svc query retry exhausted")
    chres = await svc.get_characteristics_async()
    chr_state = [c for c in chres.characteristics if c.uuid == CHR_STATE][0]
    chr_link = [c for c in chres.characteristics if c.uuid == CHR_LINK][0]
    t["connect"] = time.perf_counter() - t1

    # 订阅 link_status（drawn ack 通道）
    tkn = chr_link.add_value_changed(on_link)
    await chr_link.write_client_characteristic_configuration_descriptor_with_result_async(
        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
    t["cccd"] = time.perf_counter() - t1

    # write display=on → 等 drawn
    t2 = time.perf_counter()

    def mkwrite(payload: dict):
        w = DataWriter()
        w.write_bytes(json.dumps(payload).encode())
        return w.detach_buffer()

    await chr_state.write_value_with_result_async(mkwrite({
        "state": "IDLE", "display": "on", "qr_content": QR_SAMPLE,
        "file_count": 0, "message": ""}))
    t["write_on"] = time.perf_counter() - t2
    await asyncio.wait_for(drawn_ev.wait(), timeout=10.0)
    t["drawn"] = time.perf_counter() - t0   # 全链路：from_addr 起 → 屏上画完
    t["fw_ms"] = fw_ms

    await chr_state.write_value_with_result_async(mkwrite({"display": "off"}))
    t["total"] = time.perf_counter() - t0

    print(f"[{idx:2d}] from_addr={t['from_addr']*1000:4.0f}  connect+disc={t['connect']*1000:6.0f}  "
          f"cccd={t['cccd']*1000:6.0f}  write_on={t['write_on']*1000:4.0f}  "
          f"drawn={t['drawn']*1000:6.0f}ms (fw={fw_ms}ms)")

    chr_link.remove_value_changed(tkn)
    ble_dev.close()
    return t


async def main():
    mac = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    cache = "--cache" in sys.argv
    foruuid = "--foruuid" in sys.argv
    direct = "--direct" in sys.argv
    e2e = "--e2e" in sys.argv
    gap = 5.0 if "--gap5" in sys.argv else 1.0
    rows = []
    for i in range(1, n + 1):
        try:
            if e2e:
                rows.append(await probe_round_e2e(mac, i))
            elif direct:
                rows.append(await probe_round_direct(mac, i))
            else:
                rows.append(await probe_round(mac, i, cache, gap, foruuid))
        except Exception as e:
            import traceback
            print(f"[{i:2d}] FAIL {type(e).__name__}: {e}")
            if i == 1:
                traceback.print_exc()
        await asyncio.sleep(gap)

    if rows:
        print("\n== 分段均值(ms) ==")
        if e2e:
            keys = ["from_addr", "connect", "cccd", "write_on", "drawn"]
        elif direct:
            keys = ["from_addr", "svc", "chr", "write", "total"]
        else:
            keys = ["scan", "from_addr", "session_create", "session_active", "services", "total"]
        for k in keys:
            xs = [r[k] * 1000 for r in rows]
            print(f"  {k:14s} avg={statistics.mean(xs):7.0f}  min={min(xs):6.0f}  max={max(xs):6.0f}")


if __name__ == "__main__":
    asyncio.run(main())
