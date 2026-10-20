"""Unit tests for relay commands over the ESP32-S3 serial link."""

import json
import time
import unittest

from services.esp32_link import Esp32Link
from services.relay_controller import RelayController


class FakeSerial:
    def __init__(self) -> None:
        self.writes: list[bytes] = []
        self.closed = False

    def read(self, size: int = 1) -> bytes:
        time.sleep(0.001)
        return b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)

    def close(self) -> None:
        self.closed = True


class RelayControllerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.serial_port = FakeSerial()
        self.link = Esp32Link(
            serial_factory=lambda *args, **kwargs: self.serial_port,
        )
        self.link.start()
        self.controller = RelayController(
            self.link, {"drowsiness": None, "pothole": None}
        )

    def tearDown(self) -> None:
        self.link.stop()

    def test_trigger_writes_protocol_json_line(self) -> None:
        self.assertTrue(self.controller.trigger("drowsiness", True))
        self.assertEqual(
            self.serial_port.writes,
            [b'{"cmd":"relay","feature":"drowsiness","state":1}\n'],
        )
        self.assertEqual(self.controller.states()["drowsiness"], True)

    def test_repeated_state_does_not_write_again(self) -> None:
        self.controller.trigger("pothole", True)
        self.assertFalse(self.controller.trigger("pothole", True))
        self.assertEqual(len(self.serial_port.writes), 1)

    def test_unknown_feature_is_rejected_before_serial_write(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown relay feature"):
            self.controller.trigger("unknown", True)
        self.assertEqual(self.serial_port.writes, [])

    def test_false_state_serializes_as_zero(self) -> None:
        self.controller.trigger("drowsiness", False)
        message = json.loads(self.serial_port.writes[0])
        self.assertEqual(message["state"], 0)


if __name__ == "__main__":
    unittest.main()
