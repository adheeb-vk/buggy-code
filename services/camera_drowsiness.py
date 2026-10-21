"""USB camera MediaPipe EAR and head-yaw drowsiness service."""

from __future__ import annotations

import argparse
import json
import logging
import math
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

try:
    from services.esp32_link import Esp32Link
    from services.relay_controller import RelayController
except ModuleNotFoundError:  # Supports `python services/camera_drowsiness.py`.
    from esp32_link import Esp32Link
    from relay_controller import RelayController


LOGGER = logging.getLogger(__name__)
DEFAULT_THRESHOLDS = Path(__file__).resolve().parents[1] / "config" / "thresholds.json"
DEFAULT_FACE_MODEL = Path(__file__).resolve().parents[1] / "models" / "face_landmarker.task"

# Face Mesh landmark indexes, ordered outer corner, upper points, inner corner,
# lower points for the standard EAR calculation.
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
StatusCallback = Callable[["DrowsinessStatus"], None]


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...


@dataclass(frozen=True)
class DrowsinessConfig:
    ear_below: float
    consecutive_frames: int
    head_yaw_degrees: float
    clear_debounce_seconds: float

    @classmethod
    def from_json(cls, path: Path = DEFAULT_THRESHOLDS) -> "DrowsinessConfig":
        with path.open(encoding="utf-8") as thresholds_file:
            values = json.load(thresholds_file)["drowsiness"]
        required = (
            "ear_below",
            "consecutive_frames",
            "head_yaw_degrees",
            "clear_debounce_seconds",
        )
        if any(values.get(key) is None for key in required):
            raise ValueError("drowsiness thresholds must be configured")
        config = cls(**{key: values[key] for key in required})
        if (
            config.ear_below <= 0
            or config.consecutive_frames < 1
            or config.head_yaw_degrees <= 0
            or config.clear_debounce_seconds < 0
        ):
            raise ValueError("drowsiness thresholds must be positive")
        return config


@dataclass(frozen=True)
class DrowsinessStatus:
    ear: float | None
    yaw_degrees: float | None
    drowsy: bool
    distracted: bool
    alert_active: bool
    face_detected: bool

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


class CameraDrowsinessService:
    """Detect persistent low EAR or head yaw and drive the drowsiness relay."""

    def __init__(
        self,
        relay_controller: RelayTrigger,
        config: DrowsinessConfig,
        *,
        mock: bool = False,
        camera_index: int = 0,
        face_model_path: Path = DEFAULT_FACE_MODEL,
        on_status: StatusCallback | None = None,
        mock_frame_interval_seconds: float = 0.1,
    ) -> None:
        if mock_frame_interval_seconds <= 0:
            raise ValueError("mock_frame_interval_seconds must be greater than zero")
        self._relay_controller = relay_controller
        self._config = config
        self._mock = mock
        self._camera_index = camera_index
        self._face_model_path = face_model_path
        self._on_status = on_status
        self._mock_frame_interval_seconds = mock_frame_interval_seconds
        self._lock = threading.Lock()
        self._enabled = True
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._fatal_error: Exception | None = None
        self._eye_frames = 0
        self._yaw_frames = 0
        self._normal_since: float | None = None
        self._status = DrowsinessStatus(None, None, False, False, False, False)

    def start(self) -> None:
        """Start the USB camera pipeline or a synthetic mock measurement stream."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._fatal_error = None
        target = self._run_mock if self._mock else self._run_camera
        self._thread = threading.Thread(
            target=lambda: self._run_with_error_capture(target),
            name="camera-drowsiness",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop frame processing and clear the relay if this service set it."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        with self._lock:
            was_active = self._status.alert_active
            self._status = DrowsinessStatus(None, None, False, False, False, False)
        if was_active:
            self._relay_controller.trigger("drowsiness", False)

    def latest_status(self) -> DrowsinessStatus:
        with self._lock:
            return self._status

    def set_enabled(self, enabled: bool) -> None:
        """Enable face analysis or clear its current status and relay."""
        relay_change = False
        with self._lock:
            if self._enabled == enabled:
                return
            self._enabled = enabled
            self._eye_frames = 0
            self._yaw_frames = 0
            self._normal_since = None
            relay_change = self._status.alert_active and not enabled
            self._status = DrowsinessStatus(None, None, False, False, False, False)
            status = self._status
        if relay_change:
            self._relay_controller.trigger("drowsiness", False)
        if self._on_status:
            self._on_status(status)

    def fatal_error(self) -> Exception | None:
        """Return a camera pipeline startup/runtime error, if one occurred."""
        return self._fatal_error

    def _run_with_error_capture(self, target: Callable[[], None]) -> None:
        try:
            target()
        except Exception as error:
            self._fatal_error = error
            LOGGER.error("camera drowsiness service stopped: %s", error)

    def process_measurement(self, ear: float, yaw_degrees: float, *, now: float | None = None) -> None:
        """Process one face measurement; public for deterministic tests."""
        current_time = time.monotonic() if now is None else now
        drowsy = ear < self._config.ear_below
        distracted = abs(yaw_degrees) > self._config.head_yaw_degrees
        relay_change: bool | None = None
        with self._lock:
            if not self._enabled:
                return
            self._eye_frames = self._eye_frames + 1 if drowsy else 0
            self._yaw_frames = self._yaw_frames + 1 if distracted else 0
            trigger = (
                self._eye_frames >= self._config.consecutive_frames
                or self._yaw_frames >= self._config.consecutive_frames
            )
            active = self._status.alert_active
            if trigger and not active:
                active = True
                self._normal_since = None
                relay_change = True
            elif active:
                if drowsy or distracted:
                    self._normal_since = None
                elif self._normal_since is None:
                    self._normal_since = current_time
                elif current_time - self._normal_since >= self._config.clear_debounce_seconds:
                    active = False
                    relay_change = False
                    self._normal_since = None
            self._status = DrowsinessStatus(
                ear=ear,
                yaw_degrees=yaw_degrees,
                drowsy=drowsy,
                distracted=distracted,
                alert_active=active,
                face_detected=True,
            )
            status = self._status
        if relay_change is not None:
            self._relay_controller.trigger("drowsiness", relay_change)
        if self._on_status:
            self._on_status(status)

    def process_no_face(self) -> None:
        """Publish lost-face status without clearing an existing safety alert."""
        with self._lock:
            if not self._enabled:
                return
            self._status = DrowsinessStatus(
                ear=None,
                yaw_degrees=None,
                drowsy=False,
                distracted=False,
                alert_active=self._status.alert_active,
                face_detected=False,
            )
            status = self._status
        if self._on_status:
            self._on_status(status)

    def _run_mock(self) -> None:
        sample = 0
        while not self._stop_event.is_set():
            phase = sample % 250
            if 10 <= phase < 90:
                self.process_measurement(ear=0.16, yaw_degrees=0.0)
            elif 120 <= phase < 200:
                self.process_measurement(ear=0.31, yaw_degrees=42.0)
            else:
                self.process_measurement(ear=0.31, yaw_degrees=0.0)
            sample += 1
            self._stop_event.wait(self._mock_frame_interval_seconds)

    def _run_camera(self) -> None:
        try:
            import cv2
            import mediapipe as mp
            import numpy as np
            from mediapipe.tasks import python as mp_python
            from mediapipe.tasks.python import vision
        except ImportError as error:
            raise RuntimeError(
                "USB camera mode needs opencv-python and mediapipe; "
                "install requirements.txt or use --mock"
            ) from error
        if not self._face_model_path.is_file():
            raise RuntimeError(
                f"Face Landmarker model is missing: {self._face_model_path}. "
                "Download it as documented in README.md."
            )

        camera = cv2.VideoCapture(self._camera_index, cv2.CAP_V4L2)
        if not camera.isOpened():
            camera.release()
            raise RuntimeError(
                f"USB camera index {self._camera_index} could not be opened. "
                "Check /dev/video* and retry with --camera-index."
            )
        camera.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        camera.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        options = vision.FaceLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(self._face_model_path)),
            running_mode=vision.RunningMode.VIDEO,
            num_faces=1,
            min_face_detection_confidence=0.5,
            min_face_presence_confidence=0.5,
            min_tracking_confidence=0.5,
        )
        face_landmarker = vision.FaceLandmarker.create_from_options(options)
        last_timestamp_ms = -1
        try:
            while not self._stop_event.is_set():
                received, frame = camera.read()
                if not received:
                    LOGGER.warning("USB camera frame read failed")
                    self._stop_event.wait(0.1)
                    continue
                with self._lock:
                    enabled = self._enabled
                if not enabled:
                    continue
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
                timestamp_ms = max(last_timestamp_ms + 1, int(time.monotonic() * 1000))
                result = face_landmarker.detect_for_video(
                    image, timestamp_ms
                )
                last_timestamp_ms = timestamp_ms
                if not result.face_landmarks:
                    self.process_no_face()
                    continue
                landmarks = result.face_landmarks[0]
                height, width = frame.shape[:2]
                ear = (
                    _eye_aspect_ratio(landmarks, LEFT_EYE, width, height)
                    + _eye_aspect_ratio(landmarks, RIGHT_EYE, width, height)
                ) / 2
                yaw = _estimate_yaw_degrees(landmarks, width, height, cv2, np)
                self.process_measurement(ear, yaw)
        finally:
            face_landmarker.close()
            camera.release()


def _eye_aspect_ratio(landmarks, indexes: tuple[int, ...], width: int, height: int) -> float:
    points = [(landmarks[index].x * width, landmarks[index].y * height) for index in indexes]
    vertical_a = math.dist(points[1], points[5])
    vertical_b = math.dist(points[2], points[4])
    horizontal = math.dist(points[0], points[3])
    return (vertical_a + vertical_b) / (2 * horizontal) if horizontal else 0.0


def _estimate_yaw_degrees(landmarks, width: int, height: int, cv2, np) -> float:
    """Estimate yaw from six Face Mesh landmarks using solvePnP."""
    model_points = np.array(
        [
            (0.0, 0.0, 0.0),       # nose tip (1)
            (0.0, -63.6, -12.5),   # chin (152)
            (-43.3, 32.7, -26.0),  # left eye outer corner (33)
            (43.3, 32.7, -26.0),   # right eye outer corner (263)
            (-28.9, -28.9, -24.1), # left mouth corner (61)
            (28.9, -28.9, -24.1),  # right mouth corner (291)
        ],
        dtype="double",
    )
    image_indexes = (1, 152, 33, 263, 61, 291)
    image_points = np.array(
        [(landmarks[index].x * width, landmarks[index].y * height) for index in image_indexes],
        dtype="double",
    )
    focal_length = width
    camera_matrix = np.array(
        [[focal_length, 0, width / 2], [0, focal_length, height / 2], [0, 0, 1]],
        dtype="double",
    )
    success, rotation_vector, _ = cv2.solvePnP(
        model_points, image_points, camera_matrix, np.zeros((4, 1)), flags=cv2.SOLVEPNP_ITERATIVE
    )
    if not success:
        return 0.0
    rotation_matrix, _ = cv2.Rodrigues(rotation_vector)
    projection = np.hstack((rotation_matrix, np.zeros((3, 1))))
    _, _, _, _, _, _, euler = cv2.decomposeProjectionMatrix(projection)
    return float(euler[1][0])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="use synthetic face measurements")
    parser.add_argument("--mock-esp32", action="store_true", help="log relay commands instead of using the ESP32")
    parser.add_argument("--camera-index", type=int, default=0, help="USB camera index, usually 0")
    parser.add_argument(
        "--face-model",
        type=Path,
        default=DEFAULT_FACE_MODEL,
        help="MediaPipe Face Landmarker .task model path",
    )
    parser.add_argument("--once", action="store_true", help="print one status and exit")
    parser.add_argument("--duration", type=float, help="run for this many seconds, then print status")
    args = parser.parse_args()
    if args.duration is not None and args.duration <= 0:
        parser.error("--duration must be greater than zero")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    link = Esp32Link(mock=args.mock or args.mock_esp32)
    controller = RelayController.from_config(link)
    received_status = threading.Event()
    service = CameraDrowsinessService(
        controller,
        DrowsinessConfig.from_json(),
        mock=args.mock,
        camera_index=args.camera_index,
        face_model_path=args.face_model,
        on_status=lambda _: received_status.set(),
    )
    try:
        link.start()
        service.start()
        if args.once:
            if not received_status.wait(timeout=3):
                if service.fatal_error():
                    raise service.fatal_error()
                raise TimeoutError("no camera status received within three seconds")
        elif args.duration is not None:
            time.sleep(args.duration)
        else:
            LOGGER.info("camera drowsiness service running; press Ctrl-C to stop")
            while True:
                if service.fatal_error():
                    raise service.fatal_error()
                time.sleep(1)
        if service.fatal_error():
            raise service.fatal_error()
        print(json.dumps(service.latest_status().as_dict(), sort_keys=True))
    except KeyboardInterrupt:
        LOGGER.info("stopping camera drowsiness service")
    finally:
        service.stop()
        link.stop()


if __name__ == "__main__":
    main()
