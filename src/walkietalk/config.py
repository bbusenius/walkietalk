"""Validate configuration before touching audio or serial hardware."""

import math
from dataclasses import dataclass
from pathlib import Path

import yaml


class WalkietalkError(Exception):
    """A failure that should be shown without a Python traceback."""


def seconds(value: object, name: str, minimum: float = 0, maximum: float = 30) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WalkietalkError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum < result <= maximum:
        raise WalkietalkError(f"{name} must be greater than {minimum} and at most {maximum}")
    return result


@dataclass(frozen=True)
class Config:
    input_device: str = "SIMULATED INPUT"
    output_device: str = "SIMULATED OUTPUT"
    gain: float = 0.25
    serial_port: str = "SIMULATED PTT"
    line: str = "dtr"
    max_tx_seconds: float = 10
    settle_seconds: float = 0.2


def load_config(path: Path) -> Config:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise WalkietalkError(f"Cannot read config {path}: {exc}") from exc
    expected = {
        "audio": {"input_device", "output_device", "gain"},
        "ptt": {"serial_port", "line"},
        "radio": {"max_tx_seconds", "settle_seconds"},
    }
    if not isinstance(data, dict) or set(data) != set(expected):
        raise WalkietalkError("Config must contain exactly audio, ptt, and radio sections")
    for section, fields in expected.items():
        if not isinstance(data[section], dict) or set(data[section]) != fields:
            raise WalkietalkError(f"{section} requires these fields: {', '.join(sorted(fields))}")
    for field in ("input_device", "output_device"):
        value = data["audio"][field]
        if not isinstance(value, str) or not value.strip() or value.strip().lower() == "default":
            raise WalkietalkError(f"audio.{field} must be an explicit device name, never default")
    port = data["ptt"]["serial_port"]
    if not isinstance(port, str) or not port.startswith("/dev/"):
        raise WalkietalkError("ptt.serial_port must be an absolute /dev/ device path")
    if data["ptt"]["line"] not in ("dtr", "rts"):
        raise WalkietalkError("ptt.line must be dtr or rts (AIOC normally uses dtr)")
    gain = seconds(data["audio"]["gain"], "audio.gain", maximum=1)
    cap = seconds(data["radio"]["max_tx_seconds"], "radio.max_tx_seconds")
    settle = seconds(data["radio"]["settle_seconds"], "radio.settle_seconds", maximum=2)
    if settle >= cap:
        raise WalkietalkError("radio.settle_seconds must be less than max_tx_seconds")
    return Config(
        input_device=data["audio"]["input_device"],
        output_device=data["audio"]["output_device"],
        gain=gain,
        serial_port=port,
        line=data["ptt"]["line"],
        max_tx_seconds=cap,
        settle_seconds=settle,
    )
