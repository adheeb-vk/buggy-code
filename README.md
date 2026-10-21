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

## ESP32-S3 firmware

Flash the new minimal sensor-and-relay firmware at
`firmware/buggy_esp32s3/buggy_esp32s3.ino`. It separates all board-specific
pins into `firmware/buggy_esp32s3/board_config.h`.

Every sensor and relay starts as `-1` (disabled). With no hardware connected,
the firmware still sends a `type: telemetry` JSON line every 200 ms. It will
not read floating pins or drive any relay until you replace an individual
clearly marked `TODO` value after verifying the physical wiring.

It requires ArduinoJson 6.x, installable using Arduino IDE Library Manager.

## Verify the connected ESP32-S3

In the Arduino IDE, enable **Tools → USB CDC On Boot**, set **USB Mode** to
**Hardware CDC and JTAG**, then flash
`firmware/buggy_esp32s3/buggy_esp32s3.ino`. With sensors disconnected, the
sketch still sends a JSON line every 200 ms. It reports `"imu": 0` and omits
`accel` until an MPU6050 is connected, so the Pi cannot mistake missing IMU
data for a pothole.

```bash
source venv/bin/activate
python services/esp32_link.py --port /dev/ttyACM0 --once
```

The expected response contains `seatbelt`, `alcohol`, `water`, `current`, and
`imu` when those inputs are configured. Relay acknowledgement messages are control messages; they do
not replace the latest sensor telemetry on the Pi.

### Verify the relay protocol safely

Before any relay pin is configured, verify bidirectional serial communication:

```bash
source venv/bin/activate
python services/esp32_link.py --port /dev/ttyACM0 --relay-test drowsiness
```

The expected acknowledgement is `"ok": false` with
`"reason": "relay_unconfigured"`. That is a successful safety check: the
ESP32 received and understood the Pi command, and it did not drive any GPIO.

### ESP32-S3 USB diagnostic

If the complete firmware does not emit telemetry, first flash
`firmware/esp32_usb_test/esp32_usb_test.ino`. This temporary sketch reads no
sensors and controls no GPIO; it emits a JSON line twice per second. With the
same USB CDC settings, verify it with:

```bash
source venv/bin/activate
python services/esp32_link.py --port /dev/ttyACM0 --once
```

An output containing `"diagnostic":"usb_cdc"` proves the USB link. Reflash
`firmware/buggy_esp32s3/buggy_esp32s3.ino` only after this succeeds.

Camera setup and the ESP32-S3 udev rule will be documented as their services
are implemented.

## SIM808 UART setup

The Raspberry Pi 5 uses its dedicated debug UART by default. Enable a separate
UART0 on the 40-pin header before connecting the SIM808. Edit
`/boot/firmware/config.txt`:

```bash
sudo nano /boot/firmware/config.txt
```

Add this one line, save, and reboot:

```text
dtoverlay=uart0-pi5
```

After the reboot, the GPS service uses `/dev/ttyAMA0`, which is UART0 on the
GPIO header. Do not use `/dev/serial0` for this project: on the Pi 5 it is the
dedicated debug UART.

| SIM808 serial label | Raspberry Pi 5 GPIO header | Notes |
| --- | --- | --- |
| `TXD` | physical pin 10, GPIO15 / RXD0 | Direct only when the breakout `TXD` output is 3.3 V or lower. |
| `RXD` | physical pin 8, GPIO14 / TXD0 through a 3.3 V-to-2.8 V level shifter or divider | Do not drive SIM808 RXD directly from the Pi. |
| `GND` | physical pin 6, GND | Both devices need this common ground. |

For a simple Pi-TX divider, use 1 kOhm between Pi pin 8 and SIM808 `RXD`, then
5.6 kOhm between the SIM808 `RXD` node and GND. Do not connect the SIM808 power
input to the Pi 5 V pin; retain its separate power supply. A bare SIM808 uses
2.8 V UART logic; if the breakout raises `TXD` to 5 V, add a level shifter on
that path too.

Verify AT communication before waiting for a satellite fix:

```bash
source venv/bin/activate
python services/gps_service.py --port /dev/ttyAMA0 --at-test
```

Expected output contains `"ok": true` and `"received": "OK"`. If it does
not, try the non-destructive multi-baud probe before changing the GPS service:

```bash
source venv/bin/activate
python services/gps_service.py --port /dev/ttyAMA0 --probe
```

Then request one GPS fix:

```bash
source venv/bin/activate
python services/gps_service.py --port /dev/ttyAMA0 --once --timeout 90
```

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
python services/camera_drowsiness.py --mock --duration 12
```

The mock stream includes eye-closure and head-turn periods. The detector uses
the previous setting of five consecutive measurements for eye closure or head
turn; the time-based six-second settings have been removed. An alert clears
after one second of normal measurements. Camera calibration values are marked
`TODO` until tuned with the installed USB camera.

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

## MPU-9250 / MPU-6500 severe-shake mapping

The photographed board supports I²C. Mount it firmly with its printed **Z**
arrow pointing upward when the buggy is level. Wire it to the ESP32-S3 as
follows:

| IMU pin | ESP32-S3 pin | Purpose |
| --- | --- | --- |
| `VCC` | `3V3` | 3.3 V power and I²C logic level |
| `GND` | `GND` | Shared ground |
| `SDA` | pin labelled `1` | I²C data; right header, below `RX` |
| `SCL` | pin labelled `2` | I²C clock; right header, below pin `1` |
| `AD0` | `GND` | Usually selects I²C address `0x68`; firmware also detects `0x69`. |
| `NCS` | `3V3` | Keeps the module in I²C mode |
| `FSYNC` | `GND` | Unused input |
| `EDA`, `ECL`, `INT` | Leave disconnected | Not needed for acceleration logging |

Flash `firmware/buggy_esp32s3/buggy_esp32s3.ino`. The `imu` field in ESP32
telemetry must become `1`, and telemetry then includes `accel` with `x`, `y`,
and `z` readings at 50 Hz. It also reports `imu_address` and `imu_who_am_i`.

If `imu` remains `0`, verify the four essential connections first: `VCC` to
`3V3`, `GND` to `GND`, `SDA` to GPIO1, and `SCL` to GPIO2. `NCS` must be tied
to `3V3` for I²C mode. Do not connect the module to 5 V for this setup.

The telemetry field `i2c_devices` is an I²C address scan. With the MPU wired
correctly it must contain `104` (`0x68`) or `105` (`0x69`). An empty list means
the ESP32 cannot electrically see the board: check VCC/GND and make sure SDA
and SCL are not swapped.

Use the real USB-connected ESP32-S3 with mock GPS while the SIM808 is
unpowered:

```bash
source venv/bin/activate
python services/pothole_service.py --port /dev/ttyACM0 --mock-gps --duration 60
```

Give the firmly mounted sensor one deliberate vertical shake. A shake above
the provisional 0.35 g threshold creates one row in `data/hazards.db` with a
mock GPS coordinate. The two-second cooldown prevents one shake from creating
multiple rows.

### Use real SIM808 GPS for pothole mapping

Once `gps_service.py --at-test` reports `OK`, omit `--mock-gps`. The service
then uses the real SIM808 position and ignores shakes until it has a valid GPS
fix:

```bash
source venv/bin/activate
python services/pothole_service.py \
  --port /dev/ttyACM0 \
  --gps-port /dev/ttyAMA0 \
  --duration 300
```

Use `--gps-baudrate` if the SIM808 AT probe identified a value other than
115200. Press `Ctrl-C` to stop sooner. Each qualifying shake after a fix is
recorded in `data/hazards.db` with the live latitude and longitude.

## MQ-3 alcohol-vapour monitor

The MQ-3 is used only for alcohol-vapour detection. It is not a cabin-air
quality sensor and cannot establish a driver's blood-alcohol concentration.

### Wiring for the photographed ESP32-S3 N16R8 board

With the board oriented as in `ref-images/esp32s3.jpeg` (USB ports at the
bottom), use the pin labelled **4** on the left header for the MQ-3 signal.

| MQ-3 pin | Connect to | Notes |
| --- | --- | --- |
| `VCC` | ESP32 `5V` | The MQ-3 heater needs 5 V. |
| `GND` | ESP32 `GND` | This ground must be shared. |
| `AO` | ESP32 `GPIO4` through a divider | Never connect 5 V `AO` directly to a 3.3 V ESP32 input. |
| `DO` | Leave disconnected | The digital threshold is set by the module potentiometer and is not used. |

Build the divider between the MQ-3 `AO` and GPIO4 as follows:

```text
MQ-3 AO ── 20 kOhm ──┬── ESP32 GPIO4 (pin labelled 4)
                     |
                   10 kOhm
                     |
                    GND
```

An optional 0.1 µF capacitor from the GPIO4 node to GND makes the ADC reading
steadier. Keep the relay disconnected for this first sensor test.

After its ESP32-S3 analog input is configured, collect a warmed-up baseline:

```bash
source venv/bin/activate
python services/alcohol_service.py --port /dev/ttyACM0 --duration 60
```

The output reports raw ADC min, mean, and max values. Leave
`config/thresholds.json` alcohol thresholds as `null` during this step: no
relay command is sent. Set the two clearly marked TODO threshold values only
after comparing clean-air readings against a controlled alcohol-vapour test.

## Run the geo-fenced advisory without hardware

```bash
python3 services/geofence_service.py --mock --duration 4
```

The mock GPS track enters and exits the mock-only zone in `config/zones.json`.
The service logs the `geofence` relay HIGH on entry and LOW on exit, then prints
the active zone, advisory speed, location, and distance. Replace the clearly
marked `TODO` mock zone with final exhibition venue data before hardware use.

## Run the interactive dashboard and full stack

Install the declared dependencies, including Flask:

```bash
python3 -m pip install -r requirements.txt
```

Start every service with mock data and the dashboard bound to every network
interface:

```bash
source venv/bin/activate
python main.py --mock
```

The header shows **SIMULATION · NOT VEHICLE DATA** in this mode. Use it only
for indoor development; its readings are deliberately synthetic.

To test eye closure and distraction with the real USB camera while ESP32 and
GPS remain mocked, use:

```bash
source venv/bin/activate
python main.py --mock --real-camera --camera-index 0
```

The `--mock` option alone uses a synthetic camera stream and cannot observe
your face. Keep the real camera visible to the dashboard. The restored detector
triggers after five consecutive eye-closure or head-turn measurements.

Open `http://<pi-ip>:5000` from a device on the same Wi-Fi. The dashboard polls
its live state every second. It shows driver status, GPS, geofence advisory,
MQ-3 status, recent hazards, and raw ESP32 telemetry. The compact live map uses
Leaflet with public OpenStreetMap tiles and needs no API key; the device viewing
the dashboard needs internet access for map tiles. OpenStreetMap attribution is
shown on the map. Numeric GPS coordinates continue to work when tiles are offline.
Use **EXPAND MAP** to enlarge the GPS map and **CLOSE MAP** to return. The
header **FULL SCREEN** button switches the whole dashboard into browser full-screen
mode; press Escape or use **EXIT FULL SCREEN** to leave it.
The parking page has matching **EXPAND VIEW** / **CLOSE VIEW** controls for its
vehicle and sensor display, plus its own **FULL SCREEN** button.
Feature switches prevent
their corresponding service from operating its relay; relay buttons issue a
manual command through the ESP32-S3. Alert acknowledgement dismisses the
on-screen card but does not clear the underlying condition.

The **Driver attention** switch pauses face analysis and clears its active
alert when switched off. **Parking assistance** pauses ultrasonic processing,
clears the parking display to `WAITING`, and mutes proximity speech; turning it
back on resumes processing when the next ESP32 reading arrives. Parking has no
relay control.

Use **PARKING VIEW** in the header (or open `/parking`) for the full-height,
top-down vehicle display. It has separate front-left, front-right, and rear
distance indicators with configurable clear, caution, warning, and stop bands.
The page deliberately shows `WAITING` until it receives real ultrasonic
telemetry; simulation mode does not invent obstacle distances.

The ESP32-S3 firmware accepts these optional JSON telemetry fields:
`ultrasonic_front_left_cm`, `ultrasonic_front_right_cm`, and
`ultrasonic_rear_cm`. Proposed trigger/echo GPIO pairs are 5/6 (front-left),
7/8 (front-right), and 9/10 (rear), as recorded in
`firmware/buggy_esp32s3/board_config.h`. Check your physical wiring against the
board's GPIO labels before powering the sensors. For modules with a 5 V echo,
use a voltage divider or level shifter before the ESP32-S3 echo GPIO. Proximity
distance bands are in `config/thresholds.json` and are provisional until tested
on the buggy.

The dashboard's **Parking assistance** switch controls the ultrasonic
proximity feature and its voice alerts together. It does not use an ESP32 relay.

Run the real stack after verifying the individual devices:

```bash
source venv/bin/activate
python main.py --esp32-port /dev/ttyACM0 --gps-port /dev/ttyAMA0
```

The header shows **LIVE · VEHICLE DATA** only with this command. A sensor that
is disconnected or has no valid reading appears as `WAITING` or `—`; the
dashboard does not substitute a mock reading in live mode.

### Spoken alerts and Bluetooth speaker

The full stack speaks drowsiness, calibrated MQ-3, geofence, pothole, and
parking warnings through the Raspberry Pi's default audio output. Install the
lightweight speech program once:

```bash
sudo apt update
sudo apt install espeak-ng
```

Pair and connect the Bluetooth speaker, then select it as the Pi's audio output
in the desktop sound settings. Check the speaker from a terminal before
starting the buggy stack:

```bash
espeak-ng "Buggy voice alert test"
```

Announcements are queued so camera and sensor processing can continue while
speech plays. To run the stack silently, add `--no-voice` to the `python main.py`
command. If the speech program is missing, the rest of the stack still starts
and logs that voice alerts are unavailable.

Speech uses the female `en-us+f3` voice by default, at 140 words per minute,
with less pause between words. Try another voice and modulation directly
through the stack options, for example:

```bash
source venv/bin/activate
python main.py --mock --voice en-us+f3 --voice-speed 140 --voice-pitch 55 --voice-pitch-range 40 --voice-volume 130 --voice-word-gap 2
```

Available voice names depend on the installed eSpeak NG data; `espeak-ng
--voices=en` lists the English voices. The built-in voices remain synthetic;
these settings improve pacing and tone, while a more natural neural voice would
require installing a separate speech engine and voice model.

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
