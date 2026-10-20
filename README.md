# Buggy Intelligence Layer

## Current status

Milestone 4 provides the ESP32-S3 link, feature-scoped relay commands, and the
SIM808 GPS service. Milestone 5 adds the camera drowsiness and distraction
service. The remaining services are stubs until their respective milestone.

## Development setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

## Run the ESP32-S3 link without hardware

```bash
python3 services/esp32_link.py --mock --once
```

The command prints one synthetic sensor JSON object and exits. To keep it
running, omit `--once` and use `Ctrl-C` to stop it.

## Verify the connected ESP32-S3

In the Arduino IDE, enable **Tools → USB CDC On Boot**, then flash
`esp32s3_code.ino`. With sensors disconnected, the sketch still sends a JSON
line every 200 ms. It reports `"imu": 0` and omits `accel` until an MPU6050 is
connected, so the Pi cannot mistake missing IMU data for a pothole.

```bash
source venv/bin/activate
python services/esp32_link.py --port /dev/ttyACM0 --once
```

The expected response contains `seatbelt`, `alcohol`, `water`, `current`,
`aqi`, and `imu`. Relay acknowledgement messages are control messages; they do
not replace the latest sensor telemetry on the Pi.

Camera setup and the ESP32-S3 udev rule will be documented as their services
are implemented.

## SIM808 UART setup

Enable the Pi UART and disable its serial login shell before connecting the
SIM808:

```bash
sudo raspi-config
```

Then choose **Interface Options → Serial Port**, answer **No** to the login
shell, and answer **Yes** to enable serial hardware. The service defaults to
`/dev/serial0`; pass `--port` if the configured UART device differs.

The SIM808 must use its own suitable power supply and its serial logic level
must be verified as 3.3 V-compatible before wiring it to Pi GPIO UART pins.

## Run GPS without hardware

```bash
python3 services/gps_service.py --mock --once
```

The command prints one synthetic GPS fix and exits. The synthetic coordinates
are only a development track, not an exhibition venue zone.

## Drowsiness feature: USB camera setup

Every command below activates the project's virtual environment first:

```bash
source venv/bin/activate
```

Connect the USB camera, then confirm it is visible:

```bash
ls -l /dev/video*
v4l2-ctl --list-devices
```

Usually the camera is `/dev/video0`, which is camera index `0`. Test the full
drowsiness detector with the real USB camera but mock ESP32 relay commands.
First download the model required by the installed MediaPipe 1.x package:

```bash
source venv/bin/activate
mkdir -p models
curl -L --fail --output models/face_landmarker.task \
  https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/latest/face_landmarker.task
```

Then run:

```bash
source venv/bin/activate
python services/camera_drowsiness.py --mock-esp32 --camera-index 0
```

Press `Ctrl-C` to stop it. This starts the camera and face detection; relay
commands are only printed, so no unfinished ESP32 relay wiring is used.

To prove the drowsiness logic without a physical camera, run:

```bash
source venv/bin/activate
python services/camera_drowsiness.py --mock --duration 2
```

The mock stream cycles through normal, low-EAR, and excessive-yaw observations.
It logs relay commands as it enters or clears an advisory. Camera calibration
values are kept in `config/thresholds.json` and are marked `TODO` until tuned
with the installed USB camera.

## Run pothole mapping without hardware

```bash
python3 services/pothole_service.py --mock --duration 4
```

The mock ESP32 stream injects a synthetic vertical-acceleration spike every
three seconds. Each GPS-tagged hit is appended to `data/hazards.db` unless a
different SQLite path is supplied with `--database`. The command prints recent
hazard records, including their nearby-hit confidence count.

Pothole detection assumes `accel.z` is aligned with gravity and uses 16,384 raw
IMU units per g. Both the scale assumption and the provisional spike, pulse,
and repeat-radius thresholds are marked `TODO` in `config/thresholds.json`.

## Run the air-quality guardian without hardware

```bash
python3 services/air_quality_service.py --mock --duration 8
```

The mock AQI waveform rises above and then falls below the provisional limits.
The service logs an `air_quality` relay `HIGH` transition above the trigger and
a `LOW` transition only at or below the lower hysteresis boundary. It prints
the current state and a 60-reading in-memory history for the future dashboard.

## Run the geo-fenced advisory without hardware

```bash
python3 services/geofence_service.py --mock --duration 4
```

The mock GPS track enters and exits the mock-only zone in `config/zones.json`.
The service logs the `geofence` relay HIGH on entry and LOW on exit, then prints
the active zone, advisory speed, location, and distance. Replace the clearly
marked `TODO` mock zone with final exhibition venue data before hardware use.

## Run the read-only dashboard

Install the declared dependencies, including Flask:

```bash
python3 -m pip install -r requirements.txt
```

Then start a mock dashboard bound to every network interface:

```bash
python3 dashboard/app.py --mock
```

Open `http://<pi-ip>:5000` from a device on the same Wi-Fi. The dashboard polls
its read-only `/api/state` endpoint every second. It shows driver status, GPS,
geofence advisory, AQI, relay state, recent hazards, and raw ESP32 telemetry.

## Try a relay command without hardware

```bash
python3 services/relay_controller.py --mock --feature drowsiness --state on
```

The mock transport logs the JSON command that would be sent to the ESP32-S3
and prints the last requested state of each configured relay feature.

## Run relay-controller tests

```bash
python3 -m unittest tests.test_relay_controller
```
