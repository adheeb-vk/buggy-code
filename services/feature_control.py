"""Runtime feature enablement and manual relay control for the dashboard."""

from __future__ import annotations

import threading
from collections.abc import Callable
from collections.abc import Mapping
from typing import Protocol


class RelayTrigger(Protocol):
    def trigger(self, feature: str, active: bool) -> bool: ...

    def states(self) -> dict[str, bool | None]: ...


class FeatureController:
    """Gate feature relay requests while retaining one shared relay transport."""

    def __init__(self, relays: RelayTrigger, *, non_relay_features: tuple[str, ...] = ()) -> None:
        self._relays = relays
        self._relay_features = set(relays.states())
        self._enabled = {feature: True for feature in self._relay_features | set(non_relay_features)}
        self._enabled_callbacks: dict[str, list[Callable[[bool], None]]] = {
            feature: [] for feature in self._enabled
        }
        self._lock = threading.Lock()

    def add_enabled_callback(self, feature: str, callback: Callable[[bool], None]) -> None:
        """Register a service callback for a feature's enabled state."""
        with self._lock:
            if feature not in self._enabled:
                raise ValueError(f"unknown feature: {feature}")
            self._enabled_callbacks[feature].append(callback)
            enabled = self._enabled[feature]
        callback(enabled)

    def trigger(self, feature: str, active: bool) -> bool:
        """Forward an automatic relay request only while its feature is enabled."""
        with self._lock:
            enabled = self._enabled[feature]
        if active and not enabled:
            return False
        return self._relays.trigger(feature, active)

    def set_enabled(self, feature: str, enabled: bool) -> None:
        """Enable or disable a feature, clearing its relay immediately when off."""
        with self._lock:
            if feature not in self._enabled:
                raise ValueError(f"unknown feature: {feature}")
            self._enabled[feature] = enabled
            callbacks = tuple(self._enabled_callbacks[feature])
        if not enabled and feature in self._relay_features:
            self._relays.trigger(feature, False)
        for callback in callbacks:
            callback(enabled)

    def set_relay(self, feature: str, active: bool) -> bool:
        """Send an explicit dashboard relay command for an enabled feature."""
        with self._lock:
            if feature not in self._enabled:
                raise ValueError(f"unknown feature: {feature}")
            if feature not in self._relay_features:
                raise ValueError(f"{feature} has no relay control")
            if not self._enabled[feature]:
                raise ValueError(f"{feature} is disabled; enable it before switching its relay")
        return self._relays.trigger(feature, active)

    def enabled_features(self) -> dict[str, bool]:
        with self._lock:
            return self._enabled.copy()

    def relay_states(self) -> dict[str, bool | None]:
        return self._relays.states()

    def snapshot(self) -> Mapping[str, object]:
        return {"enabled": self.enabled_features(), "relays": self.relay_states()}
