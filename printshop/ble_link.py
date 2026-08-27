"""主控 BLE Central 链路：小屏状态推送 + 电池 notify + 快速重醒 + 周期心跳。

设计要点（S0 spike 实测定论，勿推翻）：
- 不走 bleak（Windows connect 路径冷连接 5~13s 不可用），直接调 winrt API：
  from_addr(PUBLIC) → get_gatt_services_for_uuid_async 触发建连（连接未就绪时
  Windows 抛 OSError 0x80070016，0.2s 间隔重试最多 ~30 次）→ 特征枚举 →
  写/订阅。
- 坑：同一进程内 ble_dev.close() 后立即重建 from_addr 对象会拿到失效 GATT 缓存
  （特征枚举为空）；断连重建必须新建 BluetoothLEDevice 并重新走 for_uuid 重试
  全程。Windows close()/进程退出后底层连接延迟 4~18s 才真正断开，主控不等真断。
- winrt 回调在 WinRT 线程触发，回 asyncio 必须 loop.call_soon_threadsafe。
- winrt 仅在真正建连时函数内导入：无蓝牙环境与 pytest 下可安全导入本模块。

线程模型：push_state 由事件循环线程（sm.on_change）同步调用，仅覆盖 _latest 并
唤醒 worker；单 worker task 串行消费（latest 覆盖合并），所有 IO 均为 async。
heartbeat 建连不落入 _conn（走 _pending_conn，worker 可接管），避免与 worker
写屏争用同一连接句柄。

GATT 契约（固件侧已上线）：
- Service 6e410000-3f2b-4f5c-9a01-9d6a3b21c001
- 6e410001 screen_state（Write）：{"state","display","qr_content","file_count","message"}
- 6e410002 battery_report（Notify）：{"pct","mv","chg","vbus"}；订阅 CCCD 后固件立即补发一次
- 6e410003 link_status（Read+Notify）：Read 得 {"fw","uptime_s","mtu","adv_ms"}；
  Notify 为 drawn ack {"drawn":1,"ms":N}（记日志即可）
"""
import asyncio
import json
import logging
import sys
import time
from datetime import datetime

_log = logging.getLogger(__name__)

SERVICE_UUID = "6e410000-3f2b-4f5c-9a01-9d6a3b21c001"
_CHR_STATE = "6e410001-3f2b-4f5c-9a01-9d6a3b21c001"
_CHR_BATTERY = "6e410002-3f2b-4f5c-9a01-9d6a3b21c001"
_CHR_LINK = "6e410003-3f2b-4f5c-9a01-9d6a3b21c001"

# 心跳建连后等待固件补发电池首包（订阅 CCCD 后 ~2s 内）的窗口
_BATTERY_NOTIFY_WAIT = 3.0
# 建连失败后的退避窗口（秒）：窗口内不再重复尝试建连（时间戳闸）
_CONNECT_BACKOFF = 5.0
# --ble-selftest 等电池首包的窗口（建连 ~2s + 写屏 + 固件补发，8s 余量充足）
_SELFTEST_BATTERY_TIMEOUT = 8.0
# --ble-selftest 等己方连接就绪的窗口：须盖过"首次建连失败 + 5s 退避 + 重试建连"
# （强杀旧进程后的僵尸连接场景：残留连接可干扰首连，自动重试应被等到）
_SELFTEST_CONNECT_TIMEOUT = 12.0
# --ble-selftest 推 display=off 后等 worker 消费写屏的余量（单次 GATT 写远小于此）
_SELFTEST_OFF_GRACE = 1.0


def _in_test_env() -> bool:
    """pytest 环境下禁止真实 winrt 建连：单测不得触碰实体屏（FakeConnector 除外）。"""
    return "pytest" in sys.modules


def mac_to_int(mac: str) -> int:
    """'AA:BB:CC:DD:EE:FF' → int（winrt from_bluetooth_address 用整数地址）。"""
    return int(mac.replace(":", "").replace("-", ""), 16)


def _convert_battery_report(raw) -> dict | None:
    """固件电池包 {"pct","mv","chg","vbus"} → 统一上报 dict；畸形返回 None。"""
    if not isinstance(raw, dict):
        return None
    try:
        pct = int(raw["pct"])
        mv = int(raw.get("mv") or 0)
        charging = bool(raw.get("chg"))
        vbus = bool(raw.get("vbus"))
    except (KeyError, TypeError, ValueError):
        return None
    if not (0 <= pct <= 100):
        return None
    return {
        "pct": pct,
        "mv": mv,
        "charging": charging,
        "vbus": vbus,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


class _Conn:
    """winrt 连接句柄的极简封装（句柄协议即本类的五个方法，FakeConnector 同协议）。"""

    def __init__(self, write_state, read_link_status, set_battery_cb, set_link_cb, close):
        self.write_state = write_state
        self.read_link_status = read_link_status
        self.set_battery_cb = set_battery_cb
        self.set_link_cb = set_link_cb
        self.close = close


async def _winrt_connect(address_int: int, expected_name: str | None = None,
                         on_battery=None, on_link=None):
    """winrt 直连小屏（S0 spike 定论：冷建连稳定 ~2s）。

    流程：from_addr(PUBLIC) → get_gatt_services_for_uuid_async 重试建连（连接未
    就绪抛 OSError 0x80070016，0.2s 间隔最多 ~30 次）→ 特征枚举 → 订阅
    battery/link_status 两个 notify 的 CCCD → 返回连接句柄。winrt 回调在 WinRT
    线程触发，经 loop.call_soon_threadsafe 送回事件循环线程。

    on_battery/on_link 必须在写 CCCD 之前挂好（实机缺陷修复 2026-08-25）：固件在
    onSubscribe 后的下一个 30ms tick 立即补发电池首包，若回调经 _Conn.set_*_cb
    在连接返回后才挂，首包必然被静默丢弃——而心跳连接 3s 就断开等不到 60s 周期
    包，将导致电池上报永远缺失（S1 探针对照实验实证）。
    """
    if _in_test_env():
        # 防单测经 lifespan 触发真实建连碰实体屏（既有 TestClient 测试不注入 factory）
        raise RuntimeError("pytest 环境禁止真实 winrt 建连")

    import uuid as _uuid

    from winrt.windows.devices.bluetooth import (
        BluetoothAddressType,
        BluetoothLEDevice,
    )
    from winrt.windows.devices.bluetooth.genericattributeprofile import (
        GattCommunicationStatus,
        GattClientCharacteristicConfigurationDescriptorValue,
    )
    from winrt.windows.storage.streams import DataWriter

    svc_uuid = _uuid.UUID(SERVICE_UUID)
    chr_uuid = {k: _uuid.UUID(v) for k, v in (
        ("state", _CHR_STATE), ("battery", _CHR_BATTERY), ("link", _CHR_LINK))}
    loop = asyncio.get_running_loop()

    ble_dev = await BluetoothLEDevice.from_bluetooth_address_with_bluetooth_address_type_async(
        address_int, BluetoothAddressType.PUBLIC)
    if ble_dev is None:
        raise RuntimeError("from_bluetooth_address_async 返回 None")
    if expected_name and ble_dev.name and ble_dev.name != expected_name:
        _log.warning("BLE 设备名不符：期望 %r 实际 %r（绑定核对，不阻断）",
                     expected_name, ble_dev.name)

    # 建连 + 服务发现：连接未就绪时 Windows 抛 OSError，0.2s 间隔重试
    svc = None
    for _ in range(30):
        try:
            res = await ble_dev.get_gatt_services_for_uuid_async(svc_uuid)
            if res.services:
                svc = res.services[0]
                break
        except OSError:
            pass
        await asyncio.sleep(0.2)
    if svc is None:
        ble_dev.close()
        raise RuntimeError("服务发现重试耗尽（30 x 0.2s）")

    chres = await svc.get_characteristics_async()
    chrs = {c.uuid: c for c in chres.characteristics}
    missing = [k for k in ("state", "battery", "link") if chr_uuid[k] not in chrs]
    if missing:
        ble_dev.close()
        raise RuntimeError(f"特征枚举不完整，缺失 {missing}")
    chr_state, chr_batt, chr_link = (chrs[chr_uuid[k]] for k in ("state", "battery", "link"))

    callbacks = {"battery": on_battery, "link": on_link}

    def make_cb(key: str):
        def on_value_changed(sender, args):
            try:
                data = bytes(bytearray(args.characteristic_value))
                msg = json.loads(data.decode("utf-8"))
            except Exception:
                msg = None  # 畸形包：交由上层吞掉记 warning
            cb = callbacks[key]
            if cb is not None:
                loop.call_soon_threadsafe(cb, msg)
        return on_value_changed

    batt_tkn = chr_batt.add_value_changed(make_cb("battery"))
    link_tkn = chr_link.add_value_changed(make_cb("link"))
    await chr_batt.write_client_characteristic_configuration_descriptor_with_result_async(
        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)
    await chr_link.write_client_characteristic_configuration_descriptor_with_result_async(
        GattClientCharacteristicConfigurationDescriptorValue.NOTIFY)

    async def write_state(payload_bytes: bytes) -> None:
        w = DataWriter()
        w.write_bytes(payload_bytes)
        res = await chr_state.write_value_with_result_async(w.detach_buffer())
        if res.status != GattCommunicationStatus.SUCCESS:
            raise OSError(f"GATT 状态写入失败 status={res.status}")

    async def read_link_status() -> dict | None:
        res = await chr_link.read_value_async()
        if res.status != GattCommunicationStatus.SUCCESS:
            raise OSError(f"GATT link_status 读取失败 status={res.status}")
        try:
            return json.loads(bytes(bytearray(res.value)).decode("utf-8"))
        except Exception:
            return None

    def set_battery_cb(cb) -> None:
        callbacks["battery"] = cb

    def set_link_cb(cb) -> None:
        callbacks["link"] = cb

    def close() -> None:
        for tkn in (link_tkn, batt_tkn):
            try:
                chr_link.remove_value_changed(tkn)
            except Exception:
                pass
        try:
            ble_dev.close()
        except Exception:
            pass

    return _Conn(write_state=write_state, read_link_status=read_link_status,
                 set_battery_cb=set_battery_cb, set_link_cb=set_link_cb, close=close)


class BleScreenLink:
    """小屏 BLE Central 链路：连接管理 + 状态推送 + 电池 notify + 快速重醒 + 周期心跳。

    conn 句柄协议（FakeConnector 在测试里实现同协议）：
      await conn.write_state(payload_bytes) -> None    # 失败抛异常
      await conn.read_link_status() -> dict | None
      conn.set_battery_cb(cb) / conn.set_link_cb(cb)   # cb 收到解析后的 dict
      conn.close()
    """

    def __init__(self, cfg, on_battery=None, connector_factory=None):
        self.cfg = cfg
        self.on_battery = on_battery
        self.connector_factory = connector_factory or self._default_factory
        self._loop: asyncio.AbstractEventLoop | None = None
        self._conn = None            # 当前连接句柄（worker 独占；stop 也清理）
        self._pending_conn = None    # 心跳建连待断开/可被 worker 接管的句柄
        self._worker_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None
        self._wake: asyncio.Event | None = None
        self._latest: dict | None = None
        self._rewake_active = False
        self._rewake_deadline = 0.0
        self._next_connect_at = 0.0  # monotonic 时间戳闸：建连失败后退避 _CONNECT_BACKOFF
        self._retry_handle: asyncio.TimerHandle | None = None  # display=on 建连失败的自动重试
        self._stopped = False

    async def _default_factory(self, addr: int):
        """真实 winrt 建连工厂：把原始回调直接传入 _winrt_connect，使其在写 CCCD
        订阅之前挂好（固件订阅后 ~30ms 补发电池首包，晚挂必丢——实机缺陷修复）。"""
        return await _winrt_connect(addr, self.cfg.screen_ble_name,
                                    self._on_battery_raw, self._on_link_raw)

    # ---- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """启动内部 worker 与心跳任务（须在事件循环线程调用；幂等）。"""
        if self._worker_task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._stopped = False
        self._wake = asyncio.Event()
        self._worker_task = asyncio.create_task(self._worker_loop())
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        """停止 worker/心跳并断开连接；幂等，任何异常都不逃逸。"""
        self._stopped = True
        self._cancel_retry()
        tasks = [t for t in (self._worker_task, self._heartbeat_task) if t is not None]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._worker_task = self._heartbeat_task = None
        pending, self._pending_conn = self._pending_conn, None
        if pending is not None:
            try:
                pending.close()
            except Exception:
                _log.exception("BLE 连接关闭失败")
        await self._disconnect("stop")
        self._latest = None

    # ---- 状态推送（同步，事件循环线程由 sm.on_change 调用） ---------------------

    def push_state(self, state: dict) -> None:
        """同步非阻塞入队最新状态（latest 覆盖合并）；未 start / 已 stop 时安全 no-op。"""
        if self._worker_task is None or self._stopped:
            return
        self._latest = state
        self._wake.set()

    # ---- worker：串行消费 latest -------------------------------------------------

    async def _worker_loop(self) -> None:
        while not self._stopped:
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("BLE worker 异常")
                await asyncio.sleep(0.2)  # 防异常循环自旋

    async def _run_once(self) -> None:
        if self._rewake_active and self._conn is not None:
            # 快速重醒窗口内等待下一次推送；到期无 display=on → 断连回空闲（省电）
            remaining = max(0.0, self._rewake_deadline - self._loop.time())
            try:
                await asyncio.wait_for(self._wake.wait(), remaining)
            except asyncio.TimeoutError:
                self._rewake_active = False
                await self._disconnect("快速重醒窗口到期")
                return
        else:
            await self._wake.wait()
        self._wake.clear()
        state = self._latest
        if state is None:
            return
        await self._process_state(state)

    async def _process_state(self, state: dict) -> None:
        display = state.get("display")
        if display == "off":
            # 息屏：仅当已连接才写（固件断面板电）；未连接直接丢弃，不建连（省电核心）
            if self._conn is None:
                _log.debug("display=off 且未连接：丢弃不建连（省电）")
                return
            if await self._write_state(state):
                self._arm_rewake()
        elif display == "on":
            conn = await self._ensure_connected()
            if conn is None:
                # 建连失败/退避中：display=on 不丢——保留 latest，退避到期自动重试。
                # 否则黑屏直到下个状态变化（sm.wake 复点若 display 已 on 且 qr 复用
                # 不变更，未必再产生 on_change），管理员会看到"点了没反应"。
                self._schedule_retry(state)
                return
            if await self._write_state(state):
                # on 推送成功 → 取消快速重醒与失败重试（保持连接）
                self._cancel_retry()
                self._rewake_active = False
                self._rewake_deadline = 0.0

    async def _write_state(self, state: dict) -> bool:
        """写五字段 JSON；失败 → 断开旧句柄，返回 False（下条推送走完整重建）。"""
        payload = self._state_payload(state)
        try:
            await self._conn.write_state(payload)
        except Exception:
            _log.warning("BLE 状态写入失败，断开连接（下条推送走重建）", exc_info=True)
            await self._disconnect("状态写入失败")
            return False
        return True

    @staticmethod
    def _state_payload(state: dict) -> bytes:
        payload = {
            "state": state.get("state", ""),
            "display": state.get("display"),
            "qr_content": state.get("qr_content") or "",
            "file_count": state.get("file_count", 0),
            "message": state.get("message", ""),
        }
        return json.dumps(payload, ensure_ascii=False).encode("utf-8")

    # ---- 连接管理 ---------------------------------------------------------------

    def _address_int(self) -> int | None:
        try:
            return mac_to_int(self.cfg.screen_ble_address)
        except (TypeError, ValueError):
            _log.warning("BLE MAC 地址非法：%r", self.cfg.screen_ble_address)
            return None

    async def _try_connect(self) -> object | None:
        """调用 connector_factory 建连并接线回调；失败记 warning 并设退避闸。"""
        addr = self._address_int()
        if addr is None:
            return None
        try:
            conn = await self.connector_factory(addr)
        except Exception:
            _log.warning("BLE 建连失败：%s", self.cfg.screen_ble_address, exc_info=True)
            self._next_connect_at = time.monotonic() + _CONNECT_BACKOFF
            return None
        conn.set_battery_cb(self._on_battery_raw)
        conn.set_link_cb(self._on_link_raw)
        return conn

    async def _ensure_connected(self) -> object | None:
        """确保已连接（懒建连）；不可用返回 None。"""
        if self._conn is not None:
            return self._conn
        if self._pending_conn is not None:
            # 心跳已建连未断：直接接管，避免同设备双连接
            self._conn = self._pending_conn
            self._pending_conn = None
            return self._conn
        if time.monotonic() < self._next_connect_at:
            _log.debug("BLE 建连退避中，跳过本次推送")
            return None
        conn = await self._try_connect()
        if conn is None:
            return None
        self._conn = conn
        _log.info("BLE 已连接：%s", self.cfg.screen_ble_address)
        return conn

    async def _disconnect(self, reason: str) -> None:
        conn, self._conn = self._conn, None
        self._rewake_active = False
        self._rewake_deadline = 0.0
        if conn is not None:
            try:
                conn.close()
            except Exception:
                _log.exception("BLE 连接关闭失败")
        _log.debug("BLE 断开：%s", reason)

    def _arm_rewake(self) -> None:
        self._rewake_active = True
        self._rewake_deadline = self._loop.time() + self.cfg.ble_fast_rewake_seconds

    # ---- display=on 建连失败重试 ---------------------------------------------------

    def _schedule_retry(self, state: dict) -> None:
        """保留 latest 并安排退避到期后自动重试；同一时间最多一个待触发重试。"""
        self._latest = state
        if self._retry_handle is not None or self._stopped or self._loop is None:
            return
        delay = max(_CONNECT_BACKOFF, self._next_connect_at - time.monotonic())
        self._retry_handle = self._loop.call_later(delay, self._fire_retry)

    def _fire_retry(self) -> None:
        self._retry_handle = None
        if not self._stopped and self._wake is not None:
            self._wake.set()

    def _cancel_retry(self) -> None:
        if self._retry_handle is not None:
            self._retry_handle.cancel()
            self._retry_handle = None

    # ---- notify 回调（经 call_soon_threadsafe 回到事件循环线程） ------------------

    def _on_battery_raw(self, raw) -> None:
        report = _convert_battery_report(raw)
        if report is None:
            _log.warning("BLE 电池包畸形，忽略：%r", raw)
            return
        if self.on_battery is not None:
            self.on_battery(report)

    def _on_link_raw(self, raw) -> None:
        # link_status notify = 固件 drawn ack，仅记日志
        _log.debug("BLE drawn ack: %s", raw)

    # ---- 周期心跳 -----------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        interval = max(0.01, self.cfg.ble_heartbeat_seconds)
        while not self._stopped:
            await asyncio.sleep(interval)
            try:
                await self._heartbeat()
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.exception("BLE 心跳异常")

    async def _heartbeat(self) -> None:
        if self._conn is not None:
            # 在线心跳：周期性读 link_status
            try:
                status = await self._conn.read_link_status()
                _log.debug("BLE 在线心跳 link_status=%s", status)
            except Exception:
                _log.warning("BLE 在线心跳读 link_status 失败", exc_info=True)
            return
        if self._pending_conn is not None:
            return  # 上次心跳建连仍在等电池首包
        if time.monotonic() < self._next_connect_at:
            return  # 建连退避中
        conn = await self._try_connect()
        if conn is None:
            return
        self._pending_conn = conn
        try:
            try:
                status = await conn.read_link_status()
                _log.debug("BLE 心跳建连后 link_status=%s", status)
            except Exception:
                _log.warning("BLE 心跳读 link_status 失败", exc_info=True)
            # 等固件订阅 CCCD 后补发的电池首包
            await asyncio.sleep(_BATTERY_NOTIFY_WAIT)
        finally:
            if self._pending_conn is not None:
                self._pending_conn = None
                try:
                    conn.close()
                except Exception:
                    _log.exception("BLE 心跳连接关闭失败")

    # ---- 自检辅助 -----------------------------------------------------------------

    @property
    def connected(self) -> bool:
        """己方连接句柄是否就绪（selftest 用；僵尸连接的转发包不算数）。"""
        return self._conn is not None

    async def selftest_read_link(self) -> dict | None:
        """自检用公开读 API（主链路 read 仅走内部心跳，无需对外）：确保已连接
        后读一次 link_status；不可用抛异常，由 run_ble_selftest 兜住。"""
        conn = await self._ensure_connected()
        if conn is None:
            raise RuntimeError("BLE 未连接（建连失败或退避中）")
        return await conn.read_link_status()


async def run_ble_selftest(cfg, connector_factory=None) -> int:
    """--ble-selftest CLI 模式：真实连接小屏，验证建连/写屏/订阅/notify 全链路。

    流程：打印目标 → push display=on（占位画面）→ 先等**己方连接就绪**
    （_SELFTEST_CONNECT_TIMEOUT；必须先等连接——强杀旧进程后的僵尸连接会把
    残留订阅的电池包转发给新进程，"收到电池"不能当己方建连证据，2026-08-26
    实机踩坑）→ 再等电池首包（_SELFTEST_BATTERY_TIMEOUT）→ 读 link_status
    打印 → push display=off（息屏）→ stop → 打印 PASS 返回 0。任一步失败/超时
    打印 FAIL 返回 1，全程 try/except 不向顶层抛栈（退出码即结果）；
    connector_factory 仅测试注入。
    """
    print(f"BLE SELFTEST target={cfg.screen_ble_address!r} name={cfg.screen_ble_name!r}")
    if not cfg.screen_ble_address:
        print("BLE SELFTEST FAIL: screen_ble_address 为空（BLE 已禁用）")
        return 1

    battery_evt = asyncio.Event()
    battery: dict = {}

    def on_battery(report: dict) -> None:
        battery.update(report)
        battery_evt.set()

    link = BleScreenLink(cfg, on_battery=on_battery,
                         connector_factory=connector_factory)
    try:
        link.start()
        link.push_state({"state": "IDLE", "display": "on", "qr_content": "",
                         "file_count": 0, "message": "BLE selftest"})
        # 第一关口：己方连接就绪（覆盖建连失败→退避→自动重试全程）
        loop = asyncio.get_running_loop()
        connect_deadline = loop.time() + _SELFTEST_CONNECT_TIMEOUT
        while not link.connected and loop.time() < connect_deadline:
            await asyncio.sleep(0.2)
        if not link.connected:
            print(f"BLE SELFTEST FAIL: {_SELFTEST_CONNECT_TIMEOUT}s 内未能建连"
                  "（含退避重试）")
            return 1
        # 第二关口：电池首包 = 写屏 + CCCD 订阅 + notify 回调全通的证据
        await asyncio.wait_for(battery_evt.wait(), _SELFTEST_BATTERY_TIMEOUT)
        print(f"BLE SELFTEST battery={battery}")
        status = await link.selftest_read_link()
        print(f"BLE SELFTEST link_status={status}")
        # 息屏：push 后等 worker 消费写屏再 stop（写入后连接保持，1s 余量足够）
        link.push_state({"state": "IDLE", "display": "off", "qr_content": "",
                         "file_count": 0, "message": ""})
        await asyncio.sleep(_SELFTEST_OFF_GRACE)
        print("BLE SELFTEST PASS")
        return 0
    except asyncio.TimeoutError:
        print("BLE SELFTEST FAIL: 等待电池首包超时（订阅/notify 未通）")
        return 1
    except Exception as exc:
        print(f"BLE SELFTEST FAIL: {exc}")
        _log.debug("BLE selftest 失败详情", exc_info=True)
        return 1
    finally:
        try:
            await link.stop()
        except Exception:
            _log.exception("BLE selftest stop 失败")
