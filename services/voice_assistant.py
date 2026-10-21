"""Queue short local speech announcements for dashboard alerts."""

from __future__ import annotations

import logging
import queue
import shutil
import subprocess
import threading


LOGGER = logging.getLogger(__name__)


class VoiceAssistant:
    """Speak alert transitions through the Pi's default audio output."""

    def __init__(
        self,
        command: str = "espeak-ng",
        *,
        voice: str = "en-us+f3",
        speed: int = 140,
        pitch: int = 55,
        pitch_range: int = 40,
        volume: int = 130,
        word_gap: int = 2,
    ) -> None:
        self._command_name = command
        self._voice = voice
        self._speed = speed
        self._pitch = pitch
        self._pitch_range = pitch_range
        self._volume = volume
        self._word_gap = word_gap
        self._command: str | None = None
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._states: dict[str, bool] = {}

    def start(self) -> bool:
        """Start speech output; return False when the optional TTS tool is absent."""
        self._command = shutil.which(self._command_name)
        if self._command is None:
            LOGGER.warning(
                "voice alerts unavailable: %s is not installed; install espeak-ng to enable speech",
                self._command_name,
            )
            return False
        self._thread = threading.Thread(target=self._run, name="voice-assistant", daemon=True)
        self._thread.start()
        LOGGER.info("voice alerts enabled with %s (%s)", self._command, self._voice)
        return True

    def announce(self, message: str) -> None:
        """Queue one short announcement without blocking a sensor callback."""
        if self._command is not None and message.strip():
            self._queue.put(message.strip())

    def announce_transition(
        self,
        key: str,
        active: bool,
        message: str,
        clear_message: str | None = None,
    ) -> None:
        """Announce once when a named alert turns on, and optionally when it clears."""
        with self._lock:
            previous = self._states.get(key)
            self._states[key] = active
        if active and previous is not True:
            self.announce(message)
        elif previous is True and not active and clear_message:
            self.announce(clear_message)

    def stop(self) -> None:
        if self._thread is None:
            return
        self._queue.put(None)
        self._thread.join(timeout=2)
        self._thread = None

    def _run(self) -> None:
        assert self._command is not None
        while True:
            message = self._queue.get()
            if message is None:
                return
            try:
                result = subprocess.run(
                    [
                        self._command,
                        "-v", self._voice,
                        "-s", str(self._speed),
                        "-p", str(self._pitch),
                        "-P", str(self._pitch_range),
                        "-a", str(self._volume),
                        "-g", str(self._word_gap),
                        "-z",
                        message,
                    ],
                    check=False,
                    timeout=20,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                if result.returncode != 0:
                    LOGGER.warning(
                        "speech command exited with status %d: %s",
                        result.returncode,
                        result.stderr.strip() or "no error details",
                    )
            except (OSError, subprocess.TimeoutExpired) as error:
                LOGGER.warning("could not speak alert: %s", error)
