# Buggy Intelligence Layer — Implementation Plan

## 1. Overview

Two-tier system:

- **ESP32-S3** — sensor/actuator layer. Reads all onboard sensors (seatbelt, alcohol,
  water, current, IMU, steering touch, etc. — whatever is already wired from the
  earlier build) and streams them to the Pi as JSON over USB serial. Also owns every
  relay and switches them HIGH/LOW on command from the Pi.
- **Raspberry Pi 5 (8GB)** — brain layer. Runs the camera pipeline, GPS/SIM808
  handling, and all feature logic (drowsiness, pothole mapping, air quality,
  geofencing). Decides *when* a relay should fire and tells the ESP32-S3 to do it.
  Also hosts the web dashboard (Feature 8) on the local network.

This phase implements **Features 1, 2, 3, and 6**, plus **Feature 8 as a read-only
web dashboard**. Features 4, 5, 7 are out of scope for now.

| # | Feature | Depends on |
|---|---|---|
| 1 | Drowsiness & distraction detection | Pi Camera |
| 2 | Pothole / road hazard mapping | ESP32-S3 IMU stream + SIM808 GPS |
| 3 | Cabin air quality guardian | ESP32-S3 sensor stream |
| 6 | Geo-fenced speed advisory | SIM808 GPS |
| 8 | Web dashboard (this phase: display only) | All of the above |

## 2. Hardware Interfaces

### Camera
- Pi Camera Module, connected directly to the Pi's CSI port.
- Accessed via `picamera2` (preferred on Pi 5/Bookworm) with an OpenCV/MediaPipe
  frame pipeline.

### SIM808 (GPS)
- Connected directly to the Pi's GPIO UART pins (GPIO14/TXD, GPIO15/RXD), not
  through the ESP32-S3.
- Requires UART to be enabled and the serial console disabled:
  `sudo raspi-config` → Interface Options → Serial Port → login shell: No,
  hardware enabled: Yes.
- SIM808 needs GPS powered on explicitly via AT commands before it returns a fix:
  `AT+CGNSPWR=1`, then poll with `AT+CGNSINF`.
- SIM808 boards typically need a separate 3.7–4.2V supply capable of ~2A bursts —
  reuse the buck-converter + capacitor arrangement from the earlier build, do not
  power it from a Pi GPIO pin directly.
- Verify the module's RX/TX logic level against the Pi's 3.3V GPIO before wiring
  directly; add a level shifter if the board is 5V TTL.

### ESP32-S3
- Connected to the Pi over USB (native USB-CDC — no separate USB-serial chip
  needed). Enumerates as something like `/dev/ttyACM0`; add a udev rule so it's
  always reachable at a fixed path, e.g. `/dev/esp32`.
- Sends a JSON line of sensor readings on a fixed interval (target: 5 Hz).
- Receives JSON command lines from the Pi telling it which relay to set and to
  what state.

### Relays
- Physically wired to ESP32-S3 GPIOs. Exact GPIO-to-relay wiring is **not
  finalized yet** — build against a config file (`config/relay_map.json`) so the
  real pin numbers can be dropped in later without touching any feature logic.
- Every feature in this phase (1, 2, 3, 6) only ever needs to set its relay HIGH
  (condition triggered) or LOW (condition cleared) — no PWM/analog relay control
  needed here.

## 3. Communication Protocol

### ESP32-S3 → Pi (sensor telemetry, ~5 Hz)

```json
{"seatbelt":1,"alcohol":123,"water":45,"current":210,
 "accel":{"x":1024,"y":-512,"z":16384},"aqi":78}
```

Field set will grow as more sensors are reused from the earlier Arduino build —
keep the parser tolerant of extra/missing keys.

### Pi → ESP32-S3 (relay commands)

```json
{"cmd":"relay","feature":"drowsiness","state":1}
```

The ESP32-S3 firmware keeps its own copy of `relay_map` (feature name → GPIO) and
looks up `feature` locally rather than trusting a raw pin number from the Pi —
keeps the two sides in sync via one shared config file, and keeps the Pi from
being able to toggle an arbitrary GPIO by mistake.

## 4. Feature Specs

### Feature 1 — Drowsiness & Distraction Detection
- **Input:** Pi Camera frames.
- **Logic:** MediaPipe Face Mesh → Eye Aspect Ratio (EAR). EAR below threshold for
  > N consecutive frames → drowsy. Head-pose yaw beyond a threshold for > N frames
  → distracted.
- **Relay:** `drowsiness` — HIGH while either condition is active, LOW once the
  driver's eyes/head return to normal for a short debounce window.
- **Also feeds:** dashboard (live status + EAR value), advisory to ESP32-S3 for
  any local hazard/horn behavior already wired there.

### Feature 2 — Pothole & Road Hazard Mapping
- **Input:** IMU (`accel` field from the ESP32-S3 stream) + GPS fix from SIM808.
- **Logic:** vertical-acceleration spike above a threshold, timestamped and
  tagged with the current GPS coordinate. Repeated hits within a small radius
  raise confidence and get logged as a known hazard point.
- **Relay:** `pothole` — pulses HIGH briefly on each detected hit (not latched).
- **Storage:** append-only log (SQLite or JSON lines) of `{lat, lon, time,
  magnitude}` for the dashboard's hazard map/list.

### Feature 3 — Cabin Air Quality Guardian
- **Input:** `aqi` (or raw MQ135/PMS value) field from the ESP32-S3 stream.
- **Logic:** simple threshold check, with hysteresis so it doesn't chatter at
  the boundary.
- **Relay:** `air_quality` — HIGH while above threshold, LOW once it drops back
  below (minus hysteresis band).

### Feature 6 — Geo-Fenced Speed Advisory
- **Input:** GPS fix from SIM808, zone list from `config/zones.json`
  (`{name, lat, lon, radius_m, advisory_speed}`).
- **Logic:** on each fix, compute distance to each zone centre (haversine);
  inside `radius_m` of any zone → advisory active.
- **Relay:** `geofence` — HIGH while inside any zone, LOW on exit.

### Feature 8 — Web Dashboard (this phase: read-only)
- Flask app on the Pi, bound to `0.0.0.0`, reachable from any device on the same
  Wi-Fi/LAN at `http://<pi-ip>:5000`.
- Single page showing: live drowsiness/EAR status, current GPS position (plain
  lat/lon, optionally a Leaflet map if internet/cached tiles are available),
  recent pothole log, live AQI reading + short history, current geofence zone
  status, raw ESP32-S3 sensor snapshot, and the current state of every relay.
- Polling-based (simple `fetch()` every second) is enough for this phase —
  no need for websockets yet.
- No authentication in this phase; note it as a follow-up if the dashboard needs
  to be reachable somewhere less controlled than an exhibition booth's own Wi-Fi.

## 5. Software Architecture

```
buggy-pi/
  config/
    relay_map.json        # feature name -> ESP32 GPIO (placeholder values)
    zones.json             # geofence zones
    thresholds.json         # EAR, pothole-G, AQI thresholds
  services/
    esp32_link.py            # serial read/write, JSON framing, shared sensor store
    relay_controller.py      # trigger(feature, bool) -> sends command via esp32_link
    gps_service.py           # SIM808 AT/UART handling, exposes latest fix
    camera_drowsiness.py     # Feature 1
    pothole_service.py       # Feature 2
    air_quality_service.py   # Feature 3
    geofence_service.py      # Feature 6
  dashboard/
    app.py                   # Flask app, Feature 8
    templates/index.html
    static/
  main.py                    # starts every service, shared state, graceful shutdown
  requirements.txt
  README.md
```

- Shared state lives in one in-process store (a small thread-safe class) that
  every service reads/writes and the dashboard polls — avoids needing a database
  for live values. The pothole log is the one thing that persists to disk.
- Every service should support a `--mock` mode (mock camera frames, mock GPS
  fixes, mock ESP32-S3 serial data) so the whole stack is testable indoors,
  before final wiring/relay numbers exist.

## 6. Milestones (build in this order)

1. Repo scaffolding: folder structure above, `config/*.json` with placeholder
   values, `requirements.txt`, empty service stubs that just log "not yet
   implemented."
2. `esp32_link.py` with a `--mock` sensor generator, plus the JSON framing for
   both directions of the protocol.
3. `relay_controller.py` on top of `esp32_link.py` — `trigger("drowsiness",
   True)` etc., with unit tests using a fake serial port.
4. `gps_service.py` — SIM808 AT-command sequence to enable GPS and parse
   `AT+CGNSINF`, with a `--mock` fixed/looping GPS track for indoor testing.
5. `camera_drowsiness.py` — MediaPipe EAR pipeline, wired to
   `relay_controller`.
6. `pothole_service.py` — combine IMU + GPS, write to the hazard log, wire to
   `relay_controller`.
7. `air_quality_service.py` — threshold + hysteresis, wire to
   `relay_controller`.
8. `geofence_service.py` — haversine zone check, wire to `relay_controller`.
9. `dashboard/app.py` — Flask app reading the shared state, single HTML page,
   confirm it's reachable from a second device on the same network.
10. `main.py` wiring everything together with graceful startup/shutdown, plus a
    short `README.md` covering how to run in `--mock` mode vs. with real
    hardware attached.

## 7. Open Items (fill in later)
- Real ESP32-S3 GPIO numbers for each relay (`config/relay_map.json` currently
  has placeholder values, clearly marked `TODO`).
- Final EAR / pothole-G / AQI thresholds — start with the values in
  `config/thresholds.json`, tune once on real hardware.
- Real geofence zone coordinates for the exhibition venue
  (`config/zones.json` currently has placeholder venue coordinates).
