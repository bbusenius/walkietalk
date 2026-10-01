"""Optional GMRS station ID. The operator supplies the callsign; never invent one."""

from collections.abc import Callable

import numpy as np

from .audio import Wav
from .config import Config, WalkietalkError
from .tts import RADIO_RATE, radio_wav

IDENT_GAP_SECONDS = 0.2
# ITU-R M.1677-1 timing at 20 WPM: a unit is 60 ms. The tone is inside the
# voice band, at half scale so playback gain still has headroom.
MORSE_WPM = 20
MORSE_TONE_HZ = 800
MORSE_AMPLITUDE = 0.5
MORSE_EDGE_SECONDS = 0.005
MORSE_CODE = {
    "A": ".-",
    "B": "-...",
    "C": "-.-.",
    "D": "-..",
    "E": ".",
    "F": "..-.",
    "G": "--.",
    "H": "....",
    "I": "..",
    "J": ".---",
    "K": "-.-",
    "L": ".-..",
    "M": "--",
    "N": "-.",
    "O": "---",
    "P": ".--.",
    "Q": "--.-",
    "R": ".-.",
    "S": "...",
    "T": "-",
    "U": "..-",
    "V": "...-",
    "W": ".--",
    "X": "-..-",
    "Y": "-.--",
    "Z": "--..",
    "0": "-----",
    "1": ".----",
    "2": "..---",
    "3": "...--",
    "4": "....-",
    "5": ".....",
    "6": "-....",
    "7": "--...",
    "8": "---..",
    "9": "----.",
}


class StationIDError(WalkietalkError):
    """Stop traffic on ID failure while preserving a completed message burst."""

    def __init__(self, message: str, *, message_transmitted: bool = False):
        super().__init__(f"Station ID failed: {message}; stopping talk.")
        self.message_transmitted = message_transmitted


class CallsignSession:
    def __init__(self, config: Config, clock: Callable[[], float]) -> None:
        self.config = config
        self.clock = clock
        self.last_id_at: float | None = None

    def enabled(self) -> bool:
        return bool(self.config.callsign) and self.config.callsign_mode != "off"

    def due(self) -> bool:
        if not self.enabled():
            return False
        mode = self.config.callsign_mode
        if mode == "end_of_reply":
            return True
        if self.last_id_at is None:
            return True
        return self.clock() - self.last_id_at >= self.config.callsign_interval_seconds

    def mark(self) -> None:
        self.last_id_at = self.clock()


def morse_unit_samples() -> int:
    return round(1.2 / MORSE_WPM * RADIO_RATE)


def morse_runs(text: str) -> tuple[int, ...]:
    """Dit-lengths for an audible-tone ID. Positive is tone, negative is silence.

    A word space, including a unit number after the call sign, is seven dits.
    Runs of spaces collapse to one word space.
    """
    words = text.strip().split()
    if not words:
        raise WalkietalkError("Morse station ID must contain a letter or digit")
    runs: list[int] = []
    for word_index, word in enumerate(words):
        if word_index:
            runs.append(-7)
        for char_index, char in enumerate(word):
            pattern = MORSE_CODE.get(char.upper())
            if pattern is None:
                raise WalkietalkError(
                    "Morse station ID must contain only letters, digits, and spaces"
                )
            if char_index:
                runs.append(-3)
            for element_index, element in enumerate(pattern):
                if element_index:
                    runs.append(-1)
                runs.append(3 if element == "-" else 1)
    return tuple(runs)


def _tone(count: int) -> np.ndarray:
    wave = np.sin(2 * np.pi * MORSE_TONE_HZ * np.arange(count) / RADIO_RATE)
    edge = min(round(MORSE_EDGE_SECONDS * RADIO_RATE), count // 2)
    if edge:
        ramp = np.linspace(0, 1, edge, endpoint=False)
        wave[:edge] *= ramp
        wave[-edge:] *= ramp[::-1]
    return wave * (MORSE_AMPLITUDE * 32767)


def morse_wav(text: str) -> Wav:
    """International Morse as 48 kHz PCM. The whole call sign is one clip."""
    unit = morse_unit_samples()
    pieces = [
        _tone(abs(run) * unit) if run > 0 else np.zeros(abs(run) * unit) for run in morse_runs(text)
    ]
    samples = np.concatenate(pieces)
    frames = np.rint(samples).clip(-32768, 32767).astype("<i2").tobytes()
    return Wav(frames, RADIO_RATE, len(samples) / RADIO_RATE)


def station_id_wav(config: Config, voice=None) -> Wav:
    """Voice speaks the call sign. Morse is synthesized here, not by TTS."""
    if config.callsign_method == "morse":
        return morse_wav(config.callsign)
    if voice is None:
        raise WalkietalkError("Voice station ID requires a TTS backend")
    return voice.synthesize(config.callsign, truncate=False)


def identification_transmissions(answer: Wav, ident: Wav, maximum: float) -> tuple[Wav, ...]:
    """Append the full ID when possible; otherwise plan a separate ID burst."""
    answer = radio_wav(answer, maximum)
    ident = radio_wav(ident, maximum)
    gap = max(0, int(IDENT_GAP_SECONDS * RADIO_RATE))
    ident_samples = len(ident.frames) // 2
    keep = max(0, int(maximum * RADIO_RATE) - ident_samples - gap)
    if len(answer.frames) // 2 > keep:
        return answer, ident
    frames = answer.frames + b"\x00\x00" * gap + ident.frames
    duration = len(frames) / (2 * RADIO_RATE)
    return (radio_wav(Wav(frames, RADIO_RATE, duration), maximum),)
