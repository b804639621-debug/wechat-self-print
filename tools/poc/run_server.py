"""验收专用：源码版 printshop 服务（无托盘/GUI），仓库根数据目录，端口 8000。

用法：.venv/Scripts/python.exe tools/poc/run_server.py
"""
import uvicorn

from printshop.config import Config
from printshop.logging_setup import configure_file_logging, uvicorn_log_config
from printshop.main import create_app


def main() -> None:
    log_path = configure_file_logging()
    print(f"log: {log_path}")
    cfg = Config()
    app = create_app(cfg)
    uvicorn.Server(uvicorn.Config(
        app, host=cfg.host, port=cfg.port, log_config=uvicorn_log_config(),
    )).run()


if __name__ == "__main__":
    main()
