import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    # host/port 用 None 作"未显式指定"哨兵：只有未指定时才叠加 env 覆盖，
    # 保证显式构造（模拟 CLI 直传）优先级高于环境变量（plan §3.2 表）。
    host: str | None = None
    # 18080：避开 FastAPI/dev server 常用默认端口（8000/8080/5000 等）的冷门高位端口。
    # 覆盖优先级（高→低）：CLI --port/--host > 环境变量 PRINTSHOP_PORT/PRINTSHOP_HOST
    #   > exe 同级 printshop.json > 此默认值。换端口后必须同步固件 SERVER_PORT
    #   并重烧（见 docs/plan-port-conflict-solution.md §3.5），否则小屏失联。
    port: int | None = None
    inbox_dir: Path = field(default_factory=lambda: Path("inbox"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    db_path: Path = field(default_factory=lambda: Path("printshop.db"))
    soffice_path: str = r"C:\Program Files\LibreOffice\program\soffice.exe"
    render_dpi: int = 300
    max_render_pages: int = 200
    idle_sleep_seconds: int = 180
    # 小屏 WiFi 在线判定阈值（秒）：必须大于固件闲时轮询间隔 POLL_IDLE_INTERVAL_MS(5s)
    # 的 ~2 倍；若固件调大闲时间隔，此处须同步调大（firmware .ino 同注释）
    screen_online_threshold_seconds: float = 12.0
    no_file_timeout: int = 300
    poll_interval: float = 1.5
    office_convert_timeout: float = 120.0
    network_backoff: tuple = (5, 15, 60)
    network_max_failures: int = 3
    session_save_interval: float = 600.0
    screen_ble_address: str = ""  # WiFi 现役默认，空串=禁用 BLE Central 走 WiFi 轮询；需要 BLE 回切时填 MAC 或设 PRINTSHOP_SCREEN_BLE_ADDRESS 环境变量
    screen_ble_name: str = "PRINT-SCREEN"           # 绑定核对用设备名
    ble_fast_rewake_seconds: float = 120.0          # 息屏后保持连接的快速重醒窗口
    ble_heartbeat_seconds: float = 900.0            # 空闲期周期连接拉电池+在线心跳
    auto_open_links: bool = True
    auto_open_max_links: int = 3
    login_greeting_text: str = "😊"
    send_login_greeting: bool = True

    def __post_init__(self) -> None:
        """环境变量覆盖：PRINTSHOP_SCREEN_BLE_ADDRESS（BLE 回切开关）、
        PRINTSHOP_PORT / PRINTSHOP_HOST（端口/绑定地址，优先级低于 CLI 参数）。"""
        env_addr = os.environ.get("PRINTSHOP_SCREEN_BLE_ADDRESS")
        if env_addr is not None:
            addr = env_addr.strip()
            if addr.lower() in ("off", "none"):
                addr = ""
            self.screen_ble_address = addr
        # env 覆盖仅作用于"未显式指定"的字段（None 哨兵）——显式构造值优先于 env
        if self.port is None and (env_port := os.environ.get("PRINTSHOP_PORT")):
            try:
                v = int(env_port.strip())
                if 1 <= v <= 65535:
                    self.port = v  # 非法值静默忽略，保持默认 18080
            except ValueError:
                pass
        if self.port is None:
            self.port = 18080
        if self.host is None:
            env_host = os.environ.get("PRINTSHOP_HOST")
            self.host = env_host.strip() if env_host and env_host.strip() else "0.0.0.0"

    @classmethod
    def from_json_file(cls, path: Path) -> "Config":
        """从 exe 同级 printshop.json 读 {"port":..., "host":...}（可只写其中一个键）。
        优先级 env > json（§3.2 表）：对应 env 已设置时跳过 JSON 该字段；
        文件缺失 / JSON 损坏 / 字段类型非法 一律静默忽略回退默认值——
        配置文件只是低优先级覆盖层，不允许因它阻断启动（CLI/env 才是显式意图）。"""
        c = cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return c
        if not isinstance(data, dict):
            return c
        port = data.get("port")
        if ("PRINTSHOP_PORT" not in os.environ and isinstance(port, int)
                and 1 <= port <= 65535):
            c.port = port
        host = data.get("host")
        if ("PRINTSHOP_HOST" not in os.environ and isinstance(host, str)
                and host.strip()):
            c.host = host.strip()
        return c

    def ensure_dirs(self) -> None:
        self.inbox_dir.mkdir(exist_ok=True)
        self.work_dir.mkdir(exist_ok=True)
