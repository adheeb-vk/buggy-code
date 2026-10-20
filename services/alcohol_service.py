"""Monitor the MQ-3 raw signal and optionally control an alcohol alert relay.

The MQ-3 senses alcohol vapour.  Its raw ADC value needs an on-vehicle
warm-up and calibration before it can be used as an alert threshold.  Until
both alcohol thresholds in ``config/thresholds.json`` are set, this service is
monitor-only and never sends a relay command.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

try:
    from services.esp32_link import Esp32Link
    from services.relay_controller import RelayController
except ModuleNotFoundError:  # Supports `python services/alcohol_service.py`.
    from esp32_link import Esp32Link
    from relay_controller import RelayController


LOGGER = logging.getLogger(__name__)
DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[1] / "config" / "thresholds.json"
StatusCallback = Callable[["AlcoholStatus"], None]


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...


@dataclass(frozen=True)
class AlcoholConfig:
    """Raw ADC thresholds, deliberately absent until physical calibration."""

    trigger_above: int | None
    hysteresis: int | None

    @classmethod
    def from_json(cls, path: Path = DEFAULT_THRESHOLDS) -> "AlcoholConfig":
        with path.open(encoding="utf-8") as thresholds_file:
            values = json.load(thresholds_file)["alcohol"]
        trigger = values.get("trigger_above")
        hysteresis = values.get("hysteresis")
        if (trigger is None) != (hysteresis is None):
            raise ValueError(
                "alcohol trigger_above and hysteresis must both be set, or both be null"
            )
        if trigger is None:
            return cls(None, None)
        trigger_int = int(trigger)
        hysteresis_int = int(hysteresis)
        if trigger_int < 0 or hysteresis_int < 0 or hysteresis_int > trigger_int:
            raise ValueError("invalid alcohol raw ADC threshold or hysteresis")
        return cls(trigger_int, hysteresis_int)

    @property
    def configured(self) -> bool:
        return self.trigger_above is not None

    @property
    def clear_below_or_equal(self) -> int | None:
        if self.trigger_above is None or self.hysteresis is None:
            return None
        return self.trigger_above - self.hysteresis


@dataclass(frozen=True)
class AlcoholStatus:
    raw_value: int | None
    alcohol_suspected: bool
    threshold_configured: bool
    sample_count: int
    sample_min: int | None
    sample_mean: float | None
    sample_max: int | None
    updated_at_utc: str | None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class AlcoholService:
    """Apply calibrated hysteresis to ESP32 ``alcohol`` telemetry."""

    def __init__(
        self,
        relay_controller: RelayTrigger,
        config: AlcoholConfig,
        *,
        on_status: StatusCallback | None = None,
        history_size: int = 600,
    ) -> None:
        if history_size < 1:
            raise ValueError("history_size must be at least one")
        self._relay_controller = relay_controller
        self._config = config
        self._on_status = on_status
        self._samples: deque[int] = deque(maxlen=history_size)
        self._lock = threading.Lock()
        self._status = AlcoholStatus(None, False, config.configured, 0, None, None, None, None)

    def process_telemetry(self, telemetry: Mapping[str, object]) -> AlcoholStatus | None:
        """Consume a numeric raw ``alcohol`` value, if present."""
        if "alcohol" not in telemetry:
            return None
        try:
            raw_value = int(telemetry["alcohol"])
        except (TypeError, ValueError):
            LOGGER.warning("ignoring non-numeric alcohol value: %r", telemetry["alcohol"])
            return None
        if raw_value < 0:
            LOGGER.warning("ignoring negative alcohol value: %r", raw_value)
            return None

        relay_change: bool | None = None
        timestamp = datetime.now(timezone.utc).isoformat()
        with self._lock:
            self._samples.append(raw_value)
            active = self._status.alcohol_suspected
            if self._config.configured:
                assert self._config.trigger_above is not None
                assert self._config.clear_below_or_equal is not None
                if not active and raw_value > self._config.trigger_above:
                    active = True
                    relay_change = True
                elif active and raw_value <= self._config.clear_below_or_equal:
                    active = False
                    relay_change = False
            samples = tuple(self._samples)
            self._status = AlcoholStatus(
                raw_value=raw_value,
                alcohol_suspected=active,
                threshold_configured=self._config.configured,
                sample_count=len(samples),
                sample_min=min(samples),
                sample_mean=round(statistics.fmean(samples), 2),
                sample_max=max(samples),
                updated_at_utc=timestamp,
            )
            status = self._status
        if relay_change is not None:
            self._relay_controller.trigger("alcohol", relay_change)
            LOGGER.info("alcohol relay set to %s at raw value %d", "HIGH" if relay_change else "LOW", raw_value)
        if self._on_status:
            self._on_status(status)
        return status

    def latest_status(self) -> AlcoholStatus:
        with self._lock:
            return self._status

    def stop(self) -> None:
        """Clear a previously requested alcohol relay state on shutdown."""
        with self._lock:
            was_active = self._status.alcohol_suspected
            if was_active:
                self._status = AlcoholStatus(
                    self._status.raw_value,
                    False,
                    self._status.threshold_configured,
                    self._status.sample_count,
                    self._status.sample_min,
                    self._status.sample_mean,
                    self._status.sample_max,
                    self._status.updated_at_utc,
                )
        if was_active:
            self._relay_controller.trigger("alcohol", False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use synthetic ESP32 telemetry")
    parser.add_argument("--port", default="/dev/esp32", help="ESP32-S3 serial device")
    parser.add_argument("--baudrate", type=int, default=115200, help="ESP32-S3 serial baud rate")
    parser.add_argument("--duration", type=float, default=30.0, help="sampling duration in seconds")
    args = parser.parse_args()
    if args.duration <= 0:
        parser.error("--duration must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = AlcoholConfig.from_json()
    if not config.configured:
        LOGGER.warning(
            "alcohol threshold is not configured: collecting raw values only; no relay command will be sent"
        )
    link = Esp32Link(port=args.port, baudrate=args.baudrate, mock=args.mock)
    controller = RelayController.from_config(link)
    service = AlcoholService(controller, config)
    link.add_telemetry_listener(service.process_telemetry)
    try:
        link.start()
        time.sleep(args.duration)
        status = service.latest_status()
        if status.raw_value is None:
            raise RuntimeError(
                "no alcohol field received; set PIN_ALCOHOL only after verifying the ESP32-S3 GPIO"
            )
        print(json.dumps(status.as_dict(), sort_keys=True))
    except KeyboardInterrupt:
        LOGGER.info("stopping alcohol monitor")
    finally:
        service.stop()
        link.stop()


if __name__ == "__main__":
    main()
