"""BleScreenLink 连接管理状态机单测（FakeConnector 注入，不碰真实蓝牙/winrt）。

配置用极小计时（fast_rewake=0.05s、heartbeat=0.1s 级）跑真实 asyncio 事件循环；
除心跳专项测试外一律把 heartbeat 调到极大值，避免心跳任务干扰观测目标。
"""
import asyncio
import json
import time
from unittest.mock import MagicMock

import pytest

from printshop.ble_link import BleScreenLink
from printshop.config import Config
from printshop.routes import AppContext


class FakeConnector:
    """按 BleScreenLink 的 conn 句柄协议实现的假连接。

    gate 非 None 时 write_state 先等 gate（模拟慢写，测试覆盖合并用）；
    fail_write=True 时写入抛异常；battery/link 回调可手动触发。
    """

    def __init__(self, fail_write=False, gate=None):
        self.fail_write = fail_write
        self.gate = gate
        self.writes: list[bytes] = []
        self.gate_waits = 0
        self.reads = 0
        self.closed = False
        self.battery_cb = None
        self.link_cb = None

    async def write_state(self, payload_bytes: bytes) -> None:
        if self.gate is not None:
            self.gate_waits += 1
            await self.gate.wait()
        if self.fail_write:
            raise OSError("fake GATT 写入失败")
        self.writes.append(payload_bytes)

    async def read_link_status(self) -> dict:
        self.reads += 1
        return {"fw": "1.0", "uptime_s": 12, "mtu": 517, "adv_ms": 250}

    def set_battery_cb(self, cb):
        self.battery_cb = cb

    def set_link_cb(self, cb):
        self.link_cb = cb

    def close(self):
        self.closed = True

    def trigger_battery(self, raw):
        if self.battery_cb is not None:
            self.battery_cb(raw)

    def trigger_link(self, raw):
        if self.link_cb is not None:
            self.link_cb(raw)


def _factory(made, fail_first_write=False):
    """记录每次建连的 connector_factory；首连可注入写失败。"""
    async def factory(addr):
        conn = FakeConnector(fail_write=fail_first_write and not made)
        made.append(conn)
        return conn
    return factory


def _cfg(**kw):
    cfg = Config()
    cfg.ble_fast_rewake_seconds = 0.05
    cfg.ble_heartbeat_seconds = 9999.0  # 除心跳专项外禁用心跳干扰
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def _state(**kw):
    base = {"state": "IDLE", "display": "on", "qr_content": None,
            "file_count": 0, "message": ""}
    base.update(kw)
    return base


async def _wait_until(pred, timeout=2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        await asyncio.sleep(0.005)
    return False


async def _first_conn(made, idx=0, timeout=2.0):
    assert await _wait_until(lambda: len(made) > idx, timeout)
    return made[idx]


# ---- 连接管理状态机 -------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_push_lazy_connects_and_writes_five_fields():
    """display=on 推送 → 懒建连 + 写入五字段 JSON（qr None→""），连接保持。"""
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made))
    link.start()
    link.push_state(_state(qr_content=None, file_count=2, message="打印3份"))
    conn = await _first_conn(made)
    assert await _wait_until(lambda: len(conn.writes) == 1)
    payload = json.loads(conn.writes[0])
    assert payload == {"state": "IDLE", "display": "on", "qr_content": "",
                       "file_count": 2, "message": "打印3份"}
    assert len(made) == 1          # 懒建连：仅一次
    assert conn.closed is False    # on 推送成功后保持连接
    await link.stop()
    assert conn.closed is True     # stop 断开


@pytest.mark.asyncio
async def test_pushes_while_busy_are_merged_latest_wins():
    """worker 忙（慢写被 gate 阻塞）时连续 push 合并：latest 覆盖，不逐条写。"""
    gate = asyncio.Event()
    made = []

    async def factory(addr):
        conn = FakeConnector(gate=gate)
        made.append(conn)
        return conn

    link = BleScreenLink(_cfg(), connector_factory=factory)
    link.start()
    link.push_state(_state(qr_content="qr-A"))
    conn = await _first_conn(made)
    assert await _wait_until(lambda: conn.gate_waits >= 1)  # 第一条写已入闸
    link.push_state(_state(qr_content="qr-B"))
    link.push_state(_state(qr_content="qr-C", state="CONNECTED"))
    gate.set()
    assert await _wait_until(lambda: len(conn.writes) == 2)
    got = [json.loads(w) for w in conn.writes]
    assert [g["qr_content"] for g in got] == ["qr-A", "qr-C"]  # B 被 C 覆盖合并
    assert got[1]["state"] == "CONNECTED"
    await link.stop()


@pytest.mark.asyncio
async def test_off_while_connected_writes_then_rewake_expires_disconnect():
    """display=off（已连接）→ 写入；快速重醒窗口到期且无 on 推送 → 自动断开。"""
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made))
    link.start()
    link.push_state(_state(qr_content="qr"))
    conn = await _first_conn(made)
    assert await _wait_until(lambda: len(conn.writes) == 1)
    link.push_state(_state(display="off"))
    assert await _wait_until(lambda: len(conn.writes) == 2)
    assert json.loads(conn.writes[1])["display"] == "off"
    assert await _wait_until(lambda: link._rewake_active)  # 已进入快速重醒窗口
    assert conn.closed is False                            # 窗口内保持连接
    assert await _wait_until(lambda: conn.closed, timeout=2.0)
    assert link._conn is None
    await link.stop()


@pytest.mark.asyncio
async def test_off_unconnected_dropped_without_connect():
    """display=off（未连接）→ 不建连、纯丢弃（省电核心）。"""
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made))
    link.start()
    link.push_state(_state(display="off"))
    await asyncio.sleep(0.1)  # 给 worker 足够消费时间（若错误建连应已发生）
    assert made == []
    assert link._conn is None
    await link.stop()


@pytest.mark.asyncio
async def test_write_failure_disconnects_then_next_push_rebuilds():
    """写失败 → 标记断开 + close 旧句柄；下一条 display=on 走完整重建（factory 两次）。"""
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made, fail_first_write=True))
    link.start()
    link.push_state(_state(qr_content="qr-1"))
    conn1 = await _first_conn(made)
    assert await _wait_until(lambda: conn1.closed)
    assert link._conn is None
    link.push_state(_state(qr_content="qr-2"))
    conn2 = await _first_conn(made, idx=1)
    assert conn2 is not conn1                      # 新建连（非复用失效句柄）
    assert await _wait_until(lambda: len(conn2.writes) == 1)
    assert json.loads(conn2.writes[0])["qr_content"] == "qr-2"
    assert conn2.closed is False
    await link.stop()


# ---- display=on 建连失败自动重试（主代理审阅增补） -----------------------------------


@pytest.mark.asyncio
async def test_on_connect_failure_retries_automatically_after_backoff(monkeypatch):
    """display=on 建连失败 → 推送不丢：退避到期自动重试，无需再次 on_change。"""
    monkeypatch.setattr("printshop.ble_link._CONNECT_BACKOFF", 0.05)
    made = []
    attempts = []

    async def factory(addr):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("fake 建连失败")
        conn = FakeConnector()
        made.append(conn)
        return conn

    link = BleScreenLink(_cfg(), connector_factory=factory)
    link.start()
    link.push_state(_state(qr_content="qr-retry"))
    conn = await _first_conn(made, timeout=2.0)   # 无新 push 也自动重建
    assert await _wait_until(lambda: len(conn.writes) == 1)
    assert json.loads(conn.writes[0])["qr_content"] == "qr-retry"
    assert len(attempts) >= 2
    await link.stop()
    assert link._retry_handle is None


@pytest.mark.asyncio
async def test_retry_stops_after_display_off_push(monkeypatch):
    """重试等待期间收到 display=off → latest 被覆盖，重试触发后处理 off 即终止，
    不再反复建连（IDLE 息屏后不得在后台无限重试）。"""
    monkeypatch.setattr("printshop.ble_link._CONNECT_BACKOFF", 0.05)
    attempts = []

    async def factory(addr):
        attempts.append(1)
        raise OSError("fake 建连失败")

    link = BleScreenLink(_cfg(), connector_factory=factory)
    link.start()
    link.push_state(_state(qr_content="qr"))
    assert await _wait_until(lambda: len(attempts) >= 1)
    link.push_state(_state(display="off"))
    n = len(attempts)
    await asyncio.sleep(0.3)   # 越过多个退避周期
    assert len(attempts) <= n + 1  # 允许一次在途重试，之后必须静默
    await link.stop()


# ---- 心跳 + 电池 notify -----------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_connects_reads_and_disconnects_with_battery(monkeypatch):
    """未连接时心跳到点 → 建连 → 读 link_status → 等电池首包 → 主动断开。"""
    monkeypatch.setattr("printshop.ble_link._BATTERY_NOTIFY_WAIT", 0.3)
    reports = []
    made = []
    cfg = _cfg(ble_heartbeat_seconds=0.1)
    link = BleScreenLink(cfg, on_battery=reports.append, connector_factory=_factory(made))
    link.start()
    conn = await _first_conn(made, timeout=3.0)
    assert await _wait_until(lambda: conn.reads >= 1, timeout=3.0)
    conn.trigger_battery({"pct": 73, "mv": 3905, "chg": 0, "vbus": 1})
    assert await _wait_until(lambda: len(reports) == 1, timeout=1.0)
    r = reports[0]
    assert r["pct"] == 73
    assert r["mv"] == 3905
    assert r["charging"] is False
    assert r["vbus"] is True
    assert r["updated_at"]
    # 心跳窗口结束 → 主动断开
    assert await _wait_until(lambda: conn.closed, timeout=2.0)
    assert link._pending_conn is None
    await link.stop()


@pytest.mark.asyncio
async def test_malformed_battery_packets_ignored(monkeypatch):
    """畸形电池包吞掉不触发 on_battery，也不影响心跳流程。"""
    monkeypatch.setattr("printshop.ble_link._BATTERY_NOTIFY_WAIT", 0.3)
    reports = []
    made = []
    cfg = _cfg(ble_heartbeat_seconds=0.1)
    link = BleScreenLink(cfg, on_battery=reports.append, connector_factory=_factory(made))
    link.start()
    conn = await _first_conn(made, timeout=3.0)
    assert await _wait_until(lambda: conn.reads >= 1, timeout=3.0)
    conn.trigger_battery(None)           # JSON 解码失败路径（winrt 传 None）
    conn.trigger_battery({"pct": 999})   # 字段越界
    conn.trigger_battery("junk")         # 类型畸形
    assert await _wait_until(lambda: conn.closed, timeout=2.0)
    assert reports == []
    await link.stop()


# ---- 生命周期防御 ------------------------------------------------------------------


def test_push_before_start_is_safe_noop():
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made))
    link.push_state(_state())  # 未 start：不炸、不建连
    assert made == []
    assert link._latest is None


@pytest.mark.asyncio
async def test_push_after_stop_is_safe():
    made = []
    link = BleScreenLink(_cfg(), connector_factory=_factory(made))
    link.start()
    await link.stop()
    await link.stop()  # 幂等
    link.push_state(_state())
    await asyncio.sleep(0.05)
    assert made == []


# ---- 真实 winrt 连接器接线（2026-08-25 实机缺陷回归） -------------------------------


@pytest.mark.asyncio
async def test_default_factory_prewires_raw_callbacks_before_subscribe(monkeypatch):
    """实机缺陷回归：固件在 CCCD 订阅后 ~30ms 补发电池首包，回调若在连接返回后才挂
    （set_battery_cb）首包必丢（心跳 3s 断开等不到 60s 周期包 → 电池永远缺失）。
    默认工厂必须把原始回调直接传入 _winrt_connect，使其在写 CCCD 之前挂好。"""
    import printshop.ble_link as bl

    async def fake_connect(addr, expected_name=None, on_battery=None, on_link=None):
        return (addr, expected_name, on_battery, on_link)

    monkeypatch.setattr(bl, "_winrt_connect", fake_connect)
    cfg = Config()
    reports = []
    link = bl.BleScreenLink(cfg, on_battery=reports.append)
    assert link.connector_factory.__func__ is link._default_factory.__func__
    got = await link._default_factory(123)
    assert got[0] == 123
    assert got[1] == cfg.screen_ble_name
    assert got[2].__func__ is link._on_battery_raw.__func__
    assert got[3].__func__ is link._on_link_raw.__func__


# ---- --ble-selftest CLI 模式（Task 10B） -------------------------------------------


async def _selftest_factory(made, battery_delay=0.05):
    """selftest 用工厂：建连后模拟固件订阅 CCCD 补发电池首包的时序。"""
    async def factory(addr):
        conn = FakeConnector()
        made.append(conn)
        if battery_delay is not None:
            async def _send_first_report():
                await asyncio.sleep(battery_delay)
                conn.trigger_battery({"pct": 61, "mv": 3880, "chg": 1, "vbus": 0})
            asyncio.get_running_loop().create_task(_send_first_report())
        return conn
    return factory


@pytest.mark.asyncio
async def test_run_ble_selftest_success(monkeypatch):
    """成功路径：建连+写屏 → 电池首包到达 → 读 link_status → off 息屏 → 返回 0。"""
    import printshop.ble_link as bl
    monkeypatch.setattr(bl, "_SELFTEST_OFF_GRACE", 0.1)
    made = []
    rc = await bl.run_ble_selftest(_cfg(),
                                   connector_factory=await _selftest_factory(made))
    assert rc == 0
    conn = made[0]
    assert json.loads(conn.writes[0])["message"] == "BLE selftest"  # display=on 占位
    assert conn.reads >= 1                                          # selftest_read_link
    assert await _wait_until(  # off 已消费写屏（worker 消费可能晚于 stop 前轮询）
        lambda: conn.writes and json.loads(conn.writes[-1])["display"] == "off")
    assert conn.closed is True   # stop 断开


@pytest.mark.asyncio
async def test_run_ble_selftest_battery_timeout_returns_1(monkeypatch):
    """失败路径：电池首包超时 → 返回 1，连接句柄被 stop 清理。"""
    import printshop.ble_link as bl
    monkeypatch.setattr(bl, "_SELFTEST_BATTERY_TIMEOUT", 0.2)
    made = []

    async def factory(addr):  # 建连成功但永不触发电池
        conn = FakeConnector()
        made.append(conn)
        return conn

    rc = await bl.run_ble_selftest(_cfg(), connector_factory=factory)
    assert rc == 1
    assert made[0].closed is True


@pytest.mark.asyncio
async def test_run_ble_selftest_recovers_after_first_connect_failure(monkeypatch):
    """首连失败（僵尸连接场景）→ 退避自动重试成功 → 返回 0（不误报 FAIL）。"""
    import printshop.ble_link as bl
    monkeypatch.setattr(bl, "_CONNECT_BACKOFF", 0.1)
    monkeypatch.setattr(bl, "_SELFTEST_OFF_GRACE", 0.1)
    made = []
    attempts = []

    async def factory(addr):
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("fake 首连失败（模拟僵尸连接干扰）")
        conn = FakeConnector()
        made.append(conn)
        async def _send_first_report():
            await asyncio.sleep(0.05)
            conn.trigger_battery({"pct": 61, "mv": 3880, "chg": 1, "vbus": 0})
        asyncio.get_running_loop().create_task(_send_first_report())
        return conn

    rc = await bl.run_ble_selftest(_cfg(), connector_factory=factory)
    assert rc == 0
    assert len(attempts) >= 2


def _ctx(ble):
    return AppContext(cfg=Config(), store=MagicMock(), wx=MagicMock(),
                      sm=MagicMock(), ble=ble)


def test_battery_payload_stale_after_wifi_fallback_180():
    """ble=None（WiFi 轮询回滚路径）：stale_after 为既有 180s 阈值。"""
    ctx = _ctx(ble=None)
    ctx.update_battery({"pct": 80, "mv": 4100, "charging": False,
                        "vbus": False, "updated_at": "t"})
    payload = ctx.battery_payload()
    assert payload is not None
    assert payload["pct"] == 80
    assert payload["stale_after"] == 180


def test_battery_payload_stale_after_ble_heartbeat_plus_300():
    """ble 非 None：stale_after = int(ble_heartbeat_seconds) + 300。"""
    cfg = Config()
    ctx = AppContext(cfg=cfg, store=MagicMock(), wx=MagicMock(),
                     sm=MagicMock(), ble=object())
    ctx.update_battery({"pct": 80, "mv": 4100, "charging": False,
                        "vbus": False, "updated_at": "t"})
    assert ctx.battery_payload()["stale_after"] == int(cfg.ble_heartbeat_seconds) + 300
