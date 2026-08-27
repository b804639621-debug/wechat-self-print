import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    inbox_dir: Path = field(default_factory=lambda: Path("inbox"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    db_path: Path = field(default_factory=lambda: Path("printshop.db"))
    soffice_path: str = r"C:\Program Files\LibreOffice\program\soffice.exe"
    render_dpi: int = 300
    max_render_pages: int = 200
    idle_sleep_seconds: int = 180
    no_file_timeout: int = 300
    poll_interval: float = 1.5
    office_convert_timeout: float = 120.0
    network_backoff: tuple = (5, 15, 60)
    network_max_failures: int = 3
    session_save_interval: float = 600.0
    screen_ble_address: str = "20:6E:F1:16:D9:2E"  # 小屏 BLE public MAC；空串=禁用 BLE（回滚 WiFi 轮询）
    screen_ble_name: str = "PRINT-SCREEN"           # 绑定核对用设备名
    ble_fast_rewake_seconds: float = 120.0          # 息屏后保持连接的快速重醒窗口
    ble_heartbeat_seconds: float = 900.0            # 空闲期周期连接拉电池+在线心跳
    auto_open_links: bool = True
    auto_open_max_links: int = 3

    def __post_init__(self) -> None:
        """环境变量 PRINTSHOP_SCREEN_BLE_ADDRESS 覆盖/禁用 BLE 地址（部署回滚开关）。"""
        env_addr = os.environ.get("PRINTSHOP_SCREEN_BLE_ADDRESS")
        if env_addr is not None:
            addr = env_addr.strip()
            if addr.lower() in ("off", "none"):
                addr = ""
            self.screen_ble_address = addr

    def ensure_dirs(self) -> None:
        self.inbox_dir.mkdir(exist_ok=True)
        self.work_dir.mkdir(exist_ok=True)
