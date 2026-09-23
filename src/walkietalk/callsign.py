"""Optional GMRS station ID. The operator supplies the callsign; never invent one."""

from collections.abc import Callable

from .audio import Wav
from .config import Config
from .tts import RADIO_RATE, radio_wav

IDENT_GAP_SECONDS = 0.2


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


def identification_transmissions(answer: Wav, ident: Wav, maximum: float) -> tuple[Wav, ...]:
    """Append the full ID when possible; otherwise plan a separate ID burst."""
    answer = radio_wav(answer, maximum, truncate=True)
    ident = radio_wav(ident, maximum)
    gap = max(0, int(IDENT_GAP_SECONDS * RADIO_RATE))
    ident_samples = len(ident.frames) // 2
    keep = max(0, int(maximum * RADIO_RATE) - ident_samples - gap)
    if keep < 1:
        return answer, ident
    frames = answer.frames[: keep * 2] + b"\x00\x00" * gap + ident.frames
    duration = len(frames) / (2 * RADIO_RATE)
    return (radio_wav(Wav(frames, RADIO_RATE, duration), maximum),)
