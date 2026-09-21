"""AIOC serial control. Never send radio programming bytes."""

from .config import WalkietalkError


class SerialPTT:
    def __init__(self, port: str, line: str = "dtr"):
        self.port = port
        self.line = line
        self.serial = None

    def open(self) -> None:
        import serial

        # Set idle levels before opening: Serial(port=...) would open immediately
        # with pySerial's default asserted DTR/RTS values.
        self.serial = serial.Serial(port=None, timeout=0, write_timeout=1, exclusive=True)
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.port = self.port
        try:
            self.serial.open()
        except BaseException:
            self.serial.close()
            self.serial = None
            raise

    def on(self) -> None:
        setattr(self.serial, self.line, True)
        print("PTT ON", flush=True)

    def off(self) -> None:
        if self.serial is None or not self.serial.is_open:
            return
        errors = []
        # Deassert the selected line first, and attempt both even on failure.
        other = "rts" if self.line == "dtr" else "dtr"
        for line in (self.line, other):
            try:
                setattr(self.serial, line, False)
            except OSError as exc:
                errors.append(str(exc))
        if errors:
            raise WalkietalkError(
                "PTT release could not be confirmed; turn the radio off. " + "; ".join(errors)
            )
        print("PTT OFF", flush=True)
        if getattr(self.serial, self.line, False):
            raise WalkietalkError("PTT still asserted after release; turn the radio off")

    def close(self) -> None:
        if self.serial is not None:
            self.serial.close()


class DryPTT:
    def open(self) -> None:
        print("DRY RUN: no serial port or audio device will be opened", flush=True)

    def on(self) -> None:
        print("DRY RUN: PTT ON", flush=True)

    def off(self) -> None:
        print("DRY RUN: PTT OFF", flush=True)

    def close(self) -> None:
        pass
