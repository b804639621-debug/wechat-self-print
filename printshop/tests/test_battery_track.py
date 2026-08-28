from pathlib import Path

import pytest

from printshop.battery_track import (
    _same_minute,
    append_history,
    estimate_runtime,
    load_history,
    main,
)


def _sample(timestamp, pct, vbus="false", charging="false"):
    return {
        "timestamp": timestamp,
        "pct": pct,
        "vbus": vbus,
        "charging": charging,
    }


def test_estimate_runtime_insufficient_samples():
    samples = [
        _sample("2026-08-27 19:00:00", 90),
    ]
    assert estimate_runtime(samples) == (None, 1)


def test_estimate_runtime_span_too_small():
    samples = [
        _sample("2026-08-27 19:00:00", 90),
        _sample("2026-08-27 19:05:00", 89),
    ]
    # 跨度 5 分钟，小于默认 min_span_minutes=10
    assert estimate_runtime(samples) == (None, 2)


def test_estimate_runtime_ten_percent_per_hour():
    # 1 小时掉 10%，当前（最近）100% -> 剩余约 10 小时。
    # 纯函数允许 pct 略高于 100 的构造样本，用于让最近样本正好是 100。
    samples = [
        _sample("2026-08-27 18:00:00", 110),
        _sample("2026-08-27 19:00:00", 100),
    ]
    hours, n = estimate_runtime(samples, min_span_minutes=0)
    assert n == 2
    assert hours == pytest.approx(10.0, abs=0.5)


def test_estimate_runtime_ignores_non_discharge_samples():
    samples = [
        _sample("2026-08-27 18:00:00", 100, vbus="true", charging="true"),
        _sample("2026-08-27 18:30:00", 100, vbus="true", charging="false"),
        _sample("2026-08-27 19:00:00", 100, vbus="false", charging="true"),
    ]
    # 没有任何放电样本
    assert estimate_runtime(samples, min_span_minutes=0) == (None, 0)


def test_estimate_runtime_uses_only_discharge_samples():
    samples = [
        _sample("2026-08-27 18:00:00", 100, vbus="true", charging="true"),
        _sample("2026-08-27 18:30:00", 90, vbus="false", charging="false"),
        _sample("2026-08-27 19:00:00", 80, vbus="false", charging="false"),
        _sample("2026-08-27 19:30:00", 70, vbus="false", charging="false"),
    ]
    hours, n = estimate_runtime(samples, min_span_minutes=0)
    assert n == 3
    # 只用 18:30/19:00/19:30 三个放电点：掉 20%/h，最近 70% -> 3.5h
    assert hours == pytest.approx(3.5, abs=0.1)


def test_same_minute():
    assert _same_minute("2026-08-27 20:00:00", "2026-08-27 20:00:59") is True
    assert _same_minute("2026-08-27 20:00:00", "2026-08-27 20:01:00") is False


def test_load_history_missing_returns_empty(tmp_path: Path):
    assert load_history(tmp_path / "missing.csv") == []


def test_append_history_creates_file_with_header_and_row(tmp_path: Path):
    path = tmp_path / "battery.csv"
    row = {
        "timestamp": "2026-08-27 20:00:00",
        "pct": 85,
        "mv": 3900,
        "charging": "false",
        "vbus": "false",
        "runtime_estimate_hours": "6.3",
        "samples": 3,
        "note": "discharge",
    }
    assert append_history(path, row) is True
    rows = load_history(path)
    assert len(rows) == 1
    assert rows[0]["timestamp"] == "2026-08-27 20:00:00"
    assert rows[0]["pct"] == "85"
    assert rows[0]["vbus"] == "false"


def test_append_history_dedupe_same_minute_same_pct_vbus(tmp_path: Path):
    path = tmp_path / "battery.csv"
    row1 = {
        "timestamp": "2026-08-27 20:00:00",
        "pct": 85,
        "mv": 3900,
        "charging": "false",
        "vbus": "false",
        "runtime_estimate_hours": "",
        "samples": 0,
        "note": "discharge",
    }
    row2 = dict(row1, timestamp="2026-08-27 20:00:40")
    assert append_history(path, row1) is True
    assert append_history(path, row2) is False
    assert len(load_history(path)) == 1


def test_append_history_allows_same_minute_different_pct(tmp_path: Path):
    path = tmp_path / "battery.csv"
    row1 = {
        "timestamp": "2026-08-27 20:00:00",
        "pct": 85,
        "mv": 3900,
        "charging": "false",
        "vbus": "false",
        "runtime_estimate_hours": "",
        "samples": 0,
        "note": "discharge",
    }
    row2 = dict(row1, timestamp="2026-08-27 20:00:40", pct=84)
    assert append_history(path, row1) is True
    assert append_history(path, row2) is True
    assert len(load_history(path)) == 2


def test_append_history_max_samples_keeps_recent(tmp_path: Path):
    path = tmp_path / "battery.csv"
    for i in range(5):
        row = {
            "timestamp": f"2026-08-27 20:{i:02d}:00",
            "pct": 100 - i,
            "mv": 4000,
            "charging": "false",
            "vbus": "false",
            "runtime_estimate_hours": "",
            "samples": 0,
            "note": "discharge",
        }
        append_history(path, row)
    append_history(path, {
        "timestamp": "2026-08-27 20:05:00",
        "pct": 95,
        "mv": 4000,
        "charging": "false",
        "vbus": "false",
        "runtime_estimate_hours": "",
        "samples": 0,
        "note": "discharge",
    }, max_samples=3)
    rows = load_history(path)
    assert len(rows) == 3
    assert rows[0]["timestamp"] == "2026-08-27 20:03:00"
    assert rows[-1]["timestamp"] == "2026-08-27 20:05:00"


def test_main_returns_1_when_fetch_fails(monkeypatch, tmp_path: Path, capsys):
    def boom(base_url):
        raise RuntimeError("connection refused")

    monkeypatch.setattr("printshop.battery_track.fetch_screen_state", boom)
    rc = main([
        "--base-url", "http://127.0.0.1:1",
        "--csv", str(tmp_path / "battery.csv"),
    ])
    assert rc == 1
    assert "connection refused" in capsys.readouterr().err


def test_main_returns_0_when_no_battery(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setattr("printshop.battery_track.fetch_screen_state", lambda base_url: None)
    rc = main([
        "--base-url", "http://127.0.0.1:8000",
        "--csv", str(tmp_path / "battery.csv"),
    ])
    out = capsys.readouterr().out
    assert rc == 0
    assert "未上报电量" in out
