"""运行日志落盘与 windowed 模式 stdout/stderr 重定向。

PyInstaller 使用 ``console=False`` 后，sys.stdout/sys.stderr 为 None，
vendor 中散落的裸 ``print()`` 会因此抛异常。这里把 None 的标准流替换为
按行写日志的流对象，确保应用日志、uvicorn 日志和 print 输出都进入 exe
同级的 ``logs/printshop.log``（源码运行则进入仓库根 ``logs/printshop.log``）。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_MAX_BYTES = 5 * 1024 * 1024
_BACKUP_COUNT = 5


def default_log_path() -> Path:
    """返回运行日志默认路径：frozen 在 exe 同级 logs/，源码在仓库根 logs/。"""
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).resolve().parent
    else:
        base = Path(__file__).resolve().parents[1]
    return base / "logs" / "printshop.log"


def configure_file_logging(log_path: Path | None = None) -> Path:
    """为根 logger 配置滚动文件日志，重复调用不叠加同路径 handler。

    返回实际使用的日志文件绝对路径。
    """
    log_path = Path(log_path or default_log_path()).expanduser().resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    for handler in root.handlers:
        if (isinstance(handler, logging.handlers.RotatingFileHandler)
                and Path(handler.baseFilename).resolve() == log_path):
            return log_path

    handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root.addHandler(handler)
    return log_path


def uvicorn_log_config() -> dict:
    """返回 uvicorn 日志配置：关闭 uvicorn 自带 handler，全部传播到根 logger。

    根 logger 已由 :func:`configure_file_logging` 挂上 RotatingFileHandler，
    因此这里不需要也不能再创建 file handler，避免重复写入。
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "handlers": {},
        "loggers": {
            "uvicorn": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.error": {"handlers": [], "level": "INFO", "propagate": True},
            "uvicorn.access": {"handlers": [], "level": "INFO", "propagate": True},
        },
    }


class LogStream:
    """把写入 stdout/stderr 的文本按行转发给 logging。

    空行跳过；未以换行结尾的残余内容在 flush 时输出。
    """

    def __init__(self, level: int = logging.INFO, name: str = "printshop.stdout"):
        self._level = level
        self._logger = logging.getLogger(name)
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0
        text = str(text)
        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.rstrip("\r")
            if line.strip():
                self._logger.log(self._level, line)
        return len(text)

    def flush(self) -> None:
        line = self._buffer.rstrip("\r\n")
        self._buffer = ""
        if line.strip():
            self._logger.log(self._level, line)

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    @property
    def encoding(self) -> str:
        return "utf-8"


def redirect_std_streams_to_logging() -> None:
    """仅在 sys.stdout/sys.stderr 为 None 时替换为日志流。

    源码运行（或已存在控制台）时保持原样，不干扰正常终端输出。
    """
    if sys.stdout is None:
        sys.stdout = LogStream(logging.INFO, "printshop.stdout")
    if sys.stderr is None:
        sys.stderr = LogStream(logging.ERROR, "printshop.stderr")
