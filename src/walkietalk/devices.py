"""Exact audio selection and read-only serial permission diagnostics."""

import os
from pathlib import Path

from .config import Config, WalkietalkError


def audio_devices() -> list[dict]:
    import sounddevice as sd

    try:
        return list(sd.query_devices())
    except sd.PortAudioError as exc:
        raise WalkietalkError(f"Cannot enumerate audio devices: {exc}") from exc


def resolve_device(devices: list[dict], name: str, direction: str) -> int:
    matches = [
        i
        for i, device in enumerate(devices)
        if device["name"] == name and device[f"max_{direction}_channels"] > 0
    ]
    if len(matches) != 1:
        raise WalkietalkError(
            f"Expected one {direction} device named {name!r}; found {len(matches)}. "
            "Run `walkietalk devices` and copy the exact AIOC name."
        )
    if name.lower() in {"default", "sysdefault", "pulse", "pipewire", "dmix"}:
        raise WalkietalkError("Select the physical AIOC device, not a default/router device")
    return matches[0]


def check_serial(port: str) -> None:
    path = Path(port)
    if not path.exists():
        raise WalkietalkError(
            f"Serial device not found: {port}. Connect the AIOC and check its path."
        )
    if not path.is_char_device():
        raise WalkietalkError(f"Not a serial character device: {port}")
    if not os.access(path, os.R_OK | os.W_OK):
        raise WalkietalkError(
            f"No read/write permission for {port}. See README: Linux serial permissions. "
            "Run walkietalk as your normal user, not with sudo."
        )


def preflight(config: Config, *, require_serial: bool = True) -> int:
    devices = audio_devices()
    input_id = resolve_device(devices, config.input_device, "input")
    output_id = resolve_device(devices, config.output_device, "output")
    print(f"Capture [{input_id}]: {config.input_device}", flush=True)
    print(f"Playback [{output_id}]: {config.output_device}", flush=True)
    if require_serial:
        print(
            f"PTT: {config.serial_port}; {config.line.upper()} active, other line low",
            flush=True,
        )
        check_serial(config.serial_port)
    return output_id
