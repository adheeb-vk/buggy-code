"""ESP32-S3 USB serial link with JSON-lines telemetry and command framing."""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import serial


LOGGER = logging.getLogger(__name__)
TelemetryCallback = Callable[[dict[str, Any]], None]


class SerialPort(Protocol):
    """The subset of pyserial used by Esp32Link."""

    def read(self, size: int = 1) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


class MockTelemetry:
    """Deterministic synthetic ESP32-S3 sensor readings for indoor development."""

    def __init__(self) -> None:
        self._sample = 0

    def next(self) -> dict[str, Any]:
        self._sample += 1
        phase = self._sample / 10
        vertical_acceleration = (
            24000
            if self._sample % 15 == 0
            else 16384 + round(35 * math.sin(phase * 2))
        )
        return {
            "seatbelt": 1,
            "alcohol": 0,
            "water": 45,
            "current": 210,
            "accel": {
                "x": round(120 * math.sin(phase)),
                "y": round(80 * math.cos(phase)),
                "z": vertical_acceleration,
            },
            "aqi": 72 + round(8 * math.sin(phase)),
        }


class Esp32Link:
    """Read ESP32-S3 telemetry and send newline-delimited JSON commands.

    Unknown telemetry keys are retained. Malformed lines are logged and ignored so
    one bad serial message does not stop the rest of the stack.
    """

    def __init__(
        self,
        *,
        port: str = "/dev/esp32",
        baudrate: int = 115200,
        mock: bool = False,
        sample_hz: float = 5.0,
        on_telemetry: TelemetryCallback | None = None,
        serial_factory: Callable[..., SerialPort] = serial.Serial,
    ) -> None:
        if sample_hz <= 0:
            raise ValueError("sample_hz must be greater than zero")
        self.port = port
        self.baudrate = baudrate
        self.mock = mock
        self.sample_hz = sample_hz
        self._telemetry_listeners: list[TelemetryCallback] = []
        if on_telemetry:
            self._telemetry_listeners.append(on_telemetry)
        self._serial_factory = serial_factory
        self._serial: SerialPort | None = None
        self._buffer = bytearray()
        self._latest: dict[str, Any] = {}
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self.mock_commands: list[dict[str, Any]] = []

    def start(self) -> None:
        """Start the background reader or mock telemetry generator."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        if not self.mock:
            self._serial = self._serial_factory(self.port, self.baudrate, timeout=0.2)
            LOGGER.info("connected to ESP32-S3 at %s", self.port)
        target = self._run_mock if self.mock else self._run_serial
        self._thread = threading.Thread(target=target, name="esp32-link", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop the reader and close the hardware serial port, if open."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._serial:
            self._serial.close()
            self._serial = None

    def latest_telemetry(self) -> dict[str, Any]:
        """Return a copy of the latest complete telemetry object."""
        with self._lock:
            return self._latest.copy()

    def add_telemetry_listener(self, listener: TelemetryCallback) -> None:
        """Register a callback for each complete telemetry JSON object."""
        with self._lock:
            self._telemetry_listeners.append(listener)

    def feed_bytes(self, data: bytes) -> None:
        """Feed arbitrary serial chunks into the newline JSON parser.

        This public method lets tests and alternate transports exercise the same
        framing that a physical USB serial device uses.
        """
        self._buffer.extend(data)
        while b"\n" in self._buffer:
            line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            self._handle_line(line.rstrip(b"\r"))

    def send_json(self, message: Mapping[str, Any]) -> None:
        """Send one Pi-to-ESP32 protocol message as a JSON line."""
        encoded = json.dumps(dict(message), separators=(",", ":")) + "\n"
        if self.mock:
            command = dict(message)
            self.mock_commands.append(command)
            LOGGER.info("mock ESP32 command: %s", encoded.rstrip())
            return
        if not self._serial:
            raise RuntimeError("ESP32 link is not running")
        self._serial.write(encoded.encode("utf-8"))

    def _run_mock(self) -> None:
        generator = MockTelemetry()
        interval = 1 / self.sample_hz
        while not self._stop_event.is_set():
            payload = generator.next()
            self.feed_bytes((json.dumps(payload) + "\n").encode("utf-8"))
            self._stop_event.wait(interval)

    def _run_serial(self) -> None:
        assert self._serial is not None
        while not self._stop_event.is_set():
            chunk = self._serial.read(512)
            if chunk:
                self.feed_bytes(chunk)

    def _handle_line(self, line: bytes) -> None:
        if not line:
            return
        try:
            decoded = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            LOGGER.warning("ignoring malformed ESP32 JSON line: %r", line)
            return
        if not isinstance(decoded, dict):
            LOGGER.warning("ignoring ESP32 JSON value that is not an object: %r", decoded)
            return
        telemetry_keys = {"seatbelt", "alcohol", "water", "current", "aqi", "accel"}
        if telemetry_keys.isdisjoint(decoded):
            LOGGER.info("ESP32 control message: %s", decoded)
            return
        with self._lock:
            self._latest = decoded
            listeners = tuple(self._telemetry_listeners)
        for listener in listeners:
            listener(decoded.copy())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="generate synthetic telemetry")
    parser.add_argument("--port", default="/dev/esp32", help="ESP32-S3 serial device")
    parser.add_argument("--baudrate", type=int, default=115200, help="serial baud rate")
    parser.add_argument("--once", action="store_true", help="print one reading and exit")
    parser.add_argument("--timeout", type=float, default=5.0, help="seconds to wait with --once")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    first_reading = threading.Event()
    link = Esp32Link(
        port=args.port,
        baudrate=args.baudrate,
        mock=args.mock,
        on_telemetry=lambda _: first_reading.set(),
    )
    try:
        link.start()
        if args.once:
            if not first_reading.wait(timeout=args.timeout):
                raise TimeoutError(f"no ESP32 telemetry received within {args.timeout:g} seconds")
            print(json.dumps(link.latest_telemetry(), sort_keys=True))
            return
        LOGGER.info("ESP32 link running; press Ctrl-C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOGGER.info("stopping ESP32 link")
    finally:
        link.stop()


if __name__ == "__main__":
    main()
