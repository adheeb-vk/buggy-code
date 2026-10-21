"""Convert ESP32 ultrasonic telemetry into dashboard-ready parking status."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from pathlib import Path


DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[1] / "config" / "thresholds.json"
StatusCallback = Callable[["ParkingStatus"], None]


@dataclass(frozen=True)
class ParkingConfig:
    clear_above_cm: float
    caution_above_cm: float
    warning_above_cm: float

    @classmethod
    def from_json(cls, path: Path = DEFAULT_THRESHOLDS) -> "ParkingConfig":
        with path.open(encoding="utf-8") as source:
            values = json.load(source)["parking"]
        config = cls(**{key: float(values[key]) for key in cls.__annotations__})
        if not config.clear_above_cm > config.caution_above_cm > config.warning_above_cm > 0:
            raise ValueError("parking distance bands must be descending positive values")
        return config


@dataclass(frozen=True)
class ParkingStatus:
    front_left_cm: float | None
    front_right_cm: float | None
    rear_cm: float | None
    front_left_level: str
    front_right_level: str
    rear_level: str

    def as_dict(self) -> dict[str, float | None]:
        return asdict(self)


class ParkingAssistService:
    """Read optional ultrasonic fields without treating missing sensors as clear."""

    FIELDS = {
        "front_left_cm": "ultrasonic_front_left_cm",
        "front_right_cm": "ultrasonic_front_right_cm",
        "rear_cm": "ultrasonic_rear_cm",
    }

    def __init__(
        self,
        config: ParkingConfig,
        *,
        on_status: StatusCallback | None = None,
    ) -> None:
        self._config = config
        self._on_status = on_status
        self._lock = threading.Lock()
        self._enabled = True
        self._status = ParkingStatus(None, None, None, "WAITING", "WAITING", "WAITING")

    def set_enabled(self, enabled: bool) -> None:
        """Enable proximity processing or clear the displayed readings."""
        with self._lock:
            if self._enabled == enabled:
                return
            self._enabled = enabled
            self._status = ParkingStatus(None, None, None, "WAITING", "WAITING", "WAITING")
            status = self._status
        if self._on_status:
            self._on_status(status)

    def process_telemetry(self, telemetry: Mapping[str, object]) -> ParkingStatus | None:
        if not any(field in telemetry for field in self.FIELDS.values()):
            return None
        values: dict[str, float | None] = {}
        for output_name, input_name in self.FIELDS.items():
            raw = telemetry.get(input_name)
            try:
                value = float(raw) if raw is not None else None
            except (TypeError, ValueError):
                value = None
            values[output_name] = value if value is None or value >= 0 else None
        with self._lock:
            if not self._enabled:
                return None
            self._status = ParkingStatus(
                **values,
                front_left_level=self._level(values["front_left_cm"]),
                front_right_level=self._level(values["front_right_cm"]),
                rear_level=self._level(values["rear_cm"]),
            )
            status = self._status
        if self._on_status:
            self._on_status(status)
        return status

    def latest_status(self) -> ParkingStatus:
        with self._lock:
            return self._status

    def _level(self, distance_cm: float | None) -> str:
        if distance_cm is None:
            return "WAITING"
        if distance_cm <= self._config.warning_above_cm:
            return "DANGER"
        if distance_cm <= self._config.caution_above_cm:
            return "WARNING"
        if distance_cm <= self._config.clear_above_cm:
            return "CAUTION"
        return "CLEAR"
