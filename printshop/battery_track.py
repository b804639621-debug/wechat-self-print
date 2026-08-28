"""电池电量记录与续航估算脚本。

用法：
    python -m printshop.battery_track [--base-url http://127.0.0.1:8000]
                                      [--csv battery_history.csv]
                                      [--min-span-minutes 10]
                                      [--max-samples 1000]

仅使用标准库，可被单测 import。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT_CSV = Path(__file__).resolve().parents[1] / "battery_history.csv"
CSV_FIELDS = [
    "timestamp",
    "pct",
    "mv",
    "charging",
    "vbus",
    "runtime_estimate_hours",
    "samples",
    "note",
]
_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def _bool_str(value: Any) -> str:
    """把接口返回的布尔值/字符串统一成 CSV 中的 true/false。"""
    if isinstance(value, str):
        return "true" if value.strip().lower() in ("true", "1", "yes", "on") else "false"
    return "true" if bool(value) else "false"


def _is_discharge(sample: dict[str, Any]) -> bool:
    """vbus=false 且 charging=false 视为电池放电。"""
    vbus = str(sample.get("vbus", "")).strip().lower()
    charging = str(sample.get("charging", "")).strip().lower()
    return vbus == "false" and charging == "false"


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.strptime(str(value), _TIMESTAMP_FORMAT)


def _same_minute(a: str, b: str) -> bool:
    """比较两个 'YYYY-MM-DD HH:MM:SS' 时间字符串是否在同一分钟。"""
    return str(a)[:16] == str(b)[:16]


def load_history(path: Path) -> list[dict[str, Any]]:
    """读取历史 CSV；文件不存在或为空时返回空列表。"""
    path = Path(path)
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def _should_append(history: list[dict[str, Any]], row: dict[str, Any]) -> bool:
    """幂等去重：最后一条与当前同一分钟且 pct/vbus 相同时不追加。"""
    if not history:
        return True
    last = history[-1]
    same_minute = _same_minute(last.get("timestamp", ""), row.get("timestamp", ""))
    same_value = (
        str(last.get("pct", "")) == str(row.get("pct", ""))
        and _bool_str(last.get("vbus", "")) == _bool_str(row.get("vbus", ""))
    )
    return not (same_minute and same_value)


def append_history(
    path: Path,
    row: dict[str, Any],
    max_samples: int = 1000,
) -> bool:
    """追加一行 CSV；返回 True 表示已写入，False 表示命中幂等去重跳过。"""
    path = Path(path)
    history = load_history(path)
    normalized = {
        field: (_bool_str(row.get(field, "")) if field in ("charging", "vbus") else row.get(field, ""))
        for field in CSV_FIELDS
    }
    if not _should_append(history, normalized):
        return False

    path.parent.mkdir(parents=True, exist_ok=True)
    history.append(normalized)
    if max_samples and max_samples > 0 and len(history) > max_samples:
        history = history[-max_samples:]

    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(history)
    return True


def _recent_discharge_samples(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只保留当前连续放电会话的样本：遇到充电/USB 样本就重置。

    避免上次电池放电的历史污染本次续航估算。
    """
    recent: list[dict[str, Any]] = []
    for s in samples:
        if _is_discharge(s):
            recent.append(s)
        else:
            recent = []
    return recent


def estimate_runtime(
    samples: list[dict[str, Any]],
    min_span_minutes: int = 10,
) -> tuple[float | None, int]:
    """根据放电样本估算剩余续航。

    返回 (remaining_hours, 有效放电样本数)；无法估算时 remaining_hours 为 None。
    """
    discharge = [s for s in samples if _is_discharge(s)]
    n = len(discharge)
    if n < 2:
        return (None, n)

    # 按时间升序，避免调用方顺序不稳定影响回归。
    discharge = sorted(discharge, key=lambda s: _parse_timestamp(s.get("timestamp", "")))
    first_time = _parse_timestamp(discharge[0].get("timestamp", ""))
    last_time = _parse_timestamp(discharge[-1].get("timestamp", ""))
    span_hours = (last_time - first_time).total_seconds() / 3600.0
    if span_hours < min_span_minutes / 60.0:
        return (None, n)

    xs = [(_parse_timestamp(s.get("timestamp", "")) - first_time).total_seconds() / 3600.0
          for s in discharge]
    ys = [float(s.get("pct")) for s in discharge]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    if sxx == 0:
        return (None, n)

    slope = sxy / sxx
    drain_per_hour = -slope
    if drain_per_hour <= 0:
        return (None, n)

    recent_pct = ys[-1]
    remaining_hours = recent_pct / drain_per_hour
    return (remaining_hours, n)


def fetch_screen_state(base_url: str) -> dict[str, Any] | None:
    """请求主控 /api/screen/state 并返回 battery 字典；无 battery 返回 None。"""
    url = base_url.rstrip("/") + "/api/screen/state"
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    battery = payload.get("battery") if isinstance(payload, dict) else None
    return battery


def _build_row(
    battery: dict[str, Any],
    timestamp: str,
    estimate: float | None,
    samples: int,
) -> dict[str, Any]:
    charging = _bool_str(battery.get("charging", False))
    vbus = _bool_str(battery.get("vbus", False))
    discharging = vbus == "false" and charging == "false"
    if discharging:
        note = "discharge"
    elif vbus == "true":
        note = "charging" if charging == "true" else "usb"
    else:
        note = "unknown"
    return {
        "timestamp": timestamp,
        "pct": battery.get("pct", ""),
        "mv": battery.get("mv", ""),
        "charging": charging,
        "vbus": vbus,
        "runtime_estimate_hours": f"{estimate:.1f}" if estimate is not None else "",
        "samples": samples,
        "note": note,
    }


def _format_summary(row: dict[str, Any]) -> str:
    timestamp = row.get("timestamp", "")
    pct = row.get("pct", "")
    vbus = row.get("vbus", "")
    charging = row.get("charging", "")
    estimate = row.get("runtime_estimate_hours", "")
    samples = row.get("samples", 0)
    if vbus == "false":
        power = "battery"
    else:
        power = "usb/charging"
    text = f"{timestamp} pct={pct} vbus={power} charging={charging}"
    if estimate:
        text += f" estimate={estimate}h"
    text += f" samples={samples}"
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="读取小屏电量并记录/估算续航")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--min-span-minutes", type=int, default=10)
    parser.add_argument("--max-samples", type=int, default=1000)
    args = parser.parse_args(argv)

    history = load_history(args.csv)

    try:
        battery = fetch_screen_state(args.base_url)
    except Exception as exc:
        print(f"获取电量失败: {exc}", file=sys.stderr)
        return 1

    if battery is None:
        print("未上报电量")
        return 0

    now = datetime.now().strftime(_TIMESTAMP_FORMAT)
    charging = _bool_str(battery.get("charging", False))
    vbus = _bool_str(battery.get("vbus", False))
    discharging = vbus == "false" and charging == "false"

    current_row = _build_row(battery, now, None, 0)
    estimate: float | None = None
    valid_samples = 0

    if discharging:
        current_sample = {
            "timestamp": now,
            "pct": battery.get("pct", ""),
            "vbus": vbus,
            "charging": charging,
        }
        estimate_samples = list(history)
        # 若当前样本与历史最后一条重复（同一分钟且 pct/vbus 相同），
        # 历史里已有该点，不再重复加入估算集。
        if _should_append(history, current_row):
            estimate_samples.append(current_sample)
        # 只取当前连续放电会话，避免上次电池放电样本污染本次估算。
        estimate_samples = _recent_discharge_samples(estimate_samples)
        estimate, valid_samples = estimate_runtime(
            estimate_samples,
            min_span_minutes=args.min_span_minutes,
        )

    row = _build_row(battery, now, estimate, valid_samples)
    append_history(args.csv, row, max_samples=args.max_samples)
    print(_format_summary(row))
    return 0


if __name__ == "__main__":
    sys.exit(main())
