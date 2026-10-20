"""Feature-scoped relay commands sent through the ESP32-S3 link."""

from __future__ import annotations

import argparse
import json
import logging
import threading
from pathlib import Path
from typing import Any, Mapping

try:
    from services.esp32_link import Esp32Link
except ModuleNotFoundError:  # Supports `python services/relay_controller.py`.
    from esp32_link import Esp32Link


LOGGER = logging.getLogger(__name__)
DEFAULT_RELAY_MAP = Path(__file__).resolve().parents[1] / "config" / "relay_map.json"


class RelayController:
    """Send only known feature relay commands and retain their requested state."""

    def __init__(self, link: Esp32Link, relay_map: Mapping[str, Any]) -> None:
        self._link = link
        self._features = frozenset(key for key in relay_map if not key.startswith("_"))
        if not self._features:
            raise ValueError("relay map does not define any features")
        self._states: dict[str, bool | None] = {
            feature: None for feature in self._features
        }
        self._lock = threading.Lock()

    @classmethod
    def from_config(cls, link: Esp32Link, path: Path = DEFAULT_RELAY_MAP) -> "RelayController":
        """Build a controller from the shared feature-to-GPIO configuration."""
        with path.open(encoding="utf-8") as relay_map_file:
            relay_map = json.load(relay_map_file)
        if not isinstance(relay_map, dict):
            raise ValueError("relay map must contain a JSON object")
        return cls(link, relay_map)

    def trigger(self, feature: str, active: bool) -> bool:
        """Set a feature relay state.

        Returns ``True`` when a command was sent. Repeating the existing state is
        ignored, which prevents feature loops from flooding the serial link.
        """
        if feature not in self._features:
            raise ValueError(f"unknown relay feature: {feature}")
        if not isinstance(active, bool):
            raise TypeError("active must be a bool")
        with self._lock:
            if self._states[feature] is active:
                return False
            self._link.send_json(
                {"cmd": "relay", "feature": feature, "state": int(active)}
            )
            self._states[feature] = active
        LOGGER.info("relay %s set to %s", feature, "HIGH" if active else "LOW")
        return True

    def states(self) -> dict[str, bool | None]:
        """Return each feature's last requested relay state."""
        with self._lock:
            return self._states.copy()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use the mock ESP32-S3 link")
    parser.add_argument("--port", default="/dev/esp32", help="ESP32-S3 serial device")
    parser.add_argument("--feature", default="drowsiness", help="configured feature name")
    parser.add_argument(
        "--state", choices=("on", "off"), default="on", help="requested relay state"
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    link = Esp32Link(port=args.port, mock=args.mock)
    try:
        link.start()
        controller = RelayController.from_config(link)
        controller.trigger(args.feature, args.state == "on")
        print(json.dumps(controller.states(), sort_keys=True))
    finally:
        link.stop()


if __name__ == "__main__":
    main()
