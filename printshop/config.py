from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8000
    inbox_dir: Path = field(default_factory=lambda: Path("inbox"))
    work_dir: Path = field(default_factory=lambda: Path("work"))
    db_path: Path = field(default_factory=lambda: Path("printshop.db"))
    sumatra_path: Path = field(default_factory=lambda: Path("tools/SumatraPDF.exe"))
    soffice_path: str = r"C:\Program Files\LibreOffice\program\soffice.exe"
    idle_sleep_seconds: int = 180
    qr_expire_seconds: int = 240
    no_file_timeout: int = 300
    poll_interval: float = 1.5

    def ensure_dirs(self) -> None:
        self.inbox_dir.mkdir(exist_ok=True)
        self.work_dir.mkdir(exist_ok=True)
