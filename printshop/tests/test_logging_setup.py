import logging
import logging.handlers
import sys
from pathlib import Path

from printshop.logging_setup import (
    LogStream,
    configure_file_logging,
    default_log_path,
    redirect_std_streams_to_logging,
    uvicorn_log_config,
)


def _matching_handlers(log_path: Path) -> list:
    target = log_path.resolve()
    return [
        h for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
        and Path(h.baseFilename).resolve() == target
    ]


def _remove_handler(log_path: Path) -> None:
    root = logging.getLogger()
    for handler in _matching_handlers(log_path):
        root.removeHandler(handler)
        handler.close()


def test_default_log_path_source_mode():
    assert default_log_path() == Path(__file__).resolve().parents[2] / "logs" / "printshop.log"


def test_configure_file_logging_creates_dir_and_file(tmp_path):
    log_path = tmp_path / "log" / "app.log"
    try:
        result = configure_file_logging(log_path)
        assert result == log_path.resolve()
        assert log_path.parent.exists()
        assert log_path.exists()
        assert len(_matching_handlers(log_path)) == 1
        assert logging.getLogger().level <= logging.INFO
    finally:
        _remove_handler(log_path)


def test_configure_file_logging_idempotent(tmp_path):
    log_path = tmp_path / "log" / "app.log"
    try:
        configure_file_logging(log_path)
        before = len(_matching_handlers(log_path))
        configure_file_logging(log_path)
        configure_file_logging(log_path)
        after = len(_matching_handlers(log_path))
        assert after == before == 1
    finally:
        _remove_handler(log_path)


def test_uvicorn_log_config_propagates_to_root():
    config = uvicorn_log_config()
    assert config["version"] == 1
    assert config["disable_existing_loggers"] is False
    assert config["handlers"] == {}
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger_cfg = config["loggers"][logger_name]
        assert logger_cfg["handlers"] == []
        assert logger_cfg["propagate"] is True


def test_log_stream_write_records_lines(caplog):
    stream = LogStream(logging.INFO, "test.logstream")
    with caplog.at_level(logging.INFO, logger="test.logstream"):
        stream.write("hello\n")
        stream.write("world\n")
    messages = [record.message for record in caplog.records if record.name == "test.logstream"]
    assert messages == ["hello", "world"]


def test_log_stream_flush_emits_partial_line(caplog):
    stream = LogStream(logging.INFO, "test.logstream.flush")
    with caplog.at_level(logging.INFO, logger="test.logstream.flush"):
        stream.write("partial")
        stream.flush()
    messages = [record.message for record in caplog.records if record.name == "test.logstream.flush"]
    assert messages == ["partial"]


def test_log_stream_skips_blank_lines(caplog):
    stream = LogStream(logging.INFO, "test.logstream.blank")
    with caplog.at_level(logging.INFO, logger="test.logstream.blank"):
        stream.write("\n\n")
        stream.flush()
    messages = [record.message for record in caplog.records if record.name == "test.logstream.blank"]
    assert messages == []


def test_redirect_replaces_none_stdout_and_stderr(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    redirect_std_streams_to_logging()
    assert isinstance(sys.stdout, LogStream)
    assert isinstance(sys.stderr, LogStream)


def test_redirect_keeps_existing_stdout_and_stderr():
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    redirect_std_streams_to_logging()
    assert sys.stdout is original_stdout
    assert sys.stderr is original_stderr
