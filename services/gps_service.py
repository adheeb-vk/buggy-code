"""SIM808 GPS service using AT+CGNSPWR and AT+CGNSINF."""

from __future__ import annotations

import argparse
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol

import serial


LOGGER = logging.getLogger(__name__)
FixCallback = Callable[["GpsFix"], None]
DEFAULT_PI5_GPS_PORT = "/dev/ttyAMA0"


class GpsSerialPort(Protocol):
    """The pyserial methods used by Sim808GpsService."""

    def readline(self) -> bytes: ...

    def write(self, data: bytes) -> int: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class GpsFix:
    """A valid geographic fix returned by SIM808 GNSS."""

    latitude: float
    longitude: float
    timestamp_utc: datetime
    altitude_m: float | None = None
    speed_kph: float | None = None
    course_degrees: float | None = None

    def as_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["timestamp_utc"] = self.timestamp_utc.isoformat()
        return result


def parse_cgnsinf(line: str) -> GpsFix | None:
    """Parse a SIM808 ``+CGNSINF`` line, returning only a valid GNSS fix.

    SIM808 reports speed in km/h. It may return a running GNSS engine with a
    zero fix status while it is still acquiring satellites; that is not a fix.
    """
    prefix = "+CGNSINF:"
    if not line.strip().startswith(prefix):
        return None
    fields = [field.strip() for field in line.split(":", 1)[1].split(",")]
    if len(fields) < 8 or fields[1] != "1":
        return None
    try:
        timestamp = _parse_sim808_timestamp(fields[2])
        return GpsFix(
            latitude=float(fields[3]),
            longitude=float(fields[4]),
            timestamp_utc=timestamp,
            altitude_m=_optional_float(fields[5]),
            speed_kph=_optional_float(fields[6]),
            course_degrees=_optional_float(fields[7]),
        )
    except (TypeError, ValueError):
        LOGGER.warning("ignoring invalid +CGNSINF response: %s", line.strip())
        return None


def _parse_sim808_timestamp(value: str) -> datetime:
    """Parse SIM808 UTC timestamps with or without fractional seconds."""
    base = value.split(".", 1)[0]
    return datetime.strptime(base, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def _optional_float(value: str) -> float | None:
    return float(value) if value else None


class Sim808GpsService:
    """Poll GPS fixes from a SIM808 UART or a deterministic mock track."""

    # Arbitrary indoor-development track, deliberately separate from venue zones.
    MOCK_TRACK = ((12.0000, 77.0000), (12.0001, 77.0001), (12.0002, 77.0002))

    def __init__(
        self,
        *,
        port: str = DEFAULT_PI5_GPS_PORT,
        baudrate: int = 115200,
        mock: bool = False,
        poll_interval_seconds: float = 1.0,
        on_fix: FixCallback | None = None,
        serial_factory: Callable[..., GpsSerialPort] = serial.Serial,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be greater than zero")
        self.port = port
        self.baudrate = baudrate
        self.mock = mock
        self.poll_interval_seconds = poll_interval_seconds
        self._fix_listeners: list[FixCallback] = []
        if on_fix:
            self._fix_listeners.append(on_fix)
        self._serial_factory = serial_factory
        self._serial: GpsSerialPort | None = None
        self._latest: GpsFix | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Open the GPS source and start polling for fixes."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        if self.mock:
            target = self._run_mock
        else:
            self._serial = self._serial_factory(self.port, self.baudrate, timeout=1)
            LOGGER.info("connected to SIM808 at %s", self.port)
            self._enable_gnss()
            target = self._run_hardware
        self._thread = threading.Thread(target=target, name="sim808-gps", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and close the SIM808 UART."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=self.poll_interval_seconds + 2)
            self._thread = None
        if self._serial:
            self._serial.close()
            self._serial = None

    def latest_fix(self) -> GpsFix | None:
        """Return the latest valid GPS fix, if one has been acquired."""
        with self._lock:
            return self._latest

    def add_fix_listener(self, listener: FixCallback) -> None:
        """Register a callback for each valid GPS fix."""
        with self._lock:
            self._fix_listeners.append(listener)

    def send_at_command(self, command: str) -> list[str]:
        """Send one AT command after the service has opened the UART."""
        return self._send_at(command)

    def _enable_gnss(self) -> None:
        self._send_at("AT")
        self._send_at("AT+CGNSPWR=1")
        LOGGER.info("SIM808 GNSS power requested")

    def _run_hardware(self) -> None:
        while not self._stop_event.is_set():
            response = self._send_at("AT+CGNSINF")
            for line in response:
                fix = parse_cgnsinf(line)
                if fix:
                    self._publish(fix)
                    break
            self._stop_event.wait(self.poll_interval_seconds)

    def _run_mock(self) -> None:
        for index in _cycle_indices(len(self.MOCK_TRACK)):
            if self._stop_event.is_set():
                return
            latitude, longitude = self.MOCK_TRACK[index]
            self._publish(
                GpsFix(
                    latitude=latitude,
                    longitude=longitude,
                    timestamp_utc=datetime.now(timezone.utc),
                    altitude_m=0.0,
                    speed_kph=8.0,
                    course_degrees=45.0,
                )
            )
            self._stop_event.wait(self.poll_interval_seconds)

    def _send_at(self, command: str) -> list[str]:
        if not self._serial:
            raise RuntimeError("SIM808 GPS service is not running")
        self._serial.write((command + "\r\n").encode("ascii"))
        lines: list[str] = []
        while not self._stop_event.is_set():
            raw = self._serial.readline()
            if not raw:
                break
            line = raw.decode("ascii", errors="replace").strip()
            if not line or line == command:
                continue
            lines.append(line)
            if line == "OK":
                return lines
            if line == "ERROR":
                raise RuntimeError(f"SIM808 rejected command {command}")
        raise TimeoutError(f"SIM808 did not acknowledge command {command}")

    def _publish(self, fix: GpsFix) -> None:
        with self._lock:
            self._latest = fix
            listeners = tuple(self._fix_listeners)
        for listener in listeners:
            listener(fix)


def _cycle_indices(length: int):
    index = 0
    while True:
        yield index
        index = (index + 1) % length


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use a synthetic looping GPS track")
    parser.add_argument("--port", default=DEFAULT_PI5_GPS_PORT, help="SIM808 UART device")
    parser.add_argument("--baudrate", type=int, default=115200, help="SIM808 UART baud rate")
    parser.add_argument("--once", action="store_true", help="print one fix and exit")
    parser.add_argument("--at-test", action="store_true", help="verify SIM808 UART AT communication and exit")
    parser.add_argument("--timeout", type=float, default=90.0, help="seconds to wait with --once")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    received_fix = threading.Event()
    service = Sim808GpsService(
        port=args.port,
        baudrate=args.baudrate,
        mock=args.mock,
        on_fix=lambda _: received_fix.set(),
    )
    try:
        service.start()
        if args.at_test:
            print(json.dumps({"response": service.send_at_command("AT")}, sort_keys=True))
            return
        if args.once:
            if not received_fix.wait(timeout=args.timeout):
                raise TimeoutError(f"no valid GPS fix received within {args.timeout:g} seconds")
            fix = service.latest_fix()
            assert fix is not None
            print(json.dumps(fix.as_dict(), sort_keys=True))
            return
        LOGGER.info("GPS service running; press Ctrl-C to stop")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        LOGGER.info("stopping GPS service")
    finally:
        service.stop()


if __name__ == "__main__":
    main()
