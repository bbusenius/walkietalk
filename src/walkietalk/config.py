"""Validate configuration before touching audio or serial hardware."""

import math
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import yaml

STT_MODELS = ("tiny", "base")
STT_BACKENDS = ("faster-whisper", "grok", "grok_api")
TTS_NORMALIZE = ("off", "peak")
CALLSIGN_MODES = ("off", "end_of_reply", "interval")
AGENT_BACKENDS = ("stub", "hermes", "codex", "grok", "claude", "claude_api")
AGENT_BACKEND_ERROR = (
    "agent.backend must be stub, hermes, codex, grok, claude, or claude_api; "
    "other names are not implemented. Choose claude for the official CLI's saved login "
    "or claude_api for billed Messages API access"
)
REASONING_EFFORTS = {
    "codex": ("default", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    "grok": ("default", "none", "minimal", "low", "medium", "high", "xhigh", "max"),
    "claude": ("default", "low", "medium", "high", "xhigh", "max"),
    "claude_api": ("default", "low", "medium", "high", "xhigh", "max"),
}


class WalkietalkError(Exception):
    """A failure that should be shown without a Python traceback."""


def validate_gain(value: object) -> float:
    """Allow attenuation or amplification, but never invalid numeric values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WalkietalkError("audio.gain must be a positive finite number")
    try:
        gain = float(value)
    except OverflowError:
        raise WalkietalkError("audio.gain must be a positive finite number") from None
    if not math.isfinite(gain) or gain <= 0:
        raise WalkietalkError("audio.gain must be a positive finite number")
    return gain


def seconds(value: object, name: str, minimum: float = 0, maximum: float = 30) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WalkietalkError(f"{name} must be a number")
    result = float(value)
    if not math.isfinite(result) or not minimum < result <= maximum:
        raise WalkietalkError(f"{name} must be greater than {minimum} and at most {maximum}")
    return result


def positive_integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise WalkietalkError(f"{name} must be an integer")
    if not 0 < value <= maximum:
        raise WalkietalkError(f"{name} must be greater than 0 and at most {maximum}")
    return value


@dataclass(frozen=True)
class Config:
    input_device: str = "SIMULATED INPUT"
    output_device: str = "SIMULATED OUTPUT"
    gain: float = 0.25
    serial_port: str = "SIMULATED PTT"
    line: str = "dtr"
    max_tx_seconds: float = 10
    settle_seconds: float = 0.2
    post_tx_mute_seconds: float = 0
    callsign: str = ""
    callsign_mode: str = "off"
    callsign_interval_seconds: float = 900
    energy_threshold: float = 0.02
    hangover_ms: int = 400
    max_utterance_seconds: float = 12
    stt_model: str = "base"
    stt_backend: str = "faster-whisper"
    stt_timeout_seconds: float = 30
    listening_mode: str = "wake_phrase"
    conversation_timeout_seconds: float = 60
    wake_primary: str = "charlotte"
    wake_aliases: tuple[str, ...] = ()
    agent_backend: str = "stub"
    agent_max_reply_chars: int = 600
    agent_history_turns: int = 8
    agent_timeout_seconds: float = 60
    hermes_url: str = "http://127.0.0.1:8642"
    hermes_token_env: str = "WALKIETALK_HERMES_TOKEN"
    codex_executable: str = "codex"
    codex_model: str = ""
    grok_executable: str = "grok"
    grok_model: str = "grok-4.6"
    codex_reasoning_effort: str = "low"
    grok_reasoning_effort: str = "low"
    claude_executable: str = "claude"
    claude_model: str = "claude-sonnet-5"
    claude_reasoning_effort: str = "low"
    claude_api_key_env: str = "ANTHROPIC_API_KEY"
    claude_api_model: str = "claude-sonnet-5"
    claude_api_reasoning_effort: str = "low"
    shutdown_enabled: bool = False
    shutdown_phrase: str = ""
    shutdown_phrase_aliases: tuple[str, ...] = ()
    shutdown_code: str = ""
    shutdown_code_aliases: tuple[str, ...] = ()
    shutdown_confirmation_seconds: float = 30
    shutdown_confirmation_phrase: str = ""
    tts_backend: str = "piper"
    piper_executable: str = "piper"
    piper_model: str = "~/.cache/walkietalk/piper/en_US-amy-medium.onnx"
    tts_timeout_seconds: float = 30
    grok_tts_voice: str = "eve"
    grok_tts_language: str = "en"
    grok_tts_speed: float = 1
    grok_tts_api_key_env: str = "XAI_API_KEY"
    tts_normalize: str = "off"
    hermes_tts_url: str = "http://127.0.0.1:8643"
    hermes_tts_token_env: str = "WALKIETALK_HERMES_TOKEN"


def load_config(path: Path) -> Config:
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as exc:
        raise WalkietalkError(f"Cannot read config {path}: {exc}") from exc
    expected = {
        "audio": {"input_device", "output_device", "gain"},
        "ptt": {"serial_port", "line"},
        "radio": {
            "max_tx_seconds",
            "settle_seconds",
            "post_tx_mute_seconds",
            "callsign",
            "callsign_mode",
            "callsign_interval_seconds",
        },
        "vad": {"energy_threshold", "hangover_ms", "max_utterance_seconds"},
        "stt": {"backend", "model", "timeout_seconds"},
        "tts": {
            "backend",
            "piper_executable",
            "piper_model",
            "timeout_seconds",
            "grok_voice",
            "grok_language",
            "grok_speed",
            "grok_api_key_env",
            "hermes_url",
            "hermes_token_env",
            "normalize",
        },
        "listening": {"mode", "conversation_timeout_seconds"},
        "wake": {"primary", "aliases"},
        "shutdown": {
            "enabled",
            "phrase",
            "phrase_aliases",
            "code",
            "code_aliases",
            "confirmation_seconds",
            "confirmation_phrase",
        },
        "agent": {
            "backend",
            "max_reply_chars",
            "history_turns",
            "timeout_seconds",
            "hermes_url",
            "hermes_token_env",
            "codex_executable",
            "codex_model",
            "grok_executable",
            "grok_model",
            "codex_reasoning_effort",
            "grok_reasoning_effort",
            "claude_executable",
            "claude_model",
            "claude_reasoning_effort",
            "claude_api_key_env",
            "claude_api_model",
            "claude_api_reasoning_effort",
        },
    }
    if not isinstance(data, dict) or set(data) != set(expected):
        raise WalkietalkError(
            "Config must contain exactly agent, audio, listening, ptt, radio, shutdown, stt, tts, "
            "vad, and wake sections. See config.example.yaml for required fields."
        )
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
    backend = data["stt"]["backend"]
    if backend not in STT_BACKENDS:
        raise WalkietalkError("stt.backend must be faster-whisper, grok, or grok_api")
    model = data["stt"]["model"]
    if model not in STT_MODELS:
        raise WalkietalkError("stt.model must be tiny or base")
    if data["tts"]["backend"] not in ("piper", "grok", "grok_api", "hermes"):
        raise WalkietalkError("tts.backend must be piper, grok, grok_api, or hermes; no fallback")
    normalize = data["tts"]["normalize"]
    if normalize not in TTS_NORMALIZE:
        raise WalkietalkError("tts.normalize must be off or peak")
    voice = data["tts"]["grok_voice"]
    language = data["tts"]["grok_language"]
    key_env = data["tts"]["grok_api_key_env"]
    if not isinstance(voice, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", voice):
        raise WalkietalkError("tts.grok_voice must be a built-in or custom voice ID")
    if not isinstance(language, str) or not re.fullmatch(
        r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*", language
    ):
        raise WalkietalkError("tts.grok_language must be a language code such as en, or auto")
    if not isinstance(key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise WalkietalkError("tts.grok_api_key_env must be an environment variable name")
    speed = seconds(data["tts"]["grok_speed"], "tts.grok_speed", maximum=1.5)
    if speed < 0.7:
        raise WalkietalkError("tts.grok_speed must be between 0.7 and 1.5")
    piper_executable = data["tts"]["piper_executable"]
    if (
        not isinstance(piper_executable, str)
        or not piper_executable.strip()
        or piper_executable != piper_executable.strip()
        or any(not char.isprintable() for char in piper_executable)
        or (
            not Path(piper_executable).is_absolute()
            and ("/" in piper_executable or any(char.isspace() for char in piper_executable))
        )
    ):
        raise WalkietalkError("tts.piper_executable must be an executable name or absolute path")
    piper_model = data["tts"]["piper_model"]
    if (
        not isinstance(piper_model, str)
        or not piper_model.endswith(".onnx")
        or any(not char.isprintable() for char in piper_model)
    ):
        raise WalkietalkError("tts.piper_model must be a local .onnx file path")
    # Relative voice paths are relative to this config file, never a CLI temp directory.
    model_path = Path(piper_model).expanduser()
    if not model_path.is_absolute():
        model_path = path.resolve().parent / model_path
    agent_backend = data["agent"]["backend"]
    if agent_backend not in AGENT_BACKENDS:
        raise WalkietalkError(AGENT_BACKEND_ERROR)
    for agent, allowed in REASONING_EFFORTS.items():
        field = f"{agent}_reasoning_effort"
        value = data["agent"][field]
        if not isinstance(value, str) or value not in allowed:
            raise WalkietalkError(f"agent.{field} must be one of: {', '.join(allowed)}")
    for field in ("codex_executable", "grok_executable", "claude_executable"):
        executable = data["agent"][field]
        if (
            not isinstance(executable, str)
            or not executable.strip()
            or executable != executable.strip()
            or any(not char.isprintable() for char in executable)
            or (
                not Path(executable).is_absolute()
                and ("/" in executable or any(char.isspace() for char in executable))
            )
        ):
            raise WalkietalkError(f"agent.{field} must be an executable name or absolute path")
    codex_model = data["agent"]["codex_model"]
    if not isinstance(codex_model, str) or (
        codex_model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", codex_model)
    ):
        raise WalkietalkError(
            "agent.codex_model must be a model ID or an empty string for the CLI default"
        )
    grok_model = data["agent"]["grok_model"]
    if not isinstance(grok_model, str) or not re.fullmatch(r"grok-[A-Za-z0-9._-]+", grok_model):
        raise WalkietalkError("agent.grok_model must be a first-party Grok model ID")
    for field in ("claude_model", "claude_api_model"):
        value = data["agent"][field]
        if not isinstance(value, str) or not re.fullmatch(r"claude-[A-Za-z0-9._-]+", value):
            raise WalkietalkError(f"agent.{field} must be an explicit first-party Claude model ID")
    api_key_env = data["agent"]["claude_api_key_env"]
    if not isinstance(api_key_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise WalkietalkError(
            "agent.claude_api_key_env must name an environment variable, not a key"
        )
    for section in ("agent", "tts"):
        hermes_url = data[section]["hermes_url"]
        try:
            url = urlsplit(hermes_url) if isinstance(hermes_url, str) else None
            valid_url = (
                url is not None
                and url.scheme in {"http", "https"}
                and url.hostname
                and not url.username
                and not url.password
                and not url.query
                and not url.fragment
                and not any(char.isspace() or not char.isprintable() for char in hermes_url)
            )
            if url is not None:
                _port = url.port  # Validate the port as well as the host.
        except ValueError:
            valid_url = False
        if not valid_url:
            raise WalkietalkError(
                f"{section}.hermes_url must be an HTTP(S) base URL without credentials or query"
            )
        token_env = data[section]["hermes_token_env"]
        if not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
            raise WalkietalkError(
                f"{section}.hermes_token_env must name an environment variable, not a token"
            )
    mode = data["listening"]["mode"]
    if mode not in ("wake_phrase", "conversation"):
        raise WalkietalkError("listening.mode must be wake_phrase or conversation")
    primary = data["wake"]["primary"]
    if not isinstance(primary, str) or not primary.strip():
        raise WalkietalkError("wake.primary must be a non-empty name")
    aliases = data["wake"]["aliases"]
    if not isinstance(aliases, list) or any(
        not isinstance(alias, str) or not alias.strip() for alias in aliases
    ):
        raise WalkietalkError("wake.aliases must be a list of non-empty names")
    from .shutdown import normalize_command
    from .wake import strip_wake

    shutdown = data["shutdown"]
    if not isinstance(shutdown["enabled"], bool):
        raise WalkietalkError("shutdown.enabled must be true or false")
    for field in ("phrase", "code", "confirmation_phrase"):
        value = shutdown[field]
        if (
            not isinstance(value, str)
            or len(value) > 200
            or any(not char.isprintable() for char in value)
            or (value and not normalize_command(value))
        ):
            raise WalkietalkError(
                f"shutdown.{field} must be text with words, at most 200 characters"
            )
        if field == "confirmation_phrase":
            continue
        variants = shutdown[f"{field}_aliases"]
        if not isinstance(variants, list) or any(
            not isinstance(item, str)
            or not normalize_command(item)
            or len(item) > 200
            or any(not char.isprintable() for char in item)
            for item in variants
        ):
            raise WalkietalkError(f"shutdown.{field}_aliases must be a list of non-empty phrases")
    if shutdown["enabled"]:
        if not all(
            normalize_command(shutdown[field])
            for field in ("phrase", "code", "confirmation_phrase")
        ):
            raise WalkietalkError(
                "Set shutdown.phrase, shutdown.code, and shutdown.confirmation_phrase "
                "before enabling shutdown"
            )

        def control_variants(values):
            return {
                normalize_command(candidate)
                for value in values
                for candidate in (value, strip_wake(value, primary, aliases)[1])
            }

        arm = control_variants((shutdown["phrase"], *shutdown["phrase_aliases"]))
        codes = control_variants((shutdown["code"], *shutdown["code_aliases"]))
        spoken = control_variants((shutdown["confirmation_phrase"],))
        wakes = {normalize_command(v) for v in (primary, *aliases)}
        if arm & codes or spoken & (arm | codes) or (arm | codes | spoken) & wakes:
            raise WalkietalkError(
                "Shutdown phrases, codes, confirmation phrase, and wake names must be distinct"
            )
    confirmation = seconds(
        shutdown["confirmation_seconds"], "shutdown.confirmation_seconds", maximum=300
    )
    gain = validate_gain(data["audio"]["gain"])
    cap = data["radio"]["max_tx_seconds"]
    if isinstance(cap, bool) or not isinstance(cap, (int, float)):
        raise WalkietalkError("radio.max_tx_seconds must be a number")
    cap = float(cap)
    if not math.isfinite(cap) or cap <= 0:
        raise WalkietalkError("radio.max_tx_seconds must be a finite number greater than 0")
    settle = seconds(data["radio"]["settle_seconds"], "radio.settle_seconds", maximum=2)
    if settle >= cap:
        raise WalkietalkError("radio.settle_seconds must be less than max_tx_seconds")
    mute = data["radio"]["post_tx_mute_seconds"]
    if isinstance(mute, bool) or not isinstance(mute, (int, float)):
        raise WalkietalkError("radio.post_tx_mute_seconds must be a number")
    mute = float(mute)
    if not math.isfinite(mute) or mute < 0 or mute > 30:
        raise WalkietalkError("radio.post_tx_mute_seconds must be from 0 through 30")
    callsign = data["radio"]["callsign"]
    if (
        not isinstance(callsign, str)
        or len(callsign) > 64
        or any(not char.isprintable() for char in callsign)
        or (callsign.strip() and not re.search(r"[A-Za-z0-9]", callsign))
    ):
        raise WalkietalkError(
            "radio.callsign must be empty or your station ID, at most 64 characters"
        )
    ident_mode = data["radio"]["callsign_mode"]
    if ident_mode not in CALLSIGN_MODES:
        raise WalkietalkError("radio.callsign_mode must be off, end_of_reply, or interval")
    interval = seconds(
        data["radio"]["callsign_interval_seconds"],
        "radio.callsign_interval_seconds",
        maximum=1800,
    )
    return Config(
        input_device=data["audio"]["input_device"],
        output_device=data["audio"]["output_device"],
        gain=gain,
        serial_port=port,
        line=data["ptt"]["line"],
        max_tx_seconds=cap,
        settle_seconds=settle,
        post_tx_mute_seconds=mute,
        callsign=callsign.strip(),
        callsign_mode=ident_mode,
        callsign_interval_seconds=interval,
        energy_threshold=seconds(
            data["vad"]["energy_threshold"], "vad.energy_threshold", maximum=1
        ),
        hangover_ms=positive_integer(data["vad"]["hangover_ms"], "vad.hangover_ms", maximum=5000),
        max_utterance_seconds=seconds(
            data["vad"]["max_utterance_seconds"], "vad.max_utterance_seconds"
        ),
        stt_model=model,
        stt_backend=backend,
        stt_timeout_seconds=seconds(
            data["stt"]["timeout_seconds"], "stt.timeout_seconds", maximum=120
        ),
        listening_mode=mode,
        conversation_timeout_seconds=seconds(
            data["listening"]["conversation_timeout_seconds"],
            "listening.conversation_timeout_seconds",
            maximum=600,
        ),
        wake_primary=primary.strip(),
        wake_aliases=tuple(alias.strip() for alias in aliases),
        agent_backend=agent_backend,
        agent_max_reply_chars=positive_integer(
            data["agent"]["max_reply_chars"], "agent.max_reply_chars", maximum=2000
        ),
        agent_history_turns=positive_integer(
            data["agent"]["history_turns"], "agent.history_turns", maximum=32
        ),
        agent_timeout_seconds=seconds(
            data["agent"]["timeout_seconds"], "agent.timeout_seconds", maximum=300
        ),
        hermes_url=data["agent"]["hermes_url"].rstrip("/"),
        hermes_token_env=data["agent"]["hermes_token_env"],
        codex_executable=data["agent"]["codex_executable"],
        grok_executable=data["agent"]["grok_executable"],
        grok_model=grok_model,
        codex_model=codex_model,
        codex_reasoning_effort=data["agent"]["codex_reasoning_effort"],
        grok_reasoning_effort=data["agent"]["grok_reasoning_effort"],
        claude_executable=data["agent"]["claude_executable"],
        claude_model=data["agent"]["claude_model"],
        claude_reasoning_effort=data["agent"]["claude_reasoning_effort"],
        claude_api_key_env=api_key_env,
        claude_api_model=data["agent"]["claude_api_model"],
        claude_api_reasoning_effort=data["agent"]["claude_api_reasoning_effort"],
        shutdown_enabled=shutdown["enabled"],
        shutdown_phrase=shutdown["phrase"].strip(),
        shutdown_phrase_aliases=tuple(v.strip() for v in shutdown["phrase_aliases"]),
        shutdown_code=shutdown["code"].strip(),
        shutdown_code_aliases=tuple(v.strip() for v in shutdown["code_aliases"]),
        shutdown_confirmation_seconds=confirmation,
        shutdown_confirmation_phrase=shutdown["confirmation_phrase"].strip(),
        tts_backend=data["tts"]["backend"],
        piper_executable=piper_executable,
        piper_model=str(model_path),
        tts_timeout_seconds=seconds(
            data["tts"]["timeout_seconds"], "tts.timeout_seconds", maximum=120
        ),
        grok_tts_voice=voice,
        grok_tts_language=language,
        grok_tts_speed=speed,
        grok_tts_api_key_env=key_env,
        tts_normalize=normalize,
        hermes_tts_url=data["tts"]["hermes_url"].rstrip("/"),
        hermes_tts_token_env=data["tts"]["hermes_token_env"],
    )
